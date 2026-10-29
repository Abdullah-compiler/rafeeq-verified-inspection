"""Regression tests for independently reviewed privacy/authorization issues; no model APIs."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

CAPSTONE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(CAPSTONE), str(CAPSTONE.parent)]
import agentkit as K
import madar
from rafeeq import BookingSession, PersistentMadar, make_tools, redact, save_report


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = PersistentMadar(self.root / "db")
        self.session = BookingSession(self.db, lambda *_: True)

    def ready(self, id4="6654", mobile="0532236654"):
        self.session.get_permit("FTR-30311", id4, mobile)
        self.session.mark_policy([{"policy_id": "POL-05"}])
        self.session.list_slots("Al-Rawdah", "2026-11-03", "2026-11-05")

    def assert_no_booking(self):
        self.assertEqual(self.db.sql("SELECT 1 FROM inspections WHERE permit_id='FTR-30311'"), [])
        self.assertEqual(self.db.sql("SELECT taken FROM slots WHERE district='Al-Rawdah' AND slot='2026-11-04 09:00'")[0]["taken"], 0)

    def test_labelled_id_and_formatted_mobile_redact_without_lookup(self):
        text = "My ID ends 6654, registered mobile +966 53 223 6654, national ID 1048213372."
        clean = redact(text)
        for secret in ("6654", "+966 53 223 6654", "1048213372"):
            self.assertNotIn(secret, clean)

    def test_arabic_credentials_redact_without_lookup(self):
        clean = redact("آخر أربعة أرقام من الهوية ٦٦٥٤ والجوال ٠٥٣٢٢٣٦٦٥٤")
        self.assertNotIn("٦٦٥٤", clean)
        self.assertNotIn("٠٥٣٢٢٣٦٦٥٤", clean)

    def test_unlabelled_id_echo_redacts_after_incoming_capture_without_lookup(self):
        self.session.remember_user_input("My ID ends 6654; phone +966 53 223 6654")
        self.assertNotIn("6654", redact("You gave 6654", self.session.secrets))

    def test_report_is_private_when_lookup_never_happens(self):
        self.session.remember_user_input("My ID ends 6654; phone +966 53 223 6654")
        run = SimpleNamespace(final="You gave 6654 and +966 53 223 6654", stopped="done", steps=1, cost=0,
                              tool_calls=[], trace=[K.Event("final", "ID ends 6654")])
        report = self.root / "report.json"
        save_report(report, run, self.session, SimpleNamespace(models=["unit-test-not-live"]))
        text = report.read_text()
        self.assertNotIn("6654", text)
        self.assertNotIn("+966 53 223 6654", text)
        self.assertIn("redacted", text)

    def test_policy_search_blocks_credentials_even_before_lookup(self):
        bridge = Mock()
        bridge.list_tools.return_value = [SimpleNamespace(name="search_policies")]
        search = make_tools(self.session, bridge)[0]
        for query in ("ID ends 6654", "phone +966 53 223 6654", "0532236654", "national ID 1048213372"):
            with self.subTest(query_kind=query.split()[0]):
                with self.assertRaisesRegex(madar.ToolError, "credentials"):
                    search(query)
        bridge.call.assert_not_called()

    def test_mobile_change_during_approval_revokes_authorization(self):
        self.ready()
        def approve(name, proposal):
            self.db.sql("UPDATE applicants SET mobile='0500000000' WHERE applicant_id=(SELECT applicant_id FROM permits WHERE permit_id='FTR-30311')")
            return True
        self.session.approve = approve
        with self.assertRaisesRegex(madar.ToolError, "verification"):
            self.session.book_inspection("FTR-30311", "2026-11-04 09:00")
        self.assert_no_booking()

    def test_owner_change_during_approval_revokes_authorization(self):
        self.ready()
        def approve(name, proposal):
            self.db.sql("UPDATE permits SET applicant_id=(SELECT applicant_id FROM applicants WHERE mobile='0501234821') WHERE permit_id='FTR-30311'")
            return True
        self.session.approve = approve
        with self.assertRaisesRegex(madar.ToolError, "verification"):
            self.session.book_inspection("FTR-30311", "2026-11-04 09:00")
        self.assert_no_booking()

    def test_agent_authorization_revoked_during_approval(self):
        owner = self.db.sql("SELECT applicant_id FROM permits WHERE permit_id='FTR-30311'")[0]["applicant_id"]
        agent = self.db.sql("SELECT applicant_id,national_id,mobile FROM applicants WHERE agent_for IS NOT NULL LIMIT 1")[0]
        self.db.sql("UPDATE applicants SET agent_for=? WHERE applicant_id=?", (owner, agent["applicant_id"]))
        self.ready(agent["national_id"][-4:], agent["mobile"])
        def approve(name, proposal):
            self.db.sql("UPDATE applicants SET agent_for=NULL WHERE applicant_id=?", (agent["applicant_id"],))
            return True
        self.session.approve = approve
        with self.assertRaisesRegex(madar.ToolError, "verification"):
            self.session.book_inspection("FTR-30311", "2026-11-04 09:00")
        self.assert_no_booking()


if __name__ == "__main__":
    unittest.main()
