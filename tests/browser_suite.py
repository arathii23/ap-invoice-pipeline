"""Browser (Playwright) verification of the real UI against a THROW-AWAY server.

    python3 -m unittest tests.browser_suite -v          (needs: pip install playwright; uses system Chrome, or `playwright install chromium`)

Every test drives the real page in a real browser against `python server.py` started by this suite on a free port with a
temporary database and temporary upload folder (see helpers.SubprocessApp). The suite only ever stops the processes it started,
only deletes its own temp folders and never touches the demo server on port 8765, its database or its uploads.
Set AP_SHOTS_DIR=<folder> to also save desktop + mobile screenshots of what the tests saw.
"""
import hashlib, os, re, tempfile, unittest, urllib.parse
from helpers import SubprocessApp, make_invoice, sample_bytes

try:
    from playwright.sync_api import sync_playwright, Error as PWError
except Exception as e:                                                   # pragma: no cover
    sync_playwright = None; _why = f"playwright is not installed ({e})"

SHOTS = os.environ.get("AP_SHOTS_DIR")
DESKTOP = dict(viewport={"width": 1440, "height": 900})
MOBILE = dict(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
PDF_HEADER = b"%PDF"


def sha(b): return hashlib.sha256(b).hexdigest()


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class BrowserFlows(unittest.TestCase):
    """One server for the whole journey (so history accumulates like in real use). Each test sets up whatever it needs via
    the API and does not rely on test order, except that history is shared and therefore checked by content, not by count."""

    @classmethod
    def setUpClass(cls):
        cls.app = SubprocessApp(stage_delay="0.25", allow_reset=False).start()          # reset DISABLED = the posture to share publicly
        cls.pw = sync_playwright().start()
        try: cls.browser = cls.pw.chromium.launch(channel="chrome", headless=True)
        except PWError:
            try: cls.browser = cls.pw.chromium.launch(headless=True)
            except PWError as e:
                cls.pw.stop(); cls.app.cleanup(); raise unittest.SkipTest(f"no Chrome/Chromium available for Playwright: {e}")
        cls.problems = []; cls.contexts = []
        if SHOTS: os.makedirs(SHOTS, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        for c in cls.contexts:
            try: c.close()
            except Exception: pass
        cls.browser.close(); cls.pw.stop(); cls.app.cleanup()

    # ------------------------------------------------------------ helpers
    def page(self, **ctx):
        c = self.browser.new_context(**(ctx or DESKTOP)); self.contexts.append(c); p = c.new_page(); p.set_default_timeout(20000)
        p.on("console", lambda m: self.problems.append(("console", m.text)) if m.type == "error" else None)
        p.on("pageerror", lambda e: self.problems.append(("pageerror", str(e))))
        p.on("response", lambda r: self.problems.append(("http", f"{r.status} {r.request.method} {r.url}")) if r.status >= 400 else None)
        return p

    def shot(self, page, name):
        if SHOTS: page.screenshot(path=os.path.join(SHOTS, name + ".png"), full_page=True)

    def go(self, page, route):
        page.goto(self.app.base + "/#" + route); page.reload()                       # reload: a clean, refreshed page every time

    def upload_file(self, page, pdf, filename="invoice.pdf"):
        self.go(page, "/upload")
        page.set_input_files("#file", files=[{"name": filename, "mimeType": "application/pdf", "buffer": pdf}])
        page.wait_for_selector("#pickedbox .picked")
        page.click("#go"); return self.finish_run(page)

    def pick_sample(self, page, name):
        self.go(page, "/upload"); page.click(f'.samples button[data-f="{name}"]'); page.click("#go"); return self.finish_run(page)

    def finish_run(self, page):
        page.wait_for_url(re.compile(r"#/run/[0-9a-f]+")); rid = page.url.split("/")[-1]
        page.wait_for_selector("#decision", timeout=30000); page.wait_for_function("!document.querySelector('.run-grid.live')")
        return rid

    SETTLED = """() => [...document.querySelectorAll('[data-tick]')].every(e => e.dataset.fmt === 'inr'
        ? e.textContent === '₹' + Number(e.dataset.tick || 0).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})
        : e.textContent === String(Math.round(Number(e.dataset.tick) || 0)))"""

    def settle(self, page):
        """The number tickers count up for under a second; read numbers only once they show their final value."""
        page.wait_for_function(self.SETTLED)

    def decision_text(self, page): self.settle(page); return page.inner_text("#decision")

    def api_run(self, rid): return self.app.get(f"/api/run/{rid}")[1]

    def ensure_sample01_approved(self):
        if not any(r.get("decision") == "APPROVE" and (r["extracted"] or {}).get("invoice_no") == "INV-ACM-0421" for r in self.app.runs()):
            self.app.submit_sample("01_acme_clean.pdf")

    # ------------------------------------------------------------ the journey
    def test_01_upload_a_sample_and_watch_the_stages_run_live(self):
        page = self.page(); self.go(page, "/upload")
        self.assertEqual(page.inner_text("h1"), "Process an invoice")
        self.assertTrue(page.is_disabled("#go"), "Process must stay disabled until something is chosen")
        page.click('.samples button[data-f="01_acme_clean.pdf"]'); self.assertFalse(page.is_disabled("#go"))
        self.shot(page, "desktop_upload"); page.click("#go")
        page.wait_for_url(re.compile(r"#/run/[0-9a-f]+")); rid = page.url.split("/")[-1]
        seen, running = set(), False
        for _ in range(200):                                                         # sample the live view while the stages reveal
            st = page.evaluate("""() => ({live: !!document.querySelector('.run-grid.live'), run: !!document.querySelector('.tl .st.s-running'),
                                         txt: (document.querySelector('#tlcard .ch .sub')||{}).textContent||'', dec: !!document.querySelector('#decision')})""")
            running = running or st["run"]; seen.add(st["txt"])
            if st["dec"] and not st["live"]: break
            page.wait_for_timeout(100)
        n = len(self.app.get("/api/config")[1]["stages"])
        self.assertTrue(running, "a stage should be shown as in-progress while the run is live")
        self.assertGreaterEqual(len([t for t in seen if t and "live" in t]), 3, f"progress counter should advance live, saw {sorted(seen)}")
        self.assertIn(f"{n}/{n} stages", page.inner_text("#tlcard .ch .sub")); self.assertNotIn("live", page.inner_text("#tlcard .ch .sub"))
        txt = self.decision_text(page); self.assertIn("Approved", txt); self.assertIn("eligible for payment processing", txt)
        self.assertIn("has not scheduled or sent any payment", txt); self.assertIn("Recommended next step", txt)
        self.assertEqual(page.locator(".tl .st").count(), n)
        self.assertEqual(page.locator(".tl .st.s-pass, .tl .st.s-warn").count(), n, "every stage of a clean approval passes")
        self.shot(page, "desktop_run_approved")
        self.assertEqual(self.api_run(rid)["decision"], "APPROVE")

    def test_02_stage_details_expand_and_collapse(self):
        self.ensure_sample01_approved()
        page = self.page(); rid = self.app.runs()[0]["id"]; self.go(page, f"/run/{rid}"); page.wait_for_selector("#decision")
        li = page.locator(".tl .st:has(.det)").first; btn = li.locator("> button")
        self.assertEqual(btn.get_attribute("aria-expanded"), "false"); self.assertFalse(li.locator(".det").is_visible())
        btn.click(); self.assertEqual(btn.get_attribute("aria-expanded"), "true"); self.assertTrue(li.locator(".det table.cmp").is_visible())
        btn.click(); self.assertEqual(btn.get_attribute("aria-expanded"), "false"); self.assertFalse(li.locator(".det").is_visible())
        raw = page.locator("details.raw"); raw.locator("summary").click(); self.assertIn("Acme", raw.locator("pre").inner_text())

    def test_03_each_run_shows_its_own_document_even_when_filenames_repeat(self):
        a = make_invoice(invoice_no="INV-D-0301", po="PO-2026-1007", lines=[("Pressure valve PV-7", 1, 3500.0, 3500.0)])
        b = make_invoice(invoice_no="INV-D-0302", po="PO-2026-1007", lines=[("Pressure valve PV-7", 2, 3500.0, 7000.0)])
        page = self.page()
        ra = self.upload_file(page, a, "invoice.pdf"); rb = self.upload_file(page, b, "invoice.pdf")        # SAME filename
        self.assertNotEqual(ra, rb)
        for rid, pdf in ((ra, a), (rb, b)):
            self.go(page, f"/run/{rid}"); page.wait_for_selector("#decision")
            src = page.get_attribute("#doccard iframe.pdf", "src"); self.assertTrue(src.startswith(f"/api/run/{rid}/document"), src)
            r = page.request.get(self.app.base + f"/api/run/{rid}/document")
            self.assertEqual((r.status, r.headers["content-type"]), (200, "application/pdf")); self.assertEqual(sha(r.body()), sha(pdf), "wrong document served")
            self.assertEqual(page.get_attribute("#doccard .ch a", "href"), f"/api/run/{rid}/document")
        self.assertEqual(self.api_run(ra)["decision"], "APPROVE"); self.assertEqual(self.api_run(rb)["decision"], "APPROVE")

    def test_04_missing_financials_are_held_with_a_specific_explanation(self):
        pdf = make_invoice(invoice_no="INV-B-0401", po="PO-2026-1005", lines=[], subtotal=None, tax=None, tax_rate=0, total=9999999.0)
        page = self.page(); rid = self.upload_file(page, pdf, "total-only.pdf")
        txt = self.decision_text(page)
        self.assertIn("On hold", txt); self.assertIn("could not be verified", txt); self.assertNotIn("Approved", txt)
        self.assertGreaterEqual(page.locator(".tl .st.s-unverified").count(), 1)
        self.assertIn("Not verified", page.locator(".tl .st.s-unverified .badge").first.inner_text())
        self.assertFalse(page.locator('.tl .st[data-k="amount"] .badge:has-text("Passed")').count(), "an un-run check must never read 'Passed'")
        self.assertIn("Not found", page.inner_text("#side .kv"))
        self.assertEqual(page.locator('.tl .st[data-k="amount"]').get_attribute("class").count("open"), 1, "the unverified stage opens automatically")
        self.shot(page, "desktop_run_unverified"); self.assertEqual(self.api_run(rid)["decision"], "HOLD")

    def test_05_line_arithmetic_and_exhausted_po_are_held(self):
        page = self.page()
        bad = make_invoice(invoice_no="INV-B-0501", po="PO-2026-1007", lines=[("Pressure valve PV-7", 999, 12000.0, 120000.0)])
        self.upload_file(page, bad, "math.pdf"); t = self.decision_text(page)
        self.assertIn("On hold", t); self.assertIn("arithmetic does not reconcile", t)
        self.ensure_sample01_approved()
        more = make_invoice(invoice_no="INV-B-0502", po="PO-2026-1001", lines=[("Seal kit SK-11", 1, 1000.0, 1000.0)])
        self.upload_file(page, more, "extra.pdf"); t = self.decision_text(page)
        self.assertIn("On hold", t); self.assertIn("no remaining value", t)
        po_txt = page.inner_text("#side .card:has(h2:text('Matched purchase order'))")
        self.assertIn("nothing left on this PO", po_txt); self.assertNotIn("-₹", po_txt); self.assertNotIn("NaN", po_txt)
        self.shot(page, "desktop_run_po_exhausted")

    def test_06_corrected_resubmission_links_to_the_held_run_and_is_not_a_duplicate(self):
        held = make_invoice(invoice_no="INV-C-0601", date=None, po="PO-2026-1005", lines=[("Maintenance spares kit", 1, 12000.0, 12000.0)])
        fixed = make_invoice(invoice_no="INV-C-0601", date="06 Nov 2026", po="PO-2026-1005", lines=[("Maintenance spares kit", 1, 12000.0, 12000.0)])
        page = self.page(); r1 = self.upload_file(page, held, "needs-date.pdf"); self.assertIn("On hold", self.decision_text(page))
        r2 = self.upload_file(page, fixed, "with-date.pdf"); t = self.decision_text(page)
        self.assertTrue(t.startswith("Approved")); self.assertNotIn("Rejected", t); self.assertNotRegex(t, r"\bNone\b")
        card = page.locator("#related"); self.assertIn("Corrected resubmission of a held invoice", card.inner_text())
        self.assertIn("date", card.inner_text().lower()); self.assertEqual(card.locator(f'a[href="#/run/{r1}"]').count(), 1)
        self.shot(page, "desktop_run_corrected")
        self.go(page, f"/run/{r1}"); page.wait_for_selector("#decision")
        self.assertIn("Superseded", page.inner_text("#related")); self.assertIn("On hold", self.decision_text(page), "the earlier hold stays as recorded")
        page.click(f'#related a[href="#/run/{r2}"]'); page.wait_for_selector(f'#runroot[data-id="{r2}"]')
        self.assertEqual([self.api_run(r1)["decision"], self.api_run(r2)["decision"]], ["HOLD", "APPROVE"])

    def test_07_failed_run_can_be_retried_and_reuses_its_own_document(self):
        junk = b"%PDF-1.4\nthis is not a readable pdf"
        page = self.page(); rid = self.upload_file(page, junk, "broken.pdf"); t = self.decision_text(page)
        self.assertIn("Failed", t); self.assertIn("No payment decision was made", t); self.assertEqual(page.locator("#retry").count(), 1)
        page.click("#retry"); page.wait_for_function(f"location.hash !== '#/run/{rid}'"); page.wait_for_selector("#decision"); page.wait_for_function("!document.querySelector('.run-grid.live')")
        rid2 = page.url.split("/")[-1]; self.assertNotEqual(rid, rid2); self.assertIn("Retry", page.inner_text("#hdr .meta"))
        self.assertEqual(page.request.get(self.app.base + f"/api/run/{rid2}/document").body(), junk, "a retry reuses the original stored file")
        self.assertEqual(self.api_run(rid2)["retry_of"], rid)
        ok = self.pick_sample(self.page(), "07_greenfield_price_variance.pdf"); self.go(page, f"/run/{ok}"); page.wait_for_selector("#decision")
        self.assertEqual(page.locator("#retry").count(), 0, "retry is offered only for runs that failed to process")

    def test_08_history_lists_filters_and_searches_real_runs(self):
        self.ensure_sample01_approved(); self.app.submit_sample("01_acme_clean.pdf")            # a REJECT (duplicate of the approved one)
        page = self.page(); self.go(page, "/history"); page.wait_for_selector("table.t")
        runs = self.app.runs(); rows = page.locator("table.t tr.r"); self.assertEqual(rows.count(), len(runs))
        by = lambda k: len([r for r in runs if not r.get("error") and r.get("decision") == k])
        self.settle(page); kpi = {page.locator(".kpi span").nth(i).inner_text(): page.locator(".kpi b").nth(i).inner_text() for i in range(4)}
        self.assertEqual((kpi["Approved"], kpi["Rejected"]), (str(by("APPROVE")), str(by("REJECT"))))
        sub = page.locator(".kpi").all_inner_texts(); joined = " ".join(sub)
        self.assertIn("eligible for payment", joined); self.assertIn("not approved", joined); self.assertNotRegex(joined, r"released|blocked")
        self.assertEqual(page.locator("#clear").count(), 0, "destructive reset must not be offered when it is disabled")
        for key, label in (("HOLD", "On hold"), ("REJECT", "Rejected"), ("APPROVE", "Approved")):
            page.click(f'.seg button[data-s="{key}"]'); self.assertEqual(page.get_attribute(f'.seg button[data-s="{key}"]', "aria-pressed"), "true")
            self.assertEqual(rows.count(), by(key)); self.assertTrue(all(label in t for t in page.locator("table.t tr.r .c-dec").all_inner_texts()))
        page.click('.seg button[data-s="ALL"]'); page.fill("#q", "INV-ACM-0421")
        self.assertGreaterEqual(rows.count(), 1); self.assertTrue(all("INV-ACM-0421" in t or "Acme" in t for t in rows.all_inner_texts()))
        page.fill("#q", "zzz-no-such-invoice"); self.assertEqual(rows.count(), 0); self.assertIn("No runs match", page.inner_text("#tbl"))
        page.click("#rs"); page.wait_for_function(f"document.querySelectorAll('table.t tr.r').length === {len(runs)}"); self.assertEqual(page.input_value("#q"), "")
        page.fill("#q", "Superseded")                                                          # superseded holds are labelled in the table
        page.fill("#q", ""); page.locator("table.t tr.r .c-inv a").first.click(); page.wait_for_selector("#decision")
        self.shot(page, "desktop_run_opened_from_history"); self.go(page, "/history"); page.wait_for_selector("table.t"); self.shot(page, "desktop_history")

    def test_09_refresh_and_server_restart_keep_history_and_documents(self):
        page = self.page(); mine = make_invoice(invoice_no="INV-R-0901", po="PO-2026-1007", lines=[("Pressure valve PV-7", 1, 3500.0, 3500.0)])
        rid = self.upload_file(page, mine, "restart-me.pdf"); before = {r["id"] for r in self.app.runs()}; head = self.decision_text(page)
        page.reload(); page.wait_for_selector("#decision"); self.assertEqual(self.decision_text(page), head)       # refresh
        self.app.restart()
        page.goto(self.app.base + f"/#/run/{rid}"); page.reload(); page.wait_for_selector("#decision")                # restart
        self.assertEqual(self.decision_text(page), head)
        self.assertEqual(page.request.get(self.app.base + f"/api/run/{rid}/document").body(), mine)
        self.go(page, "/history"); page.wait_for_selector("table.t tr.r"); self.assertEqual(page.locator("table.t tr.r").count(), len(before))
        self.assertIn("INV-R-0901", page.inner_text("table.t"))
        again = self.upload_file(page, mine, "restart-me-again.pdf"); self.assertIn("Rejected", self.decision_text(page), "duplicate memory survives a restart")

    def test_10_mobile_layout_has_no_horizontal_overflow(self):
        self.ensure_sample01_approved(); rid = self.app.runs()[-1]["id"]
        page = self.page(**MOBILE)
        for name, route, sel in (("upload", "/upload", ".samples button"), ("history", "/history", "table.t"), ("run", f"/run/{rid}", "#decision")):
            self.go(page, route); page.wait_for_selector(sel); page.wait_for_timeout(700)
            over = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
            self.assertLessEqual(over, 1, f"{name}: page scrolls horizontally by {over}px on a 390px phone"); self.shot(page, f"mobile_{name}")
        self.go(page, "/history"); page.wait_for_selector("table.t"); self.assertFalse(page.locator("table.t .hide-m").first.is_visible())
        page.fill("#q", "acme"); self.assertGreaterEqual(page.locator("table.t tr.r").count(), 1)

    def test_11_reduced_motion_shows_final_numbers_immediately(self):
        page = self.page(viewport=DESKTOP["viewport"], reduced_motion="reduce"); self.go(page, "/history"); page.wait_for_selector("table.t")
        self.assertTrue(page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches"))
        vals = page.evaluate("() => [...document.querySelectorAll('.kpi b')].map(e => [e.textContent, e.dataset.tick])")
        self.assertTrue(all(a == b for a, b in vals), vals)

    def test_zz_no_console_errors_or_failed_requests_anywhere(self):
        self.assertEqual(self.problems, [], "browser saw console errors, page errors or HTTP >= 400 responses")


class BrowserResetFlow(unittest.TestCase):
    """The destructive 'Clear history' control exists only when the server was explicitly started with AP_ALLOW_RESET=1,
    asks for confirmation, and keeps a safety copy."""

    @unittest.skipIf(sync_playwright is None, "playwright not installed")
    def test_clear_history_when_explicitly_enabled(self):
        app = SubprocessApp(allow_reset=True).start(); self.addCleanup(app.cleanup)
        app.submit_sample("01_acme_clean.pdf")
        with sync_playwright() as pw:
            try: br = pw.chromium.launch(channel="chrome", headless=True)
            except PWError: br = pw.chromium.launch(headless=True)
            try:
                page = br.new_page(); page.goto(app.base + "/#/history"); page.reload(); page.wait_for_selector("table.t tr.r")
                dialogs = []; page.once("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
                page.click("#clear"); self.assertEqual(len(app.runs()), 1, "dismissing the confirmation must not delete anything")
                page.once("dialog", lambda d: (dialogs.append(d.message), d.accept())); page.click("#clear")
                page.wait_for_selector(".empty:has-text('No invoices processed yet')"); self.assertEqual(app.runs(), [])
                self.assertTrue(any("Permanently delete" in m for m in dialogs)); self.assertEqual(len(os.listdir(os.path.join(app.tmp, "reset-backups"))), 1)
            finally: br.close()


if __name__ == "__main__":
    unittest.main()
