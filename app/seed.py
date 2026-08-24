from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.models import Agent, Membership, Room
from app.services.team import TeamService


async def seed_roster(session: AsyncSession, settings: Settings, team: TeamService) -> None:
    you = (await session.execute(select(Agent).where(Agent.name == "you"))).scalar_one_or_none()
    if you is None:
        you = await team.upsert_agent("you", kind="human", capabilities=["owner"])
    chief = (
        await session.execute(select(Agent).where(Agent.name == settings.primary_agent))
    ).scalar_one_or_none()
    scout = (await session.execute(select(Agent).where(Agent.name == "scout"))).scalar_one_or_none()
    if chief is None and scout is not None:
        # Preserve the row and every foreign key across the Scout -> Chief cutover.
        scout.name = settings.primary_agent
        await session.flush()
    for spec in settings.agents:
        await team.upsert_agent(
            spec.name,
            kind="hermes",
            profile=spec.profile,
            role_title=spec.role_title,
            job_description=spec.job_description,
            capabilities=spec.capabilities,
        )
    chief = await team.get_agent(settings.primary_agent)
    # Existing pre-hiring installs keep their teammates as durable staff.
    for agent in await team.list_agents():
        if agent.kind != "hermes" or agent.id == chief.id:
            continue
        if agent.reports_to_id is None:
            agent.reports_to_id = chief.id
        if not agent.role_title:
            agent.role_title = agent.name.replace("_", " ").title()
        if not agent.job_description:
            agent.job_description = (
                f"Owns {agent.role_title.lower()} work assigned by Chief. "
                "Maintains a durable specialty within the organization."
            )
    general = (
        await session.execute(
            select(Room)
            .options(selectinload(Room.memberships))
            .where(Room.type == "channel", Room.title == "general")
        )
    ).scalar_one_or_none()
    everyone = await team.list_agents()
    if general is None:
        general = Room(
            type="channel",
            title="general",
            objective="The office-wide channel for shared context and announcements.",
            owner_id=chief.id,
            lifecycle="open",
            created_at=team.clock.now(),
        )
        session.add(general)
        await session.flush()
    existing_members = {
        membership.agent_id: membership
        for membership in (
            await session.execute(select(Membership).where(Membership.room_id == general.id))
        ).scalars()
    }
    for agent in everyone:
        membership = existing_members.get(agent.id)
        role = "owner" if agent.id in {you.id, chief.id} else "member"
        if membership is None:
            session.add(
                Membership(
                    room_id=general.id,
                    agent_id=agent.id,
                    role=role,
                    notification_level="mentions",
                )
            )
        elif agent.id in {you.id, chief.id}:
            membership.role = "owner"
    dms = list(
        (
            await session.execute(
                select(Room)
                .options(selectinload(Room.memberships).selectinload(Membership.agent))
                .where(Room.type == "dm")
            )
        )
        .scalars()
        .unique()
    )
    for room in dms:
        room.title = " × ".join(sorted(member.agent.name for member in room.memberships))
    await session.flush()
