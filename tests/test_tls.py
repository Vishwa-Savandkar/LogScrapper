import os
from pathlib import Path

from logscraper.config import AppSettings
from logscraper import tls


def clear_ca_env(monkeypatch):
    for name in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE", "LANGSMITH_TRACING"):
        monkeypatch.delenv(name, raising=False)


def test_config_loads_langsmith_settings_from_env_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "LANGSMITH_TRACING=true",
                "LANGSMITH_ENDPOINT=https://api.smith.langchain.com",
                "LANGSMITH_API_KEY=langsmith-key",
                "LANGSMITH_PROJECT=LogScraper",
                "LANGSMITH_CA_BUNDLE=C:/certs/company.pem",
                "LANGSMITH_USE_WINDOWS_CERT_STORE=false",
            ]
        ),
        encoding="utf-8",
    )

    settings = AppSettings(_env_file=str(env_file))

    assert settings.langsmith_tracing == "true"
    assert settings.langsmith_endpoint == "https://api.smith.langchain.com"
    assert settings.langsmith_api_key == "langsmith-key"
    assert settings.langsmith_project == "LogScraper"
    assert settings.langsmith_ca_bundle == "C:/certs/company.pem"
    assert settings.langsmith_use_windows_cert_store is False


def test_langsmith_explicit_ca_bundle_sets_requests_environment(monkeypatch, tmp_path: Path):
    clear_ca_env(monkeypatch)
    bundle = tmp_path / "company.pem"
    bundle.write_text("certificate data\n", encoding="ascii")
    settings = AppSettings(
        _env_file=str(tmp_path / "missing.env"),
        langsmith_tracing="false",
        langsmith_ca_bundle=str(bundle),
    )

    result = tls.configure_langsmith_environment(settings)

    assert result == bundle.resolve()
    assert os.environ["REQUESTS_CA_BUNDLE"] == str(bundle.resolve())
    assert os.environ["SSL_CERT_FILE"] == str(bundle.resolve())
    assert os.environ["CURL_CA_BUNDLE"] == str(bundle.resolve())


def test_langsmith_exports_windows_ca_bundle_when_tracing_is_enabled(monkeypatch, tmp_path: Path):
    clear_ca_env(monkeypatch)
    monkeypatch.setattr(tls.sys, "platform", "win32")
    monkeypatch.setattr(tls, "_set_env_from_setting", lambda name, value: None)
    monkeypatch.setattr(tls, "windows_cert_store_pem", lambda: "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----")
    bundle = tmp_path / "windows.pem"
    settings = AppSettings(
        _env_file=str(tmp_path / "missing.env"),
        langsmith_tracing="true",
        langsmith_windows_ca_bundle_path=str(bundle),
    )

    result = tls.configure_langsmith_environment(settings)

    assert result == bundle.resolve()
    assert bundle.read_text(encoding="ascii").endswith("-----END CERTIFICATE-----\n")
    assert os.environ["REQUESTS_CA_BUNDLE"] == str(bundle.resolve())


def test_langsmith_skips_windows_export_when_tracing_is_disabled(monkeypatch, tmp_path: Path):
    clear_ca_env(monkeypatch)
    monkeypatch.setattr(tls.sys, "platform", "win32")
    settings = AppSettings(
        _env_file=str(tmp_path / "missing.env"),
        langsmith_tracing="false",
        langsmith_windows_ca_bundle_path=str(tmp_path / "windows.pem"),
    )

    assert tls.configure_langsmith_environment(settings) is None
    assert "REQUESTS_CA_BUNDLE" not in os.environ
