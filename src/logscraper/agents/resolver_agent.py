from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from logscraper.config import AppSettings
from logscraper.connectors.github_connector import GitHubConnector
from logscraper.llm.client import LLMClient
from logscraper.models import CodeFixPlan, PullRequestResult, ResolutionTask


class ResolverAgent:
    def __init__(
        self,
        settings: AppSettings,
        github: GitHubConnector | None = None,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.settings = settings
        self.github = github or GitHubConnector(settings)
        self.llm_client = llm_client or LLMClient(settings)

    def prepare_pull_request(
        self,
        task: ResolutionTask,
        plan: CodeFixPlan,
        repo_path: str | Path | None = None,
    ) -> PullRequestResult:
        return self.apply_or_dry_run_fix(task, plan, repo_path=repo_path)

    def apply_or_dry_run_fix(
        self,
        task: ResolutionTask,
        plan: CodeFixPlan,
        repo_path: str | Path | None = None,
    ) -> PullRequestResult:
        event = task.log_event
        primary_message = event.exception_message or event.error_message
        branch_name = self._branch_name(event.exception_type, task.fingerprint)
        commit_message = self._commit_message(event.exception_type, primary_message)
        pr_title = self._pr_title(event.exception_type, primary_message)
        pr_body = self._pr_body(task, plan, files_changed=[], tests_run=plan.suggested_tests)

        if self.settings.dry_run:
            return PullRequestResult(
                dry_run=True,
                branch_name=branch_name,
                commit_message=commit_message,
                pr_title=pr_title,
                pr_body=pr_body,
                pr_url=None,
                files_changed=[],
                tests_run=plan.suggested_tests,
                validation_passed=None,
            )

        if repo_path is None:
            raise RuntimeError("A prepared repository path is required before applying a live fix.")

        repo = Path(repo_path).expanduser().resolve()
        self._checkout_fix_branch(repo, branch_name)
        source_files = self._read_allowed_files(repo, plan.files_allowed_to_edit)
        if not source_files:
            raise RuntimeError("No editable source files were mapped from the stack trace.")

        changes = self.llm_client.generate_file_changes(
            resolve_context=self._resolve_context(task, plan),
            source_files=source_files,
        )
        files_changed = self._apply_file_changes(repo, changes, plan.files_allowed_to_edit)
        if not files_changed:
            raise RuntimeError("The LLM did not return any applicable file changes.")

        validation_passed, tests_run = self._validate_and_retry(
            repo, task, plan, source_files, files_changed,
        )

        self._commit_changes(repo, files_changed, commit_message)
        self._push_branch(repo, branch_name)
        pr_body = self._pr_body(task, plan, files_changed=files_changed, tests_run=tests_run)
        pr_url = self.github.create_pull_request(
            head_branch=branch_name,
            title=pr_title,
            body=pr_body,
        )

        return PullRequestResult(
            dry_run=self.settings.dry_run,
            branch_name=branch_name,
            commit_message=commit_message,
            pr_title=pr_title,
            pr_body=pr_body,
            pr_url=pr_url,
            files_changed=files_changed,
            tests_run=tests_run,
            validation_passed=validation_passed,
        )

    def _branch_name(self, exception_type: str | None, fingerprint: str) -> str:
        label = (exception_type or "dotnet-error").split(".")[-1]
        label = re.sub(r"[^a-zA-Z0-9-]+", "-", label).strip("-").lower() or "dotnet-error"
        return f"logscraper/{label}-{fingerprint[:12]}"

    def _commit_message(self, exception_type: str | None, message: str) -> str:
        label = (exception_type or "dotnet error").split(".")[-1]
        clean_message = re.sub(r"\s+", " ", message).strip()
        if len(clean_message) > 72:
            clean_message = clean_message[:69].rstrip() + "..."
        return f"Fix {label}: {clean_message}" if clean_message else f"Fix {label}"

    def _pr_title(self, exception_type: str | None, message: str) -> str:
        label = (exception_type or "DotNet error").split(".")[-1]
        clean_message = re.sub(r"\s+", " ", message).strip()
        if len(clean_message) > 90:
            clean_message = clean_message[:87].rstrip() + "..."
        return f"Resolve {label}: {clean_message}" if clean_message else f"Resolve {label}"

    def _pr_body(
        self,
        task: ResolutionTask,
        plan: CodeFixPlan,
        *,
        files_changed: list[str],
        tests_run: list[str],
    ) -> str:
        event = task.log_event
        files = "\n".join(f"- `{path}`" for path in plan.files_allowed_to_edit) or "- None mapped yet"
        changed = "\n".join(f"- `{path}`" for path in files_changed) or "- No files changed"
        tests = "\n".join(f"- `{command}`" for command in tests_run) or "- Not available"
        dry_run_note = (
            "Dry-run mode is enabled, so no GitHub writes or code edits were performed."
            if self.settings.dry_run
            else "Live mode was enabled for PR creation."
        )
        return "\n".join(
            [
                "## Error Context",
                f"- Fingerprint: `{task.fingerprint}`",
                f"- Service: `{event.service or 'unknown'}`",
                f"- Environment: `{event.environment or 'unknown'}`",
                f"- Exception: `{event.exception_type or 'unknown'}`",
                f"- Log message: {event.error_message or 'n/a'}",
                f"- Exception message: {event.exception_message or 'n/a'}",
                "",
                "## Detailed Exception",
                "```text",
                event.detailed_exception or "n/a",
                "```",
                "",
                "## Stack Trace",
                "```text",
                event.stack_trace or "n/a",
                "```",
                "",
                "## Datadog Log",
                "```json",
                self._json_text(event.raw_payload),
                "```",
                "",
                "## Root Cause Analysis",
                plan.root_cause_summary,
                "",
                "## Proposed Fix",
                plan.suggested_fix,
                "",
                "## Files Allowed To Edit",
                files,
                "",
                "## Files Changed",
                changed,
                "",
                "## Suggested Verification",
                tests,
                "",
                "## Safety",
                dry_run_note,
                "Residual risk: automated analysis may miss application-specific behavior; human review is required.",
            ]
        )

    def _json_text(self, value: object) -> str:
        return json.dumps(value, default=str, sort_keys=True)

    def _resolve_context(self, task: ResolutionTask, plan: CodeFixPlan) -> str:
        event = task.log_event
        return "\n".join(
            [
                f"Fingerprint: {task.fingerprint}",
                f"Service: {event.service or 'unknown'}",
                f"Environment: {event.environment or 'unknown'}",
                f"Exception type: {event.exception_type or 'unknown'}",
                f"Log message: {event.error_message or 'n/a'}",
                f"Exception message: {event.exception_message or 'n/a'}",
                "Detailed exception:",
                event.detailed_exception or "n/a",
                "Stack trace:",
                event.stack_trace or "n/a",
                "Parsed stack frames:",
                "\n".join(frame.raw for frame in event.frames) or "n/a",
                "Structured Datadog fields:",
                self._json_text(event.log_fields or event.log_attributes or event.raw_payload),
                "Root-cause analysis:",
                plan.root_cause_summary,
                "Suggested fix:",
                plan.suggested_fix,
                "Files allowed to edit:",
                "\n".join(plan.files_allowed_to_edit) or "none",
            ]
        )

    def _checkout_fix_branch(self, repo: Path, branch_name: str) -> None:
        base_branch = self.settings.github_base_branch or "main"
        print(f"[resolver] creating branch={branch_name} from base={base_branch}")
        self._run_git(["checkout", base_branch], cwd=repo)
        self._run_git(["pull", "--ff-only", "origin", base_branch], cwd=repo)
        self._run_git(["checkout", "-B", branch_name], cwd=repo)

    def _read_allowed_files(self, repo: Path, allowed_files: list[str]) -> list[dict[str, str]]:
        source_files: list[dict[str, str]] = []
        for path in allowed_files:
            resolved = self._resolve_repo_path(repo, path)
            if resolved is None or not resolved.exists() or not resolved.is_file():
                continue
            relative = resolved.relative_to(repo).as_posix()
            source_files.append(
                {
                    "path": relative,
                    "content": resolved.read_text(encoding="utf-8", errors="ignore"),
                }
            )
        return source_files

    def _apply_file_changes(
        self,
        repo: Path,
        changes: list[dict[str, object]],
        allowed_files: list[str],
    ) -> list[str]:
        allowed_paths = {
            resolved.relative_to(repo).as_posix()
            for path in allowed_files
            if (resolved := self._resolve_repo_path(repo, path)) is not None
        }
        changed_files: list[str] = []
        for change in changes:
            path = str(change.get("path", "")).strip()
            resolved = self._resolve_repo_path(repo, path)
            if resolved is None:
                continue
            relative = resolved.relative_to(repo).as_posix()
            if relative not in allowed_paths:
                print(f"[resolver] skipped change outside allowed files: {relative}")
                continue
            if self._apply_single_file_change(resolved, change):
                changed_files.append(relative)
        return sorted(set(changed_files))

    def _apply_single_file_change(self, path: Path, change: dict[str, object]) -> bool:
        content = change.get("content")
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
            return True

        replacements = change.get("replacements")
        if not isinstance(replacements, list):
            return False

        original = path.read_text(encoding="utf-8", errors="ignore")
        updated = original
        for replacement in replacements:
            if not isinstance(replacement, dict):
                continue
            old = replacement.get("old")
            new = replacement.get("new")
            if not isinstance(old, str) or not isinstance(new, str) or old not in updated:
                continue
            updated = updated.replace(old, new, 1)

        if updated == original:
            return False
        path.write_text(updated, encoding="utf-8")
        return True

    def _run_suggested_tests(self, repo: Path, commands: list[str]) -> list[str]:
        results: list[str] = []
        for command in commands:
            print(f"[resolver] running verification: {command}")
            completed = subprocess.run(
                command,
                cwd=str(repo),
                shell=True,
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode != 0:
                output = (completed.stderr or completed.stdout).strip()
                raise RuntimeError(f"Verification failed for `{command}`: {output[:2000]}")
            results.append(command)
        return results

    def _validate_and_retry(
        self,
        repo: Path,
        task: ResolutionTask,
        plan: CodeFixPlan,
        source_files: list[dict[str, str]],
        files_changed: list[str],
    ) -> tuple[bool, list[str]]:
        """Run tests before commit. On failure, retry once with enriched context."""
        try:
            tests_run = self._run_suggested_tests(repo, plan.suggested_tests)
            return True, tests_run
        except RuntimeError as exc:
            failure_output = str(exc)

        # Retry: re-generate fix with test failure context
        print("[resolver] tests failed; retrying with enriched context")
        enriched_context = self._resolve_context(task, plan) + (
            "\n\n--- Test Failure Output ---\n" + failure_output
        )

        # Reset files to original state before re-applying
        for file_info in source_files:
            path = repo / file_info["path"]
            path.write_text(file_info["content"], encoding="utf-8")

        changes = self.llm_client.generate_file_changes(
            resolve_context=enriched_context,
            source_files=source_files,
        )
        files_changed_retry = self._apply_file_changes(repo, changes, plan.files_allowed_to_edit)
        if not files_changed_retry:
            print("[resolver] retry produced no file changes; proceeding with original")
            # Re-apply original changes
            for file_info in source_files:
                path = repo / file_info["path"]
                path.write_text(file_info["content"], encoding="utf-8")
            changes_orig = self.llm_client.generate_file_changes(
                resolve_context=self._resolve_context(task, plan),
                source_files=source_files,
            )
            self._apply_file_changes(repo, changes_orig, plan.files_allowed_to_edit)
            return False, plan.suggested_tests

        try:
            tests_run = self._run_suggested_tests(repo, plan.suggested_tests)
            return True, tests_run
        except RuntimeError:
            print("[resolver] retry tests also failed; proceeding with best-effort fix")
            return False, plan.suggested_tests

    def _commit_changes(self, repo: Path, files_changed: list[str], commit_message: str) -> None:
        self._run_git(["add", "--", *files_changed], cwd=repo)
        diff_result = self._run_git(["diff", "--cached", "--quiet"], cwd=repo, check=False)
        if diff_result.returncode == 0:
            raise RuntimeError("No staged changes were found after applying the fix.")
        self._run_git(["commit", "-m", commit_message], cwd=repo)

    def _push_branch(self, repo: Path, branch_name: str) -> None:
        print(f"[resolver] pushing branch={branch_name}")
        self._run_git(["push", "-u", "origin", branch_name], cwd=repo)

    def _run_git(self, args: list[str], *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            askpass_path = self._write_askpass_script(Path(temp_dir))
            env = os.environ.copy()
            env["GIT_ASKPASS"] = str(askpass_path)
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GITHUB_TOKEN"] = self.settings.github_token
            completed = subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        if check and completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"git {' '.join(args[:2])} failed: {message}")
        return completed

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

    def _resolve_repo_path(self, repo: Path, path: str) -> Path | None:
        candidate = Path(path)
        resolved = candidate.resolve() if candidate.is_absolute() else (repo / candidate).resolve()
        try:
            resolved.relative_to(repo)
        except ValueError:
            return None
        return resolved
