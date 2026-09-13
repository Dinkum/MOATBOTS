from app.models import PortalReceipt
from app.services.portal import parse_inbound
from app.services.team import TeamService


async def accept_human_text(
    team: TeamService, text: str, default: str, *, delivery_id: str | None = None
) -> str:
    """A message from you on the portal. Lands in the DM with the named teammate."""
    if delivery_id:
        receipt = await team.db.get(PortalReceipt, delivery_id)
        if receipt:
            return receipt.peer
    names = [agent.name for agent in await team.list_agents() if agent.kind != "human"]
    fallback = default if default in names else (names[0] if names else "chief")
    target, body = parse_inbound(text, names, fallback)
    if delivery_id:
        # The receipt and message commit together. A failed transaction remains retryable.
        team.db.add(PortalReceipt(id=delivery_id, peer=target, received_at=team.clock.now()))
        await team.db.flush()
    you = await team.get_agent("you")
    peer = await team.get_agent(target)
    room = await team.get_or_create_dm(you, peer)
    await team.send_message(you, room.id, body)
    return target
