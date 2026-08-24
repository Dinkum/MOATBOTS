from pathlib import Path

from app.services.profiles import HermesProfileProvisioner


def test_hermes_profile_identity_is_written_under_that_agents_home(tmp_path: Path):
    provisioner = HermesProfileProvisioner(
        binary=str(tmp_path / "hermes"),
        home=str(tmp_path / "agents" / "chief"),
        state_root=str(tmp_path / "agents"),
        root=tmp_path,
    )

    provisioner._write_soul(
        "researcher",
        "Researcher",
        "Owns source review for the Chief.",
    )

    soul = tmp_path / "agents" / "researcher" / "profiles" / "researcher" / "SOUL.md"
    assert soul.read_text(encoding="utf-8") == (
        "You are @researcher, a Researcher.\n"
        "Your specialty: Owns source review for the Chief.\n"
        "Do your specialty work, report blockers, and keep team coordination in Moatbots.\n"
    )
    assert not (tmp_path / "agents" / "chief" / "profiles" / "researcher").exists()
