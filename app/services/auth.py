from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings

HERMES_PROVIDERS = {
    "grok_build": "xai-oauth",
    "codex": "openai-codex",
    "openrouter": "openrouter",
}

DEFAULT_MODELS = {
    "grok_build": "grok-4.5",
    "codex": "gpt-5.4",
    "openrouter": "x-ai/grok-4.5",
}

_ENV_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


@dataclass(frozen=True)
class MethodStatus:
    id: str
    label: str
    kind: str
    signed_in: bool
    hermes_ready: bool
    detail: str
    account: str = ""

    @property
    def ready(self) -> bool:
        return self.hermes_ready


@dataclass(frozen=True)
class AuthSnapshot:
    grok_build: MethodStatus
    codex: MethodStatus
    openrouter: MethodStatus
    selected: str

    def method(self, method_id: str) -> MethodStatus:
        return {
            "grok_build": self.grok_build,
            "codex": self.codex,
            "openrouter": self.openrouter,
        }[method_id]

    def as_dict(self) -> dict:
        return {
            "selected": self.selected,
            "methods": [self.grok_build, self.codex, self.openrouter],
        }


class AuthService:
    """Three ways onto a model: Grok Build browser session, Codex browser session, OpenRouter key.

    We do not store browser tokens. Automatic profiles let Hermes own provider
    routing and fallback. Explicit per-agent overrides use this service to pass
    the matching provider and model.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def snapshot(self) -> AuthSnapshot:
        hermes_auth = _read_json(self._path(self.settings.inference.hermes_auth_path))
        hermes_env = _read_env(self._path(self.settings.inference.hermes_env_path))
        project_env = _read_env(Path(".env"))
        grok = self._grok_status(hermes_auth)
        codex = self._codex_status(hermes_auth)
        openrouter = self._openrouter_status(hermes_env, project_env)
        selected = self.resolve(None, AuthSnapshot(grok, codex, openrouter, "auto"))
        return AuthSnapshot(grok, codex, openrouter, selected)

    def resolve(self, preferred: str | None, snap: AuthSnapshot | None = None) -> str:
        snap = snap or self.snapshot()
        choice = preferred or self.settings.inference.default or "auto"
        if choice != "auto":
            return choice
        for method_id in ("grok_build", "codex", "openrouter"):
            if snap.method(method_id).signed_in or snap.method(method_id).hermes_ready:
                return method_id
        return "grok_build"

    def hermes_invocation(self, preferred: str | None = None) -> tuple[str, str, dict[str, str]]:
        """Return (hermes --provider, model hint, extra env)."""
        snap = self.snapshot()
        method_id = self.resolve(preferred, snap)
        provider = HERMES_PROVIDERS[method_id]
        model = (
            self.settings.inference.openrouter_model
            if method_id == "openrouter"
            else DEFAULT_MODELS[method_id]
        )
        extra: dict[str, str] = {}
        if method_id == "openrouter":
            key = self._openrouter_key()
            if key:
                extra["OPENROUTER_API_KEY"] = key
        return provider, model, extra

    def save_openrouter_key(self, key: str) -> None:
        token = key.strip()
        if not token:
            raise ValueError("OpenRouter API key is empty")
        upsert_env(Path(".env"), "OPENROUTER_API_KEY", token)
        upsert_env(Path(".env"), "MOATBOTS_OPENROUTER_API_KEY", token)
        hermes_env = self._path(self.settings.inference.hermes_env_path)
        if hermes_env.parent.is_dir():
            upsert_env(hermes_env, "OPENROUTER_API_KEY", token)

    def import_host_logins(self) -> list[str]:
        """Copy existing Hermes / OpenRouter credentials into the office home.

        Does not overwrite providers the office already has. Grok Build and Codex
        browser sessions stay in ~/.grok and ~/.codex; we only copy what Hermes
        itself stored.
        """
        imported: list[str] = []
        office_auth_path = self._path(self.settings.inference.hermes_auth_path)
        host_auth_path = self._path(self.settings.inference.host_hermes_auth_path)
        host_auth = _read_json(host_auth_path)
        office_auth = _read_json(office_auth_path)
        if host_auth:
            office_auth.setdefault("providers", {})
            office_auth.setdefault("credential_pool", {})
            for bucket in ("providers", "credential_pool"):
                incoming = host_auth.get(bucket)
                if not isinstance(incoming, dict):
                    continue
                dest = office_auth.setdefault(bucket, {})
                for name, value in incoming.items():
                    if dest.get(name):
                        continue
                    dest[name] = value
                    imported.append(str(name))
            if host_auth.get("active_provider") and not office_auth.get("active_provider"):
                office_auth["active_provider"] = host_auth["active_provider"]
            office_auth_path.parent.mkdir(parents=True, exist_ok=True)
            office_auth_path.write_text(json.dumps(office_auth, indent=2) + "\n", encoding="utf-8")
            with suppress(OSError):
                office_auth_path.chmod(0o600)
        host_env = _read_env(self._path(self.settings.inference.host_hermes_env_path))
        office_env = self._path(self.settings.inference.hermes_env_path)
        for key in ("OPENROUTER_API_KEY", "XAI_API_KEY"):
            value = host_env.get(key)
            if not value:
                continue
            existing = _read_env(office_env)
            if existing.get(key):
                continue
            upsert_env(office_env, key, value)
            imported.append(key)
        return imported

    async def start_browser_login(self, method_id: str) -> str:
        if method_id == "grok_build":
            grok = shutil.which(self.settings.inference.grok_login_binary)
            if grok:
                await self._spawn(grok, "login")
                return "Opened Grok Build browser login."
            await self._spawn(
                self.settings.hermes_binary, "auth", "add", "xai-oauth", "--type", "oauth"
            )
            return "Opened Hermes xAI / Grok browser login."
        if method_id == "codex":
            await self._spawn(
                self.settings.hermes_binary, "auth", "add", "openai-codex", "--type", "oauth"
            )
            return "Opened Codex / ChatGPT browser login."
        raise ValueError("OpenRouter uses an API key, not a browser login")

    async def _spawn(self, *command: str) -> None:
        binary = shutil.which(command[0]) or command[0]
        environment = os.environ.copy()
        environment["HERMES_HOME"] = str(Path(self.settings.hermes_home).expanduser().resolve())
        process = await asyncio.create_subprocess_exec(
            binary,
            *command[1:],
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=environment,
        )
        # Browser login is interactive. We do not wait; the next status poll sees the store.

        def _reap(proc: asyncio.subprocess.Process) -> None:
            del proc

        asyncio.create_task(process.wait()).add_done_callback(lambda _: _reap(process))

    def _openrouter_key(self) -> str | None:
        for value in (
            self.settings.openrouter_api_key,
            os.environ.get("MOATBOTS_OPENROUTER_API_KEY"),
            os.environ.get("OPENROUTER_API_KEY"),
        ):
            if value:
                return value
        project = _read_env(Path(".env"))
        hermes = _read_env(self._path(self.settings.inference.hermes_env_path))
        return project.get("OPENROUTER_API_KEY") or hermes.get("OPENROUTER_API_KEY")

    def _grok_status(self, hermes_auth: dict) -> MethodStatus:
        grok_path = self._path(self.settings.inference.grok_auth_path)
        session = _grok_session(grok_path)
        hermes_ready = _hermes_provider_ready(hermes_auth, "xai-oauth")
        if session:
            account = session.get("email") or session.get("user_id") or ""
            if hermes_ready:
                detail = "Grok Build session linked through Hermes xai-oauth."
            else:
                detail = (
                    "Grok Build is signed in. Link Hermes (xai-oauth) to wake profiles with it."
                )
            return MethodStatus(
                "grok_build",
                "Grok Build",
                "browser",
                True,
                hermes_ready,
                detail,
                account,
            )
        if hermes_ready:
            return MethodStatus(
                "grok_build",
                "Grok Build",
                "browser",
                True,
                True,
                "Hermes xAI Grok OAuth is signed in.",
            )
        return MethodStatus(
            "grok_build",
            "Grok Build",
            "browser",
            False,
            False,
            "No Grok Build session and no Hermes xai-oauth login.",
        )

    def _codex_status(self, hermes_auth: dict) -> MethodStatus:
        codex_path = self._path(self.settings.inference.codex_auth_path)
        signed = _codex_signed_in(codex_path)
        hermes_ready = _hermes_provider_ready(hermes_auth, "openai-codex")
        if signed and hermes_ready:
            detail = "Codex browser session is available to Hermes."
        elif signed:
            detail = "Codex is signed in. Hermes imports ~/.codex/auth.json on openai-codex."
        elif hermes_ready:
            detail = "Hermes openai-codex is signed in."
        else:
            detail = "No Codex / ChatGPT browser session."
        return MethodStatus(
            "codex",
            "Codex",
            "browser",
            signed or hermes_ready,
            hermes_ready,
            detail,
        )

    def _openrouter_status(
        self, hermes_env: dict[str, str], project_env: dict[str, str]
    ) -> MethodStatus:
        key = (
            self._openrouter_key()
            or hermes_env.get("OPENROUTER_API_KEY")
            or project_env.get("OPENROUTER_API_KEY")
        )
        if key:
            return MethodStatus(
                "openrouter",
                "OpenRouter",
                "api_key",
                True,
                True,
                f"API key set ({_mask(key)}).",
            )
        return MethodStatus(
            "openrouter",
            "OpenRouter",
            "api_key",
            False,
            False,
            "No OPENROUTER_API_KEY in .env or the agent auth store.",
        )

    def _path(self, raw: str) -> Path:
        return Path(raw).expanduser()


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for line in text.splitlines():
        match = _ENV_KEY.match(line.strip())
        if not match:
            continue
        values[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return values


def upsert_env(path: Path, key: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing.splitlines()
    written = False
    out: list[str] = []
    for line in lines:
        match = _ENV_KEY.match(line.strip())
        if match and match.group(1) == key:
            out.append(f"{key}={value}")
            written = True
        else:
            out.append(line)
    if not written:
        if out and out[-1] != "":
            out.append("")
        out.append(f"{key}={value}")
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    with suppress(OSError):
        path.chmod(0o600)


def _grok_session(path: Path) -> dict:
    data = _read_json(path)
    now = datetime.now(UTC)
    best: dict = {}
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        if not (entry.get("key") or entry.get("refresh_token")):
            continue
        expires = _parse_dt(entry.get("expires_at"))
        if expires is not None and expires < now and not entry.get("refresh_token"):
            continue
        best = entry
        break
    return best


def _codex_signed_in(path: Path) -> bool:
    data = _read_json(path)
    tokens = data.get("tokens")
    if isinstance(tokens, dict) and (tokens.get("access_token") or tokens.get("refresh_token")):
        return True
    return bool(data.get("OPENAI_API_KEY"))


def _hermes_provider_ready(auth: dict, provider: str) -> bool:
    providers = auth.get("providers")
    if isinstance(providers, dict) and provider in providers:
        body = providers[provider]
        if body:
            return True
    pool = auth.get("credential_pool")
    if isinstance(pool, dict):
        rows = pool.get(provider) or []
        if isinstance(rows, list) and rows:
            return True
    return auth.get("active_provider") == provider


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _mask(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"
