from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    command: str
    return_code: int
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        return self.return_code == 0


class DotNetTestRunner:
    def __init__(self, repo_path: str | Path, *, timeout_seconds: int = 300) -> None:
        self.repo_path = Path(repo_path).expanduser().resolve()
        self.timeout_seconds = timeout_seconds

    def run(self, command: str) -> CommandResult:
        completed = subprocess.run(
            command,
            cwd=self.repo_path,
            shell=True,
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        return CommandResult(
            command=command,
            return_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
