from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from logscraper.models import StackFrame


_IGNORED_DIRS = {"bin", "obj", ".git", ".vs", "packages", "node_modules"}


@dataclass(frozen=True)
class DotNetRepoInventory:
    root: Path
    solutions: list[Path] = field(default_factory=list)
    projects: list[Path] = field(default_factory=list)
    source_files: list[Path] = field(default_factory=list)
    test_projects: list[Path] = field(default_factory=list)


class DotNetRepoAnalyzer:
    def __init__(self, repo_path: str | Path) -> None:
        self.root = Path(repo_path).expanduser().resolve()

    def inventory(self) -> DotNetRepoInventory:
        solutions = self._find_files("*.sln")
        projects = self._find_files("*.csproj")
        source_files = self._find_files("*.cs")
        test_projects = [project for project in projects if self._is_test_project(project)]
        return DotNetRepoInventory(
            root=self.root,
            solutions=solutions,
            projects=projects,
            source_files=source_files,
            test_projects=test_projects,
        )

    def find_candidate_files(self, frames: Iterable[StackFrame], *, limit: int = 5) -> list[Path]:
        inventory = self.inventory()
        scores: dict[Path, int] = {}
        for frame in frames:
            frame_scores = self._score_frame_against_files(frame, inventory.source_files)
            for path, score in frame_scores.items():
                scores[path] = max(scores.get(path, 0), score)
        ranked = sorted(scores.items(), key=lambda item: (-item[1], str(item[0])))
        return [path for path, score in ranked[:limit] if score > 0]

    def suggest_test_commands(self, candidate_files: Iterable[Path]) -> list[str]:
        inventory = self.inventory()
        commands: list[str] = []
        target = inventory.solutions[0] if inventory.solutions else (inventory.projects[0] if inventory.projects else None)
        if target:
            commands.append(f"dotnet build {self._relative(target)}")
        for project in inventory.test_projects[:3]:
            commands.append(f"dotnet test {self._relative(project)}")
        if not commands and list(candidate_files):
            commands.append("dotnet build")
        return commands

    def _score_frame_against_files(self, frame: StackFrame, source_files: list[Path]) -> dict[Path, int]:
        scores: dict[Path, int] = {}
        if frame.file_path:
            frame_path = Path(frame.file_path)
            for source_file in source_files:
                if source_file.name.lower() == frame_path.name.lower():
                    scores[source_file] = max(scores.get(source_file, 0), 8)

        class_pattern = re.compile(
            rf"\b(class|record|struct|interface)\s+{re.escape(frame.class_name or '')}\b"
        ) if frame.class_name else None
        method_pattern = re.compile(rf"\b{re.escape(frame.method_name or '')}\s*(?:<|\()", re.M) if frame.method_name else None
        namespace_pattern = re.compile(rf"\bnamespace\s+{re.escape(frame.namespace or '')}\b") if frame.namespace else None

        for source_file in source_files:
            score = scores.get(source_file, 0)
            try:
                text = source_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if class_pattern and class_pattern.search(text):
                score += 4
            if method_pattern and method_pattern.search(text):
                score += 2
            if namespace_pattern and namespace_pattern.search(text):
                score += 1
            if score:
                scores[source_file] = score
        return scores

    def _find_files(self, pattern: str) -> list[Path]:
        if not self.root.exists():
            return []
        files: list[Path] = []
        for path in self.root.rglob(pattern):
            if any(part in _IGNORED_DIRS for part in path.parts):
                continue
            files.append(path)
        return sorted(files)

    def _is_test_project(self, project: Path) -> bool:
        name = project.stem.lower()
        if "test" in name or "spec" in name:
            return True
        try:
            text = project.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            return False
        return "microsoft.net.test.sdk" in text or "xunit" in text or "nunit" in text

    def _relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)
