import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings, get_settings
from app.routes.pages import router
from app.services.portal import ManagedPortal, NullPortal
from tests.test_portal import ListeningPortal, ignore_text


@pytest.fixture
def portal_app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("MOATBOTS_PORTAL", "MOATBOTS_TELEGRAM_BOT_TOKEN", "MOATBOTS_TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key, raising=False)
    Path("config.yaml").write_text("portal:\n  kind: none\n")
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(router)
    app.state.settings = Settings()
    app.state.portal = ManagedPortal(NullPortal())
    yield app
    get_settings.cache_clear()


async def test_portal_form_enables_replaces_and_disables_without_yaml_edit(portal_app, monkeypatch):
    adapters = []

    def build(settings):
        if settings.portal.kind == "none":
            return NullPortal()
        assert settings.telegram_bot_token == "123456:synthetic_test_token"
        adapter = ListeningPortal()
        adapters.append(adapter)
        return adapter

    monkeypatch.setattr("app.routes.pages.build_portal", build)
    managed = portal_app.state.portal
    await managed.start(ignore_text)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=portal_app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/settings/portal",
                data={"kind": "telegram", "token": "123456:synthetic_test_token", "chat_id": "42"},
            )
            assert response.status_code == 303
            await asyncio.wait_for(adapters[0].started.wait(), 1)
            assert Settings().portal.kind == "telegram"
            assert portal_app.state.settings.telegram_chat_id == "42"
            assert Path("config.yaml").read_text() == "portal:\n  kind: none\n"
            assert json.loads(Path(".env").read_text().split("MOATBOTS_PORTAL=", 1)[1]) == {
                "kind": "telegram"
            }
            await client.post(
                "/settings/portal", data={"kind": "telegram", "token": "", "chat_id": ""}
            )
            await asyncio.wait_for(adapters[1].started.wait(), 1)
            assert adapters[0].closed and portal_app.state.settings.telegram_chat_id == ""
            await client.post("/settings/portal", data={"kind": "none", "token": "", "chat_id": ""})
            assert managed.kind == "none" and adapters[1].closed
            assert Settings().portal.kind == "none"
    finally:
        await managed.close()


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"kind": "telegram", "token": "", "chat_id": ""}, "token"),
        ({"kind": "telegram", "token": "123:token\nINJECT=value", "chat_id": "42"}, "invalid"),
        ({"kind": "telegram", "token": "123:token", "chat_id": "abc"}, "invalid"),
    ],
)
async def test_invalid_portal_form_preserves_configuration(portal_app, data, error):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=portal_app), base_url="http://test"
    ) as client:
        response = await client.post("/settings/portal", data=data)
    assert response.headers["location"] == f"/settings?portal_error={error}"
    assert not Path(".env").exists()
    assert portal_app.state.portal.kind == "none"
