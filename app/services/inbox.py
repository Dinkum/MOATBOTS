from app.services.portal import parse_inbound
from app.services.team import TeamService


async def accept_human_text(team: TeamService, text: str, default: str) -> str:
    """A message from you on the portal. Lands in the DM with the named teammate."""
    names = [agent.name for agent in await team.list_agents() if agent.kind != "human"]
    fallback = default if default in names else (names[0] if names else "chief")
    target, body = parse_inbound(text, names, fallback)
    you = await team.get_agent("you")
    peer = await team.get_agent(target)
    room = await team.get_or_create_dm(you, peer)
    await team.send_message(you, room.id, body)
    return target
