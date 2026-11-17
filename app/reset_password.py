"""Forgotten password: `docker exec ntv-allocator python -m app.reset_password`

Clears the status page password and signs everyone out. Placements and settings are kept.
Open the status page and create a new password with the setup code printed below.
"""
import os

from .auth import Auth
from .state import State


def main() -> None:
    state = State(os.getenv("STATE_PATH", "/data/state.db"))
    code = Auth(state).reset()
    bar = "#" * 64
    print("\n".join(["", bar, "#", "#   PASSWORD RESET", "#",
                      "#   The status page password is cleared and all sessions are signed out.",
                      "#   Placements and settings are kept.", "#",
                      "#   Open the status page and create a new password with this code:", "#",
                      f"#        SETUP CODE:   {code}", "#", bar, ""]))


if __name__ == "__main__":
    main()
