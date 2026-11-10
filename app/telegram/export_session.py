"""Export the logged-in Telethon session as a StringSession for hosts without a persistent disk.

    python -m app.telegram.export_session [output-file]

Reads the local session file (no connection to Telegram is made) and writes the string to
output-file (default: data/telethon_session.txt, mode 600). Put it into the TELETHON_SESSION variable
on the server. It grants full access to the Telegram account: never commit or share it.
"""
import os
import sys
from pathlib import Path

from telethon.sessions import SQLiteSession, StringSession

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    src = Path(settings.telethon_session_path + ".session")
    if not src.exists():
        raise SystemExit(f"No session file at {src}. Run `python -m app.telegram.login` first.")
    sqlite = SQLiteSession(settings.telethon_session_path)
    if not sqlite.auth_key:
        raise SystemExit("The session file is not logged in. Run `python -m app.telegram.login` first.")
    value = StringSession.save(sqlite)
    sqlite.close()
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "data/telethon_session.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(value + "\n")
    print(f"Session string written to {out} ({len(value)} chars). Paste it into TELETHON_SESSION.")


if __name__ == "__main__":
    main()
