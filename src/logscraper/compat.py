from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from enum import Enum
from typing import Any

try:  # pragma: no cover - exercised when optional runtime deps are installed.
    from pydantic import BaseModel, ConfigDict, Field
except ImportError:  # pragma: no cover - covered indirectly by local tests.
    _MISSING = object()

    class _FieldInfo:
        def __init__(
            self,
            default: Any = _MISSING,
            *,
            default_factory: Any | None = None,
            **_: Any,
        ) -> None:
            self.default = default
            self.default_factory = default_factory

    def Field(
        default: Any = _MISSING,
        *,
        default_factory: Any | None = None,
        **kwargs: Any,
    ) -> _FieldInfo:
        return _FieldInfo(default, default_factory=default_factory, **kwargs)

    def ConfigDict(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    def _annotations_for(cls: type) -> dict[str, Any]:
        annotations: dict[str, Any] = {}
        for base in reversed(cls.__mro__):
            annotations.update(getattr(base, "__annotations__", {}))
        return annotations

    def _default_for(cls: type, name: str) -> Any:
        default = getattr(cls, name, _MISSING)
        if isinstance(default, _FieldInfo):
            if default.default_factory is not None:
                return default.default_factory()
            if default.default is not _MISSING:
                return deepcopy(default.default)
            return None
        if default is _MISSING:
            return None
        return deepcopy(default)

    def _dump(value: Any, *, mode: str | None = None) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump(mode=mode)
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, (datetime, date)):
            return value.isoformat() if mode == "json" else value
        if isinstance(value, list):
            return [_dump(item, mode=mode) for item in value]
        if isinstance(value, tuple):
            return tuple(_dump(item, mode=mode) for item in value)
        if isinstance(value, dict):
            return {key: _dump(item, mode=mode) for key, item in value.items()}
        return value

    class BaseModel:
        model_config: dict[str, Any] = {}

        def __init__(self, **data: Any) -> None:
            annotations = _annotations_for(self.__class__)
            for name in annotations:
                if name == "model_config":
                    continue
                setattr(self, name, data.pop(name, _default_for(self.__class__, name)))
            for name, value in data.items():
                setattr(self, name, value)

        @classmethod
        def model_validate(cls, value: Any) -> Any:
            if isinstance(value, cls):
                return value
            if isinstance(value, dict):
                return cls(**value)
            raise TypeError(f"Cannot validate {type(value)!r} as {cls.__name__}")

        def model_dump(self, *, mode: str | None = None, **_: Any) -> dict[str, Any]:
            annotations = _annotations_for(self.__class__)
            return {
                name: _dump(getattr(self, name), mode=mode)
                for name in annotations
                if name != "model_config"
            }

        def model_copy(self, *, update: dict[str, Any] | None = None, **_: Any) -> Any:
            data = self.model_dump()
            if update:
                data.update(update)
            return self.__class__(**data)

        def __repr__(self) -> str:
            args = ", ".join(f"{key}={value!r}" for key, value in self.model_dump().items())
            return f"{self.__class__.__name__}({args})"
