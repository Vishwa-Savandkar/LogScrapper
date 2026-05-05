# LogScraper - Datadog-to-.NET Auto-Resolution Agent

## Overview

LogScraper is a Python application built with LangGraph that reads production logs from Datadog for a .NET application, detects novel or recurring errors, deduplicates them, analyzes the related C#/.NET source code, and creates a GitHub pull request with a proposed fix.

The first production-ready milestone focuses on safe, coding-first delivery:

- Datadog is the only log source.
- Python is the implementation language for the agent system.
- LangGraph is the orchestration framework.
- The target application is a .NET/C# codebase.
- The system creates PRs, but humans review and merge them.
- Dry-run mode is enabled by default for safety.

---

## Core Goal

Given a Datadog error log from a .NET application, the system should:

1. Fetch relevant Datadog logs for a configured service and environment.
2. Parse .NET exception messages and stack traces.
3. Normalize and fingerprint errors to avoid duplicate work.
4. Store error history and state transitions.
5. Analyze the target .NET repository to identify likely root cause files.
6. Generate a focused code fix and related test changes where possible.
7. Create a GitHub PR with error context, root cause analysis, changes made, and tests run.

---

## Important Hurdles

### 1. Datadog Log Quality

Datadog logs may not always contain complete stack traces, source file paths, trace IDs, or structured exception fields. The ingestion layer must support both structured JSON logs and plain text logs.

Mitigation:

- Parse known .NET fields when present: exception type, message, stack trace, service, environment, trace ID, span ID.
- Fall back to regex-based parsing for plain text stack traces.
- Store raw log payloads for audit and later debugging.

### 2. Duplicate and Recurring Errors

The same .NET exception can appear thousands of times with different timestamps, request IDs, GUIDs, user IDs, or line numbers.

Mitigation:

- Normalize error messages and stack traces before fingerprinting.
- Strip volatile values such as timestamps, GUIDs, request IDs, memory addresses, and environment-specific values.
- Use exception type, normalized message, and top meaningful stack frames for stable fingerprints.

### 3. Mapping Logs to Source Code

.NET stack traces may include namespaces and method names but not always exact file paths.

Mitigation:

- Map stack frames to C# files by namespace, class, and method.
- Inspect `.csproj`, `.sln`, and project folder structure.
- Prefer local repository analysis when `DOTNET_REPO_PATH` is configured.
- Use GitHub APIs as a fallback when no local checkout exists.

### 4. Safe Automated Code Changes

Automatic fixes can introduce regressions if the agent edits too broadly or guesses incorrectly.

Mitigation:

- Restrict edits to files connected to the detected stack trace unless explicitly configured.
- Generate a `CodeFixPlan` before applying changes.
- Require dry-run mode by default.
- Never auto-merge PRs.
- Include residual risk in every PR body.

### 5. Testing .NET Fixes

The agent must understand how to validate changes in a .NET repo.

Mitigation:

- Detect solution and project files.
- Run `dotnet build` and targeted `dotnet test` when a local checkout is available.
- If tests cannot run, state why in the PR body.
- Prefer adding or updating tests related to the failing code path.

### 6. Production Reliability

External APIs can fail, rate limit, or return partial data.

Mitigation:

- Add retries with exponential backoff.
- Use request timeouts.
- Add structured logs for all agent state transitions.
- Keep all external clients behind interfaces for testing.
- Make every workflow step idempotent where possible.

---

## Agent Architecture

```text
DatadogLogSource
      |
      v
LogScraperAgent
      |
      v
MasterAgent
      |
      v
DotNetAnalyzerAgent
      |
      v
ResolverAgent
      |
      v
PRReviewerAgent
      |
      v
GitHub Pull Request
```

---

## Agents

### 1. LogScraperAgent

Role: Fetch Datadog logs, identify .NET errors, normalize them, deduplicate them, and emit structured `LogEvent` objects.

Responsibilities:

- Query Datadog Logs API by service, environment, level, and time range.
- Support cursor pagination.
- Filter logs for relevant levels such as `ERROR`, `CRITICAL`, and `FATAL`.
- Parse .NET exception messages and stack traces.
- Normalize stack traces and messages.
- Generate error fingerprints.
- Store or update error history.
- Emit only new or recurring actionable errors.

Output:

```python
LogEvent(
    timestamp=...,
    service=...,
    environment=...,
    log_level=...,
    exception_type=...,
    error_message=...,
    stack_trace=...,
    fingerprint=...,
    trace_id=...,
    span_id=...,
    raw_payload=...
)
```

### 2. MasterAgent

Role: Coordinate the workflow and decide how each error should be handled.

Responsibilities:

- Receive `LogEvent` objects.
- Check history by fingerprint.
- Skip already resolved or ignored errors.
- Escalate recurring open errors by updating occurrence counts.
- Create a `ResolutionTask` for new actionable errors.
- Track workflow state: `open`, `in_analysis`, `fix_planned`, `in_review`, `resolved`, `ignored`.

### 3. DotNetAnalyzerAgent

Role: Understand the .NET error and locate the most relevant source code.

Responsibilities:

- Parse .NET stack frames.
- Identify likely C# classes, methods, namespaces, and projects.
- Inspect local `.sln`, `.csproj`, and `.cs` files when available.
- Fetch relevant files from GitHub when local checkout is unavailable.
- Determine likely root cause.
- Recommend target files and test commands.

Output:

```python
CodeFixPlan(
    fingerprint=...,
    root_cause_summary=...,
    files_to_inspect=[...],
    files_allowed_to_edit=[...],
    suggested_fix=...,
    suggested_tests=[...],
    confidence=...
)
```

### 4. ResolverAgent

Role: Generate and prepare the code fix for the .NET repository.

Responsibilities:

- Receive `ResolutionTask` and `CodeFixPlan`.
- Modify only approved files.
- Add or update tests where practical.
- Run `dotnet build` and targeted `dotnet test` when local checkout is available.
- Prepare commit message and PR content.
- Use dry-run mode unless live GitHub writes are explicitly enabled.

### 5. PRReviewerAgent

Role: Review the generated fix before or after PR creation.

Responsibilities:

- Review generated diff for correctness and safety.
- Check whether the fix addresses the root cause.
- Check for .NET conventions, null handling, async behavior, dependency injection patterns, and test coverage.
- Return review findings and residual risk.
- In V1, this agent is advisory only and does not auto-merge.

---

## LangGraph Workflow

The LangGraph workflow should use typed state and explicit routing.

```python
class AgentState(TypedDict):
    log_events: list[LogEvent]
    current_event: LogEvent | None
    resolution_task: ResolutionTask | None
    code_fix_plan: CodeFixPlan | None
    pull_request_result: PullRequestResult | None
    review_result: ReviewResult | None
    errors: list[str]
```

Core graph flow:

1. `fetch_datadog_logs`
2. `deduplicate_errors`
3. `route_error`
4. `analyze_dotnet_code`
5. `plan_fix`
6. `apply_or_dry_run_fix`
7. `review_fix`
8. `create_or_report_pr`
9. `update_history`

---

## Technology Stack

| Concern | Tool |
|---|---|
| Agent orchestration | LangGraph |
| Language | Python 3.11+ |
| LLM provider | OpenAI or Azure OpenAI |
| Log source | Datadog Logs API |
| Target application | .NET/C# |
| GitHub integration | GitHub REST API or PyGithub |
| Config | pydantic-settings |
| Database | SQLite for V1, PostgreSQL later |
| HTTP client | httpx |
| Retries | tenacity |
| Testing | pytest |
| Logging | Structured JSON logs |

---

## Configuration

Example `.env`:

```env
DATADOG_API_KEY=
DATADOG_APP_KEY=
DATADOG_SITE=datadoghq.com
DATADOG_SERVICE=
DATADOG_ENV=prod
DATADOG_LOG_LEVELS=ERROR,CRITICAL,FATAL
DATADOG_LOOKBACK_MINUTES=60

GITHUB_TOKEN=
GITHUB_REPO_OWNER=
GITHUB_REPO_NAME=
GITHUB_BASE_BRANCH=main

OPENAI_API_KEY=
AZURE_OPENAI_ENDPOINT=
AZURE_OPENAI_API_KEY=
LLM_PROVIDER=openai
LLM_MODEL=

DB_PATH=./data/logscraper.db
DRY_RUN=true
DOTNET_REPO_PATH=

MAX_LOGS_PER_RUN=1000
SIMILARITY_THRESHOLD=0.85
```

---

## Project Structure

```text
logScraper/
├── src/
│   └── logscraper/
│       ├── agents/
│       │   ├── log_scraper_agent.py
│       │   ├── master_agent.py
│       │   ├── dotnet_analyzer_agent.py
│       │   ├── resolver_agent.py
│       │   └── pr_reviewer_agent.py
│       ├── connectors/
│       │   ├── datadog_connector.py
│       │   └── github_connector.py
│       ├── dotnet/
│       │   ├── stacktrace_parser.py
│       │   ├── repo_analyzer.py
│       │   └── test_runner.py
│       ├── graph/
│       │   └── workflow.py
│       ├── store/
│       │   └── history_db.py
│       ├── llm/
│       │   └── client.py
│       ├── config.py
│       ├── logging.py
│       ├── models.py
│       └── main.py
├── tests/
│   ├── fixtures/
│   │   └── sample_datadog_dotnet_logs.json
│   ├── test_datadog_connector.py
│   ├── test_stacktrace_parser.py
│   ├── test_fingerprint.py
│   ├── test_history_db.py
│   └── test_workflow.py
├── data/
│   └── .gitkeep
├── pyproject.toml
├── .env.example
├── .gitignore
├── README.md
└── plan.md
```

---

## Implementation Phases

### Phase 1 - Project Foundation

1. Create Python package structure.
2. Add `pyproject.toml` with runtime and dev dependencies.
3. Add `AppSettings` using `pydantic-settings`.
4. Add typed Pydantic models.
5. Add structured logging.
6. Add `.env.example` and `.gitignore`.

### Phase 2 - Datadog Ingestion

1. Implement Datadog connector with cursor pagination.
2. Support service, environment, log level, and lookback filters.
3. Add retry and timeout handling.
4. Add fixture-based tests for Datadog responses.

### Phase 3 - .NET Error Understanding

1. Implement .NET stack trace parser.
2. Extract exception type, message, namespace, class, method, file, and line number when available.
3. Implement normalization and fingerprinting.
4. Add tests for common .NET exception formats.

### Phase 4 - History and Deduplication

1. Implement SQLite schema.
2. Add CRUD operations for error records.
3. Track first seen, last seen, occurrence count, status, and PR URL.
4. Add duplicate suppression logic.

### Phase 5 - LangGraph Workflow

1. Implement typed LangGraph state.
2. Add nodes for ingestion, deduplication, routing, .NET analysis, fix planning, PR preparation, review, and history update.
3. Add conditional edges for duplicate, ignored, resolved, and actionable errors.
4. Add workflow tests with fake connectors and fake LLM responses.

### Phase 6 - .NET Repository Analysis

1. Support local .NET repo path through `DOTNET_REPO_PATH`.
2. Detect `.sln` and `.csproj` files.
3. Map stack frames to candidate `.cs` files.
4. Identify likely test projects.
5. Add commands for `dotnet build` and targeted `dotnet test`.

### Phase 7 - Resolver and GitHub PR Flow

1. Generate a `CodeFixPlan`.
2. Apply changes only to allowed files.
3. Create branch and commit in live mode.
4. Produce PR title and body.
5. Keep dry-run mode as default.
6. Store PR URL or dry-run result in history.

### Phase 8 - Review and Production Hardening

1. Add PR review agent for advisory review.
2. Add rate limiting for Datadog and LLM calls.
3. Add stronger error handling and observability.
4. Add load-style test with large fixture logs.
5. Document setup, dry-run usage, live-mode usage, and safety limitations.

---

## Verification Checklist

- [ ] Datadog logs are fetched for one configured .NET service.
- [ ] Cursor pagination works.
- [ ] .NET stack traces are parsed into structured fields.
- [ ] Duplicate errors are suppressed by fingerprint.
- [ ] New errors create `ResolutionTask` objects.
- [ ] LangGraph routes new, duplicate, ignored, and resolved errors correctly.
- [ ] Local .NET repo analysis maps stack frames to likely C# files.
- [ ] Dry-run mode generates branch name, commit message, PR title, and PR body without GitHub writes.
- [ ] Live mode can create a GitHub PR when explicitly enabled.
- [ ] `dotnet build` and `dotnet test` run when `DOTNET_REPO_PATH` is configured.
- [ ] No secrets are committed.
- [ ] External API failures are retried and logged.

---

## Test Plan

- Unit test Datadog query construction, pagination, retries, and filtering.
- Unit test .NET stack trace parsing.
- Unit test fingerprint stability for noisy repeated logs.
- Unit test history status transitions.
- Unit test LangGraph routing decisions.
- Fixture test using sample Datadog .NET logs.
- Dry-run PR test verifying branch name, commit message, PR title, and PR body.
- Optional local integration test against a sample .NET repository.
- Load-style test with 10,000 fixture log lines.

---

## Explicit Scope

Included in V1:

- Datadog log ingestion.
- Python LangGraph agent workflow.
- .NET/C# stack trace parsing.
- Error fingerprinting and deduplication.
- SQLite history store.
- Local .NET repo analysis when path is configured.
- GitHub PR creation in live mode.
- Dry-run mode by default.

Excluded from V1:

- Multi-log-source routing.
- Auto-merge.
- Slack or email notifications.
- Multi-repo routing.
- Production PostgreSQL migration.
- Vector database similarity search.
- Full autonomous deployment or rollback.

---

## Assumptions

- Datadog is the only log provider for the first implementation.
- The target application is written in .NET/C#.
- The agent system itself is written in Python.
- LangGraph is required for orchestration.
- GitHub is the source control and PR platform.
- The first production-safe version should create PRs only; humans decide whether to merge.
- Dry-run mode remains the default until live GitHub writes are intentionally enabled.
