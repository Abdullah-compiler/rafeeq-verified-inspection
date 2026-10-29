# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Generate private local service credentials without printing them."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    path = ROOT / ".env"
    existing = path.read_text() if path.exists() else (ROOT / ".env.example").read_text()
    # Support this project's older labs/.env, never any unrelated credential store.
    legacy = {}
    old = ROOT / "labs/.env"
    if old.exists():
        legacy = dict(line.split("=", 1) for line in old.read_text().splitlines() if "=" in line and not line.lstrip().startswith("#"))
    values = dict(line.split("=", 1) for line in existing.splitlines() if "=" in line and not line.lstrip().startswith("#"))
    changes = {}
    if not values.get("OPENROUTER_API_KEY", "").strip() and legacy.get("OPENROUTER_API_KEY"):
        changes["OPENROUTER_API_KEY"] = legacy["OPENROUTER_API_KEY"]
    for name in ("RAFEEQ_USER_TOKEN", "RAFEEQ_OFFICER_TOKEN"):
        if not values.get(name, "").strip():
            changes[name] = secrets.token_urlsafe(32)
    lines = []
    for line in existing.splitlines():
        name = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else None
        lines.append(name + "=" + changes.pop(name) if name is not None and name in changes else line)
    lines.extend(name + "=" + value for name, value in changes.items())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("Local .env configured with owner-only permissions; no credential values displayed.")


if __name__ == "__main__":
    main()
