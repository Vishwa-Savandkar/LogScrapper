from __future__ import annotations

import logging

from .config import AppSettings
from .graph.workflow import build_workflow
from .logging import configure_logging
from .tls import configure_langsmith_environment


def main() -> int:
    settings = AppSettings()
    configure_logging()
    logger = logging.getLogger("logscraper")
    langsmith_ca_bundle = configure_langsmith_environment(settings)
    if langsmith_ca_bundle is not None:
        logger.info("LangSmith TLS trust configured.", extra={"ca_bundle": str(langsmith_ca_bundle)})

    workflow = build_workflow(settings=settings)
    if settings.workflow_graph_path:
        try:
            workflow.write_graph(settings.workflow_graph_path)
            logger.info("Workflow graph rendered.", extra={"path": settings.workflow_graph_path})
        except Exception as exc:
            logger.exception("Workflow graph rendering failed.", extra={"path": settings.workflow_graph_path})
            return 1
        if settings.workflow_graph_only:
            return 0

    if not settings.datadog_api_key or not settings.datadog_app_key:
        logger.info("Datadog credentials are not configured; no logs were fetched.")
        return 0

    state = workflow.run()
    logger.info(
        "LogScraper run completed.",
        extra={
            "events": len(state.get("log_events", [])),
            "fingerprint": getattr(state.get("current_event"), "fingerprint", None),
            "dry_run": getattr(state.get("pull_request_result"), "dry_run", None),
            "errors": state.get("errors", []),
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
