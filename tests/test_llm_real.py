"""OPT-IN tests against the REAL providers (Sarvam, Gemini, Anthropic). Skipped unless you explicitly enable them - they spend real API calls:

    AP_TEST_REAL_LLM=1 SARVAM_API_KEY=... GEMINI_API_KEY=... python3 -m unittest tests.test_llm_real -v

Everything else in this suite uses MOCK providers and proves only our request shape, fallback, grounding and error handling - not that the
real services accept our requests or answer in the shape we expect. Nothing here asserts WHAT a model says, only that whatever it returns is
either grounded in the document or discarded, and that the pipeline completes. (Quick manual check without the test runner: python3 tools/check_llm.py)"""
import os, unittest
from helpers import AppTestCase, make_invoice

KEYS = ("SARVAM_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "AP_SARVAM_MODEL", "AP_GEMINI_MODEL", "AP_LLM_MODEL")
KEY_ENV = {k: os.environ[k] for k in KEYS if os.environ.get(k)}
PROVIDERS = [n for n, ks in (("sarvam", ("SARVAM_API_KEY",)), ("gemini", ("GEMINI_API_KEY", "GOOGLE_API_KEY")), ("anthropic", ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))) if any(k in KEY_ENV for k in ks)]
ENABLED = os.environ.get("AP_TEST_REAL_LLM") == "1" and bool(PROVIDERS)


def odd_pdf(n):
    return make_invoice(invoice_no=f"INV-REAL-{n:04d}", bank=None, ifsc=None, lines=[], subtotal=120000.0,
                        extra_lines=["Hydraulic pump HP-200 - 10 pcs at 12,000.00 each = 120,000.00", "Payee Acct: 50200012345678"])


@unittest.skipUnless(ENABLED, "opt-in: set AP_TEST_REAL_LLM=1 and at least one of SARVAM_API_KEY / GEMINI_API_KEY / ANTHROPIC_API_KEY")
class RealProviders(AppTestCase):
    app_env = {**KEY_ENV, "AP_LLM": "always"}

    def check(self, r, expect_provider=None):
        llm = r["extraction"]["llm"]
        print("\n[REAL]", {k: llm[k] for k in ("used", "provider", "filled", "rejected", "error")}, [(a["provider"], a["outcome"]) for a in llm["attempts"]])
        self.assertEqual(r["status"], "done"); self.assertTrue(llm["used"] or llm["error"], "the provider was neither used nor reported unavailable")
        if expect_provider and llm["used"]: self.assertEqual(llm["provider"], expect_provider)
        text = r["raw_text"].replace(",", "")
        for k in llm["filled"]:
            if k in ("bank", "invoice_no", "gstin", "ifsc", "po_ref"): self.assertIn(str(r["extracted"][k]).replace(",", ""), text, k)
        self.assertIsNotNone(r["decision"]); return llm

    def test_the_configured_chain_end_to_end(self):
        self.check(self.app.submit(odd_pdf(1), "real-chain.pdf"))

    def test_each_configured_provider_on_its_own(self):
        for n, name in enumerate(PROVIDERS, 10):
            with self.subTest(provider=name):
                os.environ["AP_LLM_PROVIDERS"] = name; self.server.reset_llm_state()
                try: self.check(self.app.submit(odd_pdf(n), f"real-{name}.pdf"), name)
                finally: os.environ.pop("AP_LLM_PROVIDERS", None)


if __name__ == "__main__":
    unittest.main()
