"""Defect 10: decisions and next steps must describe what the system actually did. It records a decision; it does not release payments,
send e-mails or return invoices. Approvals are 'eligible for payment processing'; everything else is a recommendation."""
import re, unittest
from helpers import AppTestCase, ROOT

FORBIDDEN = re.compile(r"released to|release[sd]? (the )?payment|returned to (the )?vendor|is returned|auto-?draft|notify vendor|\bfreeze\b|"
                       r"payment (is|was|has been) (scheduled|sent|made)|e-?mail (was|has been) sent|we (have )?(sent|emailed)", re.I)

SAMPLES = ["01_acme_clean.pdf", "03_novatech_milestone1.pdf", "05_acme_duplicate_resent.pdf", "07_greenfield_price_variance.pdf", "09_brightline_bank_changed.pdf",
           "06_greenfield_missing_fields.pdf", "08_acme_implied_po.pdf", "04_novatech_milestone2.pdf", "10_acme_multipage.pdf", "11_greenfield_alt_layout.pdf"]


class Wording(AppTestCase):
    def test_every_decision_text_is_accurate_across_all_scenarios(self):
        seen = set()
        for n in SAMPLES:
            r = self.app.submit_sample(n); seen.add(r["decision"])
            texts = [r["headline"], r["reasoning"], r["action"]] + [s["detail"] for s in r["stages"]]
            for t in texts: self.assertIsNone(FORBIDDEN.search(t), f"{n}: {t!r}")
            self.assertTrue(r["action"].startswith("Recommended:"), f"{n}: {r['action']!r}")
        self.assertEqual(seen, {"APPROVE", "HOLD", "REJECT"})

    def test_approval_says_eligible_not_released(self):
        r = self.app.submit_sample("01_acme_clean.pdf")
        self.assertIn("is eligible for payment processing", r["reasoning"]); self.assertIn("has not scheduled or sent any payment", r["reasoning"])
        self.assertTrue(r["action"].startswith("Recommended: schedule payment"))

    def test_duplicate_and_missing_field_recommendations_do_not_claim_actions(self):
        self.app.submit_sample("01_acme_clean.pdf")
        dup = self.app.submit_sample("05_acme_duplicate_resent.pdf")
        self.assertIn("no message is sent automatically", dup["action"])
        miss = self.app.submit_sample("06_greenfield_missing_fields.pdf")
        self.assertEqual(miss["headline"], "Incomplete invoice — vendor follow-up needed"); self.assertTrue(miss["action"].startswith("Recommended: ask the vendor"))

    def test_failed_run_text(self):
        r = self.app.submit(b"%PDF-1.4\nnot really a pdf", "bad.pdf")
        self.assertIn("No payment decision was made", r["reasoning"]); self.assertTrue(r["action"].startswith("Retry the run"))

    def test_ui_copy_does_not_claim_payments_were_released(self):
        html = open(f"{ROOT}/static/index.html", encoding="utf-8").read()
        self.assertIsNone(re.search(r"\breleased\b|\bblocked\b", html, re.I), "KPI/UI copy must not imply payments were released or blocked")


if __name__ == "__main__":
    unittest.main()
