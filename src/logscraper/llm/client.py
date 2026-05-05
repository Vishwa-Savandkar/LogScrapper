from __future__ import annotations

from typing import Any

from logscraper.config import AppSettings


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
        return bool(self.settings.openai_api_key)

    def suggest_fix(self, *, error_context: str, source_context: str) -> str | None:
        if not self.is_configured():
            print("[llm] skipped; OpenAI credentials are not configured")
            return None
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
            "messages": [
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
            ],
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
