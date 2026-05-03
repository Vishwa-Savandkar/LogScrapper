from __future__ import annotations

import hashlib
import re
from typing import Iterable, Protocol


class FrameLike(Protocol):
    namespace: str | None
    class_name: str | None
    method_name: str | None
    file_path: str | None
    line_number: int | None
    raw: str


_GUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_ISO_TIMESTAMP_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b")
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
_REQUEST_ID_RE = re.compile(r"\b((?:request|trace|span|correlation|user|session)[-_ ]?id)[:= ]+[^\s,;]+", re.I)
_NUMBER_RE = re.compile(r"\b\d+\b")
_LINE_RE = re.compile(r":line\s+\d+", re.I)
_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s)]+|/[^:\s)]+")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_error_message(message: str | None) -> str:
    value = message or ""
    value = _REQUEST_ID_RE.sub(r"\1=<id>", value)
    value = _GUID_RE.sub("<guid>", value)
    value = _ISO_TIMESTAMP_RE.sub("<timestamp>", value)
    value = _HEX_RE.sub("<hex>", value)
    value = _NUMBER_RE.sub("<num>", value)
    value = _WHITESPACE_RE.sub(" ", value)
    return value.strip().lower()


def normalize_stack_trace(stack_trace: str | None) -> str:
    value = stack_trace or ""
    value = _GUID_RE.sub("<guid>", value)
    value = _ISO_TIMESTAMP_RE.sub("<timestamp>", value)
    value = _HEX_RE.sub("<hex>", value)
    value = _LINE_RE.sub(":line <num>", value)
    value = _PATH_RE.sub("<path>", value)
    value = _NUMBER_RE.sub("<num>", value)
    value = _WHITESPACE_RE.sub(" ", value)
    return value.strip().lower()


def frame_key(frame: FrameLike) -> str:
    parts = [
        frame.namespace or "",
        frame.class_name or "",
        frame.method_name or "",
    ]
    key = ".".join(part for part in parts if part)
    return key.lower() or normalize_stack_trace(frame.raw)


def meaningful_frame_keys(frames: Iterable[FrameLike], *, limit: int = 3) -> list[str]:
    keys: list[str] = []
    for frame in frames:
        key = frame_key(frame)
        if key and key not in keys:
            keys.append(key)
        if len(keys) >= limit:
            break
    return keys


def fingerprint_error(
    *,
    exception_type: str | None,
    message: str | None,
    stack_trace: str | None = None,
    frames: Iterable[FrameLike] = (),
) -> str:
    pieces = [
        (exception_type or "unknown").strip().lower(),
        normalize_error_message(message),
    ]
    frame_keys = meaningful_frame_keys(frames)
    if frame_keys:
        pieces.extend(frame_keys)
    else:
        pieces.append(normalize_stack_trace(stack_trace))
    canonical = "\n".join(piece for piece in pieces if piece)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
