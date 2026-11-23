"""Defect 1: missing financial evidence must never produce an approval; a check that could not run is never shown as Passed."""
import unittest
from helpers import AppTestCase, make_invoice, make_po, PO_ID


class MissingFinancials(AppTestCase):
    def test_total_only_invoice_is_held_not_approved(self):
        # the reported case: total 9,999,999 with no subtotal, tax or lines, against a 130,000 PO
        r = self.app.submit(make_invoice(invoice_no="INV-M-0001", subtotal=None, tax_rate=0, tax=None, lines=[], total=9999999.0), "m.pdf")
        self.assertEqual(r["decision"], "HOLD", r["reasoning"])
        self.assertEqual(self.stage(r, "math")["status"], "unverified")
        self.assertEqual(self.stage(r, "amount")["status"], "unverified")
        self.assertIn("subtotal", self.stage(r, "math")["detail"])
        self.assertIn("could not", self.stage(r, "math")["detail"].lower())
        self.assertIn("Amounts could not be verified", r["headline"])
        for k in ("math", "amount"):    # a check that could not execute must never appear as Passed
            self.assertNotEqual(self.stage(r, k)["status"], "pass")
        self.assertEqual(self.app.get("/api/dashboard")[1]["billed"]["PO-2026-1001"], 0.0, "a held invoice must not consume the PO")

    def test_missing_tax_is_held(self):
        r = self.app.submit(make_invoice(invoice_no="INV-M-0002", tax_rate=0, tax=None, total=130000.0), "m2.pdf")
        self.assertEqual(r["decision"], "HOLD")
        self.assertIn("tax amount was not found", self.stage(r, "math")["detail"])

    def test_missing_total_is_held_as_incomplete(self):
        r = self.app.submit(make_invoice(invoice_no="INV-M-0003", total=None), "m3.pdf")
        self.assertEqual(r["decision"], "HOLD")
        self.assertEqual(self.stage(r, "fields")["status"], "fail_hold")
        self.assertIn("total", self.stage(r, "fields")["detail"])

    def test_missing_net_amount_never_falls_back_to_total(self):
        r = self.app.submit(make_invoice(invoice_no="INV-M-0004", subtotal=None, lines=[], tax=23400.0, total=153400.0), "m4.pdf")
        self.assertEqual(r["decision"], "HOLD")
        self.assertEqual(self.stage(r, "amount")["status"], "unverified")

    def test_valid_complete_invoice_is_still_approved(self):
        r = self.app.submit(make_invoice(invoice_no="INV-M-0005"), "ok.pdf")
        self.assertEqual(r["decision"], "APPROVE", r["reasoning"])
        self.assertEqual(self.stage(r, "math")["status"], "pass")
        self.assertEqual(self.stage(r, "amount")["status"], "pass")
        self.assertEqual(r["po"], "PO-2026-1001")

    def test_bundled_invoice_without_line_items_can_be_approved_but_says_line_checks_did_not_run(self):
        # "line items might be itemised or bundled": subtotal + tax + total reconcile, so it is approvable, but honestly labelled
        r = self.app.submit(make_invoice(invoice_no="INV-M-0006", lines=[], subtotal=130000.0, tax_rate=18, tax=23400.0, total=153400.0), "bundled.pdf")
        self.assertEqual(r["decision"], "APPROVE", r["reasoning"])
        d = self.stage(r, "math")["detail"]
        self.assertIn("Not applicable", d)
        self.assertNotIn("Line items →", d)

    def test_decide_refuses_to_approve_when_a_required_check_did_not_run(self):
        s = self.server
        ok = [{"key": k, "status": "pass", "title": k, "detail": "", "extra": {}} for k in s.REQUIRED_STAGES]
        self.assertEqual(s.decide(ok, {"total": 100.0}, {"po": "PO-X"})[0], "APPROVE")
        for k in s.REQUIRED_STAGES:
            skipped = [dict(c, status="info") if c["key"] == k else c for c in ok]
            self.assertEqual(s.decide(skipped, {"total": 100.0}, {"po": "PO-X"})[0], "HOLD", k)
            absent = [c for c in ok if c["key"] != k]
            self.assertEqual(s.decide(absent, {"total": 100.0}, {"po": "PO-X"})[0], "HOLD", k)


if __name__ == "__main__":
    unittest.main()
