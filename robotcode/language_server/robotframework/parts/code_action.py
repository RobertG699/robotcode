from __future__ import annotations

import contextlib
import socket
import threading
import traceback
import urllib.parse
from concurrent.futures import ProcessPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import TYPE_CHECKING, Any, List, Optional, Tuple, Union, cast
from urllib.parse import parse_qs, urlparse

from ....utils.logging import LoggingDescriptor
from ....utils.net import find_free_port
from ...common.decorators import code_action_kinds, language_id
from ...common.lsp_types import (
    CodeAction,
    CodeActionContext,
    CodeActionKinds,
    Command,
    Range,
)
from ...common.text_document import TextDocument
from ..configuration import DocumentationServerConfig
from ..diagnostics.library_doc import (
    get_library_doc,
    get_robot_library_html_doc_str,
    resolve_robot_variables,
)
from ..diagnostics.namespace import LibraryEntry, Namespace
from ..utils.ast_utils import Token, get_node_at_position
from .model_helper import ModelHelperMixin

if TYPE_CHECKING:
    from ..protocol import RobotLanguageServerProtocol  # pragma: no cover

from string import Template

from .protocol_part import RobotLanguageServerProtocolPart

HTML_ERROR_TEMPLATE = Template(
    """\n
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>${type}: ${message}</title>
</head>
<body>
  <div id="content">
    <h1>
        ${type}: ${message}
    </h1>
    <pre>
${stacktrace}
    </pre>
  </div>

</body>
</html>
"""
)

MARKDOWN_TEMPLATE = Template(
    """\
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>${name}</title>
</head>
<body>
  <template type="markdown" id="markdown-content">${content}</template>
  <div id="content"></div>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <script>
    document.getElementById('content').innerHTML =
      marked.parse(document.getElementById('markdown-content').content.textContent, {gfm: true});
  </script>
</body>
</html>
"""
)


class LibDocRequestHandler(SimpleHTTPRequestHandler):
    _logger = LoggingDescriptor()

    def log_message(self, format: str, *args: Any) -> None:
        self._logger.info("%s - %s\n" % (self.address_string(), format % args))

    def log_error(self, format: str, *args: Any) -> None:
        self._logger.error("%s - %s\n" % (self.address_string(), format % args))

    def do_GET(self) -> None:  # noqa: N802

        query = parse_qs(urlparse(self.path).query)
        name = n[0] if (n := query.get("name", [])) else None
        args = n[0] if (n := query.get("args", [])) else None
        basedir = n[0] if (n := query.get("basedir", [])) else None
        type_ = n[0] if (n := query.get("type", [])) else None

        if name:
            try:
                if type_ in ["md", "markdown"]:
                    libdoc = get_library_doc(
                        name, tuple(args.split("::") if args else ()), base_dir=basedir if basedir else "."
                    )

                    def calc_md() -> str:
                        tt = str.maketrans({"<": "&lt;", ">": "&gt;"})
                        return libdoc.to_markdown(add_signature=False, only_doc=False, header_level=0).translate(tt)

                    data = MARKDOWN_TEMPLATE.substitute(content=calc_md(), name=name)

                    self.send_response(200)
                    self.send_header("Content-type", "text/html")
                    self.end_headers()

                    self.wfile.write(bytes(data, "utf-8"))
                else:
                    with ProcessPoolExecutor(max_workers=1) as executor:
                        result = executor.submit(
                            get_robot_library_html_doc_str,
                            name + ("::" + args if args else ""),
                            base_dir=basedir if basedir else ".",
                        ).result(10)

                        self.send_response(200)
                        self.send_header("Content-type", "text/html")
                        self.end_headers()

                        self.wfile.write(bytes(result, "utf-8"))
            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException as e:
                self.send_response(404)
                self.send_header("Content-type", "text/html")
                self.end_headers()

                self.wfile.write(
                    bytes(
                        HTML_ERROR_TEMPLATE.substitute(
                            type=type(e).__qualname__, message=str(e), stacktrace="".join(traceback.format_exc())
                        ),
                        "utf-8",
                    )
                )

        else:
            super().do_GET()


class DualStackServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # suppress exception when protocol is IPv4
        with contextlib.suppress(Exception):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        return super().server_bind()


class RobotCodeActionProtocolPart(RobotLanguageServerProtocolPart, ModelHelperMixin):
    _logger = LoggingDescriptor()

    def __init__(self, parent: RobotLanguageServerProtocol) -> None:
        super().__init__(parent)

        parent.code_action.collect.add(self.collect)
        self.parent.on_initialized.add(self.initialized)
        self.parent.on_shutdown.add(self.shutdown)

        self._documentation_server: Optional[ThreadingHTTPServer] = None
        self._documentation_server_lock = threading.RLock()
        self._documentation_server_port = 0

    async def initialized(self, sender: Any) -> None:
        await self._ensure_http_server_started()

    async def shutdown(self, sender: Any) -> None:
        with self._documentation_server_lock:
            if self._documentation_server is not None:
                self._documentation_server.shutdown()
                self._documentation_server = None

    def _run_server(self, start_port: int, end_port: int) -> None:

        self._documentation_server_port = find_free_port(start_port, end_port)

        self._logger.info(f"Start documentation server on port {self._documentation_server_port}")

        with DualStackServer(("127.0.0.1", self._documentation_server_port), LibDocRequestHandler) as server:
            self._documentation_server = server
            try:
                server.serve_forever()
            except BaseException:
                self._documentation_server = None
                raise

    async def _ensure_http_server_started(self) -> None:
        config = await self.parent.workspace.get_configuration(DocumentationServerConfig)

        with self._documentation_server_lock:
            if self._documentation_server is None:
                self._server_thread = Thread(
                    name="documentation_server",
                    target=self._run_server,
                    args=(config.start_port, config.end_port),
                    daemon=True,
                )
                self._server_thread.start()

    @language_id("robotframework")
    @code_action_kinds([CodeActionKinds.SOURCE + ".openDocumentation"])
    @_logger.call
    async def collect(
        self, sender: Any, document: TextDocument, range: Range, context: CodeActionContext
    ) -> Optional[List[Union[Command, CodeAction]]]:

        from robot.parsing.lexer import Token as RobotToken
        from robot.parsing.model.statements import (
            KeywordCall,
            LibraryImport,
            ResourceImport,
        )

        namespace = await self.parent.documents_cache.get_namespace(document)
        if namespace is None:
            return None

        model = await self.parent.documents_cache.get_model(document, False)

        node = await get_node_at_position(model, range.start)

        await self._ensure_http_server_started()

        if isinstance(node, (LibraryImport, ResourceImport)):

            url = await self.build_url(
                node.name, node.args if isinstance(node, LibraryImport) else (), document, namespace
            )

            return [
                CodeAction(
                    "Open Documentation",
                    kind=CodeActionKinds.SOURCE + ".openDocumentation",
                    command=Command(
                        "Open Documentation",
                        "robotcode.showDocumentation",
                        [url],
                    ),
                )
            ]

        if isinstance(node, (KeywordCall)):
            result = await self.get_keyworddoc_and_token_from_position(
                node.keyword,
                cast(Token, node.get_token(RobotToken.KEYWORD)),
                [cast(Token, t) for t in node.get_tokens(RobotToken.ARGUMENT)],
                namespace,
                range.start,
            )

            if result is not None:
                kw_doc, _ = result

                if kw_doc is not None:
                    entry: Optional[LibraryEntry] = None

                    if kw_doc.libtype == "LIBRARY":
                        entry = next(
                            (v for v in (await namespace.get_libraries()).values() if v.library_doc == kw_doc.parent),
                            None,
                        )

                    elif kw_doc.libtype == "RESOURCE":
                        entry = next(
                            (v for v in (await namespace.get_resources()).values() if v.library_doc == kw_doc.parent),
                            None,
                        )

                        self_libdoc = await namespace.get_library_doc()
                        if entry is None and self_libdoc == kw_doc.parent:

                            entry = LibraryEntry(self_libdoc.name, str(document.uri.to_path().name), self_libdoc)

                    if entry is None:
                        return None

                    url = await self.build_url(entry.import_name, entry.args, document, namespace, kw_doc.name)

                    return [
                        CodeAction(
                            "Open Documentation",
                            kind=CodeActionKinds.SOURCE + ".openDocumentation",
                            command=Command(
                                "Open Documentation",
                                "robotcode.showDocumentation",
                                [url],
                            ),
                        )
                    ]

        return None

    async def build_url(
        self,
        name: str,
        args: Tuple[Any, ...],
        document: TextDocument,
        namespace: Namespace,
        target: Optional[str] = None,
    ) -> str:

        base_dir = str(document.uri.to_path().parent)

        robot_variables = resolve_robot_variables(
            str(namespace.imports_manager.folder.to_path()),
            base_dir,
            variables=await namespace.get_resolvable_variables(),
        )
        try:
            name = robot_variables.replace_string(name.replace("\\", "\\\\"), ignore_errors=False)

            args = tuple(robot_variables.replace_string(v.replace("\\", "\\\\"), ignore_errors=False) for v in args)

        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException:
            pass

        url_args = "::".join(args) if args else ""

        base_url = f"http://localhost:{self._documentation_server_port}"
        params = urllib.parse.urlencode({"name": name, "args": url_args, "basedir": base_dir})

        url = f"{base_url}" f"/?&{params}" f"{f'#{target}' if target else ''}"

        return url
