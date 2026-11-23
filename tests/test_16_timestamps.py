"""Run timestamps carry their UTC offset so each browser shows the visitor's own local time (a hosted server runs in UTC);
explanation texts that quote a time say which zone it is."""
import unittest
from datetime import datetime
from helpers import AppTestCase


class Timestamps(AppTestCase):
    def test_new_runs_are_offset_aware(self):
        r = self.app.submit_sample("01_acme_clean.pdf"); d = datetime.fromisoformat(r["ts"])
        self.assertIsNotNone(d.utcoffset()); self.assertLess(abs((datetime.now().astimezone() - d).total_seconds()), 120)

    def test_quoted_times_are_labelled_and_old_naive_ones_unchanged(self):
        t = self.server.ts_text
        self.assertEqual(t("2026-11-23T19:52:10+00:00"), "2026-11-23 19:52 UTC")
        self.assertEqual(t("2026-11-24T01:22:10+05:30"), "2026-11-23 19:52 UTC")
        self.assertEqual(t("2026-11-23T22:41:05"), "2026-11-23 22:41")                 # history written before this change keeps its stored wording
        self.assertEqual(t(None), "None")

    def test_duplicate_explanation_names_the_zone(self):
        self.app.submit_sample("01_acme_clean.pdf"); dup = self.app.submit_sample("05_acme_duplicate_resent.pdf")
        self.assertRegex(self.stage(dup, "duplicate")["detail"], r"on \d{4}-\d\d-\d\d \d\d:\d\d UTC")


if __name__ == "__main__":
    unittest.main()
