# /// script
# requires-python = ">=3.12"
# dependencies = ["openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1", "mcp>=2.3", "fastapi>=0.135,<1", "uvicorn>=0.42,<1", "httpx>=0.28,<1"]
# ///
"""Run the real database/MCP checks without any model API calls; save test evidence."""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main():
    suite = unittest.defaultTestLoader.discover(str(HERE / "tests"))
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    summary = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "model_api_calls": 0,
        "tests_run": result.testsRun,
        "failures": [test.id() for test, _ in result.failures],
        "errors": [test.id() for test, _ in result.errors],
        "skipped": [test.id() for test, _ in result.skipped],
        "successful": result.wasSuccessful(),
    }
    folder = ROOT / "results"
    folder.mkdir(exist_ok=True)
    (folder / "offline-tests.json").write_text(json.dumps(summary, indent=2) + "\n")
    (folder / "offline-tests.txt").write_text(log.getvalue(), encoding="utf-8")
    print(log.getvalue())
    print(json.dumps(summary, indent=2))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
