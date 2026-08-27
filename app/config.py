from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class YAMLSettingsSource(PydanticBaseSettingsSource):
    """Read non-secret operator preferences from config.yaml."""

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        values = self()
        return values.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        path = Path("config.yaml")
        if not path.exists():
            return {}
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise ValueError("config.yaml must contain a YAML mapping")
        return loaded


class BudgetSettings(BaseModel):
    max_turns: int = 24
    max_handoffs: int = 6
    max_concurrency: int = Field(default=3, ge=1, le=16)
    wake_lease_seconds: int = Field(default=600, ge=30, le=3600)
    max_active_agents: int = Field(default=8, ge=1, le=64)
    max_group_participants: int = Field(default=3, ge=2, le=8)
    max_group_rounds: int = Field(default=6, ge=1, le=24)


class AgentSpec(BaseModel):
    name: str
    profile: str
    role_title: str = ""
    job_description: str = ""
    reports_to: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    provider: Literal["auto", "grok_build", "codex", "openrouter"] = "auto"


class InferenceSettings(BaseModel):
    default: Literal["auto", "grok_build", "codex", "openrouter"] = "auto"
    openrouter_model: str = "x-ai/grok-4.5"
    reasoning_effort: Literal[
        "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"
    ] = "medium"
    grok_auth_path: str = "~/.grok/auth.json"
    codex_auth_path: str = "~/.codex/auth.json"
    hermes_auth_path: str = ".moatbots-auth/auth.json"
    hermes_env_path: str = ".moatbots-auth/.env"
    host_hermes_auth_path: str = "~/.hermes/auth.json"
    host_hermes_env_path: str = "~/.hermes/.env"
    grok_login_binary: str = "grok"


class PortalSettings(BaseModel):
    kind: Literal["none", "telegram"] = "none"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MOATBOTS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8080
    debug_logging: bool = False
    database_url: str = "sqlite+aiosqlite:///data/app.db"
    public_url: str = "http://127.0.0.1:8080"
    api_token: str = Field(default="local-dev-token", min_length=8)
    hermes_binary: str = "scripts/hermes-docker.sh"
    hermes_home: str = ".moatbots-agents/chief"
    allow_host_hermes: bool = False
    hermes_timeout_seconds: int = Field(default=300, ge=10, le=3600)
    agent_workspace: str = "workspace"
    agent_state_dir: str = ".moatbots-agents"
    agent_shared_dir: str = ".moatbots-kanban"
    agent_auth_dir: str = ".moatbots-auth"
    agent_desktop_dir: str = ".moatbots-desktop"
    agent_image: str = "moatbots-agent:local"
    agent_container_name: str = "moatbots-agent-desktop"
    agent_desktop_host: Literal["127.0.0.1"] = "127.0.0.1"
    agent_desktop_port: int = 43128
    agent_api_url: str = "http://host.docker.internal:43127"
    agent_api_host: str = "0.0.0.0"
    agent_api_port: int = 43127
    openrouter_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    inference: InferenceSettings = Field(default_factory=InferenceSettings)
    portal: PortalSettings = Field(default_factory=PortalSettings)
    budgets: BudgetSettings = Field(default_factory=BudgetSettings)
    primary_agent: Literal["chief"] = "chief"
    agents: list[AgentSpec] = Field(
        default_factory=lambda: [
            AgentSpec(
                name="chief",
                profile="chief",
                role_title="Chief of Staff",
                job_description=(
                    "Runs the organization, assigns work, and decides when the team needs a new "
                    "specialist. Reuses qualified teammates before making a durable hire."
                ),
                capabilities=["monitor", "coordinate", "dispatch"],
            ),
        ]
    )

    @model_validator(mode="after")
    def validate_roster(self) -> Self:
        if self.budgets.wake_lease_seconds <= self.hermes_timeout_seconds:
            raise ValueError("wake lease must be longer than the Hermes run timeout")
        if self.agent_desktop_port == self.agent_api_port or self.agent_desktop_port == self.port:
            raise ValueError("agent desktop, tool callback, and office ports must be distinct")
        by_name = {agent.name: agent for agent in self.agents}
        if len(by_name) != len(self.agents):
            raise ValueError("agent names must be unique")
        if len({agent.profile for agent in self.agents}) != len(self.agents):
            raise ValueError("Hermes profiles must be unique")
        if self.primary_agent not in by_name:
            raise ValueError("the required chief agent is missing")
        if len(self.agents) != 1:
            raise ValueError("only chief may be configured; chief hires the rest at runtime")
        if by_name[self.primary_agent].reports_to is not None:
            raise ValueError("chief cannot report to another agent")
        for agent in self.agents:
            if agent.reports_to and agent.reports_to not in by_name:
                raise ValueError(f"{agent.name} reports to unknown agent {agent.reports_to}")
            seen = {agent.name}
            manager = agent.reports_to
            while manager:
                if manager in seen:
                    raise ValueError("agent reporting lines cannot contain a cycle")
                seen.add(manager)
                manager = by_name[manager].reports_to
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        del file_secret_settings
        return (
            env_settings,
            dotenv_settings,
            init_settings,
            YAMLSettingsSource(settings_cls),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
