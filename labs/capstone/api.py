# /// script
# requires-python = ">=3.12"
# dependencies = ["fastapi>=0.135,<1", "uvicorn>=0.42,<1", "openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1", "mcp>=2.3"]
# ///
"""Original Rafeeq HTTP adapter; isolated synthetic demo databases, not a municipality backend."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, model_validator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rafeeq as R

# agentkit's ledger/cache configuration is process-global, not thread-local.
_MODEL_LOCK = threading.RLock()
_ID = re.compile(r"[0-9a-f]{32}\Z")
BEARER = HTTPBearer(auto_error=False)


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    user_token: str
    officer_token: str
    approval_ttl: int = 600
    request_tokens: int = 60_000
    session_tokens: int = 300_000
    service_tokens: int = 1_000_000
    max_requests: int = 50
    max_sessions: int = 100
    max_body_bytes: int = 65_536

    def __post_init__(self):
        provider_key = os.environ.get("OPENROUTER_API_KEY", "")
        for token in (self.user_token, self.officer_token):
            if (len(token) < 32 or token.startswith("sk-or-") or token == provider_key
                    or not token.isascii() or any(c.isspace() for c in token)):
                raise ValueError("Use separate ASCII service tokens of at least 32 characters, never provider keys.")
        if hmac.compare_digest(self.user_token, self.officer_token):
            raise ValueError("User and officer service tokens must differ.")
        for name in ("approval_ttl", "request_tokens", "session_tokens", "service_tokens", "max_requests", "max_sessions", "max_body_bytes"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive.")
        if self.request_tokens > min(self.session_tokens, self.service_tokens):
            raise ValueError("Cumulative budgets must accommodate one request reservation.")

    @classmethod
    def from_env(cls):
        return cls(
            Path(os.environ.get("RAFEEQ_API_DATA", str(R.LABS / "artifacts" / "rafeeq-api"))),
            os.environ.get("RAFEEQ_USER_TOKEN", ""), os.environ.get("RAFEEQ_OFFICER_TOKEN", ""),
        )


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChatRequest(StrictModel):
    question: str = Field(min_length=1, max_length=12_000)

    @model_validator(mode="after")
    def not_blank(self):
        if not self.question.strip():
            raise ValueError("Question cannot be blank.")
        return self


class Proposal(StrictModel):
    permit_id: str = Field(pattern=r"^[A-Z]{3}-[0-9]{5}$")
    slot: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}$")
    district: str = Field(min_length=1, max_length=80)
    kind: Literal["foundation", "health", "site_visit"]


class DecisionRequest(StrictModel):
    decision: Literal["approve", "reject"]
    proposal: Proposal
    national_id_last4: str | None = Field(default=None, pattern=r"^[0-9]{4}$")
    mobile: str | None = Field(default=None, min_length=9, max_length=24, pattern=r"^[+0-9 ().-]+$")

    @model_validator(mode="after")
    def fresh_identity(self):
        if self.decision == "approve" and (self.national_id_last4 is None or self.mobile is None):
            raise ValueError("Approval requires fresh synthetic identity credentials.")
        return self


class TokenBudgetExceeded(Exception):
    pass


class BudgetShape(R.SafeChatShape):
    """Preflight each provider call before tools execute; failed calls still spend their reservation."""
    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit
        self.charged = 0
        self.actual = 0

    def reserve(self, amount: int):
        if self.charged + amount > self.limit:
            raise TokenBudgetExceeded
        self.charged += amount

    def step(self, state, tools, model, use_cache=False, **kwargs):
        if use_cache:
            raise RuntimeError("Service caching is disabled.")
        # UTF-8 bytes + schemas + framing is intentionally conservative, not a tokenizer claim.
        prompt = {"messages": state, "tools": [R.K.tool_schema(t) for t in tools]}
        allowance = len(canonical(prompt).encode()) + 2048 + int(kwargs.get("max_tokens", 4096))
        self.reserve(allowance)
        before = R.K.LEDGER.input_tokens + R.K.LEDGER.output_tokens
        try:
            turn = super().step(state, tools, model, use_cache=False, **kwargs)
        finally:
            actual = max(0, R.K.LEDGER.input_tokens + R.K.LEDGER.output_tokens - before)
            self.actual += actual
            if actual > allowance:
                self.charged += actual - allowance
        if self.charged > self.limit:
            # Never execute tools from an over-budget generation.
            raise TokenBudgetExceeded
        return turn


class Service:
    def __init__(self, settings: Settings, runner, bridge_factory, clock):
        self.settings, self.runner, self.bridge_factory, self.clock = settings, runner, bridge_factory, clock
        self.root = settings.data_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.registry = self.root / "service.sqlite"
        self.log_path = self.root / "operations.jsonl"
        with self.locked(), self.connect(self.registry) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS api_sessions (
                session_id TEXT PRIMARY KEY, capability_hash TEXT NOT NULL,
                created REAL NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
                charged_tokens INTEGER NOT NULL DEFAULT 0, observed_tokens INTEGER NOT NULL DEFAULT 0)""")
        self.registry.chmod(0o600)

    @contextmanager
    def locked(self):
        # Also coordinates separate service instances/workers sharing this data directory.
        with _MODEL_LOCK, (self.root / ".service.lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @contextmanager
    def connect(self, path):
        con = sqlite3.connect(path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def log(self, event: str, **fields):
        # No free-form request/response/error text, headers, URL query, credentials or tokens.
        safe = {k: v for k, v in fields.items() if k in {
            "session_id", "approval_id", "status", "charged_tokens", "observed_tokens", "elapsed_ms", "http_status"
        }}
        line = canonical(R.redact({"time": self.clock(), "event": event, **safe})) + "\n"
        fd = os.open(self.log_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)

    def database(self, session_id: str):
        if not _ID.fullmatch(session_id):
            raise HTTPException(404, "Session not found.")
        with self.connect(self.registry) as con:
            if not con.execute("SELECT 1 FROM api_sessions WHERE session_id=?", (session_id,)).fetchone():
                raise HTTPException(404, "Session not found.")
        db = R.PersistentMadar(self.root / session_id)
        (self.root / session_id).chmod(0o700)
        db.db_path.chmod(0o600)
        with self.connect(db.db_path) as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS api_approvals (
                    approval_id TEXT PRIMARY KEY, proposal TEXT NOT NULL, proposal_hash TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','approved','rejected','expired','failed')),
                    decision TEXT, result TEXT);
                CREATE TRIGGER IF NOT EXISTS api_immutable_proposal
                BEFORE UPDATE OF approval_id,proposal,proposal_hash,created,expires ON api_approvals
                BEGIN SELECT RAISE(ABORT, 'Approval proposals are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS api_terminal_decision
                BEFORE UPDATE OF status,decision,result ON api_approvals WHEN OLD.status != 'pending'
                BEGIN SELECT RAISE(ABORT, 'Approval decisions are terminal'); END;
            """)
        return db

    def owner(self, session_id: str, capability: str | None):
        # Uniform not-found result prevents session existence probing.
        if not _ID.fullmatch(session_id) or capability is None:
            raise HTTPException(404, "Session not found.")
        with self.connect(self.registry) as con:
            row = con.execute("SELECT capability_hash FROM api_sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None or not hmac.compare_digest(row[0], digest(capability)):
            raise HTTPException(404, "Session not found.")

    def create_session(self):
        with self.connect(self.registry) as con:
            if con.execute("SELECT count(*) FROM api_sessions").fetchone()[0] >= self.settings.max_sessions:
                raise HTTPException(429, "Session limit reached.")
            sid, capability = secrets.token_hex(16), secrets.token_urlsafe(32)
            con.execute("INSERT INTO api_sessions(session_id,capability_hash,created) VALUES (?,?,?)",
                        (sid, digest(capability), self.clock()))
        self.database(sid)
        self.log("session_created", session_id=sid)
        return {"session_id": sid, "session_token": capability, "data": "synthetic", "conversation_history": "not persisted"}

    def reserve_request(self, sid):
        with self.connect(self.registry) as con:
            row = con.execute("SELECT * FROM api_sessions WHERE session_id=?", (sid,)).fetchone()
            total = con.execute("SELECT COALESCE(sum(charged_tokens),0) FROM api_sessions").fetchone()[0]
            s = self.settings
            if (row["requests"] >= s.max_requests or row["charged_tokens"] + s.request_tokens > s.session_tokens
                    or total + s.request_tokens > s.service_tokens):
                raise HTTPException(429, "Durable request/token budget exhausted.")
            # Crash-safe: an interrupted request retains its whole reservation, never gets free retries.
            con.execute("UPDATE api_sessions SET requests=requests+1, charged_tokens=charged_tokens+? WHERE session_id=?",
                        (s.request_tokens, sid))

    def settle_request(self, sid, shape):
        with self.connect(self.registry) as con:
            con.execute("UPDATE api_sessions SET charged_tokens=charged_tokens+?, observed_tokens=observed_tokens+? WHERE session_id=?",
                        (shape.charged - self.settings.request_tokens, shape.actual, sid))

    def queue(self, db, proposal):
        validated = Proposal.model_validate(proposal).model_dump()
        text = canonical(validated)
        aid = secrets.token_hex(16)
        now = self.clock()
        with self.connect(db.db_path) as con:
            con.execute("INSERT INTO api_approvals(approval_id,proposal,proposal_hash,created,expires) VALUES (?,?,?,?,?)",
                        (aid, text, digest(text), now, now + self.settings.approval_ttl))
        return aid

    def read_approval(self, db, aid):
        if not _ID.fullmatch(aid):
            raise HTTPException(404, "Approval not found in this session.")
        with self.connect(db.db_path) as con:
            row = con.execute("SELECT * FROM api_approvals WHERE approval_id=?", (aid,)).fetchone()
        if row is None:
            raise HTTPException(404, "Approval not found in this session.")
        if digest(row["proposal"]) != row["proposal_hash"]:
            raise HTTPException(409, "Proposal integrity check failed.")
        if row["status"] == "pending":
            # The core writes this actor in the SAME booking transaction. Recover a crash
            # after its commit but before the queue status write without booking twice.
            recovered = self.recover_booking(db, aid, json.loads(row["proposal"]))
            if recovered:
                self.finish(db, aid, "approved", "approve", recovered)
                return self.read_approval(db, aid)
            if self.clock() >= row["expires"]:
                self.finish(db, aid, "expired", None, None)
                return self.read_approval(db, aid)
        return dict(row)

    def recover_booking(self, db, aid, proposal):
        audits = db.sql("SELECT detail FROM audit_log WHERE actor=? AND action='book_inspection'", ("api-approval:" + aid,))
        if not any(json.loads(a["detail"]) == proposal for a in audits):
            return None
        rows = db.sql("SELECT * FROM inspections WHERE permit_id=? AND slot=? AND district=? AND kind=? AND status='booked'",
                      tuple(proposal[k] for k in ("permit_id", "slot", "district", "kind")))
        if len(rows) != 1:
            raise HTTPException(409, "Committed approval cannot be reconciled; manual review required.")
        return R.BookingSession.public_booking(rows[0])

    def finish(self, db, aid, status, decision, result):
        with self.connect(db.db_path) as con:
            cursor = con.execute("UPDATE api_approvals SET status=?,decision=?,result=? WHERE approval_id=? AND status='pending'",
                                 (status, decision, canonical(result) if result else None, aid))
            if cursor.rowcount != 1:
                raise HTTPException(409, "Approval already consumed.")

    @staticmethod
    def public(row):
        return {"approval_id": row["approval_id"], "proposal": json.loads(row["proposal"]),
                "proposal_hash": row["proposal_hash"], "created_at": row["created"], "expires_at": row["expires"],
                "status": row["status"], "decision": row["decision"],
                "booking": json.loads(row["result"]) if row["result"] else None}

    def approvals(self, db):
        ids = db.sql("SELECT approval_id FROM api_approvals ORDER BY created,approval_id")
        return [self.public(self.read_approval(db, row["approval_id"])) for row in ids]

    def chat(self, sid, question):
        db = self.database(sid)
        self.reserve_request(sid)
        shape = BudgetShape(self.settings.request_tokens)
        queued = []

        def enqueue(name, proposal):
            if name != "book_inspection":
                return False
            aid = self.queue(db, proposal)
            queued.append(aid)
            self.log("approval_queued", session_id=sid, approval_id=aid, status="pending")
            return False  # Core cannot write while the HTTP approval is pending.

        session = R.BookingSession(db, enqueue)
        session.remember_user_input(question)
        start = time.monotonic()
        failure = None
        run = None
        try:
            with self.bridge_factory() as bridge:
                run, _ = self.runner(question, session, bridge, history=None, shape=shape, config="B", use_cache=False)
        except TokenBudgetExceeded:
            failure = "token_budget"
        except Exception:
            # Never expose raw exception text: SDK/tool errors can contain request data.
            failure = "model_unavailable"
        finally:
            self.settle_request(sid, shape)
            self.log("chat_finished", session_id=sid, status=failure or "complete",
                     charged_tokens=shape.charged, observed_tokens=shape.actual,
                     elapsed_ms=int((time.monotonic() - start) * 1000))
        pending = [self.public(self.read_approval(db, aid)) for aid in queued]
        if pending:
            arabic = bool(re.search(r"[\u0600-\u06ff]", question))
            reply = ("الطلب بانتظار موافقة الموظف على المقترح المحدد. لم يتم حجز أي موعد."
                     if arabic else "The exact proposal is awaiting officer approval. No inspection has been booked by this request.")
            state = "pending_approval"
        elif failure:
            raise HTTPException(429 if failure == "token_budget" else 503,
                                "Request token budget reached." if failure == "token_budget" else "Model request failed; no booking was authorized.")
        else:
            assert run is not None
            reply = R.redact(run.final or "No answer was produced.", session.secrets)
            state = "complete" if run.stopped == "done" else "stopped"
        return {"session_id": sid, "state": state, "reply": reply, "approvals": pending,
                "usage": {"charged_tokens": shape.charged, "observed_tokens": shape.actual}, "model_error": failure}

    def decide(self, sid, aid, body):
        db = self.database(sid)
        row = self.read_approval(db, aid)
        proposal = body.proposal.model_dump()
        if canonical(proposal) != row["proposal"]:
            raise HTTPException(409, "Decision must match the immutable exact proposal.")
        if row["status"] != "pending":
            if row["decision"] == body.decision and row["status"] in {"approved", "rejected"}:
                return {**self.public(row), "idempotent_replay": True}
            raise HTTPException(409, "Approval is expired, failed or already consumed by a different decision.")
        if body.decision == "reject":
            self.finish(db, aid, "rejected", "reject", None)
        else:
            db.actor = "api-approval:" + aid
            session = R.BookingSession(db, lambda name, current: (
                name == "book_inspection" and current == proposal and self.clock() < row["expires"]
            ))
            # Credentials exist only in this local object, never in pending records or logs.
            try:
                session.get_permit(proposal["permit_id"], body.national_id_last4, body.mobile)
            except R.madar.ToolError:
                self.log("decision_identity_failed", session_id=sid, approval_id=aid, status="denied")
                raise HTTPException(403, "Fresh synthetic identity verification failed.") from None
            try:
                with self.bridge_factory() as bridge:
                    R.make_tools(session, bridge)[0]("POL-05", 3)
            except Exception:
                raise HTTPException(503, "Policy verification unavailable; approval remains pending.") from None
            try:
                session.list_slots(proposal["district"], proposal["slot"][:10], proposal["slot"][:10])
                result = session.book_inspection(proposal["permit_id"], proposal["slot"])
                # Do not attribute an unrelated pre-existing booking to this approval.
                if self.recover_booking(db, aid, proposal) != result:
                    raise R.madar.ToolError("No matching approval-bound transaction.")
            except R.madar.ToolError:
                status = "expired" if self.clock() >= row["expires"] else "failed"
                self.finish(db, aid, status, "approve", None)
                self.log("decision_invalidated", session_id=sid, approval_id=aid, status=status)
                raise HTTPException(409, "Proposal is no longer valid; submit a fresh booking request.") from None
            self.finish(db, aid, "approved", "approve", result)
        outcome = self.public(self.read_approval(db, aid))
        self.log("approval_decided", session_id=sid, approval_id=aid, status=outcome["status"])
        return {**outcome, "idempotent_replay": False}


def create_app(settings: Settings | None = None, *, runner=None, bridge_factory=None, clock=time.time) -> FastAPI:
    """Dependency injection is for labelled offline protocol tests, never a production fake mode."""
    settings = settings or Settings.from_env()
    service = Service(settings, runner or R.run_question, bridge_factory or R.policy_connection, clock)
    app = FastAPI(title="Rafeeq synthetic booking API", version="1.0.0",
                  description="Isolated synthetic sessions; officer decisions authorize exact expiring proposals.")
    app.state.service = service

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default errors include rejected raw inputs (including identity secrets).
        return JSONResponse(status_code=422, content={"detail": "Invalid request schema; consult /docs. Input values are not echoed."})

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return JSONResponse(status_code=500, content={"detail": "Internal service error; retry safe reads or inspect the approval status."})

    @app.middleware("http")
    async def bounded_body(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH"}:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > settings.max_body_bytes:
                    return JSONResponse(status_code=413, content={"detail": "Request body too large."})
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Route templates only: never log raw URL/query/header/request bodies.
        if response.status_code >= 400:
            service.log("http_denied", http_status=response.status_code)
        return response

    def role(expected):
        def require(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(BEARER)]):
            if credentials is None or credentials.scheme.lower() != "bearer":
                raise HTTPException(401, "Service bearer token required.", headers={"WWW-Authenticate": "Bearer"})
            supplied = credentials.credentials
            correct = settings.user_token if expected == "user" else settings.officer_token
            if not hmac.compare_digest(supplied.encode(), correct.encode()):
                raise HTTPException(403, "Service token is not authorized for this role.")
        return require

    user = Depends(role("user"))
    officer = Depends(role("officer"))

    @app.get("/health")
    def health():
        return {"status": "ok", "data": "synthetic", "configuration": "B", "cache": False}

    @app.post("/sessions", status_code=201, dependencies=[user])
    def create_session():
        with service.locked():
            return service.create_session()

    @app.get("/sessions/{session_id}", dependencies=[user])
    def session_status(session_id: str, x_session_token: Annotated[str | None, Header()] = None):
        with service.locked():
            service.owner(session_id, x_session_token)
            with service.connect(service.registry) as con:
                row = con.execute("SELECT requests,charged_tokens,observed_tokens FROM api_sessions WHERE session_id=?", (session_id,)).fetchone()
            return {"session_id": session_id, "usage": dict(row), "approvals": service.approvals(service.database(session_id))}

    @app.post("/sessions/{session_id}/chat", dependencies=[user])
    def chat(session_id: str, body: ChatRequest, x_session_token: Annotated[str | None, Header()] = None):
        with service.locked():
            service.owner(session_id, x_session_token)
            return service.chat(session_id, body.question)

    @app.get("/officer/approvals", dependencies=[officer])
    def officer_queue():
        with service.locked():
            with service.connect(service.registry) as con:
                ids = [r[0] for r in con.execute("SELECT session_id FROM api_sessions ORDER BY created,session_id")]
            items = [{"session_id": sid, **item} for sid in ids for item in service.approvals(service.database(sid)) if item["status"] == "pending"]
            return {"items": items, "total": len(items)}

    @app.get("/officer/sessions/{session_id}/approvals/{approval_id}", dependencies=[officer])
    def approval_status(session_id: str, approval_id: str):
        with service.locked():
            db = service.database(session_id)
            return service.public(service.read_approval(db, approval_id))

    @app.post("/officer/sessions/{session_id}/approvals/{approval_id}/decision", dependencies=[officer])
    def decision(session_id: str, approval_id: str, body: DecisionRequest):
        with service.locked():
            return service.decide(session_id, approval_id, body)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "::1"], help="Loopback only; deployment beyond this demo needs a security review.")
    parser.add_argument("--port", type=int, default=8793)
    args = parser.parse_args()
    import uvicorn
    # No URL/query access logs and no proxy-derived identity; secrets never go in URLs.
    uvicorn.run(create_app(), host=args.host, port=args.port, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
