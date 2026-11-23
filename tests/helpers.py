"""Shared helpers for the isolated test suites.

Nothing here ever touches the live demo app, its database or its uploads:
  * generated invoice PDFs (PyMuPDF) so tests exercise the real pipeline,
  * `InProcessApp`  - the real HTTP handler on an ephemeral port, temp DB + temp upload folder,
  * `SubprocessApp` - `python server.py` on a free port, temp DB + temp upload folder, restartable;
                      it only ever stops the process it started and only deletes its own temp dir.
"""
import json, os, shutil, socket, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request
import fitz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INVOICES = os.path.join(ROOT, "invoices")

AUTO = object()          # "compute this value from the lines"; pass None to omit the field from the PDF


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def money(x):
    return f"{x:,.2f}"


# ---------------------------------------------------------------- invoice generator
def make_invoice(*, vendor="Acme Industrial Supplies Pvt Ltd", gstin="07AABCA1234F1Z5", invoice_no="INV-T-0001",
                 date="05 Nov 2026", po="PO-2026-1001",
                 lines=(("Hydraulic pump HP-200", 10, 12000.0, 120000.0), ("Seal kit SK-11", 5, 2000.0, 10000.0)),
                 subtotal=AUTO, tax_rate=18, tax=AUTO, total=AUTO,
                 bank="50200012345678", ifsc="HDFC0001234", extra_lines=(), as_scan=False) -> bytes:
    """Return PDF bytes laid out like the bundled sample invoices. Any header field set to None is left out."""
    lines = list(lines or [])
    sub = sum(l[3] for l in lines) if subtotal is AUTO else subtotal
    tx = (round(sub * tax_rate / 100, 2) if (sub is not None and tax_rate) else 0.0) if tax is AUTO else tax
    tot = (sub + (tx or 0)) if total is AUTO else total
    doc = fitz.open(); pg = doc.new_page(width=595, height=842)
    y = [60]

    def t(x, s, size=10, bold=False):
        pg.insert_text((x, y[0]), s, fontsize=size, fontname="hebo" if bold else "helv")

    def nl(dy=16): y[0] += dy
    if vendor: t(50, vendor, 14, True)
    nl(18); t(50, "21 Okhla Phase II, New Delhi 110020"); nl()
    if gstin: t(50, f"GSTIN: {gstin}")
    nl(26); t(50, "TAX INVOICE", 12, True); nl(20)
    if invoice_no: t(50, f"Invoice No: {invoice_no}")
    nl(14)
    if date: t(50, f"Invoice Date: {date}")
    nl(14)
    if po: t(50, f"PO Reference: {po}")
    nl(22); t(50, "Bill To: Zenith Retail Pvt Ltd"); nl(30)
    if lines:
        t(50, "Description", bold=True); t(300, "Qty", bold=True); t(360, "Unit Price", bold=True); t(460, "Amount", bold=True); nl(20)
        for d, q, u, a in lines:
            t(50, d); t(300, "" if q is None else str(q)); t(360, money(u)); t(460, money(a)); nl(17)
    for s in extra_lines:
        t(50, s); nl(16)
    nl(8)
    if sub is not None: t(50, "Subtotal"); t(460, money(sub)); nl(16)
    if tax_rate and tx is not None: t(50, f"GST @ {tax_rate}%"); t(460, money(tx)); nl(16)
    if tot is not None: t(50, "Total Due", bold=True); t(460, money(tot), bold=True); nl(30)
    if bank or ifsc:
        t(50, "Payment details", bold=True); nl(16)
        t(50, ("Bank A/C No: " + bank if bank else "") + ("   IFSC: " + ifsc if ifsc else ""))
    data = doc.tobytes()
    if not as_scan: return data
    out = fitz.open(); q = out.new_page(width=595, height=842)
    q.insert_image(q.rect, stream=doc[0].get_pixmap(dpi=170).tobytes("png"))
    return out.tobytes()


def sample_bytes(name):
    return open(os.path.join(INVOICES, name), "rb").read()


# ---------------------------------------------------------------- app wrappers
class AppBase:
    base = ""

    def request(self, method, path, body=None, headers=None, timeout=60):
        hdrs = dict(headers or {}); jar = self.__dict__.setdefault("cookies", {})       # a browser-like cookie jar (private workspaces are cookie-keyed)
        if jar: hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                self._eat(r.headers); return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            self._eat(e.headers); return e.code, dict(e.headers), e.read()

    def _eat(self, headers):
        for c in headers.get_all("Set-Cookie") or []:
            k, _, rest = c.partition("="); self.cookies[k.strip()] = rest.split(";")[0]

    def visitor(self):
        """Another 'browser' talking to the same server: its own cookie jar, so its own private workspace."""
        v = Visitor(); v.base = self.base; return v

    def get(self, path):
        s, h, b = self.request("GET", path)
        try: return s, json.loads(b)
        except Exception: return s, b

    def post(self, path, obj=None):
        s, h, b = self.request("POST", path, json.dumps(obj or {}).encode(), {"Content-Type": "application/json"})
        try: return s, json.loads(b)
        except Exception: return s, b

    def upload(self, data, filename="invoice.pdf"):
        s, h, b = self.request("POST", "/api/upload", data, {"X-Filename": urllib.parse.quote(filename), "Content-Type": "application/pdf"})
        return s, json.loads(b)

    def wait(self, rid, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            s, r = self.get(f"/api/run/{rid}")
            if s == 200 and r.get("status") == "done": return r
            time.sleep(0.02)
        raise TimeoutError(f"run {rid} did not finish")

    def submit(self, data, filename="invoice.pdf", timeout=60):
        """Upload through the real route and wait for the finished run."""
        s, r = self.upload(data, filename)
        assert s == 200, (s, r)
        return self.wait(r["id"], timeout)

    def submit_sample(self, name):
        s, r = self.post("/api/process_sample", {"name": name})
        assert s == 200, (s, r)
        return self.wait(r["id"])

    def runs(self):
        return self.get("/api/dashboard")[1]["runs"]

    def document(self, rid):
        return self.request("GET", f"/api/run/{rid}/document")


import unittest
from unittest import mock

PO_ID = "PO-2026-9001"


class Visitor(AppBase):
    pass


def make_po(net=100000, tax=18, billed=0, vendor="V001", lines=None, status="open"):
    """A synthetic PO (patched into server.POS for the duration of a test; never written to the reference data)."""
    return {"vendor": vendor, "desc": "Synthetic test PO", "net": net, "tax": tax, "billed": billed, "status": status, "lines": lines or []}


class AppTestCase(unittest.TestCase):
    """One isolated in-process app (temp DB + temp uploads, real HTTP routes) per test."""
    stage_delay = "0"
    app_env = None

    def setUp(self):
        self.app = InProcessApp(stage_delay=self.stage_delay, env=self.app_env)
        self.addCleanup(self.app.close)
        self.server = self.app.server

    def patch_pos(self, **pos):
        p = mock.patch.dict(self.server.POS, pos); p.start(); self.addCleanup(p.stop)

    def stage(self, run, key):
        return next(s for s in run["stages"] if s["key"] == key)

    def invoice(self, n=1, net=100000.0, po=PO_ID, filename=None, **kw):
        """Submit a generated invoice with one itemised line of value `net` (+18% GST) and return the finished run."""
        kw.setdefault("invoice_no", f"INV-T-{n:04d}")
        kw.setdefault("lines", [("Custom fabrication job", 1, net, net)])
        return self.app.submit(make_invoice(po=po, **kw), filename or f"inv{n}.pdf")

    def approved_net(self, po=PO_ID):
        return sum(r["extracted"]["subtotal"] for r in self.app.runs() if r["decision"] == "APPROVE" and r.get("po") == po)


class InProcessApp(AppBase):
    """The real HTTP handler of server.py, served from this process on an ephemeral port with a temp DB/upload dir."""
    ENV_KEYS = ("AP_DB", "AP_UPLOADS", "STAGE_DELAY", "AP_ALLOW_RESET", "AP_LLM", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                "SARVAM_API_KEY", "SARVAM_BASE_URL", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_BASE_URL", "AP_LLM_PROVIDERS", "AP_SARVAM_MODEL", "AP_GEMINI_MODEL",
                "AP_LLM_MAX_PER_HOUR", "AP_LLM_MAX_PER_WS_HOUR", "AP_WORKSPACES", "AP_WORKSPACES_DIR", "AP_TRUST_PROXY", "AP_RATE_RUNS_PER_MIN",
                "AP_MAX_ACTIVE_RUNS", "AP_MAX_RUNS_PER_WS", "AP_MAX_WS_MB", "AP_MAX_WORKSPACES", "AP_WORKSPACE_TTL_HOURS", "AP_COOKIE_SECURE", "AP_LOG_REQUESTS", "AP_RATE_NEW_WS_PER_10MIN")

    def __init__(self, stage_delay="0", env=None, allow_reset=False):
        sys.path.insert(0, ROOT)
        import server
        self.server = server
        self.tmp = tempfile.mkdtemp(prefix="ap-inproc-")
        self.db = os.path.join(self.tmp, "runs.db"); self.uploads = os.path.join(self.tmp, "uploads")
        self._saved = {k: os.environ.get(k) for k in self.ENV_KEYS}
        for k in self.ENV_KEYS: os.environ.pop(k, None)             # never inherit a developer's real keys or deployment settings
        os.environ.update(AP_DB=self.db, AP_UPLOADS=self.uploads, STAGE_DELAY=stage_delay, AP_ALLOW_RESET="1" if allow_reset else "0", AP_LLM="off", AP_LOG_REQUESTS="0")
        os.environ.update(env or {})
        server.RUNS.clear(); server.HOOKS.clear(); server.reset_llm_state(); server.RATE.hits.clear()
        server.init_store()
        self.httpd = server.make_server("127.0.0.1", 0)
        self.port = self.httpd.server_address[1]; self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reopen(self):
        """Simulate a restart: forget in-memory runs and re-initialise the store from the same files."""
        self.server.RUNS.clear(); self.server.init_store()

    def close(self):
        self.server.HOOKS.clear()
        self.httpd.shutdown(); self.httpd.server_close()
        for k, v in self._saved.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)


class SubprocessApp(AppBase):
    """`python server.py` on a free port with temp DB + temp uploads. Stops only its own process."""

    def __init__(self, root=ROOT, stage_delay="0", env=None, allow_reset=True, tmp=None):
        self.root = root
        self.tmp = tmp or tempfile.mkdtemp(prefix="ap-sub-")
        self._owns_tmp = tmp is None
        self.db = os.path.join(self.tmp, "runs.db"); self.uploads = os.path.join(self.tmp, "uploads")
        self.port = free_port(); self.base = f"http://127.0.0.1:{self.port}"
        self.stage_delay, self.extra_env, self.allow_reset = stage_delay, env or {}, allow_reset
        self.proc = None

    def start(self, timeout=30):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "SARVAM_", "GEMINI_", "GOOGLE_API_KEY", "AP_"))}
        env.update(PORT=str(self.port), HOST="127.0.0.1", AP_DB=self.db, AP_UPLOADS=self.uploads, STAGE_DELAY=self.stage_delay,
                   AP_ALLOW_RESET="1" if self.allow_reset else "0", AP_LLM="off", PYTHONDONTWRITEBYTECODE="1")
        env.update(self.extra_env)
        self.log = open(os.path.join(self.tmp, "server.log"), "ab")
        self.proc = subprocess.Popen([sys.executable, os.path.join(self.root, "server.py")], cwd=self.root, env=env, stdout=self.log, stderr=self.log)
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None: raise RuntimeError("server exited: " + open(os.path.join(self.tmp, "server.log")).read()[-800:])
            try:
                urllib.request.urlopen(self.base + "/api/samples", timeout=1).read(); return self
            except Exception: time.sleep(0.15)
        raise RuntimeError("server did not start")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try: self.proc.wait(10)
            except subprocess.TimeoutExpired: self.proc.kill(); self.proc.wait()
        self.proc = None

    def restart(self):
        self.stop(); return self.start()

    def cleanup(self):
        self.stop()
        try: self.log.close()
        except Exception: pass
        if self._owns_tmp: shutil.rmtree(self.tmp, ignore_errors=True)
