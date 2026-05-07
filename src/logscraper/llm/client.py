from __future__ import annotations

import json
from typing import Any

from langsmith import traceable

from logscraper.config import AppSettings


FILE_CHANGE_READ_TIMEOUT_SECONDS = 300.0
MAX_SOURCE_FILES = 4
MAX_SOURCE_CHARS_PER_FILE = 30000
MAX_TOTAL_SOURCE_CHARS = 90000


class LLMClient:
    """Thin boundary for future fix generation.

    The V1 implementation keeps code changes dry-run-first. This class centralizes
    provider configuration so live generation can be added without touching the
    workflow graph.
    """

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def is_configured(self) -> bool:
        if self.settings.llm_provider == "azure":
            return bool(self.settings.azure_openai_endpoint and self.settings.azure_openai_api_key)
        if self.settings.llm_provider == "bedrock":
            return bool(self.settings.aws_region)
        return bool(self.settings.openai_api_key)

    @traceable(run_type="llm", name="suggest_fix")
    def suggest_fix(self, *, error_context: str, source_context: str) -> str | None:
        if not self.is_configured():
            print("[llm] skipped; LLM credentials are not configured")
            return None

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a senior .NET engineer helping an automated repair agent. "
                    "Use only the provided error and source context. "
                    "Return a concise root-cause note and a practical suggested fix."
                ),
            },
            {
                "role": "user",
                "content": "\n\n".join(
                    [
                        "Error context:",
                        error_context,
                        "Candidate source context:",
                        source_context or "No source files were mapped.",
                    ]
                ),
            },
        ]

        if self.settings.llm_provider == "bedrock":
            return self._request_bedrock(messages, temperature=0.2)
        if self.settings.llm_provider != "openai":
            print(f"[llm] skipped; provider={self.settings.llm_provider} is not implemented yet")
            return None

        try:
            import httpx
        except ImportError:
            print("[llm] skipped; httpx is not installed")
            return None

        model = self.settings.llm_model or "gpt-4o-mini"
        payload: dict[str, Any] = {
            "model": model,
            "temperature": 0.2,
            "messages": messages,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }

        print(f"[llm] requesting OpenAI analysis model={model}")
        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
                response.raise_for_status()
        except Exception as exc:
            print(f"[llm] OpenAI analysis failed: {exc}")
            return None

        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            print("[llm] OpenAI returned no choices")
            return None

        message = choices[0].get("message", {})
        content = str(message.get("content", "")).strip()
        if content:
            print("[llm] OpenAI analysis completed")
            return content

        print("[llm] OpenAI returned an empty message")
        return None

    @traceable(run_type="llm", name="generate_file_changes")
    def generate_file_changes(
        self,
        *,
        resolve_context: str,
        source_files: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        if not self.is_configured():
            print("[llm] skipped; LLM credentials are not configured")
            return []

        file_change_system_prompt = (
            "You are a senior .NET engineer applying a safe automated repair. "
            "Use only the supplied error context, prior analysis, and source files. "
            "Do not ask for more data. Return strict JSON only. Prefer small exact replacements: "
            '{"changes":[{"path":"relative/path.cs","replacements":[{"old":"exact existing text",'
            '"new":"replacement text"}]}]}. '
            "Only use complete file content when a replacement cannot be expressed: "
            '{"changes":[{"path":"relative/path.cs","content":"complete new file content"}]}. '
            "Only include files that need to change, and keep the response compact."
        )
        user_content = "\n\n".join(
            [
                "Resolve context:",
                resolve_context,
                "Allowed source files:",
                json.dumps(self._compact_source_files(source_files), ensure_ascii=False),
            ]
        )

        if self.settings.llm_provider == "bedrock":
            messages = [
                {"role": "system", "content": file_change_system_prompt},
                {"role": "user", "content": user_content},
            ]
            response_text = self._request_bedrock(messages, temperature=0.1, max_tokens=6000)
        elif self.settings.llm_provider == "openai":
            response_text = self._request_openai_file_changes(
                resolve_context=resolve_context,
                source_files=self._compact_source_files(source_files),
            )
        else:
            print(f"[llm] skipped; provider={self.settings.llm_provider} is not implemented yet")
            return []

        if not response_text:
            return []
        changes = self._parse_file_changes(response_text)
        print(f"[llm] file-change generation completed; changes={len(changes)}")
        return changes

    def _request_bedrock(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str | None:
        try:
            import boto3
        except ImportError:
            print("[llm] skipped; boto3 is not installed")
            return None

        model_id = self.settings.aws_bedrock_model_id
        region = self.settings.aws_region

        # Separate system messages from conversation messages
        system_prompt = [{"text": m["content"]} for m in messages if m["role"] == "system"]
        bedrock_messages = [
            {"role": m["role"], "content": [{"text": m["content"]}]}
            for m in messages
            if m["role"] != "system"
        ]

        print(f"[llm] requesting Bedrock analysis model={model_id} region={region}")
        try:
            client_kwargs: dict[str, Any] = {"region_name": region}
            if self.settings.aws_access_key_id:
                client_kwargs["aws_access_key_id"] = self.settings.aws_access_key_id
            if self.settings.aws_secret_access_key:
                client_kwargs["aws_secret_access_key"] = self.settings.aws_secret_access_key
            if self.settings.aws_session_token:
                client_kwargs["aws_session_token"] = self.settings.aws_session_token
            client = boto3.client("bedrock-runtime", **client_kwargs)
            response = client.converse(
                modelId=model_id,
                messages=bedrock_messages,
                system=system_prompt,
                inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
            )
        except Exception as exc:
            print(f"[llm] Bedrock request failed: {exc}")
            return None

        try:
            content_blocks = response["output"]["message"]["content"]
            text = content_blocks[0]["text"] if content_blocks else ""
        except (KeyError, IndexError):
            print("[llm] Bedrock returned unexpected response structure")
            return None

        if text.strip():
            print("[llm] Bedrock analysis completed")
            return text.strip()

        print("[llm] Bedrock returned an empty message")
        return None

    def _request_openai_file_changes(
        self,
        *,
        resolve_context: str,
        source_files: list[dict[str, str]],
    ) -> str | None:
        try:
            import httpx
        except ImportError:
            print("[llm] skipped; httpx is not installed")
            return None

        model = self.settings.llm_model or "gpt-4o-mini"
        payload: dict[str, Any] = {
            "model": model,
            "temperature": 0.1,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a senior .NET engineer applying a safe automated repair. "
                        "Use only the supplied error context, prior analysis, and source files. "
                        "Do not ask for more data. Return strict JSON only. Prefer small exact replacements: "
                        '{"changes":[{"path":"relative/path.cs","replacements":[{"old":"exact existing text",'
                        '"new":"replacement text"}]}]}. '
                        "Only use complete file content when a replacement cannot be expressed: "
                        '{"changes":[{"path":"relative/path.cs","content":"complete new file content"}]}. '
                        "Only include files that need to change, and keep the response compact."
                    ),
                },
                {
                    "role": "user",
                    "content": "\n\n".join(
                        [
                            "Resolve context:",
                            resolve_context,
                            "Allowed source files:",
                            json.dumps(source_files, ensure_ascii=False),
                        ]
                    ),
                },
            ],
            "max_tokens": 6000,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }

        print(f"[llm] requesting OpenAI file changes model={model}")
        try:
            timeout = httpx.Timeout(
                timeout=FILE_CHANGE_READ_TIMEOUT_SECONDS,
                connect=20.0,
                read=FILE_CHANGE_READ_TIMEOUT_SECONDS,
                write=60.0,
                pool=20.0,
            )
            with httpx.Client(timeout=timeout) as client:
                response = client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
                response.raise_for_status()
        except Exception as exc:
            print(f"[llm] OpenAI file-change generation failed: {exc}")
            return None

        choices = response.json().get("choices", [])
        if not choices:
            print("[llm] OpenAI returned no file-change choices")
            return None
        return str(choices[0].get("message", {}).get("content", "")).strip()

    def _parse_file_changes(self, text: str) -> list[dict[str, Any]]:
        data = self._json_from_text(text)
        changes = data.get("changes") if isinstance(data, dict) else None
        if not isinstance(changes, list):
            print("[llm] OpenAI did not return a changes list")
            return []

        parsed: list[dict[str, Any]] = []
        for change in changes:
            if not isinstance(change, dict):
                continue
            path = str(change.get("path", "")).strip()
            content = change.get("content")
            if path and isinstance(content, str):
                parsed.append({"path": path, "content": content})
                continue
            replacements = self._parse_replacements(change.get("replacements"))
            if path and replacements:
                parsed.append({"path": path, "replacements": replacements})
        return parsed

    def _parse_replacements(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        replacements: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            old = item.get("old")
            new = item.get("new")
            if isinstance(old, str) and isinstance(new, str) and old:
                replacements.append({"old": old, "new": new})
        return replacements

    def _compact_source_files(self, source_files: list[dict[str, str]]) -> list[dict[str, str]]:
        compacted: list[dict[str, str]] = []
        total_chars = 0
        for source_file in source_files[:MAX_SOURCE_FILES]:
            content = source_file.get("content", "")
            remaining = MAX_TOTAL_SOURCE_CHARS - total_chars
            if remaining <= 0:
                break
            limit = min(MAX_SOURCE_CHARS_PER_FILE, remaining)
            limited = self._limit_text(content, limit=limit)
            compacted.append(
                {
                    "path": source_file.get("path", ""),
                    "content": limited,
                }
            )
            total_chars += len(limited)
        return compacted

    def _limit_text(self, value: str, *, limit: int) -> str:
        if len(value) <= limit:
            return value
        suffix = "\n\n/* content truncated before LLM repair request */"
        return value[: max(0, limit - len(suffix))] + suffix

    def _json_from_text(self, text: str) -> Any:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`").strip()
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            for index, character in enumerate(cleaned):
                if character not in "{[":
                    continue
                try:
                    value, _end = decoder.raw_decode(cleaned[index:])
                    return value
                except json.JSONDecodeError:
                    continue
        return None
