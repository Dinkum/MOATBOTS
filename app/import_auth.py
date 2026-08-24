"""Import host Hermes / OpenRouter logins into the office copy. Never overwrites."""

from app.config import get_settings
from app.services.auth import AuthService


def main() -> None:
    imported = AuthService(get_settings()).import_host_logins()
    if imported:
        print("imported:", ", ".join(imported))
    else:
        print("nothing new to import")


if __name__ == "__main__":
    main()
