from __future__ import annotations

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
