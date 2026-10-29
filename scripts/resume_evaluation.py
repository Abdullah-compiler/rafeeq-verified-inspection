# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Resume authentic evaluation after free-quota reset; never buy credits or change model tiers."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def summary():
    return json.loads((ROOT / "results/evaluation/summary.json").read_text())


def execute(command, log):
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=3300)
    output = re.sub(r"sk-or-v1-[A-Za-z0-9_-]+", "[redacted]", result.stdout + result.stderr)
    log.write_text(output, encoding="utf-8"); log.chmod(0o600)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    before = summary()
    if args.check_only:
        print(json.dumps({"complete": before["complete"], "completed": before["completed_runs"], "expected": before["expected_runs"], "stop_reason": before.get("stop_reason")}, indent=2))
        return 0
    uv = shutil.which("uv") or str(Path.home() / ".hermes/bin/uv")
    work = ROOT / "labs/artifacts/resume"
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    with (work / "resume.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another resume is active; no duplicate run started.")
            return 0
        rc = 0
        if not before["complete"]:
            rc = execute([uv, "run", "--script", "--python", "3.12", "rafeeq.py", "eval", "--resume"], work / (stamp + "-evaluation.log"))
        current = summary()
        execute([uv, "run", "scripts/render_readme.py"], work / (stamp + "-readme.log"))
        if not current["complete"]:
            print(json.dumps({"status": "waiting_for_free_quota" if current.get("stop_reason") == "quota_exhausted" else "incomplete", "complete": False, "completed": current["completed_runs"], "expected": current["expected_runs"], "evaluation_exit": rc, "stop_reason": current.get("stop_reason")}, indent=2))
            return 0 if current.get("stop_reason") == "quota_exhausted" else 1
        check_rc = execute([uv, "run", "--script", "--python", "3.12", "rafeeq.py", "checks"], work / (stamp + "-checks.log"))
        print(json.dumps({"status": "evaluation_complete", "complete": True, "completed": current["completed_runs"], "expected": current["expected_runs"], "offline_checks_exit": check_rc, "next": "Verify remaining live HTTP smoke if quota allows, regenerate docs/slides, clean-copy test, secret audit, then publish only to the approved repository."}, indent=2))
        return check_rc


if __name__ == "__main__":
    raise SystemExit(main())
