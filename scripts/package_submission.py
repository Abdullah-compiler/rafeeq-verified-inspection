# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Export only the submission, never the research checkout, secrets, caches or databases."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ["README.md", "COURSE_README.md", "THIRD_PARTY_NOTICES.md", ".gitignore", ".env.example", ".python-version", "rafeeq.py", "START_RAFEEQ.command"]
COURSE_FILES = ["labs/agentkit.py", "labs/madar.py", "labs/prepare_data.py", "labs/.env.example", "labs/data/AGENTS.md", "labs/capstone/README.md", "labs/capstone/TEMPLATE_README.md"]
REQUIRED = ["labs/capstone/api.py", "labs/capstone/evaluate.py", "labs/capstone/tasks.jsonl", "labs/capstone/redteam.py", "docs/API.md", "docs/EVALUATION.md", "docs/SECURITY.md", "docs/slides.pdf", "docs/slides.html", "docs/demo-replay.html", "results/evaluation/A.csv", "results/evaluation/B.csv", "results/security/summary.json"]


def source_files():
    selected = {ROOT / name for name in ROOT_FILES + COURSE_FILES + REQUIRED}
    for pattern in ("labs/capstone/*.py", "labs/capstone/*.lock", "labs/capstone/tests/*.py", "labs/data/policies/*.md", "docs/*", "scripts/*.py", "results/**/*.json", "results/**/*.jsonl", "results/**/*.csv", "results/**/*.txt", "results/**/*.md", "rafeeq.py.lock", ".github/workflows/rafeeq-checks.yml"):
        selected.update(p for p in ROOT.glob(pattern) if p.is_file())
    for p in selected:
        if p.is_symlink():
            raise RuntimeError(f"Symlink not allowed in export: {p.relative_to(ROOT)}")
        if not p.exists():
            raise RuntimeError(f"Missing submission file: {p.relative_to(ROOT)}")
    return sorted(selected)


def validate(allow_incomplete=False):
    for name in REQUIRED:
        if not (ROOT / name).is_file():
            raise RuntimeError(f"Required deliverable is missing: {name}")
    tasks = [json.loads(line) for line in (ROOT / "labs/capstone/tasks.jsonl").read_text().splitlines() if line.strip()]
    ids = [task["id"] for task in tasks]
    if len(ids) < 15 or len(ids) != len(set(ids)):
        raise RuntimeError("At least 15 uniquely identified tasks are required.")
    complete = True
    for config in ("A", "B"):
        with (ROOT / "results/evaluation" / f"{config}.csv").open(newline="") as f:
            rows = list(csv.DictReader(f))
        found = [row.get("id", row.get("task_id")) for row in rows]
        if len(found) != len(set(found)) or not set(found).issubset(ids):
            raise RuntimeError(f"Config {config} has duplicate or unknown task IDs.")
        if len(rows) != len(ids) or set(found) != set(ids) or any(row.get("status") != "completed" for row in rows):
            complete = False
    summary = json.loads((ROOT / "results/evaluation/summary.json").read_text())
    complete = complete and summary.get("complete") is True
    if not complete and not allow_incomplete:
        raise RuntimeError("A/B evidence is incomplete. Only an explicitly labelled --allow-incomplete draft may be exported.")
    tests = json.loads((ROOT / "results/offline-tests.json").read_text())
    if not tests.get("successful"):
        raise RuntimeError("Offline checks must pass before export.")
    security = json.loads((ROOT / "results/security/summary.json").read_text())
    if not security.get("attack_worked_in_baseline") or not security.get("defended_completed_without_leak"):
        raise RuntimeError("The before/after security demonstration is not complete.")
    return len(tasks), complete


def secret_audit(files):
    private = []
    for path in (ROOT / ".env", ROOT / "labs/.env"):
        if path.exists():
            for line in path.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    if any(t in key.upper() for t in ("KEY", "TOKEN", "SECRET")) and len(value) > 12:
                        private.append(value.strip().strip("\"'"))
    token_pattern = re.compile(rb"(?:sk-or-v1-[a-fA-F0-9]{64}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")
    for path in files:
        data = path.read_bytes()
        if any(secret.encode() in data for secret in private) or token_pattern.search(data):
            raise RuntimeError(f"Secret-like content in export candidate: {path.relative_to(ROOT)}")
        if path.name == ".env" or path.suffix in {".sqlite", ".db"}:
            raise RuntimeError(f"Private runtime file selected: {path.relative_to(ROOT)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "submission-dist")
    parser.add_argument("--allow-incomplete", action="store_true", help="Export a prominently labelled DRAFT; never treat it as submission-ready")
    args = parser.parse_args()
    count, ready = validate(args.allow_incomplete)
    files = source_files()
    secret_audit(files)
    export = args.output / "rafeeq-verified-inspection"
    if export.exists():
        raise RuntimeError("Export folder already exists; choose a new --output path rather than overwriting it.")
    export.mkdir(parents=True)
    entries = []
    for source in files:
        relative = source.relative_to(ROOT)
        target = export / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        entries.append({"path": str(relative), "bytes": target.stat().st_size, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    manifest = {"project": "Rafeeq / Verified Inspection Desk", "submission_ready": ready, "task_count": count, "files": entries,
                "excluded": ["real .env files", "provider/service secrets", "raw response cache", "runtime databases", "original Git history", "unrelated course notebooks/slides"]}
    (export / "SUBMISSION_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    archive = args.output / ("rafeeq-verified-inspection.zip" if ready else "rafeeq-verified-inspection-DRAFT.zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for file in sorted(export.rglob("*")):
            if file.is_file():
                z.write(file, file.relative_to(export.parent))
    with zipfile.ZipFile(archive) as z:
        assert len(z.namelist()) == len(files) + 1
        assert z.testzip() is None
    print(json.dumps({"folder": str(export), "zip": str(archive), "submission_ready": ready, "source_files": len(files), "archive_files": len(files) + 1, "tasks": count, "zip_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}, indent=2))


if __name__ == "__main__":
    main()
