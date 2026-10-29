# /// script
# requires-python = ">=3.12"
# dependencies = ["fastapi>=0.135,<1", "uvicorn>=0.42,<1", "httpx>=0.28,<1", "openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1", "mcp>=2.3"]
# ///
"""OFFLINE protocol/security tests. Fake model/public-policy fixtures, real SQLite and HTTP.

No fixture output is provider evidence. No model endpoint is called by this file.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api as A
from fastapi.testclient import TestClient

USER = "offline-user-service-token-" + "u" * 32
OFFICER = "offline-officer-service-token-" + "o" * 32
PERMIT, ID4, MOBILE, SLOT = "FTR-30311", "6654", "0532236654", "2026-11-04 09:00"


class FakePublicPolicyBridge:
    """Explicit public-policy test fixture; not a real MCP transport test."""
    def list_tools(self):
        return [SimpleNamespace(name="search_policies")]

    def call(self, name, arguments):
        assert name == "search_policies"
        return SimpleNamespace(is_error=False, content=[SimpleNamespace(type="text", text=json.dumps([
            {"policy_id": "POL-05", "text": "OFFLINE FIXTURE ONLY: inspection policy marker."}
        ]))])


@contextmanager
def fake_policy_connection():
    yield FakePublicPolicyBridge()


def fake_model_runner(question, session, bridge, history=None, shape=None, *, config="B", use_cache=False):
    """Deterministic OFFLINE runner drives the real guarded tools, never a provider substitute."""
    assert config == "B" and use_cache is False and history is None
    assert shape is not None
    session.begin_turn()
    session.remember_user_input(question)
    shape.reserve(100)  # labelled fixture accounting, NOT measured provider tokens
    if question == "raise secret error":
        raise RuntimeError("sensitive upstream message " + MOBILE)
    if question == "exhaust":
        shape.reserve(shape.limit)
    final = "OFFLINE FIXTURE ONLY; no provider response."
    if question.startswith("book"):
        session.get_permit(PERMIT, ID4, MOBILE)
        A.R.make_tools(session, bridge)[0]("POL-05")
        session.list_slots("Al-Rawdah", "2026-11-04", "2026-11-04")
        try:
            session.book_inspection(PERMIT, SLOT)
        except A.R.madar.ToolError:
            pass
        final = "Deliberately misleading OFFLINE fixture: human rejected and booking confirmed."
    elif question.startswith("echo"):
        final = question
    run = SimpleNamespace(final=final, stopped="done", state=[])
    return run, shape


class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rafeeq-api-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = [1_800_000_000.0]
        self.settings = A.Settings(self.root, USER, OFFICER)
        self.app = self.make_app()
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.user = {"Authorization": "Bearer " + USER}
        self.officer = {"Authorization": "Bearer " + OFFICER}
        self.sid, self.capability = self.new_session()
        self.owner = {**self.user, "X-Session-Token": self.capability}

    def make_app(self, settings=None, runner=fake_model_runner):
        return A.create_app(settings or self.settings, runner=runner,
                            bridge_factory=fake_policy_connection, clock=lambda: self.now[0])

    def new_session(self, client=None):
        response = (client or self.client).post("/sessions", headers={"Authorization": "Bearer " + USER})
        self.assertEqual(response.status_code, 201, response.text)
        value = response.json()
        return value["session_id"], value["session_token"]

    def pending(self, client=None):
        response = (client or self.client).post(f"/sessions/{self.sid}/chat", headers=self.owner, json={"question": "book fixture"})
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual(value["state"], "pending_approval")
        self.assertIn("awaiting officer approval", value["reply"])
        self.assertNotIn("rejected", value["reply"])
        self.assertNotIn("confirmed", value["reply"])
        self.assertEqual(len(value["approvals"]), 1)
        return value["approvals"][0]

    def url(self, item, sid=None):
        return f"/officer/sessions/{sid or self.sid}/approvals/{item['approval_id']}/decision"

    def body(self, item, decision="approve"):
        value = {"decision": decision, "proposal": item["proposal"]}
        if decision == "approve":
            value.update(national_id_last4=ID4, mobile=MOBILE)
        return value

    def db(self):
        return self.app.state.service.database(self.sid)

    def test_health_docs_and_openapi_are_public_without_secrets(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok", "data": "synthetic", "configuration": "B", "cache": False})
        self.assertEqual(self.client.get("/docs").status_code, 200)
        schema = self.client.get("/openapi.json")
        self.assertEqual(schema.status_code, 200)
        self.assertIn("HTTPBearer", schema.json()["components"]["securitySchemes"])
        self.assertNotIn(USER, schema.text)
        self.assertNotIn(OFFICER, schema.text)
        self.assertEqual(schema.headers["cache-control"], "no-store")

    def test_missing_wrong_and_cross_role_tokens_denied(self):
        self.assertEqual(self.client.post("/sessions").status_code, 401)
        self.assertEqual(self.client.post("/sessions", headers={"Authorization": "Bearer wrong"}).status_code, 403)
        self.assertEqual(self.client.post("/sessions", headers=self.officer).status_code, 403)
        self.assertEqual(self.client.get("/officer/approvals", headers=self.user).status_code, 403)
        item = self.pending()
        self.assertEqual(self.client.post(self.url(item), headers=self.owner, json=self.body(item)).status_code, 403)
        self.assertEqual(self.client.get(f"/sessions/{self.sid}", headers={**self.officer, "X-Session-Token": self.capability}).status_code, 403)

    def test_session_capability_blocks_cross_session_reads_chat_and_path_traversal(self):
        other, other_cap = self.new_session()
        response = self.client.get(f"/sessions/{other}", headers=self.owner)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.post(f"/sessions/{other}/chat", headers=self.owner, json={"question": "book fixture"}).status_code, 404)
        self.assertEqual(self.client.get(f"/sessions/{self.sid}", headers=self.user).status_code, 404)
        self.assertEqual(self.client.get("/sessions/not-a-uuid", headers=self.owner).status_code, 404)
        self.assertEqual(self.client.get(f"/sessions/{other}", headers={**self.user, "X-Session-Token": other_cap}).status_code, 200)

    def test_pending_is_durable_and_never_books_during_chat(self):
        item = self.pending()
        db = self.db()
        self.assertEqual(db.sql("SELECT * FROM inspections WHERE permit_id=?", (PERMIT,)), [])
        self.assertEqual(db.sql("SELECT taken FROM slots WHERE district='Al-Rawdah' AND slot=?", (SLOT,))[0]["taken"], 0)
        restarted = TestClient(self.make_app())
        self.addCleanup(restarted.close)
        state = restarted.get(f"/sessions/{self.sid}", headers=self.owner)
        self.assertEqual(state.status_code, 200)
        self.assertEqual(state.json()["approvals"][0], item)
        queue = restarted.get("/officer/approvals", headers=self.officer).json()
        self.assertEqual(queue["total"], len(queue["items"]))
        self.assertEqual(queue["items"][0]["approval_id"], item["approval_id"])
        self.assertEqual(queue["items"][0]["session_id"], self.sid)

    def test_approve_writes_and_reads_back_one_booking_and_bound_audit(self):
        item = self.pending()
        result = self.client.post(self.url(item), headers=self.officer, json=self.body(item))
        self.assertEqual(result.status_code, 200, result.text)
        value = result.json()
        self.assertEqual(value["status"], "approved")
        self.assertFalse(value["idempotent_replay"])
        booking = value["booking"]
        self.assertEqual(booking["slot"], SLOT)
        rows = self.db().sql("SELECT * FROM inspections WHERE inspection_id=?", (booking["inspection_id"],))
        self.assertEqual(len(rows), 1)
        self.assertEqual(A.R.BookingSession.public_booking(rows[0]), booking)
        audits = self.db().sql("SELECT * FROM audit_log WHERE actor=? AND action='book_inspection'", ("api-approval:" + item["approval_id"],))
        self.assertEqual(len(audits), 1)
        self.assertEqual(json.loads(audits[0]["detail"]), item["proposal"])
        self.assertEqual(self.client.get("/officer/approvals", headers=self.officer).json()["total"], 0)

    def test_same_decision_replays_without_write_opposite_decision_conflicts(self):
        item = self.pending()
        first = self.client.post(self.url(item), headers=self.officer, json=self.body(item)).json()
        restart = TestClient(self.make_app())
        self.addCleanup(restart.close)
        second = restart.post(self.url(item), headers=self.officer, json=self.body(item))
        self.assertEqual(second.status_code, 200, second.text)
        self.assertTrue(second.json()["idempotent_replay"])
        self.assertEqual(second.json()["booking"], first["booking"])
        self.assertEqual(restart.post(self.url(item), headers=self.officer, json=self.body(item, "reject")).status_code, 409)
        self.assertEqual(len(self.db().sql("SELECT * FROM inspections WHERE permit_id=?", (PERMIT,))), 1)

    def test_reject_is_terminal_idempotent_and_leaves_slot_free(self):
        item = self.pending()
        response = self.client.post(self.url(item), headers=self.officer, json=self.body(item, "reject"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "rejected")
        replay = self.client.post(self.url(item), headers=self.officer, json=self.body(item, "reject"))
        self.assertTrue(replay.json()["idempotent_replay"])
        self.assertEqual(self.client.post(self.url(item), headers=self.officer, json=self.body(item)).status_code, 409)
        self.assertEqual(self.db().sql("SELECT * FROM inspections WHERE permit_id=?", (PERMIT,)), [])
        self.assertEqual(self.db().sql("SELECT taken FROM slots WHERE district='Al-Rawdah' AND slot=?", (SLOT,))[0]["taken"], 0)

    def test_changed_and_cross_session_proposals_rejected(self):
        item = self.pending()
        body = self.body(item)
        body["proposal"] = {**body["proposal"], "slot": "2026-11-04 10:00"}
        self.assertEqual(self.client.post(self.url(item), headers=self.officer, json=body).status_code, 409)
        other, _ = self.new_session()
        self.assertEqual(self.client.post(self.url(item, other), headers=self.officer, json=self.body(item)).status_code, 404)
        self.assertEqual(self.db().sql("SELECT status FROM api_approvals")[0]["status"], "pending")

    def test_proposal_and_terminal_result_database_triggers(self):
        item = self.pending()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.db().sql("UPDATE api_approvals SET proposal='{}'")
        self.client.post(self.url(item), headers=self.officer, json=self.body(item, "reject"))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "terminal"):
            self.db().sql("UPDATE api_approvals SET status='pending'")

    def test_expiry_uses_wall_clock_not_simulation_clock(self):
        item = self.pending()
        self.now[0] = item["expires_at"]
        response = self.client.post(self.url(item), headers=self.officer, json=self.body(item))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.db().sql("SELECT status FROM api_approvals")[0]["status"], "expired")
        self.assertEqual(self.db().sql("SELECT * FROM inspections WHERE permit_id=?", (PERMIT,)), [])

    def test_fresh_identity_required_wrong_identity_does_not_consume(self):
        item = self.pending()
        missing = {"proposal": item["proposal"], "decision": "approve"}
        self.assertEqual(self.client.post(self.url(item), headers=self.officer, json=missing).status_code, 422)
        wrong = {**self.body(item), "national_id_last4": "0000"}
        response = self.client.post(self.url(item), headers=self.officer, json=wrong)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(MOBILE, response.text)
        self.assertEqual(self.db().sql("SELECT status FROM api_approvals")[0]["status"], "pending")
        self.assertEqual(self.client.post(self.url(item), headers=self.officer, json=self.body(item)).status_code, 200)

    def test_authorization_revocation_after_queue_is_rechecked(self):
        item = self.pending()
        self.db().sql("UPDATE applicants SET mobile='0500000000' WHERE applicant_id=(SELECT applicant_id FROM permits WHERE permit_id=?)", (PERMIT,))
        self.assertEqual(self.client.post(self.url(item), headers=self.officer, json=self.body(item)).status_code, 403)
        self.assertEqual(self.db().sql("SELECT * FROM inspections WHERE permit_id=?", (PERMIT,)), [])

    def test_changed_business_details_fail_exact_proposal_gate(self):
        item = self.pending()
        self.db().sql("UPDATE permits SET status='under_review' WHERE permit_id=?", (PERMIT,))
        response = self.client.post(self.url(item), headers=self.officer, json=self.body(item))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.db().sql("SELECT status FROM api_approvals")[0]["status"], "failed")
        self.assertEqual(self.db().sql("SELECT * FROM inspections WHERE permit_id=?", (PERMIT,)), [])

    def test_taken_slot_fails_without_duplicate_or_partial_write(self):
        item = self.pending()
        self.db().sql("UPDATE slots SET taken=1 WHERE district='Al-Rawdah' AND slot=?", (SLOT,))
        response = self.client.post(self.url(item), headers=self.officer, json=self.body(item))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.db().sql("SELECT * FROM inspections WHERE permit_id=?", (PERMIT,)), [])

    def test_simultaneous_duplicate_decisions_are_one_write(self):
        item = self.pending()
        second = TestClient(self.make_app())
        self.addCleanup(second.close)
        barrier = threading.Barrier(2)

        def decide(client):
            barrier.wait()
            return client.post(self.url(item), headers=self.officer, json=self.body(item))

        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(decide, [self.client, second]))
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(sorted(r.json()["idempotent_replay"] for r in responses), [False, True])
        self.assertEqual(len(self.db().sql("SELECT * FROM inspections WHERE permit_id=?", (PERMIT,))), 1)
        self.assertEqual(len(self.db().sql("SELECT * FROM audit_log WHERE action='book_inspection' AND actor=?", ("api-approval:" + item["approval_id"],))), 1)

    def test_post_commit_interruption_recovers_from_core_audit_after_restart(self):
        item = self.pending()
        service = self.app.state.service
        original = service.finish

        def interrupt(db, aid, status, decision, result):
            if status == "approved":
                raise RuntimeError("SIMULATED interruption after real core commit")
            return original(db, aid, status, decision, result)

        with patch.object(service, "finish", side_effect=interrupt):
            response = self.client.post(self.url(item), headers=self.officer, json=self.body(item))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.db().sql("SELECT status FROM api_approvals")[0]["status"], "pending")
        self.assertEqual(len(self.db().sql("SELECT * FROM inspections WHERE permit_id=?", (PERMIT,))), 1)
        self.now[0] = item["expires_at"] + 1
        restarted = TestClient(self.make_app())
        self.addCleanup(restarted.close)
        response = restarted.post(self.url(item), headers=self.officer, json=self.body(item))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "approved")
        self.assertTrue(response.json()["idempotent_replay"])
        self.assertEqual(len(self.db().sql("SELECT * FROM inspections WHERE permit_id=?", (PERMIT,))), 1)

    def test_validation_errors_and_model_errors_do_not_echo_secrets(self):
        response = self.client.post(f"/sessions/{self.sid}/chat", headers=self.owner,
                                    json={"question": "book fixture", "mobile": MOBILE, "id4": ID4, "api_key": USER})
        self.assertEqual(response.status_code, 422)
        for secret in (MOBILE, ID4, USER):
            self.assertNotIn(secret, response.text)
        failed = self.client.post(f"/sessions/{self.sid}/chat", headers=self.owner, json={"question": "raise secret error"})
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn(MOBILE, failed.text)
        self.assertNotIn("sensitive", failed.text)

    def test_response_redaction_and_metadata_only_private_logs(self):
        text = f"echo id last4 {ID4} mobile {MOBILE}"
        response = self.client.post(f"/sessions/{self.sid}/chat", headers=self.owner, json={"question": text})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(ID4, response.text)
        self.assertNotIn(MOBILE, response.text)
        item = self.pending()
        self.client.post(self.url(item), headers=self.officer, json=self.body(item))
        log = (self.root / "operations.jsonl").read_text()
        for secret in (MOBILE, ID4, USER, OFFICER, self.capability, text):
            self.assertNotIn(secret, log)
        records = [json.loads(line) for line in log.splitlines()]
        self.assertIn("approval_decided", {r["event"] for r in records})
        self.assertEqual((self.root.stat().st_mode & 0o777), 0o700)
        self.assertEqual(((self.root / "operations.jsonl").stat().st_mode & 0o777), 0o600)
        # Submitted credentials are not serialized by the adapter. The shipped synthetic
        # municipal template itself intentionally contains fake identities.
        pending = json.dumps(self.db().sql("SELECT * FROM api_approvals"))
        registry = (self.root / "service.sqlite").read_bytes()
        for secret in (MOBILE, ID4, USER, OFFICER, self.capability):
            self.assertNotIn(secret, pending)
            self.assertNotIn(secret.encode(), registry)

    def test_body_question_and_extra_field_limits(self):
        self.assertEqual(self.client.post(f"/sessions/{self.sid}/chat", headers=self.owner,
                                         json={"question": "a" * 12001}).status_code, 422)
        self.assertEqual(self.client.post(f"/sessions/{self.sid}/chat", headers=self.owner,
                                         json={"question": " "}).status_code, 422)
        self.assertEqual(self.client.post(f"/sessions/{self.sid}/chat", headers=self.owner,
                                         content=b"x" * 65537).status_code, 413)
        self.assertEqual(self.client.post(f"/sessions/{self.sid}/chat", headers=self.owner,
                                         json={"question": "book fixture", "config": "A", "use_cache": True}).status_code, 422)

    def test_request_budget_stops_before_further_work_and_persists(self):
        response = self.client.post(f"/sessions/{self.sid}/chat", headers=self.owner, json={"question": "exhaust"})
        self.assertEqual(response.status_code, 429)
        usage = self.client.get(f"/sessions/{self.sid}", headers=self.owner).json()["usage"]
        self.assertEqual(usage, {"requests": 1, "charged_tokens": 100, "observed_tokens": 0})
        restarted = TestClient(self.make_app(replace(self.settings, session_tokens=60_000)))
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.post(f"/sessions/{self.sid}/chat", headers=self.owner, json={"question": "hi"}).status_code, 429)

    def test_cumulative_service_budget_cannot_be_bypassed_with_new_session(self):
        # An in-flight/crashed full reservation is durable and charged conservatively.
        with self.app.state.service.locked():
            self.app.state.service.reserve_request(self.sid)
        restarted = TestClient(self.make_app(replace(self.settings, service_tokens=60_000)))
        self.addCleanup(restarted.close)
        other, cap = self.new_session(restarted)
        response = restarted.post(f"/sessions/{other}/chat", headers={**self.user, "X-Session-Token": cap}, json={"question": "hi"})
        self.assertEqual(response.status_code, 429)
        usage = restarted.get(f"/sessions/{self.sid}", headers=self.owner).json()["usage"]
        self.assertEqual(usage["charged_tokens"], 60_000)

    def test_request_count_and_session_count_limits(self):
        with self.app.state.service.connect(self.app.state.service.registry) as con:
            con.execute("UPDATE api_sessions SET requests=? WHERE session_id=?", (50, self.sid))
        self.assertEqual(self.client.post(f"/sessions/{self.sid}/chat", headers=self.owner, json={"question": "hi"}).status_code, 429)
        limited = TestClient(self.make_app(replace(self.settings, max_sessions=1)))
        self.addCleanup(limited.close)
        self.assertEqual(limited.post("/sessions", headers=self.user).status_code, 429)

    def test_provider_and_equal_service_tokens_rejected(self):
        for user, officer in [(USER, USER), ("short", OFFICER), ("sk-or-v1-" + "x" * 32, OFFICER)]:
            with self.assertRaises(ValueError):
                A.Settings(self.root, user, officer)
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": USER}):
            with self.assertRaises(ValueError):
                A.Settings(self.root, USER, OFFICER)

    def test_budget_shape_reserves_before_provider_call(self):
        shape = A.BudgetShape(1)
        with patch.object(A.R.SafeChatShape, "step") as provider:
            with self.assertRaises(A.TokenBudgetExceeded):
                shape.step([{"role": "user", "content": "hi"}], [], "unused:free", max_tokens=10)
            provider.assert_not_called()
        self.assertEqual(shape.charged, 0)

    def test_expiry_during_policy_verification_cannot_pass_final_gate(self):
        item = self.pending()

        @contextmanager
        def expire_during_policy():
            self.now[0] = item["expires_at"]
            yield FakePublicPolicyBridge()

        self.app.state.service.bridge_factory = expire_during_policy
        response = self.client.post(self.url(item), headers=self.officer, json=self.body(item))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.db().sql("SELECT status FROM api_approvals")[0]["status"], "expired")
        self.assertEqual(self.db().sql("SELECT * FROM inspections WHERE permit_id=?", (PERMIT,)), [])

    def test_policy_outage_keeps_approval_pending_without_write(self):
        item = self.pending()

        @contextmanager
        def offline_policy():
            raise RuntimeError("OFFLINE TEST policy outage")
            yield

        self.app.state.service.bridge_factory = offline_policy
        response = self.client.post(self.url(item), headers=self.officer, json=self.body(item))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.db().sql("SELECT status FROM api_approvals")[0]["status"], "pending")
        self.assertEqual(self.db().sql("SELECT * FROM inspections WHERE permit_id=?", (PERMIT,)), [])

    def test_model_calls_are_serialized_across_app_instances(self):
        active = [0]
        peak = [0]
        counter_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def slow_fake(*args, **kwargs):
            with counter_lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            try:
                time.sleep(0.03)
                return fake_model_runner(*args, **kwargs)
            finally:
                with counter_lock:
                    active[0] -= 1

        self.app.state.service.runner = slow_fake
        second = TestClient(self.make_app(runner=slow_fake))
        self.addCleanup(second.close)

        def chat(client):
            barrier.wait()
            return client.post(f"/sessions/{self.sid}/chat", headers=self.owner, json={"question": "hi"}).status_code

        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(list(pool.map(chat, [self.client, second])), [200, 200])
        self.assertEqual(peak[0], 1)

    def test_real_cli_process_restart_and_real_mcp_approval_no_model_call(self):
        """Fixture-generated pending action survives real API process restarts; real policy MCP on approval."""
        import httpx
        import subprocess
        item = self.pending()
        env = {**os.environ, "RAFEEQ_USER_TOKEN": USER, "RAFEEQ_OFFICER_TOKEN": OFFICER,
               "RAFEEQ_API_DATA": str(self.root)}
        # No model request is made: only reads and a guarded officer decision.
        results = []
        for iteration in range(2):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            process = subprocess.Popen([sys.executable, str(Path(A.__file__).resolve()), "--port", str(port)],
                                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=20, trust_env=False) as client:
                    deadline = time.monotonic() + 15
                    while True:
                        try:
                            health = client.get("/health")
                            if health.status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        if process.poll() is not None or time.monotonic() >= deadline:
                            self.fail("Production CLI process failed to start")
                        time.sleep(0.05)
                    status = client.get(f"/sessions/{self.sid}", headers=self.owner)
                    self.assertEqual(status.status_code, 200)
                    pending = status.json()["approvals"][0]
                    self.assertEqual(pending["approval_id"], item["approval_id"])
                    if iteration == 0:
                        self.assertEqual(pending["status"], "pending")
                    else:
                        result = client.post(self.url(item), headers=self.officer, json=self.body(item))
                        self.assertEqual(result.status_code, 200, result.text)
                        self.assertEqual(result.json()["status"], "approved")
                        results.append(result.json()["booking"]["status"])
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            self.assertIsNotNone(process.poll())
        self.assertEqual(len(self.db().sql("SELECT * FROM inspections WHERE permit_id=?", (PERMIT,))), 1)
        print("\nCLI_RESTART_SMOKE process_starts=2 real_policy_mcp=true fixture_pending=true provider_calls=0 booking="
              + json.dumps(results) + " processes_stopped=true")

    def test_real_loopback_http_smoke_offline_model_only_server_stops(self):
        """Actual TCP -> Uvicorn -> FastAPI -> guarded core -> SQLite, no TestClient transport."""
        import httpx
        import uvicorn
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(self.app, access_log=False, proxy_headers=False, log_level="critical"))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), name="rafeeq-loopback-smoke")
        thread.start()
        statuses = []
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(server.started, "Loopback server did not start")
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False) as client:
                statuses.append(client.get("/health").status_code)
                statuses.append(client.get("/docs").status_code)
                statuses.append(client.post("/sessions").status_code)
                result = client.post("/sessions", headers=self.user)
                statuses.append(result.status_code)
                session = result.json()
                owner = {**self.user, "X-Session-Token": session["session_token"]}
                response = client.post(f"/sessions/{session['session_id']}/chat", headers=owner, json={"question": "book fixture"})
                statuses.append(response.status_code)
                item = response.json()["approvals"][0]
                result = client.post(self.url(item, session["session_id"]), headers=self.officer, json=self.body(item))
                statuses.append(result.status_code)
                self.assertEqual(result.json()["status"], "approved")
                state = client.get(f"/sessions/{session['session_id']}", headers=owner)
                statuses.append(state.status_code)
                self.assertEqual(state.json()["approvals"][0]["booking"], result.json()["booking"])
            self.assertEqual(statuses, [200, 200, 401, 201, 200, 200, 200])
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()
        self.assertFalse(thread.is_alive(), "Loopback server failed to stop")
        print("\nLOOPBACK_SMOKE fixture_model=true provider_calls=0 statuses=" + json.dumps(statuses) + " server_stopped=true")


if __name__ == "__main__":
    unittest.main(verbosity=2)
