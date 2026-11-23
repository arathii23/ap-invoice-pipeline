"""OPT-IN test against the REAL Anthropic API. Skipped unless you explicitly enable it (it spends real API calls):

    AP_TEST_REAL_LLM=1 ANTHROPIC_API_KEY=sk-... python3 -m unittest tests.test_llm_real -v      (or: python3 tests/test_llm_real.py)

Everything else in this suite uses a MOCK provider and proves only our grounding/validation/error handling - not that the real
provider accepts our request or returns what we expect. Nothing here asserts what the model says, only that whatever it returns is
either grounded in the document or discarded, and that the pipeline completes."""
import os, unittest
from helpers import AppTestCase, make_invoice

KEY_ENV = {k: os.environ[k] for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "AP_LLM_MODEL") if k in os.environ}
ENABLED = os.environ.get("AP_TEST_REAL_LLM") == "1" and ("ANTHROPIC_API_KEY" in KEY_ENV or "ANTHROPIC_AUTH_TOKEN" in KEY_ENV)


@unittest.skipUnless(ENABLED, "opt-in: set AP_TEST_REAL_LLM=1 and ANTHROPIC_API_KEY to call the real provider")
class RealProvider(AppTestCase):
    app_env = {**KEY_ENV, "AP_LLM": "always"}

    def test_real_provider_round_trip(self):
        pdf = make_invoice(invoice_no="INV-REAL-0001", bank=None, ifsc=None, lines=[], subtotal=120000.0,
                           extra_lines=["Hydraulic pump HP-200 - 10 pcs at 12,000.00 each = 120,000.00", "Settlement A/C No: 50200012345678"])
        r = self.app.submit(pdf, "real.pdf")
        llm = r["extraction"]["llm"]
        print("\n[REAL PROVIDER]", {k: llm[k] for k in ("used", "filled", "rejected", "error")})
        self.assertEqual(r["status"], "done"); self.assertTrue(llm["used"] or llm["error"], "the provider was neither used nor reported unavailable")
        text = r["raw_text"].replace(",", "")
        for k in llm["filled"]:
            if k in ("bank", "invoice_no", "gstin", "ifsc", "po_ref"): self.assertIn(str(r["extracted"][k]).replace(",", ""), text, k)
        self.assertIsNotNone(r["decision"])


if __name__ == "__main__":
    unittest.main()
