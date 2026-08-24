from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException

from app.config import Settings
from app.main import agent_app, app
from app.routes.agent_api import _require_agent
from app.services.agent_auth import scoped_agent_token, valid_agent_token
from app.services.desktop import DesktopService


def test_agent_tokens_are_scoped_to_one_identity():
    master = "owner-secret-value"
    chief = scoped_agent_token(master, "chief")
    researcher = scoped_agent_token(master, "researcher")
    assert chief != researcher
    assert valid_agent_token(master, "chief", chief)
    assert not valid_agent_token(master, "researcher", chief)


def test_agent_listener_rejects_identity_spoofing():
    master = "owner-secret-value"
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(api_token=master)))
    )
    chief = scoped_agent_token(master, "chief")
    assert _require_agent(request, f"Bearer {chief}", "chief") == "chief"
    with pytest.raises(HTTPException) as exc:
        _require_agent(request, f"Bearer {chief}", "researcher")
    assert exc.value.status_code == 401


def test_host_and_agent_surfaces_are_separate():
    office_paths = {route.path for route in app.routes}
    agent_paths = {route.path for route in agent_app.routes}
    assert "/api/tools" not in office_paths
    assert agent_paths == {"/health", "/api/tools", "/api/tools/{name}"}


def test_agent_callback_uses_reserved_uncommon_port():
    settings = Settings()
    assert settings.port == 8080
    assert settings.agent_api_port == 43127
    assert settings.agent_desktop_port == 43128
    assert DesktopService(settings).url == "http://127.0.0.1:43128/"
    assert settings.agent_api_url == "http://host.docker.internal:43127"
    assert settings.hermes_binary == "scripts/hermes-docker.sh"


def test_compose_builds_an_agent_image_without_exposing_the_office():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    service = compose["services"]["agent-computer"]
    assert service["image"] == "moatbots-agent:local"
    assert "ports" not in service
    assert "volumes" not in service
    assert "env_file" not in service
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "linuxserver/webtop:ubuntu-xfce@sha256:" in dockerfile
    assert "UV_PYTHON_INSTALL_DIR=/opt/python" in dockerfile
    assert "COPY app/" not in dockerfile
    assert "EXPOSE" not in dockerfile
    assert "uvicorn" not in dockerfile.lower()


def test_shared_computer_mounts_work_surfaces_without_control_plane_state():
    wrapper = Path("scripts/hermes-docker.sh").read_text(encoding="utf-8")
    computer = Path("scripts/agent-computer.sh").read_text(encoding="utf-8")
    assert 'exec "$actor" /opt/hermes-venv/bin/hermes' in wrapper
    assert "dst=/config" in computer
    assert "dst=/agents" in computer
    assert "dst=/agent/shared" in computer
    assert "dst=/workspace" in computer
    assert '--publish "$desktop_host:$desktop_port:3000"' in computer
    assert "src=$root/data" not in computer
    assert "src=$root/.env" not in computer
    assert "/var/run/docker.sock" not in computer
    assert "0.0.0.0" not in computer
