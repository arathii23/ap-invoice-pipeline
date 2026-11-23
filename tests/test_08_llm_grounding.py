"""Defect 8: LLM output is accepted only when it is the right type, a whole token in the right field context, and (for line items)
a row that really exists in the document. Grounded presence never bypasses business validation. Everything here runs against a MOCK
provider on localhost - it proves our grounding and error handling, NOT the real provider integration (see test_llm_real.py)."""
import json, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from helpers import AppTestCase, make_invoice, make_po, PO_ID


class Provider:
    """Mock Messages API. mode: json | text | raw | status | empty"""
    def __init__(self):
        self.mode, self.payload, self.calls = "json", {}, 0
        outer = self
        class Hd(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"])); outer.calls += 1
                if outer.mode == "status": self.send_response(500); self.end_headers(); return
                body = {"json": lambda: json.dumps({"content": [{"type": "text", "text": "Sure:\n" + json.dumps(outer.payload)}]}),
                        "text": lambda: json.dumps({"content": [{"type": "text", "text": outer.payload}]}),
                        "raw": lambda: outer.payload, "empty": lambda: json.dumps({"content": []}),
                        "nocontent": lambda: json.dumps({"id": "x"})}[outer.mode]()
                body = body if isinstance(body, bytes) else body.encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        self.srv = HTTPServer(("127.0.0.1", 0), Hd); self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
    def stop(self): self.srv.shutdown(); self.srv.server_close()


G = lambda self: self.server.ground_field

# A document whose layout the rules cannot read (no table header): the LLM is consulted for bank + line items.
def odd_invoice(n, line="Hydraulic pump HP-200 - 10 pcs at 12,000.00 each = 120,000.00", **kw):
    return make_invoice(invoice_no=f"INV-H-{n:04d}", bank=None, ifsc=None, lines=[], subtotal=120000.0, extra_lines=["Order ref 999123999", line], **kw)


class LlmGrounding(AppTestCase):
    def setUp(self):
        self.prov = Provider(); self.addCleanup(self.prov.stop)
        self.app_env = {"ANTHROPIC_API_KEY": "test-key", "ANTHROPIC_BASE_URL": self.prov.url, "AP_LLM": "auto"}
        super().setUp()

    def run_with(self, payload, n=1, mode="json", **kw):
        self.prov.mode, self.prov.payload = mode, payload
        return self.app.submit(odd_invoice(n, **kw), f"llm{n}.pdf")

    # ---- unit: field grounding
    def test_numeric_substring_is_not_a_match(self):
        g = self.server.ground_field
        self.assertFalse(g("bank", "123", "Order ref 999123999")[0])                        # the reported case
        self.assertFalse(g("bank", "999123999", "Order ref 999123999")[0])                  # whole token, but not an account number in an account context
        self.assertTrue(g("bank", "999123999", "Settlement A/C No: 999123999")[0])
        self.assertFalse(g("total", 1200, "Total Due 11,200.00")[0]); self.assertTrue(g("total", 11200, "Total Due 11,200.00")[0])
        self.assertTrue(g("total", 112000, "Grand Total: Rs.1,12,000.00")[0])               # Indian grouping and Rs. prefix
        self.assertFalse(g("total", 99, "Total Due 99.50")[0]); self.assertFalse(g("total", 9.5, "Total Due 99.50")[0])
        self.assertFalse(g("invoice_no", "BL-2026-1301", "Doc ref BL-2026-1301-A issued")[0])   # prefix of a longer id
        self.assertTrue(g("invoice_no", "bl-2026-1301", "Doc ref BL-2026-1301 issued")[1] == "BL-2026-1301")  # document spelling is stored

    def test_field_context_is_required(self):
        g = self.server.ground_field
        self.assertFalse(g("total", 11200, "Subtotal 11,200.00")[0])                         # value exists, but under the wrong label
        self.assertFalse(g("subtotal", 500, "Freight 500.00")[0])
        self.assertTrue(g("tax", 1800, "IGST @ 18% 1,800.00")[0]); self.assertFalse(g("tax", 5000, "Taxable Value 5,000.00")[0])
        self.assertFalse(g("ifsc", "HDFC0001234", "Branch code HDFC0001234")[0]); self.assertTrue(g("ifsc", "HDFC0001234", "IFSC: HDFC0001234")[0])

    def test_wrong_types_are_rejected(self):
        g = self.server.ground_field; doc = "Invoice No: INV-1\nSubtotal 100.00\nTotal Due 118.00\nA/C No: 123456789012"
        for k, v in (("subtotal", "100.00"), ("total", [118.0]), ("tax", True), ("tax", None), ("total", float("nan")), ("subtotal", {"v": 100}),
                     ("invoice_no", 1), ("bank", 123456789012), ("ifsc", ["HDFC0001234"]), ("vendor", 5), ("po_ref", {}), ("gstin", 7)):
            self.assertFalse(g(k, v, doc)[0], (k, v))
        self.assertTrue(g("subtotal", 100, doc)[0]); self.assertTrue(g("bank", "123456789012", doc)[0])

    def test_line_rows_must_exist_on_one_line_of_the_document(self):
        gl = self.server.ground_lines; doc = "Hydraulic pump HP-200 - 10 pcs at 12,000.00 each = 120,000.00\nSeal kit SK-11 - 5 pcs at 2,000.00 = 10,000.00"
        good = [{"desc": "Hydraulic pump HP-200", "qty": 10, "unit": 12000, "amount": 120000}, {"desc": "Seal kit SK-11", "qty": 5, "unit": 2000, "amount": 10000}]
        self.assertEqual(len(gl(good, doc)[0]), 2)
        for bad in ([dict(good[0], qty=999)], [dict(good[0], unit=77777)], [dict(good[0], amount=130000)], [dict(good[0], desc="Hydraulic pump HP-999")],
                    [good[0], dict(good[0])],                                            # the same document line cannot back two rows
                    [good[0], dict(good[1], qty=6)], [dict(good[0], qty="10")], [dict(good[0], amount="120000")], ["not a dict"], "not a list", [], [dict(good[0], qty=-1)]):
            self.assertIsNone(gl(bad, doc)[0], bad)                                    # all-or-nothing: one bad row rejects the lot
        self.assertIsNone(gl(good * 120, doc)[0])                                      # cap on rows

    # ---- end to end through the real upload route, mock provider
    def test_substring_account_number_is_discarded(self):
        r = self.run_with({"bank": "123"})
        llm = r["extraction"]["llm"]
        self.assertTrue(llm["used"]); self.assertIn("bank", llm["rejected"]); self.assertIsNone(r["extracted"].get("bank"))
        self.assertIn("9-18 digits", llm["rejected_reasons"]["bank"])
        self.assertIn("discarded: bank", self.stage(r, "extract")["detail"])

    def test_invented_line_quantities_and_prices_are_discarded(self):
        r = self.run_with({"lines": [{"desc": "Hydraulic pump HP-200", "qty": 999, "unit": 77777, "amount": 120000}]}, 2)
        self.assertEqual(r["extracted"]["lines"], []); self.assertIn("line items", r["extraction"]["llm"]["rejected"])
        self.assertEqual(r["decision"], "APPROVE")                                       # bundled-style: subtotal+tax+total still reconcile; nothing invented was used

    def test_valid_grounded_extraction_is_used_and_the_rules_still_decide(self):
        r = self.run_with({"lines": [{"desc": "Hydraulic pump HP-200", "qty": 10, "unit": 12000, "amount": 120000}], "bank": None}, 3)
        self.assertEqual(len(r["extracted"]["lines"]), 1); self.assertIn("line items", r["extraction"]["llm"]["filled"])
        self.assertEqual((r["decision"], r["po"]), ("APPROVE", "PO-2026-1001"), r["reasoning"])
        self.assertEqual(self.stage(r, "math")["status"], "pass")

    def test_grounded_values_do_not_bypass_business_validation(self):
        line = "Hydraulic pump HP-200 - 999 pcs at 12,000.00 each = 120,000.00"        # 999 x 12,000 != 120,000, and it IS printed that way
        r = self.run_with({"lines": [{"desc": "Hydraulic pump HP-200", "qty": 999, "unit": 12000, "amount": 120000}]}, 4, line=line)
        self.assertIn("line items", r["extraction"]["llm"]["filled"])                   # grounded, so accepted as extraction...
        self.assertEqual(r["decision"], "HOLD"); self.assertEqual(self.stage(r, "math")["status"], "fail_hold")   # ...and still caught by the rules

    def test_malformed_responses_are_handled_safely(self):
        cases = [("text", "I'm sorry, I can't help with that."), ("text", '["a", "list"]'), ("text", '{"bank": "5020001'), ("text", "null"),
                 ("raw", b"<html>502 Bad Gateway</html>"), ("empty", None), ("nocontent", None), ("raw", b'{"content": "a string"}')]
        for i, (mode, payload) in enumerate(cases):
            r = self.run_with(payload, 10 + i, mode)
            llm = r["extraction"]["llm"]
            self.assertEqual(r["status"], "done", (mode, payload)); self.assertFalse(r.get("error"), (mode, payload, r.get("error")))
            self.assertFalse(llm["used"], (mode, payload)); self.assertTrue(llm["error"], (mode, payload))
            self.assertEqual(r["extracted"]["lines"], []); self.assertIsNone(r["extracted"].get("bank"))
            self.assertIn("continued with rules only", self.stage(r, "extract")["detail"])

    def test_provider_outages_do_not_break_the_run(self):
        r = self.run_with({}, 30, "status"); self.assertEqual(r["status"], "done"); self.assertIn("HTTPError", r["extraction"]["llm"]["error"])
        self.prov.stop()
        r = self.app.submit(odd_invoice(31), "down.pdf"); self.assertEqual(r["status"], "done"); self.assertTrue(r["extraction"]["llm"]["error"])
        self.assertIsNotNone(r["decision"])

    def test_llm_is_not_called_when_the_rules_read_everything(self):
        r = self.app.submit(make_invoice(invoice_no="INV-H-0099"), "clean.pdf")
        self.assertEqual(self.prov.calls, 0); self.assertFalse(r["extraction"]["llm"]["used"]); self.assertEqual(r["decision"], "APPROVE")


class LlmOff(AppTestCase):
    app_env = {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "AP_LLM": "off"}

    def test_off_means_off_even_with_a_key(self):
        r = self.app.submit(odd_invoice(1), "off.pdf")
        self.assertFalse(r["extraction"]["llm"]["configured"]); self.assertFalse(r["extraction"]["llm"]["used"])


if __name__ == "__main__":
    unittest.main()
