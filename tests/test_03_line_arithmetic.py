"""Defect 3: quantity x unit price is validated against each line amount, and lines -> subtotal -> tax -> total reconcile,
with an explicit rounding allowance (RULES: 1.00 per figure, or qty x 0.005 per line when larger)."""
import unittest
from helpers import AppTestCase, make_invoice, make_po, PO_ID


def ex(lines, sub=None, tax=None, tot=None, rate=18):
    sub = sum(l[3] for l in lines) if sub is None else sub
    tax = round(sub * rate / 100, 2) if tax is None else tax
    return {"lines": [{"desc": d, "qty": q, "unit": u, "amount": a} for d, q, u, a in lines], "subtotal": sub, "tax": tax, "tax_rate": rate,
            "total": sub + tax if tot is None else tot}


class LineArithmeticUnit(AppTestCase):
    def check(self, e): return self.server.check_arithmetic(e)

    def test_clean_invoice_passes(self):
        st, d, rows = self.check(ex([("Pump", 10, 12000.0, 120000.0), ("Seal", 5, 2000.0, 10000.0)]))
        self.assertEqual(st, "pass"); self.assertIn("rounding allowance ₹1.00", d)

    def test_quantity_times_unit_price_mismatch_fails(self):
        st, d, _ = self.check(ex([("Pump", 999, 12000.0, 120000.0)]))
        self.assertEqual(st, "fail_hold"); self.assertIn("999 × ₹12,000.00 = ₹11,988,000.00", d)

    def test_rounding_allowance_boundaries(self):
        self.assertEqual(self.check(ex([("Widget", 3, 33.33, 100.00)]))[0], "pass")           # 99.99 vs 100.00: one paisa of rounding
        self.assertEqual(self.check(ex([("Widget", 1, 100.00, 101.00)]))[0], "pass")           # exactly 1.00 off: inclusive
        self.assertEqual(self.check(ex([("Widget", 1, 100.00, 101.01)]))[0], "fail_hold")      # 1.01 off: outside the allowance
        st, _, _ = self.check(ex([("Widget", 1, 100.00, 100.00)], sub=101.00, tax=18.18, tot=119.18))
        self.assertEqual(st, "pass", "subtotal may differ from the line sum by up to ₹1.00")
        st, _, _ = self.check(ex([("Widget", 1, 100.00, 100.00)], sub=101.01, tax=18.18, tot=119.19))
        self.assertEqual(st, "fail_hold", "₹1.01 is outside the allowance")

    def test_line_allowance_scales_with_quantity(self):
        self.assertEqual(self.check(ex([("Bulk", 400, 12.34, 4937.50)]))[0], "pass")            # 4936.00 vs 4937.50 = 1.50 <= max(1.00, 400*0.005=2.00)
        self.assertEqual(self.check(ex([("Bulk", 400, 12.34, 4938.01)]))[0], "fail_hold")       # 2.01 > 2.00

    def test_sum_of_lines_vs_subtotal(self):
        st, d, _ = self.check(ex([("A", 1, 100.0, 100.0), ("B", 1, 200.0, 200.0)], sub=350.0))
        self.assertEqual(st, "fail_hold"); self.assertIn("line items sum to ₹300.00 but the subtotal is ₹350.00", d)

    def test_tax_and_total_must_reconcile(self):
        self.assertIn("should be ₹180.00", self.check(ex([("A", 1, 1000.0, 1000.0)], tax=180.0 * 5))[1])
        st, d, _ = self.check(ex([("A", 1, 1000.0, 1000.0)], tot=1200.0))
        self.assertEqual(st, "fail_hold"); self.assertIn("subtotal + tax = ₹1,180.00 but the total is ₹1,200.00", d)

    def test_unreadable_quantity_is_not_verified_not_passed(self):
        e = ex([("A", 1, 100.0, 100.0)]); e["lines"][0]["qty"] = None
        st, d, rows = self.check(e)
        self.assertEqual(st, "unverified"); self.assertIn("no readable quantity", d); self.assertIsNone(rows[0]["ok"])

    def test_multi_tax_rate_sums(self):
        self.assertEqual(self.check(ex([("A", 1, 1000.0, 1000.0)], rate=18, tax=180.0))[0], "pass")   # e.g. CGST 9 + SGST 9 read as 18


class LineArithmeticEndToEnd(AppTestCase):
    def test_reported_case_999_pumps_printed_as_120000_is_held(self):
        r = self.app.submit(make_invoice(invoice_no="INV-L-0001", lines=[("Hydraulic pump HP-200", 999, 12000.0, 120000.0)]), "l.pdf")
        self.assertEqual(r["decision"], "HOLD", r["reasoning"])
        self.assertEqual(self.stage(r, "math")["status"], "fail_hold")
        self.assertIn("999", self.stage(r, "math")["detail"])
        self.assertEqual(r["headline"], "Invoice arithmetic does not reconcile — review needed")

    def test_valid_rounding_invoice_is_approved(self):
        self.patch_pos(**{PO_ID: make_po(net=100000)})
        r = self.app.submit(make_invoice(invoice_no="INV-L-0002", po=PO_ID, lines=[("Custom job", 3, 33.33, 100.00)]), "round.pdf")
        self.assertEqual(r["decision"], "APPROVE", r["reasoning"])

    def test_sample_invoices_reconcile(self):
        for n in ("01_acme_clean.pdf", "10_acme_multipage.pdf", "11_greenfield_alt_layout.pdf"):
            self.assertEqual(self.stage(self.app.submit_sample(n), "math")["status"], "pass", n)


if __name__ == "__main__":
    unittest.main()
