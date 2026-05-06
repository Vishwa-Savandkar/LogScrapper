from __future__ import annotations

import logging
import os
import ssl
import sys
from pathlib import Path
from typing import Any


SERVER_AUTH_OID = "1.3.6.1.5.5.7.3.1"
logger = logging.getLogger(__name__)


def configure_langsmith_environment(settings: Any) -> Path | None:
    """Prepare LangSmith env vars and CA trust before the tracing client starts."""
    _set_env_from_setting("LANGSMITH_TRACING", _get_setting(settings, "langsmith_tracing"))
    _set_env_from_setting("LANGSMITH_ENDPOINT", _get_setting(settings, "langsmith_endpoint"))
    _set_env_from_setting("LANGSMITH_API_KEY", _get_setting(settings, "langsmith_api_key"))
    _set_env_from_setting("LANGSMITH_PROJECT", _get_setting(settings, "langsmith_project"))

    explicit_bundle = _get_setting(settings, "langsmith_ca_bundle")
    if explicit_bundle:
        return _configure_ca_bundle(explicit_bundle, force=True)

    if _existing_ca_bundle():
        return None
    if not _parse_bool(_get_setting(settings, "langsmith_tracing")):
        return None
    if not _parse_bool(_get_setting(settings, "langsmith_use_windows_cert_store", default="true")):
        return None
    if sys.platform != "win32":
        return None

    bundle_path = _get_setting(
        settings,
        "langsmith_windows_ca_bundle_path",
        default="./data/langsmith-windows-ca-bundle.pem",
    )
    try:
        path = export_windows_cert_store_bundle(bundle_path)
    except OSError as exc:
        logger.warning("Could not write Windows CA bundle for LangSmith.", extra={"error": str(exc)})
        return None
    if path is None:
        logger.warning("No Windows trusted certificates were available for LangSmith TLS.")
        return None
    return _configure_ca_bundle(path, force=False)


def export_windows_cert_store_bundle(path: str | os.PathLike[str]) -> Path | None:
    pem = windows_cert_store_pem()
    if not pem:
        return None

    output_path = Path(path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    content = pem.rstrip() + "\n"
    if not output_path.exists() or output_path.read_text(encoding="ascii") != content:
        output_path.write_text(content, encoding="ascii")
    return output_path


def windows_cert_store_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    pem = windows_cert_store_pem()
    if pem:
        context.load_verify_locations(cadata=pem)
    return context


def windows_cert_store_pem() -> str:
    if sys.platform != "win32" or not hasattr(ssl, "enum_certificates"):
        return ""

    pem_certificates: list[str] = []
    seen: set[str] = set()
    for store_name in ("ROOT", "CA"):
        try:
            certificates = ssl.enum_certificates(store_name)
        except OSError:
            continue
        for certificate, encoding, trust in certificates:
            if encoding != "x509_asn" or not _is_server_auth_trusted(trust):
                continue
            try:
                pem = ssl.DER_cert_to_PEM_cert(certificate)
            except ValueError:
                continue
            if pem in seen:
                continue
            seen.add(pem)
            pem_certificates.append(pem)
    return "\n".join(pem_certificates)


def _configure_ca_bundle(path: str | os.PathLike[str], *, force: bool) -> Path:
    bundle_path = Path(path).expanduser().resolve()
    value = str(bundle_path)
    for env_name in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE"):
        if force or not os.environ.get(env_name):
            os.environ[env_name] = value
    _clear_langsmith_env_caches()
    return bundle_path


def _existing_ca_bundle() -> str:
    for env_name in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE"):
        value = os.environ.get(env_name)
        if value:
            return value
    return ""


def _set_env_from_setting(name: str, value: Any) -> None:
    text = str(value).strip() if value is not None else ""
    if text and not os.environ.get(name):
        os.environ[name] = text


def _clear_langsmith_env_caches() -> None:
    try:
        from langsmith import utils as langsmith_utils
    except ImportError:
        return

    for name in ("get_env_var", "get_tracer_project"):
        cached = getattr(langsmith_utils, name, None)
        cache_clear = getattr(cached, "cache_clear", None)
        if callable(cache_clear):
            cache_clear()


def _get_setting(settings: Any, name: str, *, default: Any = "") -> Any:
    return getattr(settings, name, default)


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _is_server_auth_trusted(trust: Any) -> bool:
    if trust is True:
        return True
    if isinstance(trust, (set, frozenset, list, tuple)):
        return SERVER_AUTH_OID in trust
    return False
