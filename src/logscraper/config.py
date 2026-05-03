from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _read_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    env_path = Path(path)
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _get_setting(
    overrides: dict[str, Any],
    env: dict[str, str],
    name: str,
    *env_names: str,
    default: Any = "",
) -> Any:
    if name in overrides:
        return overrides[name]
    for env_name in env_names:
        value = env.get(env_name)
        if value not in (None, ""):
            return value
    return default


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _parse_csv(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip().upper() for item in value if str(item).strip()]
    return [item.strip().upper() for item in str(value).split(",") if item.strip()]


class AppSettings:
    """Application settings loaded from `.env`, environment variables, or tests.

    Put secrets in `.env` or your shell environment, never in this file.
    """

    def __init__(self, _env_file: str = ".env", **overrides: Any) -> None:
        env = _read_env_file(_env_file)
        env.update(os.environ)

        self.datadog_api_key = str(
            _get_setting(overrides, env, "datadog_api_key", "DATADOG_API_KEY", "DD_API_KEY")
        )
        self.datadog_app_key = str(
            _get_setting(overrides, env, "datadog_app_key", "DATADOG_APP_KEY", "DD_APP_KEY")
        )
        self.datadog_site = str(
            _get_setting(overrides, env, "datadog_site", "DATADOG_SITE", default="datadoghq.com")
        )
        self.datadog_service = str(_get_setting(overrides, env, "datadog_service", "DATADOG_SERVICE"))
        self.datadog_env = str(_get_setting(overrides, env, "datadog_env", "DATADOG_ENV", default="prod"))
        self.datadog_log_levels = _parse_csv(
            _get_setting(
                overrides,
                env,
                "datadog_log_levels",
                "DATADOG_LOG_LEVELS",
                default="ERROR,CRITICAL,FATAL",
            )
        )
        self.datadog_lookback_minutes = int(
            _get_setting(overrides, env, "datadog_lookback_minutes", "DATADOG_LOOKBACK_MINUTES", default=60)
        )

        self.github_token = str(_get_setting(overrides, env, "github_token", "GITHUB_TOKEN"))
        self.github_repo_owner = str(_get_setting(overrides, env, "github_repo_owner", "GITHUB_REPO_OWNER"))
        self.github_repo_name = str(_get_setting(overrides, env, "github_repo_name", "GITHUB_REPO_NAME"))
        self.github_base_branch = str(
            _get_setting(overrides, env, "github_base_branch", "GITHUB_BASE_BRANCH", default="main")
        )

        self.openai_api_key = str(_get_setting(overrides, env, "openai_api_key", "OPENAI_API_KEY"))
        self.azure_openai_endpoint = str(
            _get_setting(overrides, env, "azure_openai_endpoint", "AZURE_OPENAI_ENDPOINT")
        )
        self.azure_openai_api_key = str(
            _get_setting(overrides, env, "azure_openai_api_key", "AZURE_OPENAI_API_KEY")
        )
        self.llm_provider = str(_get_setting(overrides, env, "llm_provider", "LLM_PROVIDER", default="openai"))
        self.llm_model = str(_get_setting(overrides, env, "llm_model", "LLM_MODEL"))

        self.db_path = str(_get_setting(overrides, env, "db_path", "DB_PATH", default="./data/logscraper.db"))
        self.dry_run = _parse_bool(_get_setting(overrides, env, "dry_run", "DRY_RUN", default="true"))
        self.dotnet_repo_path = str(_get_setting(overrides, env, "dotnet_repo_path", "DOTNET_REPO_PATH"))
        self.workflow_graph_path = str(
            _get_setting(overrides, env, "workflow_graph_path", "WORKFLOW_GRAPH_PATH", default="")
        )
        self.workflow_graph_only = _parse_bool(
            _get_setting(overrides, env, "workflow_graph_only", "WORKFLOW_GRAPH_ONLY", default="false")
        )

        self.max_logs_per_run = int(
            _get_setting(overrides, env, "max_logs_per_run", "MAX_LOGS_PER_RUN", default=1000)
        )
        self.similarity_threshold = float(
            _get_setting(overrides, env, "similarity_threshold", "SIMILARITY_THRESHOLD", default=0.85)
        )

    @property
    def datadog_base_url(self) -> str:
        return f"https://api.{self.datadog_site.strip('/')}"

    @property
    def github_repository(self) -> str:
        if not self.github_repo_owner or not self.github_repo_name:
            return ""
        return f"{self.github_repo_owner}/{self.github_repo_name}"

    @property
    def dotnet_repo(self) -> Path | None:
        return Path(self.dotnet_repo_path).expanduser().resolve() if self.dotnet_repo_path else None
