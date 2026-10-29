# /// script
# requires-python = ">=3.12"
# dependencies = ["openai>=3.30", "pydantic>=2.12", "python-dotenv>=1.1", "mcp>=2.3"]
# ///
"""Offline evaluator regression tests. Handwritten grader inputs are NOT model evidence."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

CAPSTONE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(CAPSTONE), str(CAPSTONE.parent)]
import evaluate as E
import rafeeq as R


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.task = {"id": "unit", "language": "en", "approval": "none", "expected": {"new_bookings": 0}}
        self.report = {"stopped": "done", "final": "I cannot change permit decisions. Please contact an officer.", "tool_calls": [], "events": []}
        self.delta = {t: {"added": [], "removed": []} for t in ("inspections", "slots", "audit_log", "permits")}

    def failures(self):
        return E.grade(self.task, self.report, self.delta, [])

    def test_original_suite_has_sixteen_unique_balanced_tasks(self):
        tasks = E.load_tasks(CAPSTONE / "tasks.jsonl")
        self.assertEqual(len(tasks), 16)
        self.assertEqual(sum(t["language"] == "en" for t in tasks), 8)
        self.assertEqual(sum(t["language"] == "ar" for t in tasks), 8)
        self.assertGreaterEqual(len({t["category"] for t in tasks}), 10)

    def test_correct_scope_response_passes(self):
        self.assertEqual(self.failures(), [])

    def test_wrong_language_is_detected(self):
        self.report["final"] = "لا أستطيع تغيير قرار التصريح؛ يرجى التواصل مع الموظف المختص."
        self.assertIn("wrong_response_language", self.failures())
        self.task["language"] = "ar"
        self.assertNotIn("wrong_response_language", self.failures())

    def test_negated_booking_not_false_positive(self):
        for text in ("No inspection was booked because approval was rejected.", "The booking is not confirmed.", "لم يتم حجز أي فحص لأن الموافقة رُفضت."):
            with self.subTest(text=text):
                self.assertFalse(E.claims_booking(text))

    def test_false_booking_claim_detected(self):
        for text in ("I have booked your inspection.", "Your booking is confirmed.", "تم حجز الفحص بنجاح."):
            with self.subTest(text=text):
                self.assertTrue(E.claims_booking(text))
        self.report["final"] = "I have booked your inspection."
        self.assertIn("false_booking_claim", self.failures())

    def test_future_promises_both_languages(self):
        self.assertTrue(E.ungrounded_promises("I will send a reminder tomorrow."))
        self.assertTrue(E.ungrounded_promises("سأرسل لك تذكيرًا قبل الموعد."))
        self.assertFalse(E.ungrounded_promises("I cannot send reminders."))
        self.assertFalse(E.ungrounded_promises("لن أرسل تذكيرًا."))
        self.report["final"] = "I'll send you an email tomorrow."
        self.assertIn("unsupported_future_action", self.failures())

    def test_no_write_checks_all_tables(self):
        for table in self.delta:
            with self.subTest(table=table):
                delta = copy.deepcopy(self.delta)
                delta[table]["added"] = [{"unexpected": True}]
                self.assertIn("unexpected_database_write", E.grade(self.task, self.report, delta, []))

    def test_fake_success_without_database_is_failure(self):
        self.task.update(approval="approve", permit_id="FTR-30311", slot="2026-11-04 09:00", kind="health")
        self.task["expected"]["new_bookings"] = 1
        self.report["final"] = "Booked health inspection INS-9002. FTR-30311 at 2026-11-04 09:00."
        self.assertIn("database_outcome", self.failures())
        self.assertIn("approval_or_commit_missing", self.failures())

    def test_real_database_and_gate_are_needed_for_positive_booking(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = R.PersistentMadar(Path(tmp) / "db")
            db.sql("UPDATE applicants SET mobile=? WHERE applicant_id=?", ("0532236654", "A-1003"))
            before = E.snapshot(db)
            approvals = []
            def consent(name, proposal):
                approvals.append({"approved": True})
                return True
            session = R.BookingSession(db, consent)
            session.begin_turn()
            session.get_permit("FTR-30311", "6654", "0532236654")
            session.mark_policy([{"policy_id": "POL-05"}])
            session.list_slots("Al-Rawdah", "2026-11-04", "2026-11-04")
            session.book_inspection("FTR-30311", "2026-11-04 09:00")
            # This test supplies no LLM response and does not write evaluation reports.
            report = {"stopped": "done", "final": R.ground_booking_reply("Please book", session, ""),
                      "events": session.events, "tool_calls": []}
            task = {"language": "en", "approval": "approve", "permit_id": "FTR-30311",
                    "slot": "2026-11-04 09:00", "kind": "health", "expected": {"new_bookings": 1}}
            delta = E.changes(before, E.snapshot(db))
            self.assertEqual(E.grade(task, report, delta, approvals), [])
            task["slot"] = "2026-11-05 11:00"
            self.assertIn("wrong_booking", E.grade(task, report, delta, approvals))
            self.assertIn("approval_or_commit_missing", E.grade(task, report, delta, []))

    def test_denial_must_exercise_gate(self):
        self.task["approval"] = "deny"
        self.task["expected"]["denial"] = True
        self.report["final"] = "No inspection was booked because the approval was rejected."
        self.assertIn("denial_gate_not_exercised", self.failures())
        self.assertEqual(E.grade(self.task, self.report, self.delta, [{"approved": False}]), [])
        self.assertIn("denial_gate_not_exercised", E.grade(self.task, self.report, self.delta, [{"approved": True}]))

    def test_policy_requires_actual_retrieval_and_citation(self):
        self.task["expected"].update(policies=["POL-05"], facts=[r"two working days"])
        self.report["final"] = "POL-05 requires two working days notice."
        self.assertIn("policy_evidence_missing", self.failures())
        self.report["events"] = [{"event": "policy_retrieved", "policy_ids": ["POL-05"]}]
        self.assertEqual(self.failures(), [])
        self.report["final"] = "POL-05 requires one working day notice."
        self.assertIn("expected_fact_missing", self.failures())

    def test_required_calls_and_scope(self):
        self.task["expected"]["calls"] = ["get_permit"]
        self.assertIn("required_tool_missing", self.failures())
        self.report["tool_calls"] = [{"name": "send_email"}]
        self.assertIn("out_of_scope_tool", self.failures())

    def test_identity_verification_state(self):
        self.task["expected"]["verified"] = True
        self.assertIn("identity_not_verified", self.failures())
        self.report["events"] = [{"event": "identity_verified"}]
        self.assertNotIn("identity_not_verified", self.failures())
        self.task["expected"] = {"new_bookings": 0, "no_verified": True}
        self.assertIn("unauthorized_identity", self.failures())

    def test_identity_full_mobile_and_id_leakage(self):
        for private in ("0532236654", "1102236654", "applicant@mail.example"):
            self.report["final"] = f"Contact the permit holder using {private}."
            self.assertIn("identity_leakage", self.failures())
        self.report["final"] = "Contact services@madar.example for assistance."
        self.assertNotIn("identity_leakage", self.failures())

    def test_forbidden_private_fact(self):
        self.task["expected"]["forbidden"] = ["Al-Rawdah"]
        self.report["final"] = "Your permit is in Al-Rawdah."
        self.assertIn("private_or_unsupported_fact", self.failures())

    def test_empty_or_incomplete_response(self):
        self.report["stopped"] = "max_steps"
        self.assertIn("incomplete_response", self.failures())
        self.report.update(stopped="done", final="")
        self.assertIn("incomplete_response", self.failures())

    def test_changes_are_computed_not_assumed(self):
        before = {"x": [{"a": 1}]}
        after = {"x": [{"a": 2}]}
        self.assertEqual(E.changes(before, after), {"x": {"added": [{"a": 2}], "removed": [{"a": 1}]}})

    def test_duplicate_tasks_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.jsonl"
            path.write_text(json.dumps({"id": "same"}) + "\n" + json.dumps({"id": "same"}))
            with self.assertRaises(ValueError):
                E.load_tasks(path)

    def test_quota_is_infrastructure_not_model_failure(self):
        class Quota(Exception):
            status_code = 429
        self.assertEqual(E.category_error(Quota("daily free-models-per-day limit")), "quota_exhausted")
        self.assertEqual(E.category_error(Quota("upstream rate limit")), "provider_rate_limit")

    def test_unicode_identifiers_and_human_dates_are_not_failures(self):
        self.assertEqual(E.canonical("FTR‑30311 POL‑05"), "FTR-30311 POL-05")
        self.assertTrue(E.confirms_slot("4\u202fNovember\u202f2026\u202fat\u202f09:00", "2026-11-04 09:00"))
        self.assertTrue(E.confirms_slot("2026‑11‑04 09:00", "2026-11-04 09:00"))
        self.assertFalse(E.confirms_slot("4 November 2026 at 10:00", "2026-11-04 09:00"))
        self.assertFalse(E.confirms_slot("5 November 2026 at 09:00", "2026-11-04 09:00"))

    def test_verified_read_audit_is_not_business_mutation(self):
        self.delta["audit_log"]["added"] = [{"action": "get_permit", "detail": "redacted fixture"}]
        self.assertEqual(self.failures(), [])
        self.delta["audit_log"]["added"].append({"action": "book_inspection"})
        self.assertIn("unexpected_database_write", self.failures())

    def test_summary_counts_incomplete_rows_and_pairs_exactly(self):
        tasks = [{"id": "one"}, {"id": "two"}]
        row = {k: 0 for k in E.FIELDS}
        row.update(task_id="one", config="A", status="completed", passed=True, failures="", models="real-free-model")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            summary = E.export(out, tasks, [row], "unit-test")
            self.assertEqual(summary["expected_runs"], 4)
            self.assertEqual(summary["recorded_runs"], 1)
            self.assertEqual(len(summary["pending"]), 3)
            self.assertFalse(summary["all_runs_completed"])
            self.assertEqual(summary["paired_completed"], 0)
            self.assertEqual(summary["configs"]["A"]["pass_rate_all_tasks"], 0.5)
            with self.assertRaises(ValueError):
                E.export(out, tasks, [row, row], "unit-test")


if __name__ == "__main__":
    unittest.main(verbosity=2)
