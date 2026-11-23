"""Public-demo mode (AP_WORKSPACES=1): every visitor gets a private, cookie-keyed workspace; limits keep a free instance healthy.
Real HTTP routes, throw-away folders; nothing here touches data/ or the live demo."""
import json, os, shutil, tempfile, threading, time, unittest
from helpers import AppTestCase, InProcessApp, make_invoice, sample_bytes

S01 = "01_acme_clean.pdf"


class WorkspaceCase(AppTestCase):
    extra_env = {}

    def setUp(self):
        self.wsdir = tempfile.mkdtemp(prefix="ap-ws-"); self.addCleanup(shutil.rmtree, self.wsdir, True)
        self.app_env = {"AP_WORKSPACES": "1", "AP_WORKSPACES_DIR": self.wsdir, **self.extra_env}
        super().setUp()

    def spaces(self): return sorted(os.listdir(self.wsdir)) if os.path.isdir(self.wsdir) else []
    def ws_of(self, v): return v.cookies["ap_ws"]

    def setUp_slow(self):
        self.app = InProcessApp(stage_delay="0.3", env=self.app_env); self.addCleanup(self.app.close); self.server = self.app.server


class Isolation(WorkspaceCase):
    def test_visitors_never_see_each_others_history_runs_or_documents(self):
        a, b = self.app.visitor(), self.app.visitor()
        ra = a.submit_sample(S01)
        self.assertEqual(ra["decision"], "APPROVE"); self.assertEqual([r["id"] for r in a.runs()], [ra["id"]]); self.assertEqual(b.runs(), [])
        self.assertEqual(b.get(f"/api/run/{ra['id']}")[0], 404)
        self.assertEqual(b.document(ra["id"])[0], 404)
        self.assertEqual(a.document(ra["id"])[0], 200)
        self.assertEqual(b.post(f"/api/retry/{ra['id']}")[0], 404)
        self.assertNotEqual(self.ws_of(a), self.ws_of(b))

    def test_a_run_that_is_still_running_is_invisible_to_others(self):
        self.app.close(); self.setUp_slow()
        a, b = self.app.visitor(), self.app.visitor()
        s, r = a.post("/api/process_sample", {"name": S01}); self.assertEqual(s, 200)
        self.assertEqual(a.get(f"/api/run/{r['id']}")[0], 200); self.assertEqual(b.get(f"/api/run/{r['id']}")[0], 404)
        a.wait(r["id"])

    def test_duplicate_detection_and_po_balance_are_per_workspace(self):
        a, b = self.app.visitor(), self.app.visitor()
        self.assertEqual(a.submit_sample(S01)["decision"], "APPROVE")
        again = a.submit_sample(S01); self.assertEqual(again["decision"], "REJECT")                  # a's own history: a paid duplicate
        self.assertEqual(b.submit_sample(S01)["decision"], "APPROVE")                                # b has none, so it is not a duplicate for b
        billed = lambda v: v.get("/api/dashboard")[1]["billed"]["PO-2026-1001"]
        self.assertEqual(billed(a), billed(b))                                                       # one approval each, not two shared

    def test_concurrent_visitors_stay_separate(self):
        vs = [self.app.visitor() for _ in range(3)]; mine = {i: [] for i in range(3)}; err = []
        names = ["01_acme_clean.pdf", "03_novatech_milestone1.pdf", "07_greenfield_price_variance.pdf", "09_brightline_bank_changed.pdf"]
        def work(i):
            try:
                for n in names: mine[i].append(vs[i].submit_sample(n)["id"])
            except Exception as e: err.append(repr(e))
        ts = [threading.Thread(target=work, args=(i,)) for i in range(3)]; [t.start() for t in ts]; [t.join(120) for t in ts]
        self.assertEqual(err, [])
        for i, v in enumerate(vs):
            self.assertEqual(sorted(r["id"] for r in v.runs()), sorted(mine[i]))
        self.assertEqual(len({x for ids in mine.values() for x in ids}), 12)

    def test_each_workspace_has_its_own_database_and_documents_on_disk(self):
        a, b = self.app.visitor(), self.app.visitor(); a.submit_sample(S01); b.submit_sample("07_greenfield_price_variance.pdf")
        for v in (a, b):
            d = os.path.join(self.wsdir, self.ws_of(v)); self.assertTrue(os.path.isfile(os.path.join(d, "runs.db")))
            self.assertEqual(len([f for f in os.listdir(os.path.join(d, "uploads")) if f.endswith(".pdf")]), 1)
        self.assertEqual(len(self.spaces()), 2)
        import sqlite3
        c = sqlite3.connect(self.app.db); self.assertEqual(c.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0, "the shared database must stay empty in workspace mode"); c.close()
        self.assertEqual(os.listdir(self.app.uploads) if os.path.isdir(self.app.uploads) else [], [])


class Cookies(WorkspaceCase):
    def test_cookie_is_private_and_only_issued_when_a_workspace_is_created(self):
        s, h, _ = self.app.request("GET", "/api/dashboard"); sc = h["Set-Cookie"]
        self.assertTrue(sc.startswith("ap_ws=") and "HttpOnly" in sc and "SameSite=Lax" in sc and "Max-Age=86400" in sc and "Path=/" in sc); self.assertNotIn("Secure", sc)
        s, h, _ = self.app.request("GET", "/api/dashboard"); self.assertNotIn("Set-Cookie", h)         # the same visitor keeps the same workspace
        v = self.app.visitor(); s, h, _ = v.request("GET", "/api/dashboard", headers={"X-Forwarded-Proto": "https"}); self.assertIn("Secure", h["Set-Cookie"])

    def test_pages_that_need_no_private_data_create_no_workspace(self):
        for p in ("/", "/healthz", "/api/config", "/api/samples", "/nope"):
            s, h, b = self.app.request("GET", p); self.assertNotIn("Set-Cookie", h, p)
        self.assertEqual(self.spaces(), [])
        self.app.request("GET", "/api/dashboard"); self.assertEqual(len(self.spaces()), 1)

    def test_forged_unknown_or_malicious_cookies_grant_nothing(self):
        victim = self.app.visitor(); r = victim.submit_sample(S01)
        for bad in ("0" * 32, "../" * 8, "A" * 32, self.ws_of(victim) + "x", self.ws_of(victim)[:-1], "deadbeef"):
            v = self.app.visitor(); v.cookies = {"ap_ws": bad}
            s, h, b = v.request("GET", "/api/dashboard"); self.assertEqual(s, 200, bad); self.assertEqual(json.loads(b)["runs"], [], bad)
            self.assertIn("Set-Cookie", h, bad); self.assertNotEqual(h["Set-Cookie"].split(";")[0], "ap_ws=" + bad)
        self.assertEqual([x["id"] for x in victim.runs()], [r["id"]])
        self.assertEqual(sorted(os.listdir(self.wsdir)), sorted(set(os.listdir(self.wsdir))))
        self.assertTrue(all(len(n) == 32 and all(c in "0123456789abcdef" for c in n) for n in self.spaces()))

    def test_health_and_config(self):
        s, h, b = self.app.request("GET", "/healthz"); self.assertEqual((s, b), (200, b"ok"))
        s, cfg = self.app.get("/api/config")
        self.assertTrue(cfg["workspaces"]); self.assertEqual(cfg["workspace_ttl_hours"], 24); self.assertTrue(cfg["reset_enabled"])
        self.assertEqual(cfg["limits"]["max_upload_mb"], 10)


class Lifecycle(WorkspaceCase):
    def test_idle_workspaces_expire_and_active_ones_survive(self):
        old, live = self.app.visitor(), self.app.visitor(); old.submit_sample(S01); live.submit_sample(S01)
        d = os.path.join(self.wsdir, self.ws_of(old)); t = time.time() - 3 * 86400; os.utime(d, (t, t))
        self.assertEqual(self.server.purge_workspaces(86400), 1)
        self.assertFalse(os.path.exists(d)); self.assertEqual(len(self.spaces()), 1); self.assertEqual(len(live.runs()), 1)
        s, h, b = old.request("GET", "/api/dashboard")                                                  # the expired cookie is simply a new visitor
        self.assertEqual(json.loads(b)["runs"], []); self.assertIn("Set-Cookie", h); self.assertNotEqual(old.cookies["ap_ws"], os.path.basename(d))

    def test_reset_clears_only_the_callers_own_workspace_and_frees_its_storage(self):
        a, b = self.app.visitor(), self.app.visitor(); a.submit_sample(S01); rb = b.submit_sample(S01)
        ua = os.path.join(self.wsdir, self.ws_of(a), "uploads"); self.assertTrue(os.listdir(ua))
        self.assertEqual(a.post("/api/reset"), (200, {"ok": True}))
        self.assertEqual(a.runs(), []); self.assertFalse(os.path.exists(ua) and os.listdir(ua))
        self.assertEqual([r["id"] for r in b.runs()], [rb["id"]]); self.assertEqual(b.document(rb["id"])[0], 200)
        self.assertEqual(a.submit_sample(S01)["decision"], "APPROVE")                                    # a starts over from a clean slate

    def test_reset_does_not_disturb_another_visitors_run_that_is_still_in_progress(self):
        self.app.close(); self.setUp_slow()
        a, b = self.app.visitor(), self.app.visitor(); a.submit_sample(S01)
        s, r = b.post("/api/process_sample", {"name": "07_greenfield_price_variance.pdf"}); self.assertEqual(s, 200)
        self.assertEqual(a.post("/api/reset"), (200, {"ok": True}))
        s, live = b.get(f"/api/run/{r['id']}"); self.assertEqual(s, 200, "b's live run must survive a's reset"); self.assertEqual(live["status"], "running")
        done = b.wait(r["id"]); self.assertEqual(done["decision"], "HOLD"); self.assertEqual([x["id"] for x in b.runs()], [r["id"]]); self.assertEqual(a.runs(), [])

    def test_reset_is_refused_outside_workspace_mode_unless_explicitly_enabled(self):
        app = InProcessApp(env={}); self.addCleanup(app.close)
        s, cfg = app.get("/api/config"); self.assertFalse(cfg["workspaces"]); self.assertFalse(cfg["reset_enabled"])
        self.assertEqual(app.post("/api/reset")[0], 403)
        s, h, b = app.request("GET", "/api/dashboard"); self.assertNotIn("Set-Cookie", h)                 # single shared dataset: no cookies at all


class Caps(WorkspaceCase):
    extra_env = {"AP_MAX_RUNS_PER_WS": "2"}

    def test_runs_per_workspace_cap_then_clear_history_helps(self):
        v = self.app.visitor(); v.submit_sample(S01); v.submit_sample("07_greenfield_price_variance.pdf")
        s, r = v.post("/api/process_sample", {"name": "09_brightline_bank_changed.pdf"}); self.assertEqual(s, 429); self.assertIn("Clear history", r["error"])
        self.assertEqual(self.app.visitor().post("/api/process_sample", {"name": S01})[0], 200)           # someone else is unaffected
        v.post("/api/reset"); self.assertEqual(v.post("/api/process_sample", {"name": S01})[0], 200)


class StorageCap(WorkspaceCase):
    extra_env = {"AP_MAX_WS_MB": "1"}

    def test_megabyte_cap_per_workspace(self):
        v = self.app.visitor(); big = b"%PDF-1.4\n" + b"0" * 700_000
        s, r = v.upload(big, "a.pdf"); self.assertEqual(s, 200); v.wait(r["id"], 30)
        s, r = v.request("POST", "/api/upload", big, {"X-Filename": "b.pdf"})[0], None; self.assertEqual(s, 413)
        v.post("/api/reset"); s, r = v.upload(big, "c.pdf"); self.assertEqual(s, 200); v.wait(r["id"], 30)


class Busy(WorkspaceCase):
    extra_env = {"AP_MAX_ACTIVE_RUNS": "1"}
    stage_delay = "0.4"

    def test_too_many_simultaneous_runs_are_turned_away_politely(self):
        a, b = self.app.visitor(), self.app.visitor()
        s, r = a.post("/api/process_sample", {"name": S01}); self.assertEqual(s, 200)
        s2, r2 = b.post("/api/process_sample", {"name": S01}); self.assertEqual(s2, 503); self.assertIn("busy", r2["error"])
        a.wait(r["id"]); self.assertEqual(b.post("/api/process_sample", {"name": S01})[0], 200)


class Capacity(WorkspaceCase):
    extra_env = {"AP_MAX_WORKSPACES": "2"}

    def test_workspace_count_is_capped(self):
        self.app.visitor().get("/api/dashboard"); self.app.visitor().get("/api/dashboard")
        v = self.app.visitor(); s, r = v.get("/api/dashboard"); self.assertEqual(s, 503); self.assertIn("capacity", r["error"]); self.assertEqual(len(self.spaces()), 2)
        self.assertEqual(self.app.get("/api/samples")[0], 200)                                           # static pages still work


class RateLimit(WorkspaceCase):
    extra_env = {"AP_RATE_RUNS_PER_MIN": "2"}

    def test_per_client_rate_limit_cannot_be_dodged_by_dropping_the_cookie_or_spoofing_headers(self):
        def go(v, **h):
            s, _, b = v.request("POST", "/api/process_sample", json.dumps({"name": S01}).encode(), {"Content-Type": "application/json", **h}); return s
        a = self.app.visitor(); self.assertEqual(go(a), 200); self.assertEqual(go(a, **{"X-Forwarded-For": "9.9.9.1"}), 200)
        self.assertEqual(go(a, **{"X-Forwarded-For": "9.9.9.2"}), 429, "X-Forwarded-For must be ignored unless AP_TRUST_PROXY=1")
        self.assertEqual(go(self.app.visitor()), 429, "a brand-new cookie from the same address is still limited")
        for r in a.runs(): pass
        time.sleep(1.5)


class NewSessionLimit(WorkspaceCase):
    extra_env = {"AP_RATE_NEW_WS_PER_10MIN": "3"}

    def test_one_address_cannot_mint_unlimited_workspaces_by_dropping_its_cookie(self):
        codes = [self.app.visitor().request("GET", "/api/dashboard")[0] for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429]); self.assertEqual(len(self.spaces()), 3)
        keeper = self.app.visitor(); keeper.cookies = {"ap_ws": self.spaces()[0]}; self.assertEqual(keeper.request("GET", "/api/dashboard")[0], 200)   # existing visitors are unaffected
        self.assertEqual(self.app.get("/api/config")[0], 200)


class RateLimitBehindProxy(WorkspaceCase):
    extra_env = {"AP_RATE_RUNS_PER_MIN": "1", "AP_TRUST_PROXY": "1"}

    def test_each_forwarded_client_has_its_own_allowance(self):
        def go(ip):
            v = self.app.visitor(); return v.request("POST", "/api/process_sample", json.dumps({"name": S01}).encode(), {"Content-Type": "application/json", "X-Forwarded-For": ip + ", 10.0.0.1"})[0]
        self.assertEqual(go("1.1.1.1"), 200); self.assertEqual(go("1.1.1.1"), 429); self.assertEqual(go("2.2.2.2"), 200)
        time.sleep(1.5)


if __name__ == "__main__":
    unittest.main()
