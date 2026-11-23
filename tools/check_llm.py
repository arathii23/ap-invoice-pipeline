#!/usr/bin/env python3
"""Checks your REAL LLM keys, one provider at a time, with the exact request the app makes. Spends a handful of tokens.

    SARVAM_API_KEY=... GEMINI_API_KEY=... python3 tools/check_llm.py

Prints for each configured provider: OK + what it returned, or the reason it failed (keys are never printed). Run it locally, or in the
Render shell. It reads the keys from the environment only; nothing is stored."""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.setdefault("AP_LLM", "always")
import server

DOC = """Acme Industrial Supplies Pvt Ltd
GSTIN: 07AABCA1234F1Z5
TAX INVOICE
Invoice No: INV-CHK-0001
Invoice Date: 05 Nov 2026
PO Reference: PO-2026-1001
Subtotal 120,000.00
GST @ 18% 21,600.00
Total Due 141,600.00
Payee Acct: 50200012345678   IFSC: HDFC0001234
"""


def main():
    names = server.active_providers()
    if not names:
        print("No provider has a key. Set SARVAM_API_KEY and/or GEMINI_API_KEY (or ANTHROPIC_API_KEY) and run again."); return 2
    bad = 0
    for name in names:
        server.reset_llm_state(); t = time.time()
        try:
            got = server._CALLERS[name](server.provider_key(name), server.LLM_PROMPT + DOC)
        except Exception as e:
            bad += 1; print(f"[{name:9}] FAILED  ({server.provider_model(name)}): {server._scrub(e)}"); continue
        ok = {k: server.ground_field(k, got.get(k), DOC)[0] for k in ("invoice_no", "bank", "ifsc", "total") if got.get(k) not in (None, "")}
        print(f"[{name:9}] OK      ({server.provider_model(name)}, {time.time() - t:.1f}s) fields returned: {sorted(k for k, v in got.items() if v not in (None, '', []))}")
        print(f"            grounded in the document: {ok}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
