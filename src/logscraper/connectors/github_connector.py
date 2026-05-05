from __future__ import annotations

from typing import Any

from logscraper.config import AppSettings


class GitHubConnector:
    def __init__(self, settings: AppSettings, *, timeout_seconds: float = 20.0) -> None:
        self.settings = settings
        self.timeout_seconds = timeout_seconds

    def create_pull_request(self, *, head_branch: str, title: str, body: str) -> str:
        if not self.settings.github_token:
            raise ValueError("GITHUB_TOKEN is required for live GitHub writes.")
        if not self.settings.github_repository:
            raise ValueError("GITHUB_REPO_OWNER and GITHUB_REPO_NAME are required for live GitHub writes.")

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - requires optional deps absent.
            raise RuntimeError("httpx is required for GitHub API calls. Install project dependencies.") from exc

        url = f"https://api.github.com/repos/{self.settings.github_repository}/pulls"
        payload: dict[str, Any] = {
            "title": title,
            "head": head_branch,
            "base": self.settings.github_base_branch,
            "body": body,
            "draft": True,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            return str(response.json()["html_url"])
