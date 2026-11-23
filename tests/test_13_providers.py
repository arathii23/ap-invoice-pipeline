"""LLM providers: Sarvam first, Gemini when Sarvam does not answer (Anthropic optional). Everything here runs against MOCK providers on
localhost - it proves request shape, fallback order, cooldowns, key hygiene, budgets and that grounding still applies. It does NOT prove
the real Sarvam / Gemini services accept these requests (see tools/check_llm.py and test_llm_real.py, which are opt-in)."""
import json, threading, unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from helpers import AppTestCase, make_invoice

SK, GK = "sarvam-secret-key-1234567890", "gemini-secret-key-1234567890"
GOOD = {"bank": "50200012345678"}


class Mock:
    """One server that speaks both APIs. Behaviour per provider: ok | status:<code>[:retry-after] | think | empty | blocked | header403 | reject_options | echo_key."""
    def __init__(self):
        self.mode = {"sarvam": "ok", "gemini": "ok"}; self.payload = dict(GOOD); self.calls = []
        outer = self
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                u = urlparse(self.path); name = "sarvam" if u.path.startswith("/v1/chat/completions") else "gemini"
                outer.calls.append({"provider": name, "path": u.path, "query": parse_qs(u.query), "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                mode = outer.mode[name]; payload = json.dumps(outer.payload)
                def send(code, obj, extra=None):
                    b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Length", str(len(b)))
                    for k, v in (extra or {}).items(): self.send_header(k, v)
                    self.end_headers(); self.wfile.write(b)
                if mode.startswith("status:"):
                    parts = mode.split(":"); code = int(parts[1]); return send(code, {"error": {"message": f"boom {code}"}}, {"Retry-After": parts[2]} if len(parts) > 2 else None)
                if mode == "echo_key": return send(403, {"error": {"message": "Invalid API key: " + (self.headers.get("api-subscription-key") or self.headers.get("x-goog-api-key") or "") + " (also ?key=" + (u.query[4:] or "-") + ")"}})
                if name == "sarvam":
                    if mode == "reject_options" and ("reasoning_effort" in body or "response_format" in body): return send(400, {"error": {"message": "unsupported option"}})
                    text = {"think": "<think>let me look</think>Here you go: " + payload, "empty": "   "}.get(mode, payload)
                    return send(200, {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]})
                if mode == "header403" and "key" not in parse_qs(u.query): return send(403, {"error": {"message": "API key not valid", "status": "PERMISSION_DENIED"}})
                if mode == "blocked": return send(200, {"promptFeedback": {"blockReason": "SAFETY"}})
                if mode == "empty": return send(200, {"candidates": [{"content": {"parts": [{"text": ""}]}, "finishReason": "STOP"}]})
                return send(200, {"candidates": [{"content": {"parts": [{"thought": True, "text": "thinking"}, {"text": payload}]}, "finishReason": "STOP"}]})
        self.srv = HTTPServer(("127.0.0.1", 0), H); self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
    def stop(self): self.srv.shutdown(); self.srv.server_close()
    def n(self, name): return sum(1 for c in self.calls if c["provider"] == name)


def odd_invoice(n):
    return make_invoice(invoice_no=f"INV-P-{n:04d}", bank=None, ifsc=None, lines=[], subtotal=120000.0,
                        extra_lines=["Hydraulic pump HP-200 - 10 pcs at 12,000.00 each = 120,000.00", "Payee Acct: 50200012345678"])


class Providers(AppTestCase):
    def setUp(self):
        self.mock = Mock(); self.addCleanup(self.mock.stop)
        self.app_env = {"SARVAM_API_KEY": SK, "SARVAM_BASE_URL": self.mock.url, "GEMINI_API_KEY": GK, "GEMINI_BASE_URL": self.mock.url, "AP_LLM": "auto"}
        super().setUp()

    def call(self): return self.server.llm_call("Payee Acct: 50200012345678")

    # ---- configuration
    def test_chain_follows_configuration_and_keys(self):
        s = self.server
        self.assertEqual(s.active_providers(), ["sarvam", "gemini"]); self.assertTrue(s.llm_configured())
        self.assertEqual(s.llm_model(), "sarvam-105b → gemini-3.5-flash-lite")
        import os
        os.environ["AP_LLM_PROVIDERS"] = "gemini, bogus, sarvam, gemini"; self.assertEqual(s.active_providers(), ["gemini", "sarvam"])
        del os.environ["GEMINI_API_KEY"]; self.assertEqual(s.active_providers(), ["sarvam"])
        os.environ["GOOGLE_API_KEY"] = GK; self.assertEqual(s.active_providers(), ["gemini", "sarvam"])      # the alternative env name works
        os.environ["AP_LLM"] = "off"; self.assertEqual(s.active_providers(), []); self.assertFalse(s.llm_configured())

    # ---- request shape + fallback
    def test_sarvam_answers_and_gemini_is_not_called(self):
        got, name, attempts = self.call()
        self.assertEqual((got, name), (GOOD, "sarvam")); self.assertEqual(self.mock.n("gemini"), 0)
        c = self.mock.calls[0]; self.assertEqual(c["path"], "/v1/chat/completions")
        self.assertEqual(c["headers"]["api-subscription-key"], SK); self.assertEqual(c["headers"]["authorization"], "Bearer " + SK)
        self.assertEqual((c["body"]["model"], c["body"]["temperature"]), ("sarvam-105b", 0)); self.assertIsNone(c["body"]["reasoning_effort"])
        self.assertEqual(c["body"]["response_format"], {"type": "json_object"})

    def test_gemini_takes_over_when_sarvam_does_not_answer(self):
        for bad in ("status:500", "status:503", "empty"):
            self.server.reset_llm_state(); self.mock.calls.clear(); self.mock.mode["sarvam"] = bad
            got, name, attempts = self.call()
            self.assertEqual((got, name), (GOOD, "gemini"), bad); self.assertEqual([a["outcome"] for a in attempts], ["error", "ok"], bad)
        g = [c for c in self.mock.calls if c["provider"] == "gemini"][0]
        self.assertEqual(g["path"], "/v1beta/models/gemini-3.5-flash-lite:generateContent"); self.assertEqual(g["headers"]["x-goog-api-key"], GK)
        self.assertEqual(g["body"]["generationConfig"]["responseMimeType"], "application/json")

    def test_sarvam_options_it_rejects_are_dropped_and_the_plain_request_is_retried(self):
        self.mock.mode["sarvam"] = "reject_options"
        got, name, _ = self.call()
        self.assertEqual((got, name), (GOOD, "sarvam")); self.assertEqual(self.mock.n("sarvam"), 2); self.assertEqual(self.mock.n("gemini"), 0)
        self.assertNotIn("reasoning_effort", self.mock.calls[1]["body"]); self.assertNotIn("response_format", self.mock.calls[1]["body"])

    def test_reasoning_text_and_prose_around_the_json_are_tolerated(self):
        self.mock.mode["sarvam"] = "think"; self.assertEqual(self.call()[:2], (GOOD, "sarvam"))

    def test_gemini_key_is_resent_as_a_query_parameter_if_the_header_form_is_refused(self):
        self.mock.mode.update(sarvam="status:500", gemini="header403")
        got, name, _ = self.call(); self.assertEqual((got, name), (GOOD, "gemini"))
        gem = [c for c in self.mock.calls if c["provider"] == "gemini"]; self.assertEqual(len(gem), 2); self.assertEqual(gem[1]["query"]["key"], [GK])

    # ---- cooldowns: a bad key / no credits / rate limit must not slow every later run
    def test_bad_key_or_no_credits_pause_the_provider_and_later_calls_skip_it(self):
        for code in (402, 403):
            self.server.reset_llm_state(); self.mock.calls.clear(); self.mock.mode["sarvam"] = f"status:{code}"
            self.call(); self.assertEqual(self.mock.n("sarvam"), 1)
            got, name, attempts = self.call()
            self.assertEqual((name, self.mock.n("sarvam")), ("gemini", 1), "sarvam must not be contacted again while paused")
            self.assertEqual(attempts[0]["outcome"], "skipped"); self.assertIn("paused", attempts[0]["reason"])

    def test_rate_limit_pause_honours_retry_after(self):
        self.mock.mode["sarvam"] = "status:429:7"; self.call()
        left = self.server._COOLDOWN["sarvam"] - __import__("time").monotonic(); self.assertTrue(5 < left <= 7.5, left)

    # ---- everything failing
    def test_all_providers_failing_raises_with_every_reason(self):
        self.mock.mode.update(sarvam="status:500", gemini="blocked")
        with self.assertRaises(self.server.LlmUnavailable) as cm: self.call()
        msg = str(cm.exception); self.assertIn("sarvam: HTTPError 500", msg); self.assertIn("gemini: malformed response: no candidates (prompt blocked: SAFETY)", msg)

    # ---- keys never leak
    def test_keys_are_scrubbed_from_errors_and_never_exposed(self):
        self.mock.mode.update(sarvam="echo_key", gemini="echo_key")
        with self.assertRaises(self.server.LlmUnavailable) as cm: self.call()
        self.assertNotIn(SK, str(cm.exception)); self.assertNotIn(GK, str(cm.exception)); self.assertIn("***", str(cm.exception))
        r = self.app.submit(odd_invoice(1), "leak.pdf"); blob = json.dumps(r) + json.dumps(self.app.get("/api/config")[1]) + json.dumps(self.app.runs())
        for k in (SK, GK): self.assertNotIn(k, blob)
        self.assertEqual(self.app.get("/api/config")[1]["extraction"]["llm_providers"], ["sarvam", "gemini"])

    # ---- budgets
    def test_hourly_budgets_cap_provider_calls(self):
        import os
        os.environ["AP_LLM_MAX_PER_HOUR"] = "2"; self.call(); self.call()
        with self.assertRaises(self.server.LlmUnavailable) as cm: self.call()
        self.assertIn("hourly LLM budget", str(cm.exception)); self.assertEqual(self.mock.n("sarvam"), 2)

    # ---- end to end: the pipeline, grounding and the explanation
    def test_pipeline_uses_the_provider_and_reports_which_one_answered(self):
        r = self.app.submit(odd_invoice(2), "odd.pdf"); llm = r["extraction"]["llm"]
        self.assertTrue(llm["used"]); self.assertEqual(llm["provider"], "sarvam"); self.assertIn("bank", llm["filled"]); self.assertEqual(r["extracted"]["bank"], "50200012345678")
        self.assertIn("LLM (sarvam) filled gaps", self.stage(r, "extract")["detail"])

    def test_pipeline_shows_the_fallback_in_the_explanation(self):
        self.mock.mode["sarvam"] = "status:500"; r = self.app.submit(odd_invoice(3), "odd.pdf"); llm = r["extraction"]["llm"]
        self.assertEqual(llm["provider"], "gemini"); self.assertEqual([a["outcome"] for a in llm["attempts"]], ["error", "ok"])
        self.assertIn("LLM (gemini after sarvam failed) filled gaps", self.stage(r, "extract")["detail"])

    def test_grounding_still_rejects_invented_values_from_any_provider(self):
        self.mock.payload = {"bank": "123", "invoice_no": "INV-FAKE-999"}
        for prov in ("sarvam", "gemini"):
            self.server.reset_llm_state(); self.mock.mode.update(sarvam="status:500" if prov == "gemini" else "ok")
            r = self.app.submit(odd_invoice(4 if prov == "sarvam" else 5), f"{prov}.pdf"); llm = r["extraction"]["llm"]
            self.assertEqual(llm["provider"], prov); self.assertIn("bank", llm["rejected"]); self.assertNotEqual(r["extracted"]["bank"], "123")
            self.assertNotEqual(r["extracted"]["invoice_no"], "INV-FAKE-999")

    def test_a_run_still_completes_with_rules_only_when_every_provider_is_down(self):
        self.mock.mode.update(sarvam="status:500", gemini="status:503"); r = self.app.submit(odd_invoice(6), "down.pdf")
        llm = r["extraction"]["llm"]; self.assertEqual(r["status"], "done"); self.assertFalse(r.get("error")); self.assertFalse(llm["used"])
        self.assertIn("sarvam", llm["error"]); self.assertIn("gemini", llm["error"]); self.assertIn("continued with rules only", self.stage(r, "extract")["detail"])


if __name__ == "__main__":
    unittest.main()
