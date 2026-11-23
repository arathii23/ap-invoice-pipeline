"""Defect 5: duplicate check + PO-balance validation + persistence are one protected operation.
Deterministic synchronisation: server.HOOKS pause the first request INSIDE the critical section, after its decision is made
but before it commits; the second request is started while it is paused and must then see the first request's committed decision."""
import threading, time, unittest
from helpers import AppTestCase, make_invoice, make_po, PO_ID


class Gate:
    """Deterministic ordering with server.HOOKS (no sleeps decide the outcome):
      1. both runs reach 'before_critical' (extraction done, critical section not yet entered);
      2. run A enters the critical section first and PAUSES inside it, decision made but not committed;
      3. run B is released only then, so it must block on the critical section until A commits."""
    def __init__(self, server, a, b):
        self.a_ready, self.b_ready, self.a_in, self.release = (threading.Event() for _ in range(4))
        def before(run):
            if run["filename"] == a: self.a_ready.set(); assert self.b_ready.wait(30)
            elif run["filename"] == b: self.b_ready.set(); assert self.a_in.wait(30)
        def in_crit(run):
            if run["filename"] == a: self.a_in.set(); assert self.release.wait(30)
        server.HOOKS["before_critical"] = before; server.HOOKS["in_critical"] = in_crit


class Concurrency(AppTestCase):
    def race(self, first, second):
        """first/second = (pdf, filename). B starts while A is paused inside the critical section. Returns (run_a, run_b)."""
        gate, out = Gate(self.server, first[1], second[1]), {}
        ta = threading.Thread(target=lambda: out.update(a=self.app.submit(*first))); ta.start()
        self.assertTrue(gate.a_ready.wait(30))
        tb = threading.Thread(target=lambda: out.update(b=self.app.submit(*second))); tb.start()
        self.assertTrue(gate.a_in.wait(30))                                   # A is inside the critical section, NOT committed
        self.assertEqual(self.app.runs(), [], "nothing is visible before the first commit")
        time.sleep(0.3)                                                        # B is now blocked on the critical section
        self.assertNotIn("b", out)
        gate.release.set(); ta.join(30); tb.join(30)
        return out["a"], out["b"]

    def test_identical_invoices_sent_at_the_same_time_are_approved_once(self):
        pdf = make_invoice(invoice_no="INV-X-0001")
        a, b = self.race((pdf, "first.pdf"), (pdf, "second.pdf"))
        self.assertEqual((a["decision"], b["decision"]), ("APPROVE", "REJECT"), b["reasoning"])
        self.assertTrue(b["is_duplicate"]); self.assertIn(a["id"], self.stage(b, "duplicate")["detail"])
        self.assertEqual([r["decision"] for r in self.app.runs()].count("APPROVE"), 1)

    def test_same_invoice_number_different_bytes_is_still_one_approval(self):
        a, b = self.race((make_invoice(invoice_no="INV-X-0002"), "a.pdf"), (make_invoice(invoice_no="INV-X-0002", extra_lines=["Thank you"]), "b.pdf"))
        self.assertEqual((a["decision"], b["decision"]), ("APPROVE", "REJECT"))

    def test_different_invoices_competing_for_the_same_po_balance(self):
        self.patch_pos(**{PO_ID: make_po(net=100000)})
        mk = lambda n: make_invoice(invoice_no=f"INV-X-{n:04d}", po=PO_ID, lines=[("Custom job", 1, 100000.0, 100000.0)])
        a, b = self.race((mk(3), "a.pdf"), (mk(4), "b.pdf"))
        self.assertEqual((a["decision"], b["decision"]), ("APPROVE", "HOLD"), b["reasoning"])
        self.assertEqual(self.stage(b, "amount")["extra"]["kind"], "exhausted")
        self.assertLessEqual(self.approved_net(), 100000 * 1.02)

    def test_competing_invoices_that_fit_together_are_both_approved(self):
        self.patch_pos(**{PO_ID: make_po(net=100000)})
        mk = lambda n, v: make_invoice(invoice_no=f"INV-X-{n:04d}", po=PO_ID, lines=[("Custom job", 1, v, v)])
        a, b = self.race((mk(5, 40000.0), "a.pdf"), (mk(6, 50000.0), "b.pdf"))
        self.assertEqual((a["decision"], b["decision"]), ("APPROVE", "APPROVE"), b["reasoning"])
        self.assertIn("₹10,000.00 stays open", self.stage(b, "amount")["detail"])

    def test_failure_inside_the_critical_section_commits_nothing_partial(self):
        def boom(run):
            if run["filename"] == "boom.pdf": raise RuntimeError("disk on fire")
        self.server.HOOKS["in_critical"] = boom
        pdf = make_invoice(invoice_no="INV-X-0007")
        r = self.app.submit(pdf, "boom.pdf")
        self.assertEqual(r["decision"], "HOLD"); self.assertIn("disk on fire", r["error"])
        rows = self.app.runs(); self.assertEqual(len(rows), 1); self.assertTrue(rows[0].get("error"))   # only the failed-run record, no half-written approval
        self.server.HOOKS.clear()
        again = self.app.submit(pdf, "again.pdf")                                                         # a failed run is not a prior decision
        self.assertEqual(again["decision"], "APPROVE")

    def test_unsynchronised_parallel_identical_submissions_approve_once(self):
        out = []
        ts = [threading.Thread(target=lambda n=n: out.append(self.app.submit(make_invoice(invoice_no="INV-X-0008"), f"p{n}.pdf"))) for n in range(6)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(sorted(r["decision"] for r in out), ["APPROVE"] + ["REJECT"] * 5)


if __name__ == "__main__":
    unittest.main()
