"""Defect 4: missing/unreadable identifiers are different from contradictory ones. A clear GSTIN conflict goes to review;
OCR recovery is an explicit, corroborated, accurately described policy - not a blanket 'OCR noise' excuse. No sample-file exceptions."""
import unittest
from helpers import AppTestCase, make_invoice

V1 = dict(vendor="Acme Industrial Supplies Pvt Ltd", gstin="07AABCA1234F1Z5", bank="50200012345678", ifsc="HDFC0001234")


class VendorIdentityEndToEnd(AppTestCase):
    def test_exact_identity_is_approved(self):
        r = self.app.submit(make_invoice(invoice_no="INV-G-0001"), "ok.pdf")
        self.assertEqual(r["decision"], "APPROVE")
        self.assertIn("GSTIN exact match", self.stage(r, "vendor")["detail"]); self.assertEqual(self.stage(r, "vendor")["status"], "pass")

    def test_conflicting_gstin_on_a_digital_pdf_is_held_even_though_the_name_matches(self):
        r = self.app.submit(make_invoice(invoice_no="INV-G-0002", gstin="07AABCA9999F1Z5"), "conflict.pdf")     # well-formed, but not Acme's number
        self.assertEqual(r["decision"], "HOLD", r["reasoning"])
        v = self.stage(r, "vendor")
        self.assertEqual(v["status"], "fail_hold"); self.assertEqual(v["extra"]["identity"], "conflict")
        self.assertIn("different, well-formed registration number", v["detail"])
        self.assertNotIn("OCR noise", v["detail"]); self.assertNotIn("OCR noise", r["reasoning"])
        self.assertEqual(r["headline"], "Vendor identity needs review")
        self.assertIsNone(r["vendor_id"], "an unconfirmed identity must not be treated as a verified vendor")
        self.assertEqual(self.stage(r, "bank")["status"], "info")      # downstream checks are skipped, not passed

    def test_gstin_of_a_different_vendor_is_a_conflict(self):
        r = self.app.submit(make_invoice(invoice_no="INV-G-0003", gstin="27AAFFB5678K1Z2"), "other.pdf")      # Brightline's GSTIN on an Acme-named invoice
        self.assertEqual(r["decision"], "HOLD"); self.assertIn("belongs to Brightline Logistics LLP", self.stage(r, "vendor")["detail"])

    def test_malformed_gstin_in_a_digital_pdf_is_a_conflict_not_noise(self):
        r = self.app.submit(make_invoice(invoice_no="INV-G-0004", gstin="07AABCA1234"), "short.pdf")
        self.assertEqual(r["decision"], "HOLD"); self.assertIn("is not a valid GSTIN", self.stage(r, "vendor")["detail"])

    def test_missing_gstin_with_two_independent_corroborations_is_accepted_with_a_note(self):
        r = self.app.submit(make_invoice(invoice_no="INV-G-0005", gstin=None), "nogstin.pdf")                  # name + bank + IFSC + PO owner agree
        self.assertEqual(r["decision"], "APPROVE", r["reasoning"])
        v = self.stage(r, "vendor"); self.assertEqual(v["status"], "warn"); self.assertEqual(v["extra"]["identity"], "recovered")
        self.assertIn("not as an exact identity match", v["detail"]); self.assertIn("at least 2 independent signals", v["detail"])

    def test_missing_gstin_without_corroboration_is_held(self):
        r = self.app.submit(make_invoice(invoice_no="INV-G-0006", gstin=None, bank="11112222333344", ifsc="HDFC0009999"), "weak.pdf")   # only the PO agrees
        self.assertEqual(r["decision"], "HOLD")
        v = self.stage(r, "vendor"); self.assertEqual(v["status"], "unverified"); self.assertIn("only 1 of the required 2", v["detail"])

    def test_unknown_vendor_is_rejected(self):
        r = self.app.submit(make_invoice(invoice_no="INV-G-0007", vendor="Totally Different Traders", gstin="29ABCDE1234F1Z5"), "unknown.pdf")
        self.assertEqual(r["decision"], "REJECT")

    def test_scanned_invoice_still_approved(self):
        r = self.app.submit_sample("02_brightline_scanned.pdf")
        self.assertEqual((r["decision"], r["ocr"]), ("APPROVE", True), r["reasoning"])


class VendorIdentityUnit(AppTestCase):
    """The OCR-recovery policy, with synthetic extractions (the real OCR output cannot be dictated)."""
    def ident(self, ocr, po="V001", **kw):
        ex = {"vendor": V1["vendor"], "gstin": V1["gstin"], "bank": V1["bank"], "ifsc": V1["ifsc"]}; ex.update(kw)
        return self.server.resolve_vendor(ex, ocr, po)

    def test_ocr_confusable_difference_with_corroboration_is_recovered(self):
        i = self.ident(True, gstin="07AABCA1234FIZ5")                       # 1 read as I
        self.assertEqual((i["kind"], i["status"], i["vid"]), ("recovered", "warn", "V001"))
        self.assertIn("only in characters OCR confuses", i["detail"]); self.assertIn("'I' read for '1'", i["detail"])

    def test_same_confusable_difference_in_a_digital_pdf_is_a_conflict(self):
        self.assertEqual(self.ident(False, gstin="07AABCA1234FIZ5")["kind"], "conflict")     # text layers are exact: no OCR excuse

    def test_ocr_difference_that_is_not_confusable_is_a_conflict(self):
        self.assertEqual(self.ident(True, gstin="07AABCA9999F1Z5")["kind"], "conflict")

    def test_ocr_confusable_but_uncorroborated_is_uncertain(self):
        i = self.ident(True, po=None, gstin="07AABCA1234FIZ5", bank="99999999999999", ifsc="HDFC0000000")
        self.assertEqual((i["kind"], i["status"]), ("uncertain", "unverified"))
        j = self.server.resolve_vendor({"vendor": V1["vendor"], "gstin": "07AABCA1234FIZ5", "bank": "1", "ifsc": "x"}, True, None)
        self.assertEqual(j["kind"], "uncertain")

    def test_garbled_unreadable_gstin_needs_two_corroborations(self):
        self.assertEqual(self.ident(True, gstin="07AAB?A12")["kind"], "recovered")
        self.assertEqual(self.ident(True, gstin="07AAB?A12", bank=None, ifsc=None)["kind"], "uncertain")

    def test_confusable_helper(self):
        c = self.server.confusable_diff
        self.assertEqual(c("0O1I", "0O1I"), None); self.assertTrue(c("07AABCA1234F1Z5", "O7AABCA1234FIZ5"))
        self.assertIsNone(c("07AABCA1234F1Z5", "07AABCA1234F1Z9")); self.assertIsNone(c("ABC", "ABCD"))


if __name__ == "__main__":
    unittest.main()
