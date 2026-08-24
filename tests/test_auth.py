import json
from pathlib import Path

from app.config import InferenceSettings, Settings
from app.services.auth import AuthService


def test_detects_grok_build_session(tmp_path: Path):
    grok = tmp_path / "grok.json"
    grok.write_text(
        json.dumps(
            {
                "https://auth.x.ai::demo": {
                    "key": "tok",
                    "email": "you@x.ai",
                    "refresh_token": "r",
                }
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        inference=InferenceSettings(
            grok_auth_path=str(grok),
            codex_auth_path=str(tmp_path / "missing-codex.json"),
            hermes_auth_path=str(tmp_path / "missing-hermes.json"),
            hermes_env_path=str(tmp_path / "missing.env"),
        )
    )
    snap = AuthService(settings).snapshot()
    assert snap.grok_build.signed_in
    assert snap.grok_build.account == "you@x.ai"
    assert snap.selected == "grok_build"


def test_openrouter_key_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        inference=InferenceSettings(
            grok_auth_path=str(tmp_path / "g.json"),
            codex_auth_path=str(tmp_path / "c.json"),
            hermes_auth_path=str(tmp_path / "h.json"),
            hermes_env_path=str(tmp_path / "hermes.env"),
        )
    )
    auth = AuthService(settings)
    auth.save_openrouter_key("sk-or-test-key")
    assert "OPENROUTER_API_KEY=sk-or-test-key" in Path(".env").read_text(encoding="utf-8")
    snap = auth.snapshot()
    assert snap.openrouter.signed_in


def test_openrouter_invocation_uses_configured_model(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        inference={
            "default": "openrouter",
            "openrouter_model": "openai/gpt-5.6-luna",
            "reasoning_effort": "high",
        },
        openrouter_api_key="sk-or-test-key",
    )
    provider, model, environment = AuthService(settings).hermes_invocation()
    assert provider == "openrouter"
    assert model == "openai/gpt-5.6-luna"
    assert environment == {"OPENROUTER_API_KEY": "sk-or-test-key"}


def test_import_does_not_overwrite_office_auth(tmp_path: Path):
    host = tmp_path / "host"
    office = tmp_path / "office"
    host.mkdir()
    office.mkdir()
    (host / "auth.json").write_text(
        json.dumps({"providers": {"xai-oauth": {"ok": True}, "openai-codex": {"ok": True}}}),
        encoding="utf-8",
    )
    (office / "auth.json").write_text(
        json.dumps({"providers": {"xai-oauth": {"ok": "keep-me"}}}),
        encoding="utf-8",
    )
    (host / ".env").write_text("OPENROUTER_API_KEY=sk-host\n", encoding="utf-8")
    settings = Settings(
        inference=InferenceSettings(
            grok_auth_path=str(tmp_path / "g.json"),
            codex_auth_path=str(tmp_path / "c.json"),
            hermes_auth_path=str(office / "auth.json"),
            hermes_env_path=str(office / ".env"),
            host_hermes_auth_path=str(host / "auth.json"),
            host_hermes_env_path=str(host / ".env"),
        )
    )
    imported = AuthService(settings).import_host_logins()
    assert "openai-codex" in imported
    assert "OPENROUTER_API_KEY" in imported
    office_auth = json.loads((office / "auth.json").read_text(encoding="utf-8"))
    assert office_auth["providers"]["xai-oauth"] == {"ok": "keep-me"}
    assert office_auth["providers"]["openai-codex"] == {"ok": True}
