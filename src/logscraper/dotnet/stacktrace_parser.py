from __future__ import annotations

import re
from dataclasses import dataclass

from logscraper.models import StackFrame


_EXCEPTION_HEADER_RE = re.compile(
    r"^\s*(?P<type>(?:[A-Za-z_][\w`]*\.)+[A-Za-z_][\w`]*(?:Exception|Error)?):\s*(?P<message>.*)\s*$"
)
_FRAME_RE = re.compile(
    r"^\s*at\s+(?P<member>.+?)(?:\s+in\s+(?P<file>.*?):line\s+(?P<line>\d+))?\s*$"
)
_ASYNC_MARKER_RE = re.compile(r"^---\s+End of stack trace", re.I)


@dataclass(frozen=True)
class ParsedException:
    exception_type: str | None
    message: str
    stack_trace: str
    frames: list[StackFrame]


class DotNetStackTraceParser:
    def parse(self, text: str | None) -> ParsedException:
        value = text or ""
        lines = value.splitlines()
        exception_type: str | None = None
        message = ""
        stack_start = 0

        for index, line in enumerate(lines):
            header = _EXCEPTION_HEADER_RE.match(line)
            if header:
                exception_type = header.group("type")
                message = header.group("message").strip()
                stack_start = index + 1
                break

        stack_trace = "\n".join(lines[stack_start:]).strip() if stack_start else value.strip()
        frames = self.parse_stack_trace(stack_trace)
        return ParsedException(
            exception_type=exception_type,
            message=message or value.strip(),
            stack_trace=stack_trace,
            frames=frames,
        )

    def parse_stack_trace(self, stack_trace: str | None) -> list[StackFrame]:
        frames: list[StackFrame] = []
        for raw_line in (stack_trace or "").splitlines():
            line = raw_line.strip()
            if not line or _ASYNC_MARKER_RE.match(line):
                continue
            match = _FRAME_RE.match(line)
            if not match:
                continue
            member = match.group("member").strip()
            namespace, class_name, method_name = self._split_member(member)
            line_number = int(match.group("line")) if match.group("line") else None
            frames.append(
                StackFrame(
                    raw=line,
                    namespace=namespace,
                    class_name=class_name,
                    method_name=method_name,
                    file_path=match.group("file"),
                    line_number=line_number,
                )
            )
        return frames

    def _split_member(self, member: str) -> tuple[str | None, str | None, str | None]:
        without_parameters = member.split("(", 1)[0].strip()
        without_parameters = without_parameters.replace("+", ".")
        parts = [part for part in without_parameters.split(".") if part]
        if len(parts) >= 3:
            return ".".join(parts[:-2]), parts[-2], parts[-1]
        if len(parts) == 2:
            return None, parts[0], parts[1]
        if len(parts) == 1:
            return None, None, parts[0]
        return None, None, None
