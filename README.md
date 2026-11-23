# AP Invoice Pipeline (PS-1)

PDF invoice → extract (text layer / OCR / optional LLM) → match PO → validate → **APPROVE / HOLD / REJECT** with reasons a finance manager can read.

> **Delivery status, plainly.** A public copy runs on Render's **free** tier: <https://ap-invoice-pipeline.onrender.com> (deployed from this repo's `main` branch).
> It sleeps after ~15 minutes idle, so the first request can take ~50 s. The optional LLM path is live with a Sarvam key: on 24 Nov 2026 a real Sarvam call filled a grounded bank account on one test invoice, and on a repeat the model returned nothing usable
> so the rules alone decided (the LLM only ever suggests, and is non-deterministic). The Gemini fallback has so far been exercised against a **mock** server only; check it with `tools/check_llm.py`. There is **no demo video in this repo**. Details in [Verification status](#verification-status).

## What the desk does
| Area | What you get |
|---|---|
| **Inputs** | PDF (digital or scanned), phone photos and scans (JPG, PNG, TIFF incl. multi-page, WebP, BMP, GIF — EXIF-rotated upright), forwarded e-mails (`.eml`: every invoice attachment becomes a run; inline logos/signatures skipped), ZIP batches (up to 25 documents, processed in order). Word/Excel, iPhone HEIC and Outlook `.msg` are refused with a specific fix. The fingerprint of the file *as sent* drives duplicate detection, so a re-sent photo or e-mail is still caught. |
| **Live run view** | Ten stages appear as they execute, each with the comparison behind it; decision card with amount, reasons and recommended next step; how the document arrived; audit-record download and print. |
| **Human in the loop** | Held invoices go to the **Review queue**. A reviewer approves, rejects or asks the vendor (a ready-to-copy vendor query is drafted from the failed checks). A reason is mandatory; the automated decision stays on record. Safeguards: no approval without vendor match, invoice number and total, never a second copy of an approved invoice, call-back warning on bank changes, warning when approving above the PO balance. |
| **Dashboard** | KPIs, value by decision, straight-through rate, source mix, search and filters (incl. *Needs review*), **CSV export** (formula-injection safe). |
| **Reference data** | Purchase-order register with live balances and utilisation; vendor master (bank accounts masked). |
| **Help** | *Rules & edge cases* (tolerances, the ten checks, decision matrix, 18 edge cases with one-click samples), *Guide* (how it works, step-by-step, formats, five-minute demo script, FAQ), an optional guided tour (`t`), keyboard shortcuts (`?`), light/dark theme, mobile layout. |

## Run
```
pip3 install -r requirements.txt
python3 server.py            # http://localhost:8765   (HOST / PORT env vars supported; default HOST is 127.0.0.1)
```
Data lives in `data/` (`runs.db`, `uploads/`) next to the reference files `pos.json` and `vendors.json`.

| Environment variable | Default | Meaning |
|---|---|---|
| `PORT`, `HOST` | `8765`, `127.0.0.1` | where the server listens (the Docker image uses `0.0.0.0`) |
| `AP_DB`, `AP_UPLOADS` | `data/runs.db`, `data/uploads/` | database and stored-document folder (tests and demos use temp locations) |
| `AP_ALLOW_RESET` | unset (**disabled**) | `1` enables `POST /api/reset` and the *Clear history* button on a single shared dataset (a SQLite backup is written first). Leave it off for anything shared. In workspace mode reset is always on, but only ever clears the caller's own workspace. |
| `STAGE_DELAY` | `0.55` | seconds between stages on the live run view (cosmetic; the decision is already committed) |
| `AP_OCR_ENGINE` | auto | force an OCR engine |
| `AP_OCR_MAX_SIDE`, `AP_OCR_MAX_PAGES`, `AP_OCR_TIMEOUT_S` | `960`, `8`, `180` | OCR bounds: pixels on the long side used for text detection, scanned pages read per document, seconds per page |
| `AP_OCR_MAX_RSS_MB` | `0` (off); Docker image `290` | the OCR worker process is killed if its memory passes this, and the run is held with a clear error instead of the host running out of memory |
| `AP_LLM` | `auto` | `auto` = ask the LLM only when the rules left gaps, `always`, `off` |
| `SARVAM_API_KEY` | — | Sarvam key (tried **first**) |
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | — | Gemini key (tried when Sarvam does not answer) |
| `ANTHROPIC_API_KEY` | — | optional third provider |
| `AP_LLM_PROVIDERS` | `sarvam,gemini,anthropic` | order / subset of providers to use |
| `AP_SARVAM_MODEL`, `AP_GEMINI_MODEL`, `AP_LLM_MODEL` | `sarvam-105b`, `gemini-3.5-flash-lite`, `claude-haiku-5-5` | model names (Anthropic uses `AP_LLM_MODEL`) |
| `AP_LLM_MAX_PER_HOUR`, `AP_LLM_MAX_PER_WS_HOUR` | `120`, `20` | provider-call budgets for the whole demo / per visitor workspace |
| `AP_WORKSPACES` | unset (Docker image: `1`) | `1` = **public-demo mode**: a private, cookie-keyed workspace per visitor (see below) |
| `AP_WORKSPACE_TTL_HOURS`, `AP_MAX_WORKSPACES`, `AP_WORKSPACES_DIR` | `24`, `500`, `data/workspaces` | expiry, capacity, location of the workspaces |
| `AP_MAX_RUNS_PER_WS`, `AP_MAX_WS_MB`, `AP_MAX_ACTIVE_RUNS`, `AP_RATE_RUNS_PER_MIN` | `100`, `60`, `8`, `30` | per-workspace runs / megabytes, concurrent runs (server-wide), submissions per minute per client address |
| `AP_RATE_NEW_WS_PER_10MIN` | `30` | new workspaces one address may create per 10 minutes (so discarding the cookie cannot fill the quota) |
| `AP_TRUST_PROXY` | unset | `1` behind a reverse proxy (Render): the client address comes from `X-Forwarded-For`. Unset, that header is ignored so it cannot be used to dodge the rate limit. |
| `AP_LOG_REQUESTS` | `1` | one log line per write or error request (never per poll); `0` silences it |

## LLM providers (optional) — Sarvam first, Gemini as fallback
The rules always run first. If they leave gaps (and `AP_LLM` allows it) the chain **Sarvam → Gemini → Anthropic** is tried in order, using only providers that have a key.
A provider that answers wins; one that errors, times out, is rate-limited, returns nothing usable or is out of credits hands over to the next. A bad key / no credits (402/403) pauses that provider for
15 minutes and a rate limit (429) for its `Retry-After`, so later runs do not wait on it. If every provider fails the run **still completes on rules alone** and the extract stage says so. The stage also names
the provider that filled the gaps (`LLM (gemini after sarvam failed) filled gaps: …`). Keys are read from the environment only, never logged, never returned by any endpoint, and scrubbed from error text.

**The LLM never decides.** It can only suggest values for fields the rules missed, and each suggestion is kept only if it is grounded in the document (right type, whole token, next to the field's own label; line items only if the whole row appears on one line). Approve / hold / reject stays deterministic.

Check your keys against the real services (a few tokens): `SARVAM_API_KEY=… GEMINI_API_KEY=… python3 tools/check_llm.py`.

## Public demo mode (`AP_WORKSPACES=1`)
Built for sharing the link with strangers on a free instance:
- **Private workspace per visitor.** The first API call sets a random `ap_ws` cookie (HttpOnly, SameSite=Lax, Secure over HTTPS) and creates `data/workspaces/<id>/` with its own database and stored documents. History, duplicate detection and PO balances are per workspace, so one reviewer can never see, approve against or retry another's runs; an unknown/forged/expired cookie just gets a new empty workspace. *Clear history* clears only your own workspace. The UI says "Private workspace · cleared after N h idle".
- **Limits.** Per-client submissions/minute, concurrent runs, runs and megabytes per workspace, number of workspaces, and hourly LLM budgets (all configurable above). Rejections are polite (429/503/413) and never touch other visitors.
- **Expiry.** A janitor deletes workspaces idle longer than the TTL (24 h). The container disk on a free instance is ephemeral anyway: a restart or redeploy wipes **all** workspaces, and a sleeping instance wakes empty.
- `GET /healthz` is a liveness probe that creates nothing.
- Not authentication: a workspace is a convenience boundary for a demo, not a security boundary for real finance data.

### Deploying on Render
`render.yaml` is a Blueprint (docker runtime, free plan, auto-deploy from `main`). After the first deploy add the secrets in **Dashboard → service → Environment**: `SARVAM_API_KEY` and `GEMINI_API_KEY` (never commit them), then the service restarts. Every push to `main` redeploys (about 1–2 minutes of downtime, and all workspaces are wiped) — avoid pushing while someone is reviewing.

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
- Run timestamps are stored with their UTC offset, so each browser shows the visitor's own local time; times quoted inside explanations are labelled UTC. History written before this change keeps its stored (offset-less) wording.

## Extraction
- Text layer first; pages with no text are OCR'd page by page. OCR engine chain: Apple Vision (macOS) → Tesseract → RapidOCR (pip, any OS).
- **OCR is memory-bounded for small hosts.** The Docker image uses **Tesseract** (about 40-80 MB; RapidOCR is not installed there). Every OCR engine runs as a short-lived child process, one page at a time, with a timeout and a memory watchdog (`AP_OCR_MAX_RSS_MB`; 290 in the image): a runaway worker is stopped, that run is held with a clear retryable message, and the web server and other visitors are unaffected. Pages are rendered at no more than 2400 px on the long side and a document may have at most 8 scanned pages. When RapidOCR is the engine (pip installs without Tesseract) detection runs on a 960 px version of the page (~280 MB peak instead of ~900 MB). (The first Render deploy ran RapidOCR with the library defaults inside the web server; the scanned sample used over 512 MB and the free instance was killed.)
- The rule parser tolerates label synonyms (Bill No., Dated, Taxable Value, Grand Total, Account Number), several date formats, Rs./INR prefixes, CGST+SGST, repeated table headers across pages, and OCR output with dropped spaces.
- Optional LLM fills only fields the rules missed, and only values it can **ground in the document**: right type (no booleans / numeric strings), whole tokens (so `123` is not accepted inside `999123999`), next to the field's own label, and line items only if the whole row (description, quantity, unit price, amount) appears on one line of the document. Anything else is discarded and the reason is recorded. Malformed answers and outages are handled; rules still decide.

## Tests
All suites use throw-away servers and data (free port, temp database, temp upload folder). They never read, write or stop the demo server on 8765 or `data/`.
```
pip3 install -r requirements-dev.txt                       # playwright, for the browser suite only
python3 -W ignore -m unittest discover -s tests            # backend + real-pipeline regression suites (generated PDFs, real HTTP routes)
python3 -W ignore -m unittest tests.browser_suite          # real Chrome via Playwright (uses system Chrome, else `playwright install chromium`)
AP_SHOTS_DIR=shots python3 -m unittest tests.browser_suite # same, and save desktop + mobile screenshots
AP_TEST_REAL_LLM=1 SARVAM_API_KEY=… GEMINI_API_KEY=… python3 -m unittest tests.test_llm_real   # OPT-IN, spends real API calls
```
`tests/test_13_providers.py` (mock Sarvam/Gemini: request shape, fallback order, cooldowns, key hygiene, budgets, grounding) and `tests/test_14_workspaces.py` (cookie isolation, caps, rate limits, expiry, reset) cover the public-demo features.
`python3 tools/make_samples.py` regenerates samples 10–11 and their POs.

## Verification status
| Item | Status |
|---|---|
| Backend regression suites (`discover -s tests`) | run and passing locally (macOS, Python 3.13), including provider-fallback and workspace-isolation suites |
| Browser suite (Chrome, desktop + mobile, refresh, restart, retry, filters, previews) | run and passing locally |
| OCR | Apple Vision, RapidOCR and Tesseract (5.5, macOS) exercised locally, incl. the scanned sample through the full pipeline (`tests/test_15_ocr.py`). On Render (Linux, Tesseract from Debian, 512 MB) all 11 bundled samples were run live on 24 Nov 2026 with the expected decisions; the scanned sample was read in ~11 s and approved. Other scanned documents are untested there |
| LLM extraction | request shape, fallback, cooldowns and grounding proven against **mock** Sarvam/Gemini servers. **Sarvam verified live on Render (24 Nov 2026)**: answered on the first attempt, one run filled a grounded field, a repeat run returned nothing usable and was handled safely. **Gemini not yet run against the real service** (`tools/check_llm.py` or the opt-in `tests/test_llm_real.py` with a real key does that) |
| Docker | the image builds and runs on Render (free plan); not tested on any other host |
| Public URL | <https://ap-invoice-pipeline.onrender.com> on Render's free tier (sleeps when idle, ephemeral disk). No authentication — private workspaces separate visitors, they are not a security boundary |
| 5-minute demo video | not part of this repository |

## Limitations
- Reference data is JSON (stand-in for an ERP); no e-mail ingestion or ERP write-back; no user accounts or authentication.
- Single-node SQLite; fine for a demo, not a multi-node deployment. Free-tier hosting: cold starts, ephemeral storage, one small instance.
