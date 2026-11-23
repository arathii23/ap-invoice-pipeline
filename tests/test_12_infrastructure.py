"""Isolation, restart persistence, the reset guard, cross-process safety and the bundled demo scenarios - all on throw-away servers
(free port, temp DB, temp uploads). These tests never touch the live demo server, its database or its uploads."""
import hashlib, os, shutil, sqlite3, subprocess, tempfile, threading, unittest
from helpers import SubprocessApp, ROOT, make_invoice, sample_bytes

LIVE_DB = os.path.join(ROOT, "data", "runs.db")
LIVE_UPLOADS = os.path.join(ROOT, "data", "uploads")


def live_fingerprint():
    out = {}
    if os.path.exists(LIVE_DB): out["db"] = hashlib.sha256(open(LIVE_DB, "rb").read()).hexdigest()
    if os.path.isdir(LIVE_UPLOADS): out["uploads"] = {n: hashlib.sha256(open(os.path.join(LIVE_UPLOADS, n), "rb").read()).hexdigest() for n in sorted(os.listdir(LIVE_UPLOADS))}
    return out


def listener_pids(port):
    try: return sorted(subprocess.run(["lsof", "-nP", f"-tiTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split())
    except Exception: return None


class Isolated(unittest.TestCase):
    def make(self, **kw):
        app = SubprocessApp(**kw); self.addCleanup(app.cleanup); return app.start()

    def test_isolation_from_the_live_demo(self):
        live_before, pids_before = live_fingerprint(), listener_pids(8765)
        app = self.make()
        self.assertNotEqual(app.port, 8765); self.assertTrue(app.db.startswith(tempfile.gettempdir()) or "/ap-sub-" in app.db)
        app.submit_sample("01_acme_clean.pdf"); app.submit(make_invoice(invoice_no="INV-I-0001"), "x.pdf")
        self.assertTrue(os.path.exists(app.db)); self.assertGreaterEqual(len(os.listdir(app.uploads)), 2)       # its own files
        tmp = app.tmp; app.cleanup(); self.assertFalse(os.path.exists(tmp)); self.assertIsNone(app.proc)           # removed only its own temp dir
        self.assertEqual(live_fingerprint(), live_before, "the live DB and uploads must be untouched")
        self.assertEqual(listener_pids(8765), pids_before, "the existing demo server must keep running, untouched")

    def test_history_documents_and_duplicates_survive_a_real_restart(self):
        app = self.make()
        mine = make_invoice(invoice_no="INV-I-0002", po="PO-2026-1007", lines=[("Pressure valve PV-7", 10, 3500.0, 35000.0)])   # sample 01 already used up PO-2026-1001
        r1, r2 = app.submit_sample("01_acme_clean.pdf"), app.submit(mine, "invoice.pdf")
        self.assertEqual((r1["decision"], r2["decision"]), ("APPROVE", "APPROVE"))
        app.restart()
        ids = {r["id"] for r in app.runs()}; self.assertEqual(ids, {r1["id"], r2["id"]})
        self.assertEqual(app.document(r1["id"])[2], sample_bytes("01_acme_clean.pdf")); self.assertEqual(app.document(r2["id"])[2], mine)
        again = app.submit_sample("01_acme_clean.pdf"); self.assertEqual(again["decision"], "REJECT")                # duplicate memory survives
        self.assertEqual(app.get(f"/api/run/{r2['id']}")[1]["decision"], "APPROVE")

    def test_history_reset_is_disabled_by_default(self):
        app = self.make(allow_reset=False)
        app.submit_sample("01_acme_clean.pdf")
        s, body = app.post("/api/reset"); self.assertEqual(s, 403); self.assertIn("disabled", body["error"])
        self.assertEqual(len(app.runs()), 1); self.assertFalse(app.get("/api/config")[1]["reset_enabled"])
        self.assertEqual(app.submit_sample("01_acme_clean.pdf")["decision"], "REJECT", "duplicate memory must survive a reset attempt")

    def test_reset_when_explicitly_enabled_takes_a_safety_copy_and_keeps_documents(self):
        app = self.make(allow_reset=True)
        r = app.submit_sample("01_acme_clean.pdf"); self.assertTrue(app.get("/api/config")[1]["reset_enabled"])
        files = set(os.listdir(app.uploads))
        self.assertEqual(app.post("/api/reset")[0], 200); self.assertEqual(app.runs(), [])
        backups = os.listdir(os.path.join(app.tmp, "reset-backups")); self.assertEqual(len(backups), 1)
        self.assertEqual(sqlite3.connect(os.path.join(app.tmp, "reset-backups", backups[0])).execute("SELECT id FROM runs").fetchall(), [(r["id"],)])
        self.assertEqual(set(os.listdir(app.uploads)), files, "stored documents are retained")

    def test_two_server_processes_sharing_one_database_cannot_both_approve(self):
        shared = tempfile.mkdtemp(prefix="ap-shared-"); self.addCleanup(shutil.rmtree, shared, True)
        a, b = SubprocessApp(tmp=shared), SubprocessApp(tmp=shared); self.addCleanup(a.stop); self.addCleanup(b.stop); a.start(); b.start()
        for round_ in range(3):
            pdf = make_invoice(invoice_no=f"INV-I-{100 + round_}", lines=[("Custom job", 1, 1000.0, 1000.0)]); out = {}   # 1,000 of the 130,000 PO per round
            ts = [threading.Thread(target=lambda k=k, app=app: out.update({k: app.submit(pdf, f"{k}.pdf")})) for k, app in (("a", a), ("b", b))]
            [t.start() for t in ts]; [t.join() for t in ts]
            self.assertEqual(sorted(r["decision"] for r in out.values()), ["APPROVE", "REJECT"], round_)

    def test_garbage_requests_do_not_hurt_the_server(self):
        app = self.make()
        self.assertEqual(app.request("POST", "/api/process_sample", b"not json")[0], 400)
        self.assertEqual(app.request("POST", "/api/process_sample", b'{"name": "../../etc/passwd"}', {"Content-Type": "application/json"})[0], 404)
        self.assertEqual(app.request("POST", "/api/upload", b"", {"Content-Length": "0"})[0], 400)
        self.assertEqual(app.request("GET", "/api/run/zzzz")[0], 404); self.assertEqual(app.request("GET", "/api/run/ffffffff/document")[0], 404)
        self.assertEqual(app.request("POST", "/api/nope", b"{}")[0], 404)
        self.assertEqual(app.submit_sample("01_acme_clean.pdf")["decision"], "APPROVE")

    def test_bundled_demo_scenarios_keep_their_decisions(self):
        app = self.make()
        expect = [("01_acme_clean.pdf", "APPROVE"), ("03_novatech_milestone1.pdf", "APPROVE"), ("05_acme_duplicate_resent.pdf", "REJECT"), ("07_greenfield_price_variance.pdf", "HOLD"),
                  ("09_brightline_bank_changed.pdf", "HOLD"), ("06_greenfield_missing_fields.pdf", "HOLD"), ("08_acme_implied_po.pdf", "APPROVE"), ("04_novatech_milestone2.pdf", "APPROVE"),
                  ("10_acme_multipage.pdf", "APPROVE"), ("11_greenfield_alt_layout.pdf", "APPROVE"), ("02_brightline_scanned.pdf", "APPROVE")]
        for name, want in expect:
            r = app.submit_sample(name); self.assertEqual(r["decision"], want, f"{name}: {r['headline']} - {r['reasoning'][:200]}")
        self.assertTrue(app.get(f"/api/run/{r['id']}")[1]["ocr"])                                             # the scanned sample really went through OCR


if __name__ == "__main__":
    unittest.main()
