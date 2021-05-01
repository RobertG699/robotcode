from __future__ import annotations

import itertools
import threading
import weakref
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Any, Deque, Dict, List, Literal, NamedTuple, Optional

from ...utils.event import event
from ...utils.logging import LoggingDescriptor
from ..types import (
    Breakpoint,
    ContinuedEvent,
    ContinuedEventBody,
    Event,
    OutputCategory,
    OutputEvent,
    OutputEventBody,
    Scope,
    Source,
    SourceBreakpoint,
    StackFrame,
    StackFrameFormat,
    SteppingGranularity,
    StoppedEvent,
    StoppedEventBody,
    StoppedReason,
    Thread,
    ValueFormat,
    Variable,
)


class State(Enum):
    Stopped = 0
    Running = 1
    Paused = 2


class RequestedState(Enum):
    Nothing = 0
    Pause = 1
    Next = 2
    StepIn = 3
    StepOut = 4


class BreakpointsEntry:
    def __init__(self, breakpoints: List[SourceBreakpoint], lines: List[int]) -> None:
        self.breakpoints = breakpoints
        self.lines = lines


class StackTraceResult(NamedTuple):
    stack_frames: List[StackFrame]
    total_frames: int


class StackFrameEntry:
    def __init__(
        self, context: weakref.ref[Any], name: str, type: str, source: str, line: int, column: int = 1
    ) -> None:
        self.context = context
        self.name = name
        self.type = type
        self.source = source
        self.line = line
        self.column = column
        self._suite_marker = object()
        self._test_marker = object()
        self._local_marker = object()
        self._global_marker = object()

    @property
    def id(self) -> int:
        return id(self)

    def test_id(self) -> int:
        return id(self._test_marker)

    def suite_id(self) -> int:
        return id(self._suite_marker)

    def local_id(self) -> int:
        return id(self._local_marker)

    def global_id(self) -> int:
        return id(self._global_marker)


class Debugger:
    __instance = None
    __lock = threading.RLock()
    __inside_instance = False

    _logger = LoggingDescriptor()

    @classmethod
    def instance(cls) -> Debugger:
        if cls.__instance is not None:
            return cls.__instance
        with cls.__lock:
            # re-check, perhaps it was created in the mean time...
            if cls.__instance is None:
                cls.__inside_instance = True
                try:
                    cls.__instance = cls()
                finally:
                    cls.__inside_instance = False
        return cls.__instance

    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        if cls.__instance is None:
            with cls.__lock:
                if cls.__instance is None and cls.__inside_instance:
                    return super().__new__(cls)

        raise RuntimeError(f"Attempt to create a '{cls.__qualname__}' instance outside of instance()")

    def __init__(self) -> None:
        self.breakpoints: Dict[str, BreakpointsEntry] = {}
        self.main_thread: Optional[threading.Thread] = None
        self.stack_frames: Deque[StackFrameEntry] = deque()
        self.condition = threading.Condition()
        self.state: State = State.Stopped
        self.requested_state: RequestedState = RequestedState.Nothing
        self.stop_stack_len = 0

    @_logger.call
    def start(self) -> None:
        with self.condition:
            self.state = State.Running
            self.condition.notify_all()

    @_logger.call
    def stop(self) -> None:
        with self.condition:
            self.state = State.Stopped

            if self.main_thread is not None and self.main_thread.ident:
                self.send_event(
                    self,
                    ContinuedEvent(
                        body=ContinuedEventBody(thread_id=self.main_thread.ident, all_threads_continued=True)
                    ),
                )

            self.condition.notify_all()

    @_logger.call
    def continue_thread(self, thread_id: int) -> None:
        if self.main_thread is None or thread_id != self.main_thread.ident:
            raise RuntimeError("Invalid threadId")

        with self.condition:
            self.state = State.Running
            self.condition.notify_all()

    @_logger.call
    def pause_thread(self, thread_id: int) -> None:
        if self.main_thread is None or thread_id != self.main_thread.ident:
            raise RuntimeError("Invalid threadId")

        with self.condition:
            self.requested_state = RequestedState.Pause
            self.state = State.Paused

            self.condition.notify_all()

    @_logger.call
    def next(self, thread_id: int, granularity: Optional[SteppingGranularity] = None) -> None:
        if self.main_thread is None or thread_id != self.main_thread.ident:
            raise RuntimeError("Invalid threadId")

        with self.condition:
            self.state = State.Running

            if self.stack_frames and self.stack_frames[0].type in ["TEST", "SUITE"]:
                self.requested_state = RequestedState.StepIn
            else:
                self.requested_state = RequestedState.Next

                self.stop_stack_len = len(self.stack_frames)
                if self.stack_frames and self.stack_frames[0].type in ["FOR", "FOR ITERATION", "IF", "ELSE", "ELSE IF"]:
                    self.stop_stack_len += 1

            self.condition.notify_all()

    @_logger.call
    def step_in(
        self, thread_id: int, target_id: Optional[int] = None, granularity: Optional[SteppingGranularity] = None
    ) -> None:
        if self.main_thread is None or thread_id != self.main_thread.ident:
            raise RuntimeError("Invalid threadId")

        with self.condition:
            self.requested_state = RequestedState.StepIn
            self.state = State.Running

            self.condition.notify_all()

    @_logger.call
    def step_out(self, thread_id: int, granularity: Optional[SteppingGranularity] = None) -> None:
        if self.main_thread is None or thread_id != self.main_thread.ident:
            raise RuntimeError("Invalid threadId")

        with self.condition:
            self.requested_state = RequestedState.StepOut
            self.state = State.Running
            self.stop_stack_len = len(self.stack_frames) - 1

            i = 1

            while i < len(self.stack_frames) and self.stack_frames[i].type in [
                "FOR",
                "FOR ITERATION",
                "IF",
                "ELSE",
                "ELSE IF",
            ]:
                self.stop_stack_len -= 1
                i += 1

            self.condition.notify_all()

    @event
    def send_event(sender, event: Event) -> None:
        ...

    def set_breakpoints(
        self,
        source: Source,
        breakpoints: Optional[List[SourceBreakpoint]] = None,
        lines: Optional[List[int]] = None,
        source_modified: Optional[bool] = None,
    ) -> List[Breakpoint]:
        path = str(Path(source.path).resolve()) if source.path else ""

        if path in self.breakpoints and not breakpoints and not lines:
            self.breakpoints.pop(path)
        elif path:
            self.breakpoints[path] = result = BreakpointsEntry(breakpoints or [], lines or [])
            return [
                Breakpoint(id=id(v), source=Source(path=path), verified=True, line=v.line) for v in result.breakpoints
            ]
        else:
            self._logger.error("not supported breakpoint")

        return []

    def process_state(self, source: str, line_no: int, type: str) -> None:
        if self.state == State.Stopped:
            return

        elif self.requested_state == RequestedState.Pause:
            self.state = State.Paused
            self.send_event(
                self,
                StoppedEvent(
                    body=StoppedEventBody(
                        reason=StoppedReason.PAUSE,
                        thread_id=threading.current_thread().ident,
                    )
                ),
            )
            self.requested_state = RequestedState.Nothing
        elif self.requested_state == RequestedState.Next:
            if len(self.stack_frames) <= self.stop_stack_len:
                self.state = State.Paused
                self.send_event(
                    self,
                    StoppedEvent(
                        body=StoppedEventBody(
                            reason=StoppedReason.STEP,
                            thread_id=threading.current_thread().ident,
                        )
                    ),
                )
                self.requested_state = RequestedState.Nothing
        elif self.requested_state == RequestedState.StepIn:
            self.state = State.Paused
            self.send_event(
                self,
                StoppedEvent(
                    body=StoppedEventBody(
                        reason=StoppedReason.STEP,
                        thread_id=threading.current_thread().ident,
                    )
                ),
            )
            self.requested_state = RequestedState.Nothing
        elif self.requested_state == RequestedState.StepOut:
            if len(self.stack_frames) <= self.stop_stack_len:
                self.state = State.Paused
                self.send_event(
                    self,
                    StoppedEvent(
                        body=StoppedEventBody(
                            reason=StoppedReason.STEP,
                            thread_id=threading.current_thread().ident,
                        )
                    ),
                )
                self.requested_state = RequestedState.Nothing

        source = str(Path(source).resolve())
        if source in self.breakpoints:
            breakpoints = [v for v in self.breakpoints[source].breakpoints if v.line == line_no]
            if len(breakpoints) > 0:
                self.state = State.Paused
                self.send_event(
                    self,
                    StoppedEvent(
                        body=StoppedEventBody(
                            reason=StoppedReason.BREAKPOINT,
                            thread_id=threading.current_thread().ident,
                            hit_breakpoint_ids=[id(v) for v in breakpoints],
                        )
                    ),
                )

    @_logger.call
    def wait_for_running(self) -> None:
        with self.condition:
            self.condition.wait_for(lambda: self.state in [State.Running, State.Stopped])

    def start_suite(self, name: str, attributes: Dict[str, Any]) -> None:
        from robot.running.context import EXECUTION_CONTEXTS

        source = attributes.get("source", None)
        line_no = attributes.get("lineno", 1)
        longname = attributes.get("longname", "")
        type = "SUITE"

        self.stack_frames.appendleft(
            StackFrameEntry(weakref.ref(EXECUTION_CONTEXTS.current), longname, type, source, line_no)
        )

        self.process_state(source, line_no, type)

        self.wait_for_running()

    def end_suite(self, name: str, attributes: Dict[str, Any]) -> None:
        if self.stack_frames:
            self.stack_frames.popleft()

    def start_test(self, name: str, attributes: Dict[str, Any]) -> None:
        from robot.running.context import EXECUTION_CONTEXTS

        source = attributes.get("source", None)
        line_no = attributes.get("lineno", 1)
        longname = attributes.get("longname", "")
        type = "TEST"

        self.stack_frames.appendleft(
            StackFrameEntry(weakref.ref(EXECUTION_CONTEXTS.current), longname, type, source, line_no)
        )

        self.process_state(source, line_no, type)

        self.wait_for_running()

    def end_test(self, name: str, attributes: Dict[str, Any]) -> None:
        if self.stack_frames:
            self.stack_frames.popleft()

    def start_keyword(self, name: str, attributes: Dict[str, Any]) -> None:
        from robot.running.context import EXECUTION_CONTEXTS

        status = attributes.get("status", "")

        if status == "NOT RUN":
            return

        source = attributes.get("source", None)
        line_no = attributes.get("lineno", 1)
        longname = attributes.get("kwname", "")
        type = attributes.get("type", "KEYWORD")

        self.stack_frames.appendleft(
            StackFrameEntry(weakref.ref(EXECUTION_CONTEXTS.current), longname, type, source, line_no)
        )

        self.process_state(source, line_no, type)

        self.wait_for_running()

    def end_keyword(self, name: str, attributes: Dict[str, Any]) -> None:
        status = attributes.get("status", "")

        if status == "NOT RUN":
            return

        if self.stack_frames:
            self.stack_frames.popleft()

    def set_main_thread(self, thread: threading.Thread) -> None:
        self.main_thread = thread

    def get_threads(self) -> List[Thread]:
        main_thread = self.main_thread or threading.main_thread()

        return [Thread(id=main_thread.ident if main_thread.ident else 0, name=main_thread.name or "")]

    def get_stack_trace(
        self,
        thread_id: int,
        start_frame: Optional[int] = None,
        levels: Optional[int] = None,
        format: Optional[StackFrameFormat] = None,
    ) -> StackTraceResult:
        start_frame = start_frame or 0
        levels = start_frame + 1 + (levels or len(self.stack_frames))
        return StackTraceResult(
            [
                StackFrame(id=v.id, name=v.name, line=v.line, column=v.column, source=Source(path=v.source))
                for v in itertools.islice(self.stack_frames, start_frame, levels)
            ],
            len(self.stack_frames),
        )

    def log_message(self, message: Dict[str, Any]) -> None:
        self.send_event(
            self,
            OutputEvent(
                body=OutputEventBody(output="LOG> {timestamp} {level}: {message}\n".format(**message), category="log")
            ),
        )

    def message(self, message: Dict[str, Any]) -> None:
        self.send_event(
            self,
            OutputEvent(
                body=OutputEventBody(
                    output="MSG> {timestamp} {level}: {message}\n".format(**message), category=OutputCategory.CONSOLE
                )
            ),
        )

    def get_scopes(self, frame_id: int) -> List[Scope]:
        result: List[Scope] = []
        entry = next((v for v in self.stack_frames if v.id == frame_id), None)
        if entry is not None:
            context = entry.context()
            if context is not None:
                result.append(
                    Scope(
                        name="Local",
                        expensive=False,
                        presentation_hint="local",
                        variables_reference=entry.local_id(),
                    )
                )
                if context.variables._test is not None and context.variables._test != context.variables.current:
                    result.append(
                        Scope(
                            name="Test",
                            expensive=False,
                            presentation_hint="test",
                            variables_reference=entry.test_id(),
                        )
                    )
                if context.variables._suite is not None and context.variables._suite != context.variables.current:
                    result.append(
                        Scope(
                            name="Suite",
                            expensive=False,
                            presentation_hint="suite",
                            variables_reference=entry.suite_id(),
                        )
                    )
                if context.variables._global is not None:
                    result.append(
                        Scope(
                            name="Global",
                            expensive=False,
                            presentation_hint="global",
                            variables_reference=entry.global_id(),
                        )
                    )

        return result

    def get_variables(
        self,
        variables_reference: int,
        filter: Optional[Literal["indexed", "named"]] = None,
        start: Optional[int] = None,
        count: Optional[int] = None,
        format: Optional[ValueFormat] = None,
    ) -> List[Variable]:
        result: List[Variable] = []
        entry = next(
            (
                v
                for v in self.stack_frames
                if variables_reference in [v.global_id(), v.local_id(), v.suite_id(), v.test_id()]
            ),
            None,
        )
        if entry is not None:
            context = entry.context()
            if context is not None:

                if entry.global_id() == variables_reference:
                    result += [
                        Variable(name=k, value=repr(v), type=repr(type(v)))
                        for k, v in context.variables._global.as_dict().items()
                    ]
                elif entry.suite_id() == variables_reference:
                    # current_index = context.variables._scopes.index(context.variables._suite)
                    # globals = context.variables._scopes[max(current_index - 1, 0)].as_dict()
                    globals = context.variables._global.as_dict()
                    result += [
                        Variable(name=k, value=repr(v), type=repr(type(v)))
                        for k, v in context.variables._suite.as_dict().items()
                        if k not in globals or globals[k] != v
                    ]
                elif entry.test_id() == variables_reference:
                    # current_index = context.variables._scopes.index(context.variables._test)
                    # globals = context.variables._scopes[max(current_index - 1, 0)].as_dict()
                    globals = context.variables._suite.as_dict()
                    result += [
                        Variable(name=k, value=repr(v), type=repr(type(v)))
                        for k, v in context.variables._test.as_dict().items()
                        if k not in globals or globals[k] != v
                    ]
                elif entry.local_id() == variables_reference:
                    current_index = context.variables._scopes.index(context.variables.current)
                    globals = context.variables._scopes[max(current_index - 1, 0)].as_dict()
                    result += [
                        Variable(name=k, value=repr(v), type=repr(type(v)))
                        for k, v in context.variables.current.as_dict().items()
                        if k not in globals or globals[k] != v
                    ]

        return result