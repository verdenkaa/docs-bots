"""Queryable step projection preserves correlation, timing hierarchy and privacy."""
from contextlib import redirect_stdout
from io import StringIO
import json
import unittest

from rag.runtime.events import emit_event, _event_context


class MonitoringProjectionTests(unittest.TestCase):
    def test_projection_keeps_final_event_last_and_does_not_copy_parent_result(self):
        output = StringIO()
        with redirect_stdout(output), _event_context(request_id=7, route="rag.questions"):
            emit_event("rag_core", "handle_rag_question", "finished", "run", result_status="answered",
                       duration_ms=100, http_status=200, steps=[dict(name="search", duration_ms=90, outcome="completed",
                           extra_metadata=True, steps=[dict(name="embed_texts", duration_ms=50,
                               model_role="embedding", outcome="completed")])])
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([r["operation"] for r in records], ["step", "step", "handle_rag_question"])
        outer, inner, final = records
        self.assertEqual((outer["step_depth"], inner["step_depth"], inner["parent_step"]), (0, 1, "search"))
        self.assertEqual((outer["duration_ms"], inner["duration_ms"]), (90, 50))
        self.assertNotIn("result_status", outer)
        self.assertNotIn("http_status", outer)
        self.assertNotIn("extra_metadata", outer)
        self.assertTrue(all(r["request_id"] == 7 and r["run_id"] == "run" for r in records))
        self.assertTrue(final["steps"][0]["extra_metadata"])  # original event stays compatible

    def test_flat_api_hierarchy_and_http_status_are_preserved(self):
        output = StringIO()
        with redirect_stdout(output):
            emit_event("rag_api", "handle_rag_result", "finished", "run", steps=[dict(
                name="sendMessage", step_depth=1, parent_step="send_part", duration_ms=12,
                request_id=7, http_status=429, telegram_error_code=429, outcome="rejected_or_unknown")])
        first = json.loads(output.getvalue().splitlines()[0])
        self.assertEqual(first["step_depth"], 1)
        self.assertEqual(first["parent_step"], "send_part")
        self.assertEqual(first["http_status"], 429)
        self.assertEqual(first["telegram_error_code"], 429)
        self.assertEqual(first["request_id"], 7)
