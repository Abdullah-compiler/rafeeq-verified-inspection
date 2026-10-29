# /// script
# requires-python = ">=3.12"
# dependencies = ["openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1", "mcp>=2.3", "fastapi>=0.135,<1", "uvicorn>=0.42,<1", "httpx>=0.28,<1"]
# ///
"""One entry point for Rafeeq's CLI, service, evaluation and security evidence."""
from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")  # optional root setup; original labs/.env remains supported
TARGETS = {"ask": "rafeeq.py", "chat": "rafeeq.py", "serve": "api.py", "eval": "evaluate.py", "redteam": "redteam.py", "checks": "checks.py"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=TARGETS)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    target = ROOT / "labs" / "capstone" / TARGETS[args.command]
    if not target.exists():
        parser.error(f"Component not present: {target.name}")
    sys.path.insert(0, str(target.parent))
    sys.path.insert(0, str(target.parent.parent))
    sys.argv = [str(target), *([args.command] if args.command in {"ask", "chat"} else []), *args.arguments]
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()
