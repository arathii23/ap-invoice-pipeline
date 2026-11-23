# AP Invoice Pipeline (PS-1)

PDF invoice → extract (text layer / OCR / optional LLM) → match PO → validate → **APPROVE / HOLD / REJECT** with reasons a finance manager can read.

> **Delivery status, plainly.** This is a *local* demo. `http://localhost:8765` only works on the machine running it. There is **no public link yet**,
> **no demo video yet**, the Dockerfile has **never been built**, and the optional LLM path has **only been tested against a mock**. Details in [Verification status](#verification-status).

## Run
```
pip3 install -r requirements.txt
python3 server.py            # http://localhost:8765   (HOST / PORT env vars supported; default HOST is 127.0.0.1)
```
Data lives in `data/` (`runs.db`, `uploads/`) next to the reference files `pos.json` and `vendors.json`.

| Environment variable | Default | Meaning |
|---|---|---|
| `PORT`, `HOST` | `8765`, `127.0.0.1` | where the server listens |
| `AP_DB`, `AP_UPLOADS` | `data/runs.db`, `data/uploads/` | database and stored-document folder (tests and demos use temp locations) |
| `AP_ALLOW_RESET` | unset (**disabled**) | `1` enables `POST /api/reset` and the *Clear history* button (a SQLite backup is written to `<db folder>/reset-backups/` first). Leave it off for anything shared. |
| `STAGE_DELAY` | `0.55` | seconds between stages on the live run view (cosmetic; the decision is already committed) |
| `AP_OCR_ENGINE` | auto | force an OCR engine |
| `ANTHROPIC_API_KEY`, `AP_LLM` (`auto`/`always`/`off`), `AP_LLM_MODEL` | no key → LLM off | optional LLM field-filling |

## Recording a demo (fresh data, live history untouched)
`Clear history` is off by default, so record on a fresh, separate data set instead of wiping anything:
```
AP_DB=/tmp/demo/runs.db AP_UPLOADS=/tmp/demo/uploads PORT=8770 python3 server.py     # mkdir -p /tmp/demo first
```
Suggested order (this exact order is asserted in `tests/test_12_infrastructure.py`):
1. 01 Clean PDF → Approved ("eligible for payment processing")
2. 03 Milestone 1 → Approved (partial billing), later 04 Milestone 2 → Approved (PO fully billed)
3. 05 Duplicate re-send → Rejected ("do not pay twice")
4. 07 Price variance → On hold · 09 Bank details changed → On hold · 06 Missing fields → On hold
5. 08 Implied PO → Approved (PO inferred, flagged) · 10 Two-page · 11 Different layout → Approved
6. 02 Scanned image → Approved via OCR
7. Upload your own corrected copy of a held invoice → *Related submissions* card links it to the held run, nothing is approved twice.
Restart the server and re-run 01: rejected as a duplicate (history and documents persist).

## How decisions are made
The model never decides. Extraction (rules, OCR, optional LLM) produces fields; deterministic checks decide. **Every required check must have actually run and passed** — a check that could not run shows **Not verified** and holds the invoice; it is never shown as "Passed".

| Situation | Result |
|---|---|
| Subtotal / tax / total missing, so amounts cannot be checked | HOLD — "Amounts could not be verified" (nothing is inferred from the total) |
| PO has no remaining value (or invoice would exceed remaining + 2 % tolerance) | HOLD — remaining balance = PO value − opening billed − approved runs, in exact decimals; partial and milestone billing still approve |
| Line quantity × unit price ≠ line amount, lines ≠ subtotal, tax ≠ rate × subtotal, subtotal + tax ≠ total | HOLD — "Invoice arithmetic does not reconcile" (allowance: the larger of ₹1.00 and ₹0.005 × quantity on a line, ₹1.00 on sums; invoices without itemised lines are bundled invoices and skip line checks, stated on the stage) |
| GSTIN missing/unreadable | not a conflict — recovered only if **≥ 2 independent signals** agree (bank account, IFSC, PO owner) and only for digital-vs-OCR confusable characters; otherwise "Not verified" |
| GSTIN is a different, well-formed number (or belongs to another vendor) | HOLD — identity conflict; a matching name never overrides it |
| Same vendor + invoice number as an **approved** run (or identical file) | REJECT — duplicate |
| Same as a **held** run, unchanged | stays HOLD (not a paid duplicate) |
| Same as a held run, but corrected | evaluated from scratch, linked to the held run (kept as audit trail), approved at most once |
| Impossible date (`31/99/2026`) | HOLD for vendor follow-up with the reason shown |

**Dates** are read day-first (`03/04/2026` = 3 April 2026, and the page says so). Supported: `DD Mon YYYY`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD.MM.YYYY`, `YYYY-MM-DD`, `DD-Mon-YYYY`, `DD/MM/YY`. Years outside 2000–2100 are invalid; no rule compares a date with today.

**Wording.** The system records a decision; it does **not** release payments, e-mail vendors or return invoices. Approvals read "eligible for payment processing; this system has not scheduled or sent any payment", and every next step starts with "Recommended:".

**Concurrency.** The history read, duplicate check, PO-balance check and the write of the decision happen inside one SQLite `BEGIN IMMEDIATE` transaction (also across several server processes sharing one database), so two simultaneous copies of an invoice, or two invoices competing for the last of a PO, cannot both be approved.

## Documents and history
- Each upload is stored once under a unique id (`data/uploads/<uuid>.pdf`, read-only, checksum verified on every read). The original filename is a label only, so two uploads named `invoice.pdf` never overwrite each other. Preview, *Open PDF* and *Retry* all use the run's own stored document: `GET /api/run/<id>/document`. Retry is offered only for runs that failed to process.
- **Older history** (written before this version) is never rewritten. At start-up each old run is linked to its original file *only if* a file with the checksum recorded on that run still exists; otherwise the run shows "Original document not available" with the reason (for example "overwritten by a later upload"). A wrong document is never shown. Old runs keep their stored wording; runs saying "released to payment run" carry a note that no payment was ever released or sent.
- Oversize (> 10 MB), empty and non-PDF uploads are rejected with clear messages.

## Extraction
- Text layer first; pages with no text are OCR'd page by page. OCR engine chain: Apple Vision (macOS) → Tesseract → RapidOCR (pip, any OS).
- The rule parser tolerates label synonyms (Bill No., Dated, Taxable Value, Grand Total, Account Number), several date formats, Rs./INR prefixes, CGST+SGST, repeated table headers across pages, and OCR output with dropped spaces.
- Optional LLM fills only fields the rules missed, and only values it can **ground in the document**: right type (no booleans / numeric strings), whole tokens (so `123` is not accepted inside `999123999`), next to the field's own label, and line items only if the whole row (description, quantity, unit price, amount) appears on one line of the document. Anything else is discarded and the reason is recorded. Malformed answers and outages are handled; rules still decide.

## Tests
All suites use throw-away servers and data (free port, temp database, temp upload folder). They never read, write or stop the demo server on 8765 or `data/`.
```
pip3 install -r requirements-dev.txt                       # playwright, for the browser suite only
python3 -W ignore -m unittest discover -s tests            # backend + real-pipeline regression suites (generated PDFs, real HTTP routes)
python3 -W ignore -m unittest tests.browser_suite          # real Chrome via Playwright (uses system Chrome, else `playwright install chromium`)
AP_SHOTS_DIR=shots python3 -m unittest tests.browser_suite # same, and save desktop + mobile screenshots
AP_TEST_REAL_LLM=1 ANTHROPIC_API_KEY=… python3 -m unittest tests.test_llm_real   # OPT-IN, spends real API calls
```
`python3 tools/make_samples.py` regenerates samples 10–11 and their POs.

## Verification status
| Item | Status |
|---|---|
| Backend regression suites (`discover -s tests`) | run and passing locally (macOS, Python 3.13) |
| Browser suite (Chrome, desktop + mobile, refresh, restart, retry, filters, previews) | run and passing locally |
| OCR | Apple Vision and RapidOCR exercised on macOS only; Tesseract path never run; **no Linux run** |
| LLM extraction | grounding/validation proven against a **mock** provider only; the real-API test exists but is opt-in and **has not been run** |
| Docker | `Dockerfile` written, **not built or run** (Docker is not installed in the build environment) — it is not a deployment |
| Public URL | **none** — `localhost` is a local demo address only. Before sharing publicly, keep `AP_ALLOW_RESET` unset and put the app behind authentication: it has none |
| 5-minute demo video | **not recorded** |

## Limitations
- Reference data is JSON (stand-in for an ERP); no e-mail ingestion or ERP write-back; no user accounts or authentication.
- Single-node SQLite; fine for a demo, not a multi-node deployment.
