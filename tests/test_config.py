import pytest
from pydantic import ValidationError

from app.config import AgentSpec, Settings


def test_chief_is_the_only_configured_teammate():
    settings = Settings()
    by_name = {agent.name: agent for agent in settings.agents}

    assert settings.primary_agent == "chief"
    assert settings.budgets.max_group_participants == 3
    assert settings.budgets.max_group_rounds == 6
    assert list(by_name) == ["chief"]
    assert by_name["chief"].role_title == "Chief of Staff"


def test_config_rejects_preset_hires():
    with pytest.raises(ValidationError, match="only chief"):
        Settings(
            agents=[
                AgentSpec(name="chief", profile="chief"),
                AgentSpec(name="researcher", profile="researcher", reports_to="chief"),
            ]
        )


def test_desktop_port_must_not_overlap_host_services():
    with pytest.raises(ValidationError, match="ports must be distinct"):
        Settings(agent_desktop_port=43127)
