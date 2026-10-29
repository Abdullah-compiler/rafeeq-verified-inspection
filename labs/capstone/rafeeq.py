# /// script
# requires-python = ">=3.12"
# dependencies = ["openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1", "mcp>=2.3"]
# ///
"""Rafeeq's scoped inspection-booking demo. All municipal data is synthetic."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Callable

LABS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LABS))
import agentkit as K
import madar

CAPSTONE = Path(__file__).resolve().parent
MAX_STEPS = 10
MAX_COST = 0.05
MAX_RESULT_CHARS = 6000
TOOLS = {"search_policies", "get_permit", "list_slots", "book_inspection"}
IDENTITY_ERROR = "Identity verification failed (POL-08). Provide the last 4 ID digits and registered mobile. No permit existence is confirmed."

DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
MOBILE_PATTERN = re.compile(r"(?<!\d)(?:(?:\+?966|00966)[\s().-]*|0[\s().-]*)?5(?:[\s().-]*\d){8}(?!\d)")
NATIONAL_ID_PATTERN = re.compile(r"(?<!\d)[12]\d{9}(?!\d)")
ID4_PATTERN = re.compile(
    r"(?:\b(?:national[\s_-]*)?id(?:entity)?(?:[\s_-]*last[\s_-]*4)?\b|"
    r"\blast[\s_-]*(?:4|four)[\s_-]*digits\b|"
    r"(?:آخر|اخر)\s*(?:أربعة|اربعة|4)\s*(?:أرقام|ارقام)|الهوية)"
    r"[^0-9\n]{0,50}([0-9]{4})(?![0-9])", re.IGNORECASE)
API_TOKEN_PATTERN = re.compile(r"sk-or-v1-[A-Za-z0-9_-]+")


def credential_spans(text: str):
    """Detect credential spans independent of whether an identity tool was called."""
    normalized = text.translate(DIGITS)  # one-to-one translation preserves original spans
    spans = []
    for pattern, group in ((MOBILE_PATTERN, 0), (NATIONAL_ID_PATTERN, 0), (ID4_PATTERN, 1), (API_TOKEN_PATTERN, 0)):
        spans.extend(match.span(group) for match in pattern.finditer(normalized))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def redact(value, secrets=()):
    """Remove identity fields, known credentials and API tokens from persisted evidence."""
    if isinstance(value, dict):
        return {str(k): ("[redacted]" if str(k).lower() in {"mobile", "phone", "id4", "national_id", "national_id_last4", "authorization", "api_key", "openrouter_api_key"}
                         else redact(v, secrets)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        text = value
        for start, end in reversed(credential_spans(value)):
            text = text[:start] + "[redacted]" + text[end:]
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                text = text.replace(secret, "[redacted]")
        return text
    return value


def trim_history(history: list, maximum: int = 18) -> list:
    """Trim complete old chat turns, never leaving an orphan tool message first."""
    if len(history) <= maximum:
        return history
    tail = history[-maximum:]
    while tail and tail[0].get("role") != "user":
        tail = tail[1:]
    return tail


class PersistentMadar(madar.Madar):
    """Reuse a session's DB; unlike the teaching class, never overwrite it on reopen."""
    def __post_init__(self):
        if self.workdir is None:
            raise ValueError("A private session workdir is required.")
        self.workdir = Path(self.workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.workdir / "madar.sqlite"
        with (self.workdir / ".init.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not self.db_path.exists():
                if not madar.DB_TEMPLATE.exists():
                    import prepare_data
                    prepare_data.build()
                shutil.copy2(madar.DB_TEMPLATE, self.db_path)
            fcntl.flock(lock, fcntl.LOCK_UN)


class BookingSession:
    """Trusted state and approval controls, separate from the model and its history."""
    def __init__(self, db: PersistentMadar, approve: Callable):
        self.db = db
        self.approve = approve
        self.verified = {}
        self.offered = {}
        self.policy_seen = False
        self.cancelled = False
        self.events = []
        self.secrets = set()
        self.turn_event_start = 0

    def begin_turn(self):
        self.cancelled = False
        self.turn_event_start = len(self.events)

    def remember_user_input(self, question: str):
        # RAM only: capture before model/tool execution, including failed/no lookup paths.
        for start, end in credential_spans(question):
            raw = question[start:end]
            normalized = raw.translate(DIGITS)
            self.secrets.update({raw, normalized})
            digits = re.sub(r"\D", "", normalized)
            if len(digits) >= 4:
                self.secrets.update({digits, digits[-4:]})

    def record(self, event: str, **data):
        cleaned = redact(data, self.secrets)
        assert isinstance(cleaned, dict)
        self.events.append({"event": event, **cleaned})

    def mark_policy(self, hits):
        if any(h.get("policy_id") == "POL-05" for h in hits if isinstance(h, dict)):
            self.policy_seen = True
        self.record("policy_retrieved", policy_ids=[h.get("policy_id") for h in hits if isinstance(h, dict)])

    def get_permit(self, permit_id: str, national_id_last4: str, mobile: str) -> dict:
        """Verify identity, then return minimal permit status and existing inspections (POL-08).

        Args:
            permit_id: Exact permit ID supplied by the user, e.g. FTR-30311.
            national_id_last4: Exactly four ID digits supplied by the user; never guess.
            mobile: The registered mobile supplied by the user; never guess.
        """
        self.secrets.update({str(national_id_last4), str(mobile)})
        self.verified.pop(permit_id, None)
        if not re.fullmatch(r"[A-Z]{3}-\d{5}", permit_id) or not re.fullmatch(r"\d{4}", national_id_last4):
            self.record("identity_failed")
            raise madar.ToolError(IDENTITY_ERROR)
        try:
            info = self.db.get_permit(permit_id, national_id_last4, mobile)
        except madar.ToolError:
            self.record("identity_failed")
            raise madar.ToolError(IDENTITY_ERROR) from None
        self.verified[permit_id] = {"id4": national_id_last4, "mobile": mobile, "district": info["district"]}
        self.record("identity_verified", permit_id=permit_id)
        return {k: info[k] for k in ("permit_id", "type", "status", "district", "inspections")}

    def list_slots(self, district: str, date_from: str, date_to: str) -> list[str]:
        """Return at most 24 real free slots for an already verified permit's district.

        Args:
            district: The exact district returned by get_permit.
            date_from: First date, YYYY-MM-DD, using the simulation clock.
            date_to: Last date, YYYY-MM-DD, at most 30 days after date_from.
        """
        if not any(v["district"] == district for v in self.verified.values()):
            raise madar.ToolError("Verify a permit in this district with get_permit first.")
        try:
            start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
            if end < start or (end - start).days > 30:
                raise ValueError
        except ValueError:
            raise madar.ToolError("Use valid YYYY-MM-DD dates with a range of 0–30 days.") from None
        slots = self.db.list_slots(district, date_from, date_to)[:24]
        self.offered.setdefault(district, set()).update(slots)
        self.record("slots_listed", district=district, count=len(slots))
        return slots

    @staticmethod
    def eligible(permit):
        allowed = {("BLD", "approved"), ("FTR", "approved"), ("FTR", "under_review"), ("EVT", "under_review")}
        if (permit["type"], permit["status"]) not in allowed:
            raise madar.ToolError("This permit is not eligible for inspection booking under POL-05; hand over to an officer.")

    def authorize_in_transaction(self, con: sqlite3.Connection, permit_id: str, credentials: dict) -> sqlite3.Row:
        current = con.execute(
            "SELECT p.*, a.national_id, a.mobile FROM permits p JOIN applicants a USING(applicant_id) WHERE p.permit_id=?",
            (permit_id,),
        ).fetchone()
        actors = [] if current is None else [current, *con.execute(
            "SELECT national_id,mobile FROM applicants WHERE agent_for=?", (current["applicant_id"],)
        ).fetchall()]
        allowed = any(str(actor["national_id"])[-4:] == credentials["id4"]
                      and madar._norm_mobile(actor["mobile"]) == madar._norm_mobile(credentials["mobile"])
                      for actor in actors)
        if not allowed:
            self.verified.pop(permit_id, None)
            self.record("identity_revoked")
            raise madar.ToolError(IDENTITY_ERROR)
        assert current is not None
        return current

    def book_inspection(self, permit_id: str, slot: str) -> dict:
        """Propose a listed slot and request human approval before atomically booking it.

        A user's message never substitutes for this separate approval gate.
        A denied action is cancelled for the rest of this user turn. No ID/mobile is needed again here.

        Args:
            permit_id: A permit verified by get_permit in this conversation.
            slot: Exact YYYY-MM-DD HH:MM value previously returned by list_slots.
        """
        if self.cancelled:
            raise madar.ToolError("The human denied booking. Do not retry until a new user message.")
        if permit_id not in self.verified:
            raise madar.ToolError("Verify identity with get_permit before booking.")
        if not self.policy_seen:
            raise madar.ToolError("Retrieve the inspection policy POL-05 with search_policies first.")
        credentials = self.verified[permit_id]
        permit = self.db._permit(permit_id)
        self.db._verify(permit, credentials["id4"], credentials["mobile"])
        self.eligible(permit)
        district = permit["district"]
        if slot not in self.offered.get(district, set()):
            raise madar.ToolError("Call list_slots and choose an exact returned slot before booking.")
        if slot < madar.add_working_days(madar.TODAY, 2).isoformat():
            raise madar.ToolError("Booking must be at least two working days ahead (POL-05).")
        existing = self.db.sql("SELECT * FROM inspections WHERE permit_id=? AND slot=? AND status='booked'", (permit_id, slot))
        if existing:
            return self.public_booking(existing[0])
        if not self.db.sql("SELECT 1 FROM slots WHERE district=? AND slot=? AND taken=0", (district, slot)):
            raise madar.ToolError("That slot is no longer available; call list_slots again.")
        kind = "foundation" if permit["type"] == "BLD" else ("health" if permit["type"] == "FTR" and permit["status"] == "approved" else "site_visit")
        proposal = {"permit_id": permit_id, "slot": slot, "district": district, "kind": kind}
        allowed = self.approve("book_inspection", dict(proposal)) is True
        self.record("approval_decision", approved=allowed, **proposal)
        if not allowed:
            self.cancelled = True
            raise madar.ToolError("The human denied booking. No inspection was booked. Do not retry this turn.")
        # A single write transaction prevents races and partial slot/inspection updates.
        con = sqlite3.connect(self.db.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN IMMEDIATE")
            current = self.authorize_in_transaction(con, permit_id, credentials)
            self.eligible(current)
            if any(current[key] != permit[key] for key in ("type", "status", "district")):
                raise madar.ToolError("Permit details changed during approval; verify and list slots again.")
            existing = con.execute("SELECT * FROM inspections WHERE permit_id=? AND slot=? AND status='booked'", (permit_id, slot)).fetchone()
            if existing:
                con.rollback()
                return self.public_booking(dict(existing))
            cursor = con.execute("UPDATE slots SET taken=1 WHERE district=? AND slot=? AND taken=0", (district, slot))
            if cursor.rowcount != 1:
                raise madar.ToolError("That slot was taken before approval completed. No booking was made.")
            number = con.execute("SELECT COALESCE(MAX(CAST(substr(inspection_id,5) AS INTEGER)),9000)+1 FROM inspections").fetchone()[0]
            iid = f"INS-{number}"
            con.execute("INSERT INTO inspections VALUES (?,?,?,?,?,?,?)", (iid, permit_id, kind, slot, district, "booked", None))
            con.execute("INSERT INTO audit_log VALUES (?,?,?,?)", (madar.NOW.isoformat(), self.db.actor, "book_inspection", json.dumps(proposal)))
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        result = {"inspection_id": iid, **proposal, "status": "booked"}
        self.record("booking_committed", **result)
        return result

    @staticmethod
    def public_booking(row):
        return {k: row[k] for k in ("inspection_id", "permit_id", "kind", "slot", "district", "status")}


def approval_cli(name: str, proposal: dict) -> bool:
    print("\nHUMAN APPROVAL REQUIRED / مطلوب موافقة بشرية")
    print(json.dumps(proposal, ensure_ascii=False, indent=2))
    try:
        return input("Approve this exact booking? Type y to approve; anything else rejects [y/N]: ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False


def ground_booking_reply(question: str, session: BookingSession, model_final: str) -> str:
    """Use verified business outcomes for confirmations, never generated promises."""
    arabic = bool(re.search(r"[\u0600-\u06ff]", question))
    events = session.events[session.turn_event_start:]
    committed = [e for e in events if e["event"] == "booking_committed"]
    if committed:
        event = committed[-1]
        rows = session.db.sql("SELECT * FROM inspections WHERE inspection_id=? AND status='booked'", (event["inspection_id"],))
        if not rows or any(rows[0][k] != event[k] for k in ("permit_id", "slot", "district", "kind")):
            raise RuntimeError("Booking outcome could not be verified; no success confirmation will be shown.")
        row = rows[0]
        if arabic:
            kind = {"health": "الفحص الصحي", "foundation": "فحص الأساسات", "site_visit": "الزيارة الميدانية"}[row["kind"]]
            return (f"تم حجز {kind} بعد الموافقة البشرية.\nرقم الفحص: {row['inspection_id']}\n"
                    f"التصريح: {row['permit_id']}\nالموعد: {row['slot']}\nالحي: {row['district']}\n"
                    "تم التحقق من تسجيل الحجز في قاعدة البيانات. (POL-05)")
        return (f"Booked {row['kind']} inspection {row['inspection_id']} after human approval.\n"
                f"Permit: {row['permit_id']}\nSlot: {row['slot']}\nDistrict: {row['district']}\n"
                "The booking was verified in the database. (POL-05)")
    if any(e["event"] == "approval_decision" and e["approved"] is False for e in events):
        return ("لم يتم حجز أي فحص لأن طلب الموافقة البشرية رُفض. لم يُحجز الموعد بواسطة هذا الطلب."
                if arabic else "No inspection was booked because the human rejected the approval request. This request did not reserve the slot.")
    return model_final


class SafeChatShape(K.ChatShape):
    """Reject incomplete generations before any tool calls can execute."""
    def __init__(self):
        super().__init__()
        self.models = []

    def step(self, *args, **kwargs):
        turn = super().step(*args, **kwargs)
        choice = turn.raw.choices[0]
        if choice.finish_reason not in ("stop", "tool_calls"):
            raise RuntimeError("Model output was incomplete; no tools from that response were executed.")
        self.models.append(turn.raw.model)
        return turn


class PolicyBridge(K.MCPBridge):
    def _stop_loop(self):
        super()._stop_loop()
        if not self.thread.is_alive():
            self.loop.close()


def policy_connection():
    from mcp import StdioServerParameters
    return PolicyBridge(StdioServerParameters(
        command=sys.executable, args=[str(CAPSTONE / "policies_mcp.py")],
        env={"PATH": os.environ.get("PATH", ""), "PYTHONUTF8": "1"},
    ))


def make_tools(session: BookingSession, bridge):
    names = {t.name for t in bridge.list_tools()}
    if names != {"search_policies"}:
        raise RuntimeError("Refusing an MCP server with capabilities beyond public policy search.")

    def search_policies(query: str, k: int = 3) -> str:
        """Retrieve public municipal rules over MCP. Read POL-05 before booking and cite policy IDs.

        Args:
            query: A policy topic or ID, e.g. POL-05 inspection booking; never include credentials.
            k: Maximum passages, 1 to 5.
        """
        digits = re.sub(r"\D", "", query.translate(DIGITS))
        if (credential_spans(query) or any(secret and secret in query for secret in session.secrets)
                or any(secret.isdigit() and len(secret) >= 4 and secret in digits for secret in session.secrets)):
            raise madar.ToolError("Policy queries must not contain identity credentials.")
        result = bridge.call("search_policies", {"query": query, "k": k})
        text = K.mcp_result_text(result)
        if result.is_error:
            raise madar.ToolError("Policy retrieval failed. Do not proceed to booking.")
        hits = json.loads(text)
        session.mark_policy(hits)
        return text

    return [search_policies, session.get_permit, session.list_slots, session.book_inspection]


def run_question(question: str, session: BookingSession, bridge, history=None, shape=None, *, config="B", use_cache=False):
    if len(question) > 12000:
        raise ValueError("Request exceeds the 12,000-character context limit.")
    if config not in {"A", "B"}:
        raise ValueError("Configuration must be A or B.")
    if K.PROVIDER != "openrouter" or not K.MODEL.endswith(":free") or any(not m.endswith(":free") for m in K.FALLBACKS):
        raise RuntimeError("This demo is restricted to OpenRouter free model IDs, including fallbacks.")
    session.begin_turn()
    session.remember_user_input(question)
    adapter = shape or SafeChatShape()
    if use_cache:
        # Opt-in for synthetic paired evaluations only; never included in submission.
        K.CACHE.mkdir(parents=True, exist_ok=True)
        K.CACHE.chmod(0o700)
    # Reuse the shipped context unchanged; no new protected AGENTS.md is created.
    system = madar.SYSTEM_PROMPT + "\n\n" + (LABS / "data" / "AGENTS.md").read_text(encoding="utf-8")
    start_usage = {key: getattr(K.LEDGER, key) for key in ("calls", "cache_hits", "input_tokens", "output_tokens")}
    run = K.run_agent(
        question, make_tools(session, bridge), system=system, history=trim_history(history or []),
        shape=adapter, max_steps=MAX_STEPS, max_cost=MAX_COST,
        permission=lambda name, args: "allow" if name in TOOLS else "deny",
        after_tool=lambda name, args, result: result[:MAX_RESULT_CHARS] if len(result) > MAX_RESULT_CHARS else None,
        verbose=False, use_cache=use_cache, max_tokens=4096,
    )
    setattr(run, "model_final", run.final)
    if config == "B":
        run.final = ground_booking_reply(question, session, run.final)
    setattr(run, "configuration", config)
    setattr(run, "usage", {key: getattr(K.LEDGER, key) - value for key, value in start_usage.items()})
    if run.state and run.state[-1].get("role") == "assistant":
        run.state[-1]["content"] = run.final
    return run, adapter


def save_report(path: Path, run, session: BookingSession, adapter):
    report = {
        "schema": "rafeeq.booking-run.v1", "data": "synthetic", "simulation_date": str(madar.TODAY),
        "configuration": getattr(run, "configuration", "B"),
        "stopped": run.stopped, "steps": run.steps, "reported_cost_usd": run.cost,
        "models": adapter.models, "final": redact(run.final, session.secrets),
        "model_final": redact(getattr(run, "model_final", run.final), session.secrets),
        "response_grounded_in_outcome": run.final != getattr(run, "model_final", run.final),
        "usage": getattr(run, "usage", {}),
        "tool_calls": [{"name": n, "arguments": redact(a, session.secrets)} for n, a in run.tool_calls],
        "events": redact(session.events[session.turn_event_start:], session.secrets),
        "trace": [{"kind": e.kind, "text": redact(e.text, session.secrets)} for e in run.trace if e.kind != "user"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("ask", "chat"))
    parser.add_argument("question", nargs="?")
    parser.add_argument("--session", default="demo")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", args.session):
        parser.error("Session must be 1–40 letters, digits, underscores or hyphens.")
    if args.command == "ask" and not args.question:
        parser.error("ask requires a question")
    folder = LABS / "artifacts" / "rafeeq" / args.session
    session = BookingSession(PersistentMadar(folder), approval_cli)
    history = []
    print("Rafeeq / رفيق — synthetic municipal demo; simulation date: 1 November 2026")
    with policy_connection() as bridge:
        while True:
            try:
                question = args.question if args.command == "ask" else input("\nYou / أنت (empty to exit): ").strip()
                if not question:
                    break
                run, adapter = run_question(question, session, bridge, history)
                print("\nRafeeq:", redact(run.final or f"Stopped: {run.stopped}", session.secrets))
                print(f"[steps={run.steps}, reported cost=${run.cost:.6f}]")
                history = run.state[1:]
                save_report(args.report or folder / "last-run.json", run, session, adapter)
                if args.command == "ask":
                    return 0 if run.stopped == "done" else 1
            except (EOFError, KeyboardInterrupt):
                break
            except Exception as exc:
                print("Run failed:", redact(str(exc), session.secrets), file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
