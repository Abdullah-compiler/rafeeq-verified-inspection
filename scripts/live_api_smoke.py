# /// script
# requires-python = ">=3.12"
# dependencies = ["openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1", "mcp>=2.3", "fastapi>=0.135,<1", "uvicorn>=0.42,<1", "httpx>=0.28,<1"]
# ///
"""Exercise the actual loopback service; --model-chat uses a real free-provider request."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-chat", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "labs/.env")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    data = ROOT / "labs/artifacts/api-smoke" / stamp
    data.mkdir(parents=True, mode=0o700)
    user, officer = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    env = {**os.environ, "RAFEEQ_USER_TOKEN": user, "RAFEEQ_OFFICER_TOKEN": officer, "RAFEEQ_API_DATA": str(data)}
    proc = subprocess.Popen([sys.executable, str(ROOT / "rafeeq.py"), "serve", "--host", "127.0.0.1", "--port", str(port)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = {"recorded_at": datetime.now(timezone.utc).isoformat(), "live_model_chat": args.model_chat, "verified": False}
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=180) as client:
            for _ in range(100):
                if proc.poll() is not None:
                    raise RuntimeError("Service exited before readiness")
                try:
                    response = client.get("/health")
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Service did not become ready")
            assert response.json()["configuration"] == "B"
            denied = client.post("/sessions")
            assert denied.status_code == 401
            created = client.post("/sessions", headers={"Authorization": "Bearer " + user})
            assert created.status_code == 201
            session = created.json()
            result.update(health_status=response.status_code, unauthenticated_status=denied.status_code, create_status=created.status_code)
            if args.model_chat:
                chat = client.post("/sessions/" + session["session_id"] + "/chat",
                    headers={"Authorization": "Bearer " + user, "X-Session-Token": session["session_token"]},
                    json={"question": "Please retrieve POL-05 and explain in English how far ahead an inspection must be booked. Cite the policy ID."})
                result["chat_status"] = chat.status_code
                if chat.status_code != 200:
                    raise RuntimeError("Live model chat did not complete: HTTP " + str(chat.status_code))
                body = chat.json()
                assert body["state"] == "complete" and body["usage"]["observed_tokens"] > 0 and "POL-05" in body["reply"]
                result.update(reply=body["reply"], usage=body["usage"], state=body["state"])
            result["verified"] = True
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(timeout=10)
        result["server_stopped"] = proc.poll() is not None
    folder = ROOT / "results"; folder.mkdir(exist_ok=True)
    (folder / "api-live-smoke.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
