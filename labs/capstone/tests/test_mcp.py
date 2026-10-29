"""Real stdio MCP transport checks; no model API requests."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

CAPSTONE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(CAPSTONE), str(CAPSTONE.parent)]

import agentkit as K
from rafeeq import BookingSession, PersistentMadar, SafeChatShape, approval_cli, make_tools, policy_connection


class PolicyTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = policy_connection()
        cls.bridge.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.bridge.__exit__(None, None, None)

    def test_server_exposes_only_public_policy_search(self):
        self.assertEqual({t.name for t in self.bridge.list_tools()}, {"search_policies"})

    def test_real_mcp_query_returns_inspection_policy(self):
        result = self.bridge.call("search_policies", {"query": "POL-05 inspection booking", "k": 3})
        self.assertFalse(result.is_error)
        hits = json.loads(K.mcp_result_text(result))
        self.assertIn("POL-05", [h["policy_id"] for h in hits])

    def test_policy_id_only_queries_return_the_exact_document(self):
        for policy_id in ("POL-05", "POL-08"):
            with self.subTest(policy=policy_id):
                result = self.bridge.call("search_policies", {"query": policy_id, "k": 1})
                hits = json.loads(K.mcp_result_text(result))
                self.assertEqual([h["policy_id"] for h in hits], [policy_id])

    def test_unknown_explicit_policy_id_does_not_return_unrelated_rules(self):
        result = self.bridge.call("search_policies", {"query": "POL-99", "k": 3})
        self.assertEqual(json.loads(K.mcp_result_text(result)), [])

    def test_agent_has_exactly_four_tools_and_policy_is_marked(self):
        with tempfile.TemporaryDirectory() as temp:
            session = BookingSession(PersistentMadar(Path(temp)), lambda *_: False)
            tools = make_tools(session, self.bridge)
            self.assertEqual({t.__name__ for t in tools}, {"search_policies", "get_permit", "list_slots", "book_inspection"})
            tools[0]("POL-05 inspection booking")
            self.assertTrue(session.policy_seen)

    def test_credential_cannot_be_sent_as_policy_query(self):
        with tempfile.TemporaryDirectory() as temp:
            session = BookingSession(PersistentMadar(Path(temp)), lambda *_: False)
            session.secrets.add("0532236654")
            tool = make_tools(session, self.bridge)[0]
            with self.assertRaisesRegex(Exception, "credentials"):
                tool("lookup 0532236654")


class CompletionAndInputTests(unittest.TestCase):
    def test_eof_denies_human_approval(self):
        with patch("builtins.input", side_effect=EOFError):
            self.assertFalse(approval_cli("book_inspection", {"permit_id": "FTR-30311"}))

    def test_incomplete_generation_cannot_execute_tools(self):
        from types import SimpleNamespace
        turn = K.Turn("", [K.Call("x", "book_inspection", "{}")],
                      raw=SimpleNamespace(choices=[SimpleNamespace(finish_reason="length")], model="unit-test-not-a-live-model"))
        with patch.object(K.ChatShape, "step", return_value=turn):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                SafeChatShape().step([], [], "unit-test-not-a-live-model")


if __name__ == "__main__":
    unittest.main()
