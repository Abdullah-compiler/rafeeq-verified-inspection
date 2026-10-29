"""Offline checks against real synthetic SQLite databases (no model API calls)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

CAPSTONE = Path(__file__).resolve().parents[1]
LABS = CAPSTONE.parent
sys.path[:0] = [str(CAPSTONE), str(LABS)]

import madar
from rafeeq import BookingSession, PersistentMadar, ground_booking_reply, redact, trim_history

PERMIT = "FTR-30311"
ID4 = "6654"
MOBILE = "0532236654"
SLOT = "2026-11-04 09:00"


class BookingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.approve = Mock(return_value=True)
        self.db = PersistentMadar(self.root / "db")
        self.session = BookingSession(self.db, self.approve)

    def verified(self, session=None):
        session = session or self.session
        session.get_permit(PERMIT, ID4, MOBILE)
        session.mark_policy([{"policy_id": "POL-05"}])
        return session.list_slots("Al-Rawdah", "2026-11-03", "2026-11-05")

    def bookings(self):
        return self.db.sql("SELECT * FROM inspections WHERE permit_id=?", (PERMIT,))

    def test_approval_creates_one_real_booking(self):
        self.assertIn(SLOT, self.verified())
        result = self.session.book_inspection(PERMIT, SLOT)
        self.assertEqual(result["status"], "booked")
        self.assertEqual(result["slot"], SLOT)
        self.assertEqual(len(self.bookings()), 1)
        self.approve.assert_called_once()
        self.assertEqual(self.db.sql("SELECT taken FROM slots WHERE district=? AND slot=?", ("Al-Rawdah", SLOT))[0]["taken"], 1)

    def test_denial_never_writes_a_booking(self):
        self.verified()
        self.approve.return_value = False
        with self.assertRaisesRegex(madar.ToolError, "denied"):
            self.session.book_inspection(PERMIT, SLOT)
        self.assertEqual(self.bookings(), [])
        self.assertEqual(self.db.sql("SELECT taken FROM slots WHERE district=? AND slot=?", ("Al-Rawdah", SLOT))[0]["taken"], 0)

    def test_denial_latches_for_rest_of_model_turn(self):
        self.verified()
        self.approve.return_value = False
        for _ in range(2):
            with self.assertRaises(madar.ToolError):
                self.session.book_inspection(PERMIT, SLOT)
        self.approve.assert_called_once()
        self.assertEqual(self.bookings(), [])

    def test_new_user_turn_can_reconsider_denial(self):
        self.verified()
        self.approve.return_value = False
        with self.assertRaises(madar.ToolError):
            self.session.book_inspection(PERMIT, SLOT)
        self.session.begin_turn()
        self.approve.return_value = True
        self.session.book_inspection(PERMIT, SLOT)
        self.assertEqual(len(self.bookings()), 1)

    def test_fail_closed_for_non_boolean_approval(self):
        self.verified()
        self.approve.return_value = "yes"
        with self.assertRaises(madar.ToolError):
            self.session.book_inspection(PERMIT, SLOT)
        self.assertEqual(self.bookings(), [])

    def test_booking_requires_verified_identity(self):
        with self.assertRaisesRegex(madar.ToolError, "Verify"):
            self.session.book_inspection(PERMIT, SLOT)
        self.approve.assert_not_called()

    def test_slots_require_verified_district(self):
        with self.assertRaises(madar.ToolError):
            self.session.list_slots("Al-Rawdah", "2026-11-03", "2026-11-05")

    def test_other_district_is_blocked(self):
        self.verified()
        with self.assertRaises(madar.ToolError):
            self.session.list_slots("Al-Shati", "2026-11-03", "2026-11-05")

    def test_wrong_and_unknown_permit_have_same_public_error(self):
        messages = []
        for permit in (PERMIT, "FTR-99999"):
            with self.assertRaises(madar.ToolError) as caught:
                self.session.get_permit(permit, "0000", MOBILE)
            messages.append(str(caught.exception))
        self.assertEqual(messages[0], messages[1])
        self.assertNotIn("30311", messages[0])

    def test_failed_reverification_revokes_access(self):
        self.verified()
        with self.assertRaises(madar.ToolError):
            self.session.get_permit(PERMIT, "0000", MOBILE)
        with self.assertRaises(madar.ToolError):
            self.session.book_inspection(PERMIT, SLOT)

    def test_id_requires_exactly_four_digits(self):
        with self.assertRaises(madar.ToolError):
            self.session.get_permit(PERMIT, "12346654", MOBILE)

    def test_lookup_returns_no_name_id_or_mobile(self):
        result = self.session.get_permit(PERMIT, ID4, MOBILE)
        self.assertNotIn("applicant", result)
        self.assertNotIn("national_id", result)
        self.assertNotIn("mobile", result)
        self.assertEqual(result["status"], "approved")

    def test_booking_requires_inspection_policy(self):
        self.session.get_permit(PERMIT, ID4, MOBILE)
        self.session.list_slots("Al-Rawdah", "2026-11-03", "2026-11-05")
        with self.assertRaisesRegex(madar.ToolError, "POL-05"):
            self.session.book_inspection(PERMIT, SLOT)
        self.approve.assert_not_called()

    def test_booking_must_have_been_offered(self):
        self.session.get_permit(PERMIT, ID4, MOBILE)
        self.session.mark_policy([{"policy_id": "POL-05"}])
        with self.assertRaisesRegex(madar.ToolError, "list_slots"):
            self.session.book_inspection(PERMIT, SLOT)
        self.approve.assert_not_called()

    def test_does_not_offer_too_early_slot(self):
        slots = self.verified()
        self.assertNotIn("2026-11-02 09:00", slots)

    def test_date_range_and_format_validation(self):
        self.verified()
        for start, end in [("bad", "2026-11-04"), ("2026-11-05", "2026-11-04"), ("2026-11-01", "2027-11-01")]:
            with self.assertRaises(madar.ToolError):
                self.session.list_slots("Al-Rawdah", start, end)

    def test_read_after_write_uses_same_state(self):
        self.verified()
        result = self.session.book_inspection(PERMIT, SLOT)
        info = self.session.get_permit(PERMIT, ID4, MOBILE)
        self.assertIn(result["inspection_id"], [i["inspection_id"] for i in info["inspections"]])
        self.assertNotIn(SLOT, self.session.list_slots("Al-Rawdah", "2026-11-03", "2026-11-05"))

    def test_reopening_does_not_reset_database(self):
        self.verified()
        self.session.book_inspection(PERMIT, SLOT)
        reopened = PersistentMadar(self.root / "db")
        self.assertEqual(len(reopened.sql("SELECT 1 FROM inspections WHERE permit_id=?", (PERMIT,))), 1)
        new_session = BookingSession(reopened, self.approve)
        with self.assertRaises(madar.ToolError):
            new_session.book_inspection(PERMIT, SLOT)

    def test_same_booking_is_idempotent(self):
        self.verified()
        first = self.session.book_inspection(PERMIT, SLOT)
        second = self.session.book_inspection(PERMIT, SLOT)
        self.assertEqual(first["inspection_id"], second["inspection_id"])
        self.assertEqual(len(self.bookings()), 1)
        self.approve.assert_called_once()

    def test_slot_taken_during_approval_rolls_back_booking(self):
        self.verified()
        def approve(name, proposal):
            self.db.sql("UPDATE slots SET taken=1 WHERE district=? AND slot=?", ("Al-Rawdah", SLOT))
            return True
        self.session.approve = approve
        with self.assertRaisesRegex(madar.ToolError, "taken"):
            self.session.book_inspection(PERMIT, SLOT)
        self.assertEqual(self.bookings(), [])

    def test_permit_changed_during_approval_is_not_booked(self):
        self.verified()
        def approve(name, proposal):
            self.db.sql("UPDATE permits SET status='under_review' WHERE permit_id=?", (PERMIT,))
            return True
        self.session.approve = approve
        with self.assertRaisesRegex(madar.ToolError, "changed"):
            self.session.book_inspection(PERMIT, SLOT)
        self.assertEqual(self.bookings(), [])
        self.assertEqual(self.db.sql("SELECT taken FROM slots WHERE district=? AND slot=?", ("Al-Rawdah", SLOT))[0]["taken"], 0)

    def test_approval_exception_fails_without_booking(self):
        self.verified()
        self.approve.side_effect = RuntimeError("approval UI unavailable")
        with self.assertRaises(RuntimeError):
            self.session.book_inspection(PERMIT, SLOT)
        self.assertEqual(self.bookings(), [])

    def test_confirmation_is_grounded_and_matches_english_request(self):
        self.verified()
        self.session.book_inspection(PERMIT, SLOT)
        reply = ground_booking_reply("Please book my inspection", self.session, "سأرسل تذكيرًا")
        self.assertIn("Booked health inspection", reply)
        self.assertIn("INS-9002", reply)
        self.assertNotIn("تذكير", reply)
        self.assertNotIn(MOBILE, reply)

    def test_confirmation_matches_arabic_request(self):
        self.verified()
        self.session.book_inspection(PERMIT, SLOT)
        reply = ground_booking_reply("احجز الفحص", self.session, "I will send a reminder")
        self.assertIn("تم حجز الفحص الصحي", reply)
        self.assertNotIn("reminder", reply)

    def test_denial_confirmation_does_not_invent_policy_requirement(self):
        self.verified()
        self.approve.return_value = False
        with self.assertRaises(madar.ToolError):
            self.session.book_inspection(PERMIT, SLOT)
        reply = ground_booking_reply("Please book", self.session, "POL-05 requires officer approval")
        self.assertIn("No inspection was booked", reply)
        self.assertNotIn("POL-05", reply)

    def test_no_success_confirmation_if_committed_row_disappears(self):
        self.verified()
        self.session.book_inspection(PERMIT, SLOT)
        self.db.sql("DELETE FROM inspections WHERE permit_id=?", (PERMIT,))
        with self.assertRaisesRegex(RuntimeError, "could not be verified"):
            ground_booking_reply("Please book", self.session, "Booked!")

    def test_previous_turn_booking_does_not_override_new_policy_reply(self):
        self.verified()
        self.session.book_inspection(PERMIT, SLOT)
        self.session.begin_turn()
        self.assertEqual(ground_booking_reply("What are the hours?", self.session, "Office hours"), "Office hours")

    def test_rejected_permit_cannot_book(self):
        self.session.get_permit("SGN-40220", "0093", "0540410093")
        self.session.mark_policy([{"policy_id": "POL-05"}])
        slots = self.session.list_slots("Al-Olaya", "2026-11-03", "2026-11-05")
        with self.assertRaises(madar.ToolError):
            self.session.book_inspection("SGN-40220", slots[0])
        self.approve.assert_not_called()

    def test_competing_writers_cannot_double_book(self):
        def worker(_):
            db = PersistentMadar(self.root / "db")
            session = BookingSession(db, lambda *_: True)
            self.verified(session)
            try:
                return session.book_inspection(PERMIT, SLOT)["inspection_id"]
            except madar.ToolError:
                return "blocked"
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(worker, range(2)))
        self.assertEqual(len(self.bookings()), 1)

    def test_redaction_removes_credentials_and_api_tokens(self):
        value = {"mobile": MOBILE, "national_id_last4": ID4,
                 "text": f"mobile {MOBILE}; token sk-or-v1-{'a' * 64}"}
        output = json.dumps(redact(value))
        self.assertNotIn(MOBILE, output)
        self.assertNotIn(ID4, output)
        self.assertNotIn("a" * 64, output)

    def test_history_trim_never_starts_at_tool_result(self):
        history = [{"role": "user", "content": "old"}] + [{"role": "tool", "content": "x"}] * 20
        self.assertEqual(trim_history(history), [])
        self.assertEqual(trim_history(history + [{"role": "user", "content": "new"}])[0]["role"], "user")


if __name__ == "__main__":
    unittest.main()
