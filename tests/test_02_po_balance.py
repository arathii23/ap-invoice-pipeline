"""Defect 2: a fully consumed PO must not keep approving. Tolerance is enforced, zero/negative balances are explicit,
legitimate partial and milestone billing still works."""
import threading, unittest
from helpers import AppTestCase, make_invoice, make_po, PO_ID


class PoBalance(AppTestCase):
    def test_reported_case_second_invoice_after_full_consumption_is_held(self):
        r1 = self.app.submit(make_invoice(invoice_no="INV-C-0001"), "c1.pdf")           # 130,000 net == PO-2026-1001 net
        r2 = self.app.submit(make_invoice(invoice_no="INV-C-0002"), "c2.pdf")
        self.assertEqual(r1["decision"], "APPROVE")
        self.assertEqual(r2["decision"], "HOLD", r2["reasoning"])
        d = self.stage(r2, "amount")
        self.assertEqual(d["status"], "fail_hold"); self.assertEqual(d["extra"]["kind"], "exhausted")
        self.assertIn("no remaining value", d["detail"])
        self.assertEqual(r2["headline"], "PO has no remaining value — buyer review needed")
        self.assertEqual(sum(x["extracted"]["subtotal"] for x in self.app.runs() if x["decision"] == "APPROVE"), 130000.0)

    def test_many_positive_invoices_after_exhaustion_all_held(self):
        self.patch_pos(**{PO_ID: make_po(net=100000)})
        self.assertEqual(self.invoice(1, 100000).get("decision"), "APPROVE")
        for n in range(2, 6):
            self.assertEqual(self.invoice(n, 1000 * n)["decision"], "HOLD", n)
        self.assertLessEqual(self.approved_net(), 100000 * 1.02)

    def test_tolerance_boundary_is_inclusive_and_exact(self):
        self.patch_pos(**{PO_ID: make_po(net=100000)})                      # 2% tolerance -> limit 102,000.00
        r = self.invoice(1, 102000.00)                                      # exactly at the limit
        self.assertEqual(r["decision"], "APPROVE", r["reasoning"])

    def test_one_paisa_over_the_limit_is_held(self):
        self.patch_pos(**{PO_ID: make_po(net=100000)})
        r = self.invoice(2, 102000.01)
        self.assertEqual(r["decision"], "HOLD", r["reasoning"])
        self.assertEqual(self.stage(r, "amount")["extra"]["kind"], "variance")
        self.assertIn("at most ₹102,000.00", self.stage(r, "amount")["detail"])

    def test_negative_remaining_after_tolerance_overrun_blocks_the_next_invoice(self):
        self.patch_pos(**{PO_ID: make_po(net=100000)})
        r1 = self.invoice(1, 102000.00)                                      # allowed by tolerance, leaves remaining = -2,000
        self.assertEqual(r1["decision"], "APPROVE")
        self.assertIn("no value left", self.stage(r1, "amount")["detail"])
        r2 = self.invoice(2, 1.00)
        self.assertEqual(r2["decision"], "HOLD")
        self.assertIn("over-approved by ₹2,000.00", self.stage(r2, "amount")["detail"])
        self.assertEqual(self.app.get("/api/dashboard")[1]["billed"][PO_ID], 102000.0)

    def test_partial_and_milestone_billing_still_works(self):
        self.patch_pos(**{PO_ID: make_po(net=100000)})
        r1, r2 = self.invoice(1, 40000), self.invoice(2, 60000)
        self.assertEqual((r1["decision"], r2["decision"]), ("APPROVE", "APPROVE"), r2["reasoning"])
        self.assertIn("partial billing", self.stage(r1, "amount")["detail"])
        self.assertEqual(self.invoice(3, 1000)["decision"], "HOLD")

    def test_registered_opening_billed_amount_counts(self):
        self.patch_pos(**{PO_ID: make_po(net=100000, billed=100000)})
        r = self.invoice(1, 5000)
        self.assertEqual(r["decision"], "HOLD"); self.assertEqual(self.stage(r, "amount")["extra"]["kind"], "exhausted")

    def test_sample_milestones_split_a_po_in_two(self):
        a, b = self.app.submit_sample("03_novatech_milestone1.pdf"), self.app.submit_sample("04_novatech_milestone2.pdf")
        self.assertEqual((a["decision"], b["decision"]), ("APPROVE", "APPROVE"))
        self.assertEqual(self.app.submit(make_invoice(vendor="NovaTech Software Services", gstin="29AAECN9012M1Z8", invoice_no="NT-9999", po="PO-2026-1003",
                         lines=[("Milestone 3 - extra", 1, 10000.0, 10000.0)], bank="91702003456789", ifsc="UTIB0002345"), "extra.pdf")["decision"], "HOLD")

    def test_non_positive_amounts_are_held(self):
        self.patch_pos(**{PO_ID: make_po(net=100000)})
        r = self.app.submit(make_invoice(invoice_no="INV-NEG-1", po=PO_ID, lines=[("Credit adjustment", 1, 500.0, 500.0)], subtotal=-500.0, tax=-90.0, total=-590.0), "neg.pdf")
        self.assertEqual(r["decision"], "HOLD")

    def test_stress_parallel_invoices_never_exceed_policy(self):
        self.patch_pos(**{PO_ID: make_po(net=100000)})
        out = []
        ts = [threading.Thread(target=lambda n=n: out.append(self.invoice(n, 40000))) for n in range(1, 8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(sorted(r["decision"] for r in out).count("APPROVE"), 2)          # 40k + 40k; the third (40k > 20k remaining +2%) is held
        self.assertLessEqual(self.approved_net(), 100000 * 1.02)


if __name__ == "__main__":
    unittest.main()
