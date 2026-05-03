LogScraper
==========

LogScraper is a Python agent foundation for turning Datadog .NET error logs into a safe, reviewable resolution workflow. V1 is dry-run-first: it fetches and parses Datadog logs, fingerprints duplicate .NET exceptions, stores history in SQLite, maps stack frames to a local C# repository when configured, and prepares PR metadata for human review.

## Current V1 Capabilities

- Datadog Logs API query construction with cursor pagination support.
- Structured and plain .NET exception parsing.
- Stable fingerprinting that strips volatile IDs, timestamps, paths, and line numbers.
- SQLite history for first seen, last seen, occurrence count, status, and PR URL.
- Explicit workflow state for ingestion, routing, analysis, PR preparation, review, and history updates.
- Local .NET repository analysis through `DOTNET_REPO_PATH`.
- Dry-run PR branch, commit message, title, and body generation.

## Setup

```powershell
uv sync --extra dev
Copy-Item .env.example .env
```

Fill in Datadog credentials and service settings in `.env`. Keep `DRY_RUN=true` until you intentionally want live GitHub writes.

The app currently uses Datadog's Logs REST API directly. It accepts either `DATADOG_API_KEY` / `DATADOG_APP_KEY` or the MCP-style aliases `DD_API_KEY` / `DD_APP_KEY`.

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
- Automated analysis only edits or proposes work around mapped stack-frame files; human review remains required.
