"""Defect 6: a corrected resubmission of an unresolved held invoice is re-evaluated and linked to the original; an unchanged resend
of a held invoice stays held; an already-approved invoice can never be approved again; the audit trail is retained."""
import unittest
from helpers import AppTestCase, make_invoice, make_po, PO_ID

WRONG_BANK = dict(bank="99999999999999")


class Resubmission(AppTestCase):
    def test_corrected_resubmission_of_a_held_invoice_is_evaluated_and_linked(self):
        held = self.app.submit(make_invoice(invoice_no="INV-R-0001", **WRONG_BANK), "v1.pdf")
        self.assertEqual((held["decision"], held["headline"]), ("HOLD", "Bank details changed — verify before paying"))
        fixed = self.app.submit(make_invoice(invoice_no="INV-R-0001"), "v2_corrected.pdf")
        self.assertEqual(fixed["decision"], "APPROVE", fixed["reasoning"])
        rel = fixed["related"]
        self.assertEqual(rel["relation"], "corrected_resubmission"); self.assertEqual([x["id"] for x in rel["of"]], [held["id"]])
        self.assertIn("bank account", [c["field"] for c in rel["changes"]])
        d = self.stage(fixed, "duplicate"); self.assertEqual(d["status"], "warn"); self.assertIn(held["id"], d["detail"])
        # audit trail: the held submission is retained, unchanged, and shown as superseded
        again = self.app.get(f"/api/run/{held['id']}")[1]
        self.assertEqual((again["decision"], again["superseded_by"]), ("HOLD", fixed["id"]))
        self.assertEqual(len(self.app.runs()), 2)

    def test_correction_text_is_readable_when_a_field_was_missing_before(self):
        held = self.app.submit(make_invoice(invoice_no="INV-R-0002", date=None), "no_date.pdf"); self.assertEqual(held["decision"], "HOLD")
        fixed = self.app.submit(make_invoice(invoice_no="INV-R-0002", date="06 Nov 2026"), "dated.pdf"); self.assertEqual(fixed["decision"], "APPROVE", fixed["reasoning"])
        d = self.stage(fixed, "duplicate")
        self.assertIn("invoice date: not stated → 06 Nov 2026", d["detail"]); self.assertNotRegex(d["detail"] + fixed["reasoning"], r"\bNone\b")
        self.assertNotIn("..", fixed["reasoning"]); self.assertEqual(d["extra"]["compare"][0]["reference"], "not stated (earlier, held)")

    def test_unchanged_pending_resubmission_stays_on_hold_and_is_not_a_paid_duplicate(self):
        pdf = make_invoice(invoice_no="INV-R-0002", **WRONG_BANK)
        first = self.app.submit(pdf, "a.pdf"); second = self.app.submit(pdf, "a_again.pdf")
        self.assertEqual((first["decision"], second["decision"]), ("HOLD", "HOLD"))
        self.assertEqual(second["related"]["relation"], "unchanged_resubmission")
        self.assertEqual(second["headline"], "Already on hold — resubmitted without changes"); self.assertFalse(second["is_duplicate"])
        self.assertEqual(self.stage(second, "duplicate")["status"], "fail_hold")
        # a re-export with different bytes but the same content is also "unchanged"
        third = self.app.submit(make_invoice(invoice_no="INV-R-0002", extra_lines=["Thank you for your business"], **WRONG_BANK), "a_reexport.pdf")
        self.assertEqual((third["decision"], third["related"]["relation"]), ("HOLD", "unchanged_resubmission"))
        self.assertNotIn("APPROVE", [r["decision"] for r in self.app.runs()])

    def test_resubmission_of_an_already_approved_invoice_is_rejected_as_duplicate(self):
        ok = self.app.submit(make_invoice(invoice_no="INV-R-0003"), "ok.pdf")
        same = self.app.submit(make_invoice(invoice_no="INV-R-0003"), "ok_again.pdf")
        changed = self.app.submit(make_invoice(invoice_no="INV-R-0003", date="06 Nov 2026"), "ok_changed_date.pdf")
        for r in (same, changed):
            self.assertEqual((r["decision"], r["headline"]), ("REJECT", "Duplicate invoice — do not pay twice"))
            self.assertEqual(r["related"]["relation"], "duplicate_of_approved"); self.assertEqual(r["related"]["of"][0]["id"], ok["id"])
        self.assertEqual([r["decision"] for r in self.app.runs()].count("APPROVE"), 1)

    def test_after_the_correction_is_approved_the_original_held_version_cannot_be_approved(self):
        self.app.submit(make_invoice(invoice_no="INV-R-0004", **WRONG_BANK), "v1.pdf")
        self.assertEqual(self.app.submit(make_invoice(invoice_no="INV-R-0004"), "v2.pdf")["decision"], "APPROVE")
        r = self.app.submit(make_invoice(invoice_no="INV-R-0004", **WRONG_BANK), "v1_again.pdf")
        self.assertEqual(r["decision"], "REJECT")

    def test_several_corrections_still_yield_one_approval(self):
        r1 = self.app.submit(make_invoice(invoice_no="INV-R-0005", **WRONG_BANK), "1.pdf")
        r2 = self.app.submit(make_invoice(invoice_no="INV-R-0005", date="31/99/2026"), "2.pdf")           # still wrong, differently
        self.assertEqual((r1["decision"], r2["decision"]), ("HOLD", "HOLD")); self.assertEqual(r2["related"]["relation"], "corrected_resubmission")
        r3 = self.app.submit(make_invoice(invoice_no="INV-R-0005"), "3.pdf")
        self.assertEqual(r3["decision"], "APPROVE"); self.assertEqual({x["id"] for x in r3["related"]["of"]}, {r1["id"], r2["id"]})
        self.assertEqual([r["decision"] for r in self.app.runs()].count("APPROVE"), 1)

    def test_correction_of_an_identity_conflict_is_linked_too(self):
        held = self.app.submit(make_invoice(invoice_no="INV-R-0006", gstin="07AABCA9999F1Z5"), "1.pdf")
        self.assertEqual(held["decision"], "HOLD")
        fixed = self.app.submit(make_invoice(invoice_no="INV-R-0006"), "2.pdf")
        self.assertEqual((fixed["decision"], fixed["related"]["relation"]), ("APPROVE", "corrected_resubmission"))

    def test_same_invoice_number_from_different_vendors_is_not_a_duplicate(self):
        self.patch_pos(**{"PO-2026-9002": make_po(net=100000, vendor="V002")})
        a = self.app.submit(make_invoice(invoice_no="INV-9-0001"), "acme.pdf")
        b = self.app.submit(make_invoice(invoice_no="INV-9-0001", vendor="Brightline Logistics LLP", gstin="27AAFFB5678K1Z2", bank="60110045678901", ifsc="ICIC0004567",
                                         po="PO-2026-9002", lines=[("Freight", 1, 50000.0, 50000.0)]), "bright.pdf")
        self.assertEqual((a["decision"], b["decision"]), ("APPROVE", "APPROVE"), b["reasoning"])

    def test_sample_duplicate_flow(self):
        self.assertEqual(self.app.submit_sample("01_acme_clean.pdf")["decision"], "APPROVE")
        self.assertEqual(self.app.submit_sample("05_acme_duplicate_resent.pdf")["decision"], "REJECT")
        self.assertEqual(self.app.submit_sample("01_acme_clean.pdf")["decision"], "REJECT")

    def test_a_duplicate_is_not_also_blamed_for_using_up_the_po(self):
        """The PO is full only because the ORIGINAL was approved. Telling the reader the copy 'needs a PO amendment' would be misleading."""
        self.assertEqual(self.app.submit_sample("01_acme_clean.pdf")["decision"], "APPROVE")
        dup = self.app.submit_sample("05_acme_duplicate_resent.pdf")
        self.assertEqual(dup["decision"], "REJECT"); self.assertEqual(dup["headline"], "Duplicate invoice — do not pay twice")
        amount = self.stage(dup, "amount"); self.assertEqual(amount["status"], "info"); self.assertIn("not counted a second time", amount["detail"])
        self.assertNotIn("no remaining value", dup["reasoning"] + dup["action"]); self.assertNotIn("amendment", dup["reasoning"])
        self.assertEqual(dup["po_info"]["billed"], 130000.0, "the PO card still shows the true balance")


if __name__ == "__main__":
    unittest.main()
