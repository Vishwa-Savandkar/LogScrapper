LogScraper
==========

LogScraper is a Python agent foundation for turning Datadog .NET error logs into a safe, reviewable resolution workflow. It fetches and parses Datadog logs, fingerprints duplicate .NET exceptions, stores history in SQLite, prepares a GitHub-backed repo workspace, maps stack frames to C# files, asks OpenAI for fix guidance when configured, and prepares PR metadata for human review.

## Current V1 Capabilities

- Datadog Logs API query construction with cursor pagination support.
- Structured and plain .NET exception parsing.
- Stable fingerprinting that strips volatile IDs, timestamps, paths, and line numbers.
- SQLite history for first seen, last seen, occurrence count, status, and PR URL.
- Explicit workflow state for ingestion, routing, analysis, PR preparation, review, and history updates.
- GitHub repository checkout for .NET analysis, with `DOTNET_REPO_PATH` as a local fallback.
- OpenAI-assisted fix guidance using mapped stack frames and compact source snippets.
- Dry-run PR branch, commit message, title, and body generation.

## Setup

```powershell
uv sync --extra dev
Copy-Item .env.example .env
```

Fill in Datadog credentials, GitHub repository settings, and OpenAI settings in `.env`. Keep `DRY_RUN=true` until you intentionally want live GitHub writes.

The app accepts either `DATADOG_API_KEY` / `DATADOG_APP_KEY` or the MCP-style aliases `DD_API_KEY` plus `DD_APP_KEY` or `DD_APPLICATION_KEY`.

Set `DATADOG_FETCH_MODE` to choose how logs are fetched:

```powershell
DATADOG_FETCH_MODE=api   # Datadog Logs Search API; default
DATADOG_FETCH_MODE=mcp   # Datadog remote MCP server
DATADOG_FETCH_MODE=auto  # Try MCP, then fall back to API for MCP permission/setup errors
```

The API mode requires `Logs Read Data` and `Logs Read Index Data` on the application key owner. The MCP mode fetches logs through Datadog's remote MCP server using the `search_datadog_logs` tool. Set `DATADOG_MCP_URL` for your Datadog site, for example:

```powershell
DATADOG_MCP_URL=https://mcp.datadoghq.eu/api/unstable/mcp-server/mcp
```

To fetch logs that Datadog marks as `info` but whose message looks like an error, configure query fallbacks and inferred error matching:

```powershell
DATADOG_PRIMARY_QUERY=service:basecone.test17.matching.processor.worker error
DATADOG_FALLBACK_QUERY=service:basecone.test17.matching.processor.worker ("[ERR]" OR error OR exception OR failed OR fatal OR (*stack* AND *trace*))
DATADOG_INITIAL_LOOKBACK_DAYS=7
DATADOG_EXPANDED_LOOKBACK_DAYS=30
DATADOG_INCLUDE_INFERRED_ERRORS=true
DATADOG_ERROR_KEYWORDS=[ERR],error,exception,failed,fatal,stack trace
MAX_LOGS_PER_RUN=10
```

The Datadog application key must have MCP access plus the log-read permissions needed by `search_datadog_logs`. On Windows, the app also loads certificates from the Windows trusted certificate store for corporate TLS proxies. If your company uses a separate PEM bundle, set `DATADOG_MCP_CA_BUNDLE` to that file. `DATADOG_MCP_VERIFY_SSL=false` is available for a temporary local connectivity check, but should not be used as the normal configuration.

The Datadog VS Code extension may work through its signed-in/OAuth session even when API/app-key header authentication does not. LogScraper runs as a standalone Python app, so the owner of the app key used by `DD_APP_KEY`, `DD_APPLICATION_KEY`, or `DATADOG_APP_KEY` must have Datadog MCP permissions.

## GitHub and OpenAI

For cloud-style agent runs, configure the GitHub repository instead of relying on a local checkout:

```powershell
GITHUB_TOKEN=your-classic-or-fine-grained-token
GITHUB_REPO_OWNER=your-org-or-user
GITHUB_REPO_NAME=your-dotnet-repo
GITHUB_BASE_BRANCH=main
```

When an actionable error is selected, the workflow clones the repo into:

```text
data/workspaces/{fingerprint}/repo
```

`DOTNET_REPO_PATH` is still supported as a local fallback when GitHub settings are not present.

OpenAI analysis is enabled with:

```powershell
LLM_PROVIDER=openai
OPENAI_API_KEY=your-openai-key
LLM_MODEL=gpt-4o-mini
```

If OpenAI is not configured or the request fails, the analyzer falls back to the existing stack-frame based fix plan.

## Run

```powershell
uv run logscraper
```

If Datadog credentials are missing, the CLI exits without fetching logs.

To render the workflow graph before running Datadog ingestion, set `WORKFLOW_GRAPH_PATH`:

```powershell
$env:WORKFLOW_GRAPH_PATH = "data/workflow-graph.mmd"
uv run logscraper
```

Use a `.png` path when you want LangGraph to call its Mermaid PNG renderer.
Set `WORKFLOW_GRAPH_ONLY=true` if you want to render the graph and exit without fetching logs.

In a notebook, you can display the same graph directly:

```python
from IPython.display import Image, display
from logscraper.graph.workflow import build_workflow

workflow = build_workflow()
display(Image(workflow.draw_mermaid_png()))
```

## Test

```powershell
uv run pytest
```

## Important Safety Notes

- Dry-run mode is enabled by default.
- The workflow never auto-merges.
- Live GitHub PR creation requires `DRY_RUN=false`, `GITHUB_TOKEN`, `GITHUB_REPO_OWNER`, and `GITHUB_REPO_NAME`.
- The GitHub token is used for cloning through a temporary askpass script and is not written into the repo URL.
- Automated analysis only edits or proposes work around mapped stack-frame files; human review remains required.
