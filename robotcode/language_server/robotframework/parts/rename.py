from __future__ import annotations

import ast
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    List,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)

# from ....utils.async_itertools import async_next
from ....utils.async_tools import threaded
from ....utils.logging import LoggingDescriptor

# from ....utils.uri import Uri
from ...common.decorators import language_id
from ...common.lsp_types import (
    CreateFile,
    DeleteFile,
    OptionalVersionedTextDocumentIdentifier,
    Position,
    PrepareRenameResult,
    PrepareRenameResultWithPlaceHolder,
    RenameFile,
    TextDocumentEdit,
    TextEdit,
    WorkspaceEdit,
)
from ...common.text_document import TextDocument

# from ..diagnostics.entities import VariableDefinition
from ..diagnostics.library_doc import KeywordDoc
from ..utils.ast_utils import (  # HasTokens,; Statement,; get_tokens_at_position,
    Token,
    get_nodes_at_position,
    range_from_token,
)

if TYPE_CHECKING:
    from ..protocol import RobotLanguageServerProtocol

from .model_helper import ModelHelperMixin
from .protocol_part import RobotLanguageServerProtocolPart

_RenameMethod = Callable[[ast.AST, TextDocument, Position, str], Awaitable[Optional[WorkspaceEdit]]]
_PrepareRenameMethod = Callable[[ast.AST, TextDocument, Position], Awaitable[Optional[PrepareRenameResult]]]

_T = TypeVar("_T", bound=Callable[..., Any])


class RobotRenameProtocolPart(RobotLanguageServerProtocolPart, ModelHelperMixin):
    _logger = LoggingDescriptor()

    def __init__(self, parent: RobotLanguageServerProtocol) -> None:
        super().__init__(parent)

        parent.rename.collect.add(self.collect)
        parent.rename.collect_prepare.add(self.collect_prepare)

    def _find_method(self, cls: Type[Any], prefix: str) -> Optional[_T]:
        if cls is ast.AST:
            return None
        method_name = prefix + "_" + cls.__name__
        if hasattr(self, method_name):
            method = getattr(self, method_name)
            if callable(method):
                return cast(_T, method)
        for base in cls.__bases__:
            method = self._find_method(base, prefix)
            if method:
                return cast(_T, method)
        return None

    @language_id("robotframework")
    @threaded()
    @_logger.call
    async def collect(
        self,
        sender: Any,
        document: TextDocument,
        position: Position,
        new_name: str,
    ) -> Optional[WorkspaceEdit]:

        result_nodes = await get_nodes_at_position(await self.parent.documents_cache.get_model(document), position)

        if not result_nodes:
            return None

        result_node = result_nodes[-1]

        if result_node is None:
            return None

        result = await self._rename_default(result_nodes, document, position, new_name)
        if result:
            return result

        method: Optional[_RenameMethod] = self._find_method(type(result_node), "rename")
        if method is not None:
            result = await method(result_node, document, position, new_name)
            if result is not None:
                return result

        return None

    @language_id("robotframework")
    @threaded()
    @_logger.call
    async def collect_prepare(
        self,
        sender: Any,
        document: TextDocument,
        position: Position,
    ) -> Optional[PrepareRenameResult]:

        result_nodes = await get_nodes_at_position(await self.parent.documents_cache.get_model(document), position)

        if not result_nodes:
            return None

        result_node = result_nodes[-1]

        if result_node is None:
            return None

        result = await self._prepare_rename_default(result_nodes, document, position)
        if result:
            return result

        method: Optional[_PrepareRenameMethod] = self._find_method(type(result_node), "prepare_rename")
        if method is not None:
            result = await method(result_node, document, position)
            if result is not None:
                return result

        return None

    async def _prepare_rename_default(
        self, nodes: List[ast.AST], document: TextDocument, position: Position
    ) -> Optional[PrepareRenameResult]:
        pass

    async def _rename_default(
        self, nodes: List[ast.AST], document: TextDocument, position: Position, new_name: str
    ) -> Optional[WorkspaceEdit]:
        # from robot.parsing.lexer.tokens import Token as RobotToken

        # namespace = await self.parent.documents_cache.get_namespace(document)
        # if namespace is None:
        #     return None

        # if not nodes:
        #     return None

        # node = nodes[-1]

        # if not isinstance(node, HasTokens):
        #     return None

        # tokens = get_tokens_at_position(node, position)

        # token_and_var: Optional[Tuple[Token, VariableDefinition]] = None

        # for token in tokens:
        #     token_and_var = await async_next(
        #         (
        #             (var_token, var)
        #             async for var_token, var in self.iter_variables_from_token(token, namespace, nodes, position)
        #             if position in range_from_token(var_token)
        #         ),
        #         None,
        #     )

        # if (
        #     token_and_var is None
        #     and isinstance(node, Statement)
        #     and isinstance(node, self.get_expression_statement_types())
        #     and (token := node.get_token(RobotToken.ARGUMENT)) is not None
        #     and position in range_from_token(token)
        # ):
        #     token_and_var = await async_next(
        #         (
        #             (var_token, var)
        #             async for var_token, var in self.iter_expression_variables_from_token(
        #                 token, namespace, nodes, position
        #             )
        #             if position in range_from_token(var_token)
        #         ),
        #         None,
        #     )

        # if token_and_var is not None:
        #     _, variable = token_and_var

        #     return [
        #         DocumentHighlight(e.range, DocumentHighlightKind.TEXT)
        #         for e in await self.parent.robot_references.find_variable_references_in_file(document, variable)
        #     ]

        return None

    async def prepare_rename_KeywordCall(  # noqa: N802
        self, node: ast.AST, document: TextDocument, position: Position
    ) -> Optional[PrepareRenameResult]:
        result = await self._find_KeywordCall(node, document, position)
        if result is not None:
            kw_doc, token = result
            return PrepareRenameResultWithPlaceHolder(range_from_token(token), token.value)

        return None

    async def rename_KeywordCall(  # noqa: N802
        self, node: ast.AST, document: TextDocument, position: Position, new_name: str
    ) -> Optional[WorkspaceEdit]:
        result = await self._find_KeywordCall(node, document, position)
        if result is not None:
            kw_doc, token = result
            references = await self.parent.robot_references.find_keyword_references(document, kw_doc)
            changes: List[Union[TextDocumentEdit, CreateFile, RenameFile, DeleteFile]] = []

            for reference in references:
                changes.append(
                    TextDocumentEdit(
                        OptionalVersionedTextDocumentIdentifier(reference.uri, None),
                        [TextEdit(reference.range, new_name)],
                    )
                )

            return WorkspaceEdit(document_changes=changes)

        return None

    async def _find_KeywordCall(  # noqa: N802
        self, node: ast.AST, document: TextDocument, position: Position
    ) -> Optional[Tuple[KeywordDoc, Token]]:
        from robot.parsing.lexer.tokens import Token as RobotToken
        from robot.parsing.model.statements import KeywordCall

        namespace = await self.parent.documents_cache.get_namespace(document)
        if namespace is None:
            return None

        kw_node = cast(KeywordCall, node)
        result = await self.get_keyworddoc_and_token_from_position(
            kw_node.keyword,
            cast(Token, kw_node.get_token(RobotToken.KEYWORD)),
            [cast(Token, t) for t in kw_node.get_tokens(RobotToken.ARGUMENT)],
            namespace,
            position,
        )

        if result is not None:
            keyword_doc, keyword_token = result

            keyword_token = self.strip_bdd_prefix(keyword_token)

            lib_entry, kw_namespace = await self.get_namespace_info_from_keyword(namespace, keyword_token)

            kw_range = range_from_token(keyword_token)

            if lib_entry and kw_namespace:
                r = range_from_token(keyword_token)
                r.end.character = r.start.character + len(kw_namespace)
                kw_range.start.character = r.end.character + 1
                if position in r:
                    # TODO namespaces
                    return None
            if (
                position in kw_range
                and keyword_doc is not None
                and not keyword_doc.is_error_handler
                and keyword_doc.source
            ):
                return (
                    keyword_doc,
                    RobotToken(
                        keyword_token.type,
                        keyword_token.value[len(kw_namespace) + 1 :],
                        keyword_token.lineno,
                        keyword_token.col_offset + len(kw_namespace) + 1,
                        keyword_token.error,
                    )
                    if lib_entry and kw_namespace
                    else keyword_token,
                )

        return None

    # async def highlight_KeywordName(  # noqa: N802
    #     self, node: ast.AST, document: TextDocument, position: Position
    # ) -> Optional[List[DocumentHighlight]]:
    #     from robot.parsing.lexer.tokens import Token as RobotToken
    #     from robot.parsing.model.statements import KeywordName

    #     namespace = await self.parent.documents_cache.get_namespace(document)
    #     if namespace is None:
    #         return None

    #     kw_node = cast(KeywordName, node)

    #     name_token = cast(RobotToken, kw_node.get_token(RobotToken.KEYWORD_NAME))

    #     if not name_token:
    #         return None

    #     doc = await namespace.get_library_doc()
    #     if doc is not None:
    #         keyword = next(
    #             (v for v in doc.keywords.keywords if v.name == name_token.value and v.line_no == kw_node.lineno),
    #             None,
    #         )

    #         if keyword is not None and keyword.source and not keyword.is_error_handler:
    #             return [
    #                 DocumentHighlight(keyword.range, DocumentHighlightKind.TEXT),
    #                 *(
    #                     DocumentHighlight(e.range, DocumentHighlightKind.TEXT)
    #                     for e in await self.parent.robot_references.find_keyword_references_in_file(document, keyword)
    #                 ),
    #             ]

    #     return None

    # async def highlight_Fixture(  # noqa: N802
    #     self, node: ast.AST, document: TextDocument, position: Position
    # ) -> Optional[List[DocumentHighlight]]:
    #     from robot.parsing.lexer.tokens import Token as RobotToken
    #     from robot.parsing.model.statements import Fixture

    #     namespace = await self.parent.documents_cache.get_namespace(document)
    #     if namespace is None:
    #         return None

    #     fixture_node = cast(Fixture, node)

    #     name_token = cast(Token, fixture_node.get_token(RobotToken.NAME))
    #     if name_token is None or name_token.value is None or name_token.value.upper() in ("", "NONE"):
    #         return None

    #     result = await self.get_keyworddoc_and_token_from_position(
    #         fixture_node.name,
    #         name_token,
    #         [cast(Token, t) for t in fixture_node.get_tokens(RobotToken.ARGUMENT)],
    #         namespace,
    #         position,
    #     )

    #     if result is not None:
    #         keyword_doc, keyword_token = result

    #         keyword_token = self.strip_bdd_prefix(keyword_token)

    #         lib_entry, kw_namespace = await self.get_namespace_info_from_keyword(namespace, keyword_token)

    #         kw_range = range_from_token(keyword_token)

    #         if lib_entry and kw_namespace:
    #             r = range_from_token(keyword_token)
    #             r.end.character = r.start.character + len(kw_namespace)
    #             kw_range.start.character = r.end.character + 1
    #             if position in r:
    #                 # TODO highlight namespaces
    #                 return None

    #         if position in kw_range and keyword_doc is not None and not keyword_doc.is_error_handler:
    #             return [
    #                 *(
    #                     [DocumentHighlight(keyword_doc.range, DocumentHighlightKind.TEXT)]
    #                     if keyword_doc.source == str(document.uri.to_path())
    #                     else []
    #                 ),
    #                 *(
    #                     DocumentHighlight(e.range, DocumentHighlightKind.TEXT)
    #                     for e in await self.parent.robot_references.find_keyword_references_in_file(
    #                         document, keyword_doc
    #                     )
    #                 ),
    #             ]

    #     return None

    # async def _highlight_Template_or_TestTemplate(  # noqa: N802
    #     self, node: ast.AST, document: TextDocument, position: Position
    # ) -> Optional[List[DocumentHighlight]]:
    #     from robot.parsing.lexer.tokens import Token as RobotToken
    #     from robot.parsing.model.statements import Template, TestTemplate

    #     template_node = cast(Union[Template, TestTemplate], node)
    #     if template_node.value:

    #         keyword_token = cast(RobotToken, template_node.get_token(RobotToken.NAME))
    #         if keyword_token is None or keyword_token.value is None or keyword_token.value.upper() in ("", "NONE"):
    #             return None

    #         keyword_token = self.strip_bdd_prefix(keyword_token)

    #         if position.is_in_range(range_from_token(keyword_token), False):
    #             namespace = await self.parent.documents_cache.get_namespace(document)
    #             if namespace is None:
    #                 return None

    #             keyword_doc = await namespace.find_keyword(template_node.value)

    #             if keyword_doc is not None:

    #                 lib_entry, kw_namespace = await self.get_namespace_info_from_keyword(namespace, keyword_token)

    #                 kw_range = range_from_token(keyword_token)

    #                 if lib_entry and kw_namespace:
    #                     r = range_from_token(keyword_token)
    #                     r.end.character = r.start.character + len(kw_namespace)
    #                     kw_range.start.character = r.end.character + 1
    #                     if position in r:
    #                         # TODO highlight namespaces
    #                         return None

    #                 if not keyword_doc.is_error_handler:
    #                     return [
    #                         *(
    #                             [DocumentHighlight(keyword_doc.range, DocumentHighlightKind.TEXT)]
    #                             if keyword_doc.source == str(document.uri.to_path())
    #                             else []
    #                         ),
    #                         *(
    #                             DocumentHighlight(e.range, DocumentHighlightKind.TEXT)
    #                             for e in await self.parent.robot_references.find_keyword_references_in_file(
    #                                 document, keyword_doc
    #                             )
    #                         ),
    #                     ]
    #     return None

    # async def highlight_TestTemplate(  # noqa: N802
    #     self, result_node: ast.AST, document: TextDocument, position: Position
    # ) -> Optional[List[DocumentHighlight]]:
    #     return await self._highlight_Template_or_TestTemplate(result_node, document, position)

    # async def highlight_Template(  # noqa: N802
    #     self, result_node: ast.AST, document: TextDocument, position: Position
    # ) -> Optional[List[DocumentHighlight]]:
    #     return await self._highlight_Template_or_TestTemplate(result_node, document, position)