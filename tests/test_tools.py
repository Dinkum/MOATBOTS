import pytest

from agent import mcp_server
from app.errors import TeamError
from app.services.tools import TOOL_NAMES, Toolbelt, tool_schemas
from tests.conftest import open_team


def test_collaboration_surface_is_small():
    names = {item["name"] for item in tool_schemas()}
    assert names == set(TOOL_NAMES)
    assert "kanban_create" not in names


def test_mcp_lists_the_same_tools(monkeypatch):
    monkeypatch.setattr(
        mcp_server,
        "_request",
        lambda method, path, payload=None: {"tools": tool_schemas()},
    )
    reply = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert reply is not None
    listed = {item["name"] for item in reply["result"]["tools"]}
    assert listed == set(TOOL_NAMES)


def test_mcp_passes_actor_and_run_headers(monkeypatch):
    monkeypatch.setenv("MOATBOTS_AGENT_TOKEN", "scoped-secret")
    monkeypatch.setenv("MOATBOTS_ACTOR", "chief")
    monkeypatch.setenv("MOATBOTS_RUN_ID", "run-1")
    assert mcp_server._headers() == {
        "Authorization": "Bearer scoped-secret",
        "X-Moatbots-Actor": "chief",
        "X-Moatbots-Run": "run-1",
        "Content-Type": "application/json",
    }


def test_mcp_ignores_an_unresolved_optional_run(monkeypatch):
    monkeypatch.setenv("MOATBOTS_RUN_ID", "${MOATBOTS_RUN_ID}")
    assert "X-Moatbots-Run" not in mcp_server._headers()


@pytest.mark.asyncio
async def test_chief_hires_updates_and_retires_a_durable_teammate(world):
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        analyst = await team.hire_agent(
            chief,
            "maya",
            "Researcher",
            "Tracks AI companies and verifies corporate disclosures. Specializes in SEC filings.",
        )
        assert analyst.reports_to_id == chief.id
        assert analyst.role_title == "Researcher"
        assert world["profiles"].profiles["maya"].startswith("Researcher.")
        assert [agent.name for agent in await team.find_agents(["filing"])] == ["maya"]

        updated = await team.update_agent(
            chief,
            "maya",
            job_description=(
                "Tracks AI companies and verifies corporate disclosures. "
                "Specializes in earnings calls and product launches."
            ),
        )
        assert "earnings" in updated.capabilities_json

        retired = await team.retire_agent(chief, "maya")
        assert retired.status == "retired"
        assert "maya" not in {agent.name for agent in await team.list_agents()}


@pytest.mark.asyncio
async def test_non_chief_cannot_hire(world):
    async with open_team(world) as team:
        you = await team.get_agent("you")
        with pytest.raises(TeamError, match="Only chief"):
            await team.hire_agent(
                you,
                "maya",
                "Researcher",
                "Tracks AI companies and verifies corporate disclosures. Specializes in filings.",
            )


@pytest.mark.asyncio
async def test_agents_can_discover_and_start_conversations_through_tools(world):
    async with open_team(world) as team:
        chief = await team.get_agent("chief")
        researcher = await team.hire_agent(
            chief,
            "maya",
            "Researcher",
            "Tracks AI companies and verifies corporate disclosures from primary sources.",
        )
        tools = Toolbelt(team, researcher)

        listed = await tools.call("agents.list", {})
        assert {agent["name"] for agent in listed["agents"]} == {"chief", "maya"}
        org = await tools.call("agents.org", {})
        assert (
            next(row for row in org["organization"] if row["name"] == "maya")["reports_to"]
            == "chief"
        )

        channels = await tools.call("channels.list", {"query": "gen"})
        assert channels["channels"] == [
            {
                "id": channels["channels"][0]["id"],
                "name": "general",
                "description": "The office-wide channel for shared context and announcements.",
                "joined": True,
                "owners": ["chief", "you"],
            }
        ]

        started = await tools.call("conversations.start", {"type": "dm", "participants": ["chief"]})
        assert started["type"] == "dm"
        root = await tools.call(
            "messages.send", {"room": started["room"], "message": "Initial finding"}
        )
        reply = await tools.call(
            "messages.reply", {"message": root["message"], "reply": "Source attached"}
        )
        assert reply["thread"] == root["message"]
