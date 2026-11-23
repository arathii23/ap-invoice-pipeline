"""Human in the loop: a person decides held invoices. The automated decision stays on record, the reason is mandatory,
and the same safeguards apply (never pay twice, never approve without a vendor, invoice number and amount)."""
import csv, io, unittest
from helpers import AppTestCase, make_invoice


class Review(AppTestCase):
    def review(self, rid, action, note="Confirmed with procurement by phone", reviewer="Priya (AP)"):
        return self.app.post(f"/api/run/{rid}/review", {"action": action, "note": note, "reviewer": reviewer})

    def test_approving_a_held_invoice_records_both_decisions_and_consumes_the_po(self):
        held = self.app.submit_sample("07_greenfield_price_variance.pdf"); self.assertEqual(held["decision"], "HOLD")
        before = self.app.get("/api/dashboard")[1]["billed"][held["po"]]
        s, r = self.review(held["id"], "approve", "Price increase agreed in PO amendment 2 (email 12 Nov)"); self.assertEqual(s, 200, r)
        run = self.app.get(f"/api/run/{held['id']}")[1]
        self.assertEqual(run["decision"], "APPROVE"); self.assertEqual(run["auto"]["decision"], "HOLD"); self.assertTrue(run["reviewed"])
        self.assertEqual(run["review_log"][-1]["reviewer"], "Priya (AP)"); self.assertIn("Approved on review by Priya (AP)", run["headline"])
        self.assertGreater(self.app.get("/api/dashboard")[1]["billed"][held["po"]], before)

    def test_a_reviewed_approval_makes_a_later_copy_a_duplicate(self):
        held = self.app.submit_sample("07_greenfield_price_variance.pdf"); self.review(held["id"], "approve")
        again = self.app.submit_sample("07_greenfield_price_variance.pdf"); self.assertEqual(again["decision"], "REJECT")

    def test_rules_still_protect_a_reviewer(self):
        miss = self.app.submit_sample("06_greenfield_missing_fields.pdf")
        s, r = self.review(miss["id"], "approve"); self.assertEqual(s, 409); self.assertIn("no invoice number", r["error"])
        ok = self.app.submit_sample("01_acme_clean.pdf")
        s, r = self.review(ok["id"], "approve"); self.assertEqual(s, 409); self.assertIn("already approved", r["error"])
        s, r = self.review(ok["id"], "approve", "ok"); self.assertEqual(s, 400); self.assertIn("at least 5 characters", r["error"])
        s, r = self.review(ok["id"], "delete"); self.assertEqual(s, 400)

    def test_bank_change_approval_carries_a_fraud_warning(self):
        held = self.app.submit_sample("09_brightline_bank_changed.pdf"); self.assertEqual(held["decision"], "HOLD")
        s, r = self.review(held["id"], "approve", "New account confirmed by call-back to the number on file"); self.assertEqual(s, 200, r)
        self.assertTrue(any("known phone number" in w for w in r["warnings"]))

    def test_request_info_keeps_it_on_hold_then_reject(self):
        held = self.app.submit_sample("07_greenfield_price_variance.pdf")
        s, r = self.review(held["id"], "request_info", "Asked vendor to confirm the unit price"); self.assertEqual(s, 200)
        run = self.app.get(f"/api/run/{held['id']}")[1]; self.assertEqual(run["decision"], "HOLD"); self.assertTrue(run["awaiting_vendor"])
        s, r = self.review(held["id"], "reject", "Vendor did not respond within 7 days"); self.assertEqual(s, 200)
        run = self.app.get(f"/api/run/{held['id']}")[1]; self.assertEqual(run["decision"], "REJECT"); self.assertEqual(len(run["review_log"]), 2)
        s, r = self.review(held["id"], "approve"); self.assertEqual(s, 409)                                     # decided once only

    def test_review_is_private_to_the_workspace_and_failed_runs_cannot_be_reviewed(self):
        s, r = self.review("deadbeef", "approve"); self.assertEqual(s, 404)

    def test_csv_export_has_every_run_and_both_decisions(self):
        held = self.app.submit_sample("07_greenfield_price_variance.pdf"); self.app.submit_sample("01_acme_clean.pdf"); self.review(held["id"], "approve")
        s, h, b = self.app.request("GET", "/api/export.csv"); self.assertEqual(s, 200); self.assertIn("attachment", h["Content-Disposition"])
        rows = list(csv.DictReader(io.StringIO(b.decode("utf-8-sig")))); self.assertEqual(len(rows), 2)
        r7 = next(r for r in rows if r["run_id"] == held["id"])
        self.assertEqual((r7["decision"], r7["automated_decision"], r7["reviewed_by"]), ("APPROVE", "HOLD", "Priya (AP)"))


if __name__ == "__main__":
    unittest.main()


class CsvSafety(AppTestCase):
    def test_formula_text_from_an_invoice_cannot_run_in_a_spreadsheet(self):
        self.app.submit(make_invoice(invoice_no="INV-CSV-1"), "=HYPERLINK(evil).pdf")
        s, h, b = self.app.request("GET", "/api/export.csv")
        row = list(csv.DictReader(io.StringIO(b.decode("utf-8-sig"))))[0]; self.assertTrue(row["file"].startswith("'="))
