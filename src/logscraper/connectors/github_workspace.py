from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from logscraper.config import AppSettings


class GitHubRepoWorkspace:
    def __init__(self, settings: AppSettings, workspace_root: str | Path = "data/workspaces") -> None:
        self.settings = settings
        self.workspace_root = Path(workspace_root)

    def prepare(self, fingerprint: str) -> Path:
        if not self.settings.github_token:
            raise ValueError("GITHUB_TOKEN is required to clone the GitHub repository.")
        if not self.settings.github_repository:
            raise ValueError("GITHUB_REPO_OWNER and GITHUB_REPO_NAME are required to clone the GitHub repository.")

        run_folder = self._safe_name(fingerprint)
        repo_path = self.workspace_root / run_folder / "repo"
        repo_url = f"https://github.com/{self.settings.github_repository}.git"
        branch = self.settings.github_base_branch or "main"

        if (repo_path / ".git").exists():
            print(f"[github] refreshing {self.settings.github_repository} branch={branch}")
            self._run_git(["fetch", "origin", branch], cwd=repo_path)
            self._run_git(["checkout", branch], cwd=repo_path)
            self._run_git(["pull", "--ff-only", "origin", branch], cwd=repo_path)
            print(f"[github] repo ready path={repo_path}")
            return repo_path

        repo_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"[github] cloning {self.settings.github_repository} branch={branch}")
        self._run_git(["clone", "--branch", branch, "--single-branch", repo_url, str(repo_path)], cwd=None)
        print(f"[github] repo ready path={repo_path}")
        return repo_path

    def _run_git(self, args: list[str], cwd: Path | None) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            askpass_path = self._write_askpass_script(Path(temp_dir))
            env = os.environ.copy()
            env["GIT_ASKPASS"] = str(askpass_path)
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GITHUB_TOKEN"] = self.settings.github_token

            completed = subprocess.run(
                ["git", *args],
                cwd=str(cwd) if cwd else None,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"git {' '.join(args[:2])} failed: {message}")

    def _write_askpass_script(self, folder: Path) -> Path:
        if os.name == "nt":
            path = folder / "git-askpass.cmd"
            path.write_text(
                "\n".join(
                    [
                        "@echo off",
                        "echo %* | findstr /I \"Username\" >nul",
                        "if %ERRORLEVEL%==0 (",
                        "  echo x-access-token",
                        ") else (",
                        "  echo %GITHUB_TOKEN%",
                        ")",
                    ]
                ),
                encoding="utf-8",
            )
            return path

        path = folder / "git-askpass.sh"
        path.write_text(
            "\n".join(
                [
                    "#!/bin/sh",
                    "case \"$1\" in",
                    "  *Username*) printf '%s\\n' 'x-access-token' ;;",
                    "  *) printf '%s\\n' \"$GITHUB_TOKEN\" ;;",
                    "esac",
                ]
            ),
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def _safe_name(self, value: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")
        return safe or "run"
