#!/usr/bin/env python3
"""AP invoice pipeline: PDF -> extract (text or OCR) -> validate -> reasoned decision.
Run:  python3 server.py   then open http://localhost:8765
Needs pypdf + PyMuPDF (+ an OCR engine for scanned pages). See README.md.
"""
import contextlib, contextvars, difflib, hashlib, json, os, re, shutil, socket, sqlite3, ssl, subprocess, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

import pypdf

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
INV_DIR = os.path.join(BASE, "invoices")
OCR_BIN = os.path.join(BASE, "ocr", "ocr_bin")
SCHEMA_VERSION = 2               # run records written by this version carry ver=2 (older records have no "ver")
MAX_UPLOAD = 10 * 1024 * 1024
MAX_JSON_BODY = 64 * 1024

# ---------- configuration (read at call time so tests and deployments can isolate DB / uploads via env) ----------
_WS = contextvars.ContextVar("workspace", default=None)      # the visitor's private workspace for this request / run (None = single shared dataset)
def workspaces_enabled(): return os.environ.get("AP_WORKSPACES", "") == "1"
def workspaces_dir(): return os.environ.get("AP_WORKSPACES_DIR") or os.path.join(DATA, "workspaces")
def ws_dir(ws): return os.path.join(workspaces_dir(), ws)
def _int_env(name, default):
    try: return int(os.environ.get(name, default))
    except ValueError: return default
def db_path():
    ws = _WS.get()
    return os.path.join(ws_dir(ws), "runs.db") if ws else (os.environ.get("AP_DB") or os.path.join(DATA, "runs.db"))
def uploads_dir():
    ws = _WS.get()
    return os.path.join(ws_dir(ws), "uploads") if ws else (os.environ.get("AP_UPLOADS") or os.path.join(DATA, "uploads"))
def reset_enabled():          # with workspaces a reset can only ever touch the caller's OWN workspace, so it is safe to offer publicly
    return os.environ.get("AP_ALLOW_RESET", "") == "1" or workspaces_enabled()
def llm_mode(): return os.environ.get("AP_LLM", "auto").lower()
def anthropic_model(): return os.environ.get("AP_LLM_MODEL", "claude-haiku-5-5")
def stage_delay():
    try: return max(0.0, float(os.environ.get("STAGE_DELAY", "0.55")))
    except ValueError: return 0.55

# ---------- business rules (shown in the UI) ----------
RULES = {
    "price_tolerance_pct": 2.0,          # invoice net vs PO remaining value / unit price: auto-approve inside this band
    "fuzzy_vendor_name": 0.85,           # name similarity needed to identify a vendor by name
    "required_fields": ["vendor", "invoice_no", "invoice_date", "total"],
    "rounding_allowance_inr": 1.00,      # arithmetic: totals, tax and line sums may differ by up to this much
    "line_rounding_per_unit_inr": 0.005, # arithmetic: unit prices are printed to 2 dp, so a line may drift by qty x this
    "ocr_identity_min_corroborations": 2,  # identity recovery on OCR/name only: independent signals that must agree with the master
    "date_year_range": [2000, 2100],     # dates are read day-first; no rule compares them to today's date
}
def read_bytes(path):
    with open(path, "rb") as f: return f.read()

VENDORS = json.loads(read_bytes(os.path.join(DATA, "vendors.json")))
POS = json.loads(read_bytes(os.path.join(DATA, "pos.json")))

LOCK = threading.Lock()          # decision critical section (history read -> checks -> decision -> persist), within this process
UP_LOCK = threading.Lock()       # makes the "same upload already running" guard atomic
RUNS = {}                        # live in-memory runs (every run is also persisted when its decision is made)
HOOKS = {}                       # test-only synchronisation points: name -> callable(run); empty in production

def hook(name, run=None):
    fn = HOOKS.get(name)
    if fn: fn(run)

def D(x):
    return Decimal(str(x))

def public(r):
    return {k: v for k, v in r.items() if not k.startswith("_")}

def own_run(rid):
    """An in-memory run, but only if it belongs to the current visitor's workspace."""
    r = RUNS.get(rid)
    return r if r is not None and r.get("_ws") == _WS.get() else None

def purge_runs(keep_s=600):
    now = time.time()
    for k, v in list(RUNS.items()):
        if v.get("status") == "done" and now - v.get("_done", now) > keep_s: RUNS.pop(k, None)   # the database is the record; memory is only for live runs

# ---------- private demo workspaces (AP_WORKSPACES=1): each visitor gets an isolated database + upload folder, keyed by a random cookie ----------
WS_ID = re.compile(r"[0-9a-f]{32}")
class WorkspaceBusy(Exception): pass
_TOUCHED = {}

def _ws_names():
    try: return [n for n in os.listdir(workspaces_dir()) if WS_ID.fullmatch(n) and os.path.isdir(ws_dir(n))]
    except OSError: return []

def touch_ws(ws):
    now = time.time()
    if now - _TOUCHED.get(ws, 0) < 60: return
    _TOUCHED[ws] = now
    try: os.utime(ws_dir(ws), None)
    except OSError: pass

def purge_workspaces(max_idle_s):
    now, n = time.time(), 0
    for name in _ws_names():
        d = ws_dir(name)
        try: idle = now - os.path.getmtime(d)
        except OSError: continue
        if idle > max_idle_s:
            shutil.rmtree(d, ignore_errors=True); _SCHEMA_READY.discard(os.path.join(d, "runs.db")); _TOUCHED.pop(name, None); n += 1
    return n

def create_workspace():
    os.makedirs(workspaces_dir(), exist_ok=True)
    cap = _int_env("AP_MAX_WORKSPACES", 500)
    if len(_ws_names()) >= cap:
        purge_workspaces(3600)
        if len(_ws_names()) >= cap: raise WorkspaceBusy()
    ws = uuid.uuid4().hex; os.makedirs(ws_dir(ws)); return ws

def start_janitor():
    ttl = _int_env("AP_WORKSPACE_TTL_HOURS", 24) * 3600
    def loop():
        while True:
            time.sleep(600)
            try: purge_workspaces(ttl); purge_runs()
            except Exception as e: print("janitor error:", e, flush=True)
    threading.Thread(target=loop, daemon=True).start()

class RateLimiter:
    def __init__(self): self.hits, self.lock = {}, threading.Lock()
    def allow(self, key, limit, window=60):
        now = time.monotonic()
        with self.lock:
            q = [t for t in self.hits.get(key, []) if now - t < window]
            ok = len(q) < limit
            if ok: q.append(now)
            self.hits[key] = q
            if len(self.hits) > 5000: self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < window}
            return ok
RATE = RateLimiter()

# ---------- persistence ----------
_SCHEMA_READY = set()

def _schema(c):
    c.execute("""CREATE TABLE IF NOT EXISTS runs(
        id TEXT PRIMARY KEY, ts TEXT, filename TEXT, file_hash TEXT, vendor_id TEXT,
        invoice_no TEXT, po TEXT, net REAL, total REAL, decision TEXT, headline TEXT, body TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS attachments(
        id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, size INTEGER NOT NULL, stored_name TEXT NOT NULL,
        original_name TEXT, source TEXT, created TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS run_attachments(
        run_id TEXT PRIMARY KEY, attachment_id TEXT, state TEXT NOT NULL, note TEXT)""")

def db():
    path = db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    c = sqlite3.connect(path, timeout=30, isolation_level=None)      # autocommit; transactions are explicit
    c.execute("PRAGMA busy_timeout=30000")
    if path not in _SCHEMA_READY:
        _schema(c); _SCHEMA_READY.add(path)
    return c

@contextlib.contextmanager
def conn():
    c = db()
    try: yield c
    finally: c.close()

@contextlib.contextmanager
def exclusive_txn():
    """THE critical section. Holds the process lock and SQLite's write lock (BEGIN IMMEDIATE) from the history read until the
    commit, so the duplicate check, PO-balance check, decision and its persistence are one operation: a concurrent request
    always evaluates against the other request's committed decision, never against stale history."""
    with LOCK:
        c = db()
        try:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK"); raise
            c.execute("COMMIT")
        finally:
            c.close()

def history(c):
    """All saved runs, oldest first."""
    return [json.loads(b[0]) for b in c.execute("SELECT body FROM runs ORDER BY ts, rowid").fetchall()]

def load_runs():
    with conn() as c: return list(reversed(history(c)))

def get_run_row(rid):
    with conn() as c: b = c.execute("SELECT body FROM runs WHERE id=?", (rid,)).fetchone()
    return json.loads(b[0]) if b else None

def persist_run(c, r):
    """Insert the finished run (never replaces an existing history row) and its attachment link, inside the caller's transaction."""
    ex = r.get("extracted") or {}
    c.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
        r["id"], r["ts"], r["filename"], r.get("file_hash", ""), r.get("vendor_id"),
        ex.get("invoice_no"), r.get("po"), ex.get("subtotal"), ex.get("total"),
        r["decision"], r["headline"], json.dumps(public(r))))
    if r.get("attachment_id"):
        c.execute("INSERT OR REPLACE INTO run_attachments VALUES(?,?,?,?)", (r["id"], r["attachment_id"], "ok", None))

def init_store():
    """Create tables and link every pre-existing run to its original document where that is provably recoverable."""
    _SCHEMA_READY.discard(db_path())
    with conn() as c: pass
    migrate_legacy()

# ---------- immutable document store ----------
class AttachmentUnavailable(Exception):
    pass

def safe_name(name):
    n = os.path.basename((name or "").replace("\\", "/"))
    n = re.sub(r"[\x00-\x1f\x7f\"]", "", n).strip()
    return (n or "upload.pdf")[:120]

def sha_hex(b): return hashlib.sha256(b).hexdigest()

def store_attachment(data, original_name, source, c=None):
    """Keep an immutable copy of the document under a unique id. The display filename is metadata only; it never addresses a file,
    so repeated or colliding filenames cannot overwrite or shadow another run's document."""
    d = uploads_dir(); os.makedirs(d, exist_ok=True)
    aid = uuid.uuid4().hex
    path = os.path.join(d, aid + ".pdf")
    with open(path, "xb") as f: f.write(data)            # exclusive create: refuses to overwrite
    os.chmod(path, 0o444)
    row = (aid, sha_hex(data), len(data), aid + ".pdf", safe_name(original_name), source, datetime.now().isoformat(timespec="seconds"))
    if c is not None: c.execute("INSERT INTO attachments VALUES(?,?,?,?,?,?,?)", row)
    else:
        with conn() as c2: c2.execute("INSERT INTO attachments VALUES(?,?,?,?,?,?,?)", row)
    return aid

def read_attachment(aid):
    """-> (bytes, original_name). Raises AttachmentUnavailable with a user-facing reason; never returns another document."""
    with conn() as c:
        row = c.execute("SELECT sha256, stored_name, original_name FROM attachments WHERE id=?", (aid,)).fetchone()
    if not row: raise AttachmentUnavailable("The attachment record is missing.")
    try: data = read_bytes(os.path.join(uploads_dir(), row[1]))
    except OSError: raise AttachmentUnavailable("The stored file is missing from disk.")
    if sha_hex(data) != row[0]: raise AttachmentUnavailable("The stored file no longer matches its recorded checksum, so it is not shown.")
    return data, row[2]

def _inside(path, dirs):
    rp = os.path.realpath(path)
    return any(rp == d or rp.startswith(d + os.sep) for d in dirs)

def link_legacy(c, run):
    """Runs written before attachments existed point at data/uploads/<filename> (or a bundled sample). Link such a run to its
    document only when the file's checksum equals the hash recorded on the run; otherwise report it unavailable. The run
    record itself is never modified."""
    rid, wanted, name = run["id"], run.get("file_hash") or "", safe_name(run.get("filename"))
    dirs = {os.path.realpath(d) for d in (uploads_dir(), os.path.join(DATA, "uploads"), INV_DIR)}
    cands = [run.get("path"), os.path.join(uploads_dir(), name), os.path.join(DATA, "uploads", name), os.path.join(INV_DIR, name)]
    found, differs = None, False
    for p in dict.fromkeys(x for x in cands if x):
        if not os.path.isfile(p) or not _inside(p, dirs): continue
        data = read_bytes(p)
        if wanted and sha_hex(data)[:16] == wanted: found = data; break
        differs = True
    if found is not None:
        aid = store_attachment(found, name, "legacy", c)
        c.execute("INSERT OR REPLACE INTO run_attachments VALUES(?,?,?,?)", (rid, aid, "ok", None))
        return
    note = ("The original file was overwritten by a later upload with the same filename, so it can no longer be shown."
            if differs else "The original file was not kept by the earlier version, so it can no longer be shown.")
    c.execute("INSERT OR REPLACE INTO run_attachments VALUES(?,?,?,?)", (rid, None, "unavailable", note))

def migrate_legacy():
    with exclusive_txn() as c:
        rows = c.execute("SELECT body FROM runs WHERE id NOT IN (SELECT run_id FROM run_attachments)").fetchall()
        for (b,) in rows: link_legacy(c, json.loads(b))

def attachment_state(rid):
    """-> (state, attachment_id, note) for a run: 'ok' | 'unavailable' | None (unknown run)."""
    r = own_run(rid)
    if r and r.get("attachment_id"): return "ok", r["attachment_id"], None
    with conn() as c:
        row = c.execute("SELECT state, attachment_id, note FROM run_attachments WHERE run_id=?", (rid,)).fetchone()
        if row is None:
            b = c.execute("SELECT body FROM runs WHERE id=?", (rid,)).fetchone()
            if b is None: return None, None, "Run not found."
            with LOCK:
                c.execute("BEGIN IMMEDIATE")
                try:
                    if not c.execute("SELECT 1 FROM run_attachments WHERE run_id=?", (rid,)).fetchone(): link_legacy(c, json.loads(b[0]))
                    c.execute("COMMIT")
                except BaseException:
                    c.execute("ROLLBACK"); raise
            row = c.execute("SELECT state, attachment_id, note FROM run_attachments WHERE run_id=?", (rid,)).fetchone()
    return row[0], row[1], row[2]

def document_info(rid):
    state, aid, note = attachment_state(rid)
    if state == "ok":
        try: read_attachment(aid); return {"available": True, "url": f"/api/run/{rid}/document", "reason": None}
        except AttachmentUnavailable as e: return {"available": False, "url": None, "reason": str(e)}
    return {"available": False, "url": None, "reason": note}


# ---------- extraction ----------
import importlib.util, shutil, sys, urllib.request
import fitz  # PyMuPDF: renders every page, any OS

CUR = r"(?:₹|Rs\.?|INR)?\s*"
AMT = CUR + r"([\d,]+\.\d{1,2}|[\d,]{4,})"        # labelled totals (decimals optional)
AMTD = CUR + r"([\d,]+\.\d{2})"                    # table cells (decimals required)
DATE = r"(\d{4}-\d{2}-\d{2}|\d{1,2}[ \-/.]?(?:[A-Za-z]{3,9}|\d{1,2})[ \-/.]?\d{2,4})"   # ISO first, or it is truncated to YYYY-MM

def num(s):
    return float(s.replace(",", "")) if s else None

def pdf_pages_text(path):
    return [(p.extract_text() or "").strip() for p in pypdf.PdfReader(path).pages]

# ---- OCR engine chain: Apple Vision -> Tesseract -> RapidOCR. First available wins. ----
def ocr_engines():
    forced = os.environ.get("AP_OCR_ENGINE")
    avail = []
    if sys.platform == "darwin" and os.access(OCR_BIN, os.X_OK): avail.append("apple-vision")
    if shutil.which("tesseract"): avail.append("tesseract")
    if importlib.util.find_spec("rapidocr_onnxruntime"): avail.append("rapidocr")
    return [forced] if forced in avail else avail

from ocr_worker import group_lines as _group_lines      # shared with the OCR worker process

_OCR_LOCK = threading.Semaphore(1)     # one OCR at a time: a small (512 MB) host cannot hold two engines' working memory
def ocr_render_dpi(rect, dpi=200, max_side_px=2400):
    """Render resolution for OCR: 200 dpi, but never more than max_side_px on the long side (a poster-sized page must not become a 200 MB bitmap)."""
    return max(30, int(min(dpi, max_side_px * 72.0 / max(rect.width, rect.height, 1.0))))

def _ocr_run(png, engine):
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "page.png"); open(f, "wb").write(png)
        if engine == "apple-vision":
            return subprocess.run([OCR_BIN, f], check=True, capture_output=True, text=True).stdout.strip()
        if engine == "tesseract":
            return subprocess.run(["tesseract", f, "stdout", "--psm", "6"], check=True, capture_output=True, text=True).stdout.strip()
        if engine == "rapidocr":      # in a child process: its memory is returned afterwards, and an out-of-memory kill takes down only the child
            r = subprocess.run([sys.executable, os.path.join(BASE, "ocr_worker.py"), f], capture_output=True, text=True, timeout=_int_env("AP_OCR_TIMEOUT_S", 180),
                               env={**{k: v for k, v in os.environ.items() if not re.search(r"KEY|TOKEN|SECRET", k, re.I)}, "PYTHONDONTWRITEBYTECODE": "1"})
            if r.returncode != 0:
                raise RuntimeError("OCR worker " + (f"was killed (signal {-r.returncode}; out of memory?)" if r.returncode < 0 else f"failed: {(r.stderr or '').strip()[-200:]}"))
            return r.stdout.strip()
    raise RuntimeError(f"unknown OCR engine {engine}")

def ocr_png(png, engine):
    with _OCR_LOCK: return _ocr_run(png, engine)

def extract_text(path):
    """Per-page: use the text layer when present, otherwise OCR just that page.
    Returns (text, info) with info = {pages, ocr_pages, engine}."""
    pages = pdf_pages_text(path); out, ocr_pages, engine = [], [], None
    need, cap = sum(1 for t in pages if len(t) < 40), _int_env("AP_OCR_MAX_PAGES", 8)
    if need > cap: raise RuntimeError(f"{need} pages of this document need OCR; this server reads at most {cap} scanned pages per document. Upload a text-based PDF or split it.")
    doc = None
    for i, t in enumerate(pages):
        if len(t) >= 40: out.append(t); continue
        engs = ocr_engines()
        if not engs: raise RuntimeError("page %d has no text layer and no OCR engine is installed (need Apple Vision, tesseract or rapidocr)" % (i + 1))
        doc = doc or fitz.open(path)
        png = doc[i].get_pixmap(dpi=ocr_render_dpi(doc[i].rect)).tobytes("png")
        last = None
        for e in engs:
            try: out.append(ocr_png(png, e)); engine = e; ocr_pages.append(i + 1); break
            except Exception as ex: last = ex
        else: raise RuntimeError(f"OCR failed on page {i+1}: {last}")
    return "\n".join(out), {"pages": len(pages), "ocr_pages": ocr_pages, "engine": engine}

# ---- rule-based parser (layout tolerant) ----
def parse_invoice(text):
    """Regex extraction with label synonyms; fields not found stay None (never guessed)."""
    t = text
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    f = {}
    f["vendor"] = next((l for l in lines if not re.match(r"^(tax\s+)?invoice\b|^page\b|^original|^duplicate", l, re.I)), None)
    head = re.split(r"Bill(?:ed)?\s*To", t, maxsplit=1, flags=re.I)[0]
    m = re.search(r"GSTIN(?:/UIN)?[ \t]*[:.]?[ \t]*([0-9A-Z]{10,16})", head, re.I) or re.search(r"GSTIN(?:/UIN)?[ \t]*[:.]?[ \t]*([0-9A-Z]{10,16})", t, re.I)
    f["gstin"] = m.group(1).upper() if m else None
    m = re.search(r"(?:Invoice|Inv\.?|Bill)[ \t]*(?:No\.?|Number|Num|#)[ \t]*[:.]?[ \t]*((?=[A-Za-z0-9/\-_.]*\d)[A-Za-z0-9][A-Za-z0-9/\-_.]*[A-Za-z0-9])", t, re.I)
    f["invoice_no"] = m.group(1) if m else None
    m = re.search(r"(?:Invoice[ \t]+|Bill[ \t]+)?Date[d]?[ \t]*[:.]?[ \t]*" + DATE, t, re.I)
    f["invoice_date"] = m.group(1) if m else None
    m = re.search(r"(PO-\d{4}-\d{3,5})", t); f["po_ref"] = m.group(1) if m else None
    m = re.search(r"Bill(?:ed)?[ \t]*To[ \t]*:?[ \t]*(.+)", t, re.I); f["bill_to"] = m.group(1).strip() if m else None
    m = re.search(r"(?:Sub[\s-]?total|Net[ \t]*Amount|Taxable[ \t]*(?:Value|Amount))[ \t]*:?[ \t]*" + AMT, t, re.I); f["subtotal"] = num(m.group(1)) if m else None
    taxes = re.findall(r"\b(IGST|CGST|SGST|UTGST|GST|VAT)[ \t]*(?:@|\()?[ \t]*(\d+(?:\.\d+)?)[ \t]*%\)?[ \t]*:?[ \t]*" + AMT, t, re.I)
    if taxes:
        f["tax_type"] = "+".join(dict.fromkeys(x[0].upper() for x in taxes))
        rate = sum(float(x[1]) for x in taxes); f["tax_rate"] = int(rate) if rate == int(rate) else rate
        f["tax"] = round(sum(num(x[2]) for x in taxes), 2)
    else: f["tax_type"] = f["tax_rate"] = f["tax"] = None
    tot = re.findall(r"(?:Total[ \t]*Due|Grand[ \t]*Total|Total[ \t]*Payable|Amount[ \t]*Payable|Invoice[ \t]*Total|Total[ \t]*Amount)[ \t]*:?[ \t]*" + AMT, t, re.I)
    f["total"] = num(tot[-1]) if tot else None
    m = re.search(r"(?:A/C|A/?C[ \t]*No|Account)[^\n\d]{0,20}(\d{9,18})", t, re.I); f["bank"] = m.group(1) if m else None
    m = re.search(r"IFSC(?:[ \t]*Code)?[ \t]*[:.]?[ \t]*([A-Z1]{4}0[A-Z0-9]{6})", t, re.I)
    f["ifsc"] = (m.group(1)[:4].upper().replace("1", "I") + m.group(1)[4:].upper()) if m else None
    items, started = [], False
    for l in lines:
        if re.match(r"^(Description|Item|Particulars)\b", l, re.I): started = True; continue
        if re.match(r"^(Sub[\s-]?total|Net Amount|Taxable)", l, re.I): break
        if not started: continue
        m = re.match(r"^(.*?[A-Za-z].*?)\s{2,}(?:(\d[\d,]*)\s+)?" + AMTD + r"\s+" + AMTD + r"$", l) or \
            re.match(r"^(.*?[A-Za-z].*?)\s+(?:(\d[\d,]*)\s+)?" + AMTD + r"\s+" + AMTD + r"$", l)
        if m:
            qty = int(m.group(2).replace(",", "")) if m.group(2) else None
            items.append({"desc": m.group(1).strip(), "qty": qty, "unit": num(m.group(3)), "amount": num(m.group(4)), "qty_inferred": qty is None})
    for it in items:
        if it["qty"] is None and it["unit"] == it["amount"]: it["qty"] = 1
    f["lines"] = items
    return f

# ---- optional LLM extraction: fills gaps only; every accepted value must be grounded in the document text ----
LLM_PROMPT = ("Extract fields from this vendor invoice text. Reply with ONLY a JSON object with keys: vendor, gstin, invoice_no, "
              "invoice_date, po_ref, subtotal, tax_type, tax_rate, tax, total, bank, ifsc, lines (array of {desc, qty, unit, amount}). "
              "Use null for anything not literally present in the text. Never guess, infer or calculate values.\n\nINVOICE TEXT:\n")
SCALARS = ["vendor", "gstin", "invoice_no", "invoice_date", "po_ref", "subtotal", "tax_type", "tax_rate", "tax", "total", "bank", "ifsc"]
TAX_TYPES = {"IGST", "CGST", "SGST", "UTGST", "GST", "VAT"}
MAX_LLM_LINES = 200

# ---------- LLM providers ----------
# Tried in order (AP_LLM_PROVIDERS, default "sarvam,gemini,anthropic"); only providers that have a key are active. The first usable answer
# wins, and it is still only a SUGGESTION: every value must be grounded in the document text (below) and then pass the business rules.
# Keys are read from the server's environment only. They are never sent to the browser, stored, or written into error messages.
LLM_TIMEOUT = 20
KEY_ENV = {"sarvam": ("SARVAM_API_KEY",), "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}

class ProviderError(Exception):
    def __init__(self, msg, status=None, retry_after=None):
        super().__init__(msg); self.status, self.retry_after = status, retry_after

def provider_key(name):
    for k in KEY_ENV.get(name, ()):
        v = (os.environ.get(k) or "").strip()
        if v: return v
    return ""

def active_providers():
    if llm_mode() == "off": return []
    order = [x.strip().lower() for x in os.environ.get("AP_LLM_PROVIDERS", "sarvam,gemini,anthropic").split(",")]
    return [n for n in dict.fromkeys(order) if n in KEY_ENV and provider_key(n)]

def llm_configured(): return bool(active_providers())

def provider_model(name):
    return {"sarvam": os.environ.get("AP_SARVAM_MODEL", "sarvam-105b"), "gemini": os.environ.get("AP_GEMINI_MODEL", "gemini-3.5-flash-lite"),
            "anthropic": anthropic_model()}[name]

def llm_model():
    """For display only: the chain that will be tried, e.g. 'sarvam-105b → gemini-3.5-flash-lite'."""
    return " → ".join(provider_model(n) for n in active_providers())

def _scrub(msg):
    msg = str(msg)
    for n in KEY_ENV:
        k = provider_key(n)
        if len(k) >= 6: msg = msg.replace(k, "***")
    return re.sub(r"([?&]key=)[^&\s'\"]+", r"\1***", msg)[:240]

_SSL_CTX = []
def _ssl_ctx():
    """Verified TLS, using certifi's CA bundle when installed (a python.org build on macOS has no system CAs until its certificates are installed)."""
    if not _SSL_CTX:
        try: import certifi; _SSL_CTX.append(ssl.create_default_context(cafile=certifi.where()))
        except Exception: _SSL_CTX.append(ssl.create_default_context())
    return _SSL_CTX[0]

def _http_json(url, headers, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT, context=_ssl_ctx()) as resp: raw = resp.read(1_000_000)
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read(4000)); err = err.get("error", err) if isinstance(err, dict) else err
            detail = (err.get("message") if isinstance(err, dict) else str(err)) or e.reason
        except Exception: detail = e.reason
        raise ProviderError(f"HTTPError {e.code}: {_scrub(detail)}", status=e.code, retry_after=e.headers.get("Retry-After"))
    except (TimeoutError, socket.timeout): raise ProviderError("timeout: no answer within %ds" % LLM_TIMEOUT)
    except urllib.error.URLError as e: raise ProviderError(f"network error: {_scrub(e.reason)}")
    try: out = json.loads(raw)
    except ValueError: raise ProviderError("malformed response: not JSON")
    if not isinstance(out, dict): raise ProviderError("malformed response: not a JSON object")
    return out

def _json_object(raw):
    raw = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    if "{" not in raw or "}" not in raw: raise ProviderError("malformed response: no JSON object in the reply")
    try: got = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except ValueError: raise ProviderError("malformed response: the reply is not valid JSON")
    if not isinstance(got, dict): raise ProviderError("malformed response: expected a JSON object")
    return got

def _call_sarvam(key, prompt):
    base = os.environ.get("SARVAM_BASE_URL", "https://api.sarvam.ai").rstrip("/")
    body = {"model": provider_model("sarvam"), "messages": [{"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 1500,
            "reasoning_effort": None, "response_format": {"type": "json_object"}}          # extraction needs no reasoning trace; keep it fast and cheap
    hdr = {"api-subscription-key": key, "Authorization": "Bearer " + key}
    try: out = _http_json(base + "/v1/chat/completions", hdr, body)
    except ProviderError as e:
        if e.status not in (400, 422): raise
        body.pop("reasoning_effort"); body.pop("response_format"); body["max_tokens"] = 4000       # an option this model rejects: retry with the plain request
        out = _http_json(base + "/v1/chat/completions", hdr, body)
    ch = out.get("choices")
    if not isinstance(ch, list) or not ch or not isinstance(ch[0], dict) or not isinstance(ch[0].get("message"), dict): raise ProviderError("malformed response: no choices[0].message")
    content = ch[0]["message"].get("content")
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("malformed response: empty reply" + (" (cut off at the token limit)" if ch[0].get("finish_reason") == "length" else ""))
    return _json_object(content)

def _call_gemini(key, prompt):
    base = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com").rstrip("/")
    url = f"{base}/v1beta/models/{urllib.parse.quote(provider_model('gemini'), safe='')}:generateContent"
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0, "maxOutputTokens": 2048, "responseMimeType": "application/json"}}
    try: out = _http_json(url, {"x-goog-api-key": key}, body)
    except ProviderError as e:
        if e.status not in (400, 401, 403): raise
        out = _http_json(url + "?key=" + urllib.parse.quote(key, safe=""), {}, body)         # the documented query-parameter form, if the header form is refused
    cands = out.get("candidates")
    if not isinstance(cands, list) or not cands:
        pf = out.get("promptFeedback"); why = pf.get("blockReason") if isinstance(pf, dict) else None
        raise ProviderError("malformed response: no candidates" + (f" (prompt blocked: {why})" if why else ""))
    content = cands[0].get("content") if isinstance(cands[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list): raise ProviderError("malformed response: no content parts" + (f" (finish reason {cands[0].get('finishReason')})" if isinstance(cands[0], dict) else ""))
    raw = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and isinstance(p.get("text", ""), str) and not p.get("thought"))
    if not raw.strip(): raise ProviderError("malformed response: empty reply")
    return _json_object(raw)

def _call_anthropic(key, prompt):
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    out = _http_json(base + "/v1/messages", {"x-api-key": key, "anthropic-version": "2023-06-01"},
                     {"model": provider_model("anthropic"), "max_tokens": 1500, "messages": [{"role": "user", "content": prompt}]})
    if not isinstance(out.get("content"), list): raise ProviderError("malformed response: no content list")
    return _json_object("".join(c.get("text", "") for c in out["content"] if isinstance(c, dict) and isinstance(c.get("text", ""), str)))

_CALLERS = {"sarvam": _call_sarvam, "gemini": _call_gemini, "anthropic": _call_anthropic}
_COOLDOWN = {}                   # provider -> monotonic time until which it is skipped (bad key / no credits / rate limited)
_LLM_CALLS = []                  # (monotonic time, workspace) of recent provider calls, for the hourly budgets
_LLM_LOCK = threading.Lock()

def reset_llm_state():
    with _LLM_LOCK: _COOLDOWN.clear(); _LLM_CALLS.clear()

def _budget_left():
    """Hourly caps so a public demo cannot burn through the owner's API credits."""
    try: total, per = int(os.environ.get("AP_LLM_MAX_PER_HOUR", "120")), int(os.environ.get("AP_LLM_MAX_PER_WS_HOUR", "20"))
    except ValueError: total, per = 120, 20
    now, ws = time.monotonic(), _WS.get()
    with _LLM_LOCK:
        _LLM_CALLS[:] = [c for c in _LLM_CALLS if now - c[0] < 3600]
        if len(_LLM_CALLS) >= total: return "the demo's hourly LLM budget is used up"
        if ws and sum(1 for c in _LLM_CALLS if c[1] == ws) >= per: return "this workspace's hourly LLM budget is used up"
        _LLM_CALLS.append((now, ws))
    return None

def llm_call(text):
    """-> (grounding candidates dict, provider name, attempts). Raises LlmUnavailable (carrying the attempts) if no provider gave a usable answer."""
    attempts, prompt = [], LLM_PROMPT + text[:12000]
    chain = active_providers()
    if not chain: raise LlmUnavailable("no LLM provider is configured", attempts)
    limited = _budget_left()
    if limited: raise LlmUnavailable(limited, attempts)
    for name in chain:
        with _LLM_LOCK: wait = _COOLDOWN.get(name, 0) - time.monotonic()
        if wait > 0: attempts.append({"provider": name, "outcome": "skipped", "reason": f"paused for {int(wait)}s after an earlier failure"}); continue
        try:
            got = _CALLERS[name](provider_key(name), prompt)
            attempts.append({"provider": name, "outcome": "ok", "model": provider_model(name)}); return got, name, attempts
        except ProviderError as e:
            attempts.append({"provider": name, "outcome": "error", "reason": _scrub(e)})
            st = e.status
            pause = 900 if st in (402, 403) else (min(int(e.retry_after), 120) if st == 429 and str(e.retry_after or "").isdigit() else 30 if st == 429 else 0)
            if pause:
                with _LLM_LOCK: _COOLDOWN[name] = time.monotonic() + pause
        except Exception as e:                                  # a provider must never be able to break a run
            attempts.append({"provider": name, "outcome": "error", "reason": _scrub(f"{type(e).__name__}: {e}")})
    raise LlmUnavailable("; ".join(f"{a['provider']}: {a.get('reason', a['outcome'])}" for a in attempts), attempts)

class LlmUnavailable(Exception):
    def __init__(self, msg, attempts): super().__init__(msg); self.attempts = attempts

# --- grounding: a value is accepted only if it is a whole token on a line of the document, in the right field context ---
def _lines(text):
    return [re.sub(r"\s+", " ", l).strip() for l in text.splitlines() if l.strip()]

def indian_group(n):
    s = str(int(n))
    if len(s) <= 3: return s
    return re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", s[:-3]) + "," + s[-3:]

def _num_re(v):
    q = D(v).quantize(Decimal("0.01"), ROUND_HALF_UP)
    ip, fp = f"{abs(q):.2f}".split(".")
    forms = set()
    for g in (ip, f"{int(ip):,}", indian_group(int(ip))):
        forms.add(g + "." + fp)
        if fp == "00": forms.add(g)
        elif fp[1] == "0": forms.add(g + "." + fp[0])
    alt = "|".join(re.escape(f) for f in sorted(forms, key=len, reverse=True))
    # whole numeric token: not inside a longer digit run (999123999), not the fraction/grouping of another number
    return re.compile(r"(?<!\d)(?<!\d[,.])(?:" + alt + r")(?!\d)(?![,.]\d)")

def _id_re(s):
    parts = [re.escape(p) for p in str(s).split()]
    return re.compile(r"(?<![A-Za-z0-9/_\-])(?<![A-Za-z0-9]\.)" + r"\s+".join(parts) + r"(?![A-Za-z0-9/_\-])(?!\.[A-Za-z0-9])", re.I)

def _word_re(s):
    parts = [re.escape(p) for p in str(s).split()]
    return re.compile(r"(?<![A-Za-z0-9])" + r"\s+".join(parts) + r"(?![A-Za-z0-9])", re.I)

CTX = {   # a label that must appear earlier on the same line as the value
    "invoice_no": r"invoice|\binv\b|\bbill\b|\bdoc(?:ument)?\b|\bref(?:erence)?\b|\bno\b|number|#",
    "invoice_date": r"date|\bdated\b|issued|\bdt\b",
    "gstin": r"gst",
    "bank": r"a/?c\b|account|acct",
    "ifsc": r"ifsc",
    "subtotal": r"sub[\s-]?total|net\s*amount|taxable",
    "tax": r"\b(?:i|c|s|ut)?gst\b|\bvat\b|\btax\b(?!able)",
    "total": r"(?<![A-Za-z])(?<!sub-)(?<!sub )(?:total|payable|amount\s*due|grand)",
}
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")

def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) < 1e12

def _ground_num(v, lines, ctx=None):
    if not _is_num(v) or v < 0: return False
    rx = _num_re(v)
    for l in lines:
        for m in rx.finditer(l):
            if ctx is None or re.search(CTX[ctx], l[:m.start()], re.I): return True
    return False

def _ground_str(rx, lines, ctx=None):
    for l in lines:
        for m in rx.finditer(l):
            if ctx is None or re.search(CTX[ctx], l[:m.start()], re.I): return m.group(0)
    return None

def ground_field(k, v, text):
    """-> (accepted, value_to_store, reason). Strings come back as the document's own text, numbers as plain floats."""
    L = _lines(text)
    if k in ("subtotal", "tax", "total"):
        if not _is_num(v): return False, None, "expected a number"
        return (True, float(v), "") if _ground_num(v, L, k) else (False, None, f"{v} is not printed next to a {k} label in the document")
    if k == "tax_rate":
        if not _is_num(v) or not 0 < v <= 100: return False, None, "expected a percentage between 0 and 100"
        rx = re.compile(r"(?<![\d.])" + re.escape(f"{v:g}") + r"(?:\.0+)?\s*%")
        return (True, v if isinstance(v, int) else float(v), "") if any(rx.search(l) for l in L) else (False, None, f"{v}% is not in the document")
    if not isinstance(v, str): return False, None, "expected text"
    s = re.sub(r"\s+", " ", v).strip()
    if not s or len(s) > 200: return False, None, "empty or too long"
    if k == "vendor":
        got = _ground_str(_word_re(s), L); return (True, got, "") if got and len(s) >= 3 else (False, None, "name is not in the document")
    if k == "gstin":
        if not GSTIN_RE.match(s.upper()): return False, None, "not a valid GSTIN format"
        got = _ground_str(_id_re(s), L, "gstin"); return (True, got.upper(), "") if got else (False, None, "not printed as a GSTIN in the document")
    if k == "invoice_no":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/\-_.]{0,39}", s) or not re.search(r"\d", s): return False, None, "not an invoice-number shape"
        got = _ground_str(_id_re(s), L, "invoice_no"); return (True, got, "") if got else (False, None, "not found as a whole token after an invoice/reference label")
    if k == "invoice_date":
        if read_date(s)[0] is None: return False, None, "not a valid supported date"
        got = _ground_str(_word_re(s), L, "invoice_date"); return (True, got, "") if got else (False, None, "not printed after a date label")
    if k == "po_ref":
        if not re.fullmatch(r"PO-\d{4}-\d{3,5}", s.upper()): return False, None, "not a PO reference shape"
        got = _ground_str(_id_re(s), L); return (True, got.upper(), "") if got else (False, None, "not in the document")
    if k == "tax_type":
        parts = [p.strip().upper() for p in s.split("+")]
        if not parts or any(p not in TAX_TYPES for p in parts): return False, None, "unknown tax type"
        return (True, "+".join(parts), "") if all(any(_word_re(p).search(l) for l in L) for p in parts) else (False, None, "tax type not in the document")
    if k == "bank":
        digits = re.sub(r"\s+", "", s)
        if not re.fullmatch(r"\d{9,18}", digits): return False, None, "an account number has 9-18 digits"
        rx = re.compile(r"(?<!\d)" + digits + r"(?!\d)")
        ok = any(rx.search(l) and re.search(CTX["bank"], l[:rx.search(l).start()], re.I) for l in L)
        return (True, digits, "") if ok else (False, None, "not found as a whole number after an account label")
    if k == "ifsc":
        if not IFSC_RE.match(s.upper()): return False, None, "not a valid IFSC shape"
        got = _ground_str(_id_re(s), L, "ifsc"); return (True, got.upper(), "") if got else (False, None, "not printed after an IFSC label")
    return False, None, "unknown field"

def ground_lines(rows, text):
    """All-or-nothing: every row must be a real line of the document containing its description, quantity, unit price and amount
    as whole tokens. Returns (lines, reason)."""
    if not isinstance(rows, list): return None, "expected a list"
    if not rows: return None, "empty"
    if len(rows) > MAX_LLM_LINES: return None, f"more than {MAX_LLM_LINES} rows"
    L, used, out = _lines(text), set(), []
    for n, r in enumerate(rows, 1):
        if not isinstance(r, dict) or not isinstance(r.get("desc"), str) or not _is_num(r.get("amount")): return None, f"row {n} is malformed"
        q, u = r.get("qty"), r.get("unit")
        if (q is not None and (not _is_num(q) or q <= 0)) or (u is not None and (not _is_num(u) or u < 0)): return None, f"row {n} has a wrong-typed quantity or unit price"
        desc = re.sub(r"\s+", " ", r["desc"]).strip()
        if len(desc) < 3 or len(desc) > 200: return None, f"row {n} description is empty or too long"
        rx, hit = _word_re(desc), None
        for i, l in enumerate(L):
            m = rx.search(l)
            if i in used or not m: continue
            if all(x is None or _num_re(x).search(l) for x in (q, u, r["amount"])): hit = (i, m.group(0)); break
        if hit is None: return None, f"row {n} ('{desc[:30]}'): description, quantity, unit price and amount do not all appear together on one line of the document"
        used.add(hit[0]); out.append({"desc": hit[1], "qty": q, "unit": u, "amount": r["amount"], "qty_inferred": q is None})
    return out, ""

def needs_llm(ex):
    miss = [k for k in RULES["required_fields"] if not ex.get(k)]
    return bool(miss or not ex.get("lines") or ex.get("subtotal") is None or ex.get("bank") is None)

def llm_enrich(ex, text):
    """Fills gaps with grounded values only. Returns an info dict; never raises. Business validation still runs on whatever is accepted."""
    mode = llm_mode()
    info = {"mode": mode, "configured": llm_configured(), "used": False, "provider": None, "attempts": [], "filled": [], "rejected": [], "rejected_reasons": {}, "error": None}
    if not info["configured"] or (mode == "auto" and not needs_llm(ex)): return info
    try:
        got, info["provider"], info["attempts"] = llm_call(text); info["used"] = True
    except LlmUnavailable as e:
        info["attempts"] = e.attempts; info["error"] = _scrub(e)[:200]; return info
    except Exception as e:
        info["error"] = _scrub(f"{type(e).__name__}: {e}")[:200]; return info
    for k in SCALARS:
        v = got.get(k)
        if v is None or v == "": continue
        if ex.get(k) in (None, "") or (mode == "always" and ex.get(k) != v):
            ok, val, why = ground_field(k, v, text)
            if ok: ex[k] = val; info["filled"].append(k)
            else: info["rejected"].append(k); info["rejected_reasons"][k] = why
    if not ex.get("lines") and got.get("lines") not in (None, []):
        lines, why = ground_lines(got["lines"], text)
        if lines:
            for it in lines:
                if it["qty"] is None and it["unit"] == it["amount"]: it["qty"] = 1
            ex["lines"] = lines; info["filled"].append("line items")
        else: info["rejected"].append("line items"); info["rejected_reasons"]["line items"] = why
    return info

# ---------- validation ----------
def fmt(x): return f"₹{x:,.2f}" if x is not None else "—"

def row(label, inv, ref, ok):
    """ok: True = agrees, False = disagrees, None = could not be compared / not applicable."""
    return {"label": label, "invoice": inv, "reference": ref, "ok": ok}

# --- dates: read DAY-FIRST (the Indian invoice convention); no rule compares a date with today's date ---
DATE_FORMATS = ("%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%d-%b-%Y", "%d/%m/%y")
SUPPORTED_DATES = "DD Mon YYYY, DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY, YYYY-MM-DD, DD-Mon-YYYY, DD/MM/YY"

def parse_date(s):
    s = re.sub(r"\s+", " ", s or "").strip()
    for f in DATE_FORMATS:
        try: return datetime.strptime(s, f)
        except ValueError: pass
    return None

def _date_why(raw):
    m = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", raw)
    if m:
        a, b = int(m[1]), int(m[2])
        if b > 12 and a <= 12:
            return f"there is no month {b}. Dates are read day first (DD/MM/YYYY); a month-first date must be reissued with the month written out"
        if a == 0 or b == 0 or a > 31 or b > 12: return "the day or month is out of range"
        return "that day does not exist in that month"
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m: return f"there is no month {int(m[2])}" if int(m[2]) > 12 or int(m[2]) == 0 else "that day does not exist in that month"
    m = re.fullmatch(r"(\d{1,2})[ \-]([A-Za-z]{3,9})[ \-](\d{4})", raw)
    if m:
        for f in ("%d %b %Y", "%d %B %Y"):
            try: datetime.strptime(f"1 {m[2]} {m[3]}", f); return f"{m[2]} {m[3]} has no day {int(m[1])}"
            except ValueError: pass
        return f"'{m[2]}' is not a month name"
    return "it is not one of the supported date formats"

def read_date(s):
    """-> (datetime | None, interpretation note | None, problem text | None)."""
    raw = re.sub(r"\s+", " ", s or "").strip()
    if not raw: return None, None, "no date found"
    dt = parse_date(raw)
    if dt is None: return None, None, f"Invoice date “{raw}” is not a valid date: {_date_why(raw)}. Supported formats: {SUPPORTED_DATES}"
    lo, hi = RULES["date_year_range"]
    if not lo <= dt.year <= hi: return None, None, f"Invoice date “{raw}” has year {dt.year}, outside the accepted range {lo}–{hi}"
    m = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-]\d{2,4}", raw)
    note = f"“{raw}” is read day first, as {dt.day} {dt:%B %Y}" if m and int(m[1]) <= 12 and int(m[2]) <= 12 and int(m[1]) != int(m[2]) else None
    return dt, note, None

def FIELDROWS(ex, date_ok=None):
    rows = []
    for l, k in (("Vendor name", "vendor"), ("Invoice number", "invoice_no"), ("Invoice date", "invoice_date"), ("Total", "total")):
        v = ex.get(k)
        rows.append(row(l, (fmt(v) if k == "total" and v else v), "valid calendar date" if k == "invoice_date" else "required",
                        (bool(v) and date_ok is not False) if k == "invoice_date" else bool(v)))
    return rows

# --- arithmetic: Decimal throughout, with an explicit, documented rounding allowance ---
def check_arithmetic(ex):
    """-> (status, detail, rows). A figure that is missing or unreadable is 'not verified', never silently passed."""
    allow, per_unit = D(RULES["rounding_allowance_inr"]), D(RULES["line_rounding_per_unit_inr"])
    rows, fails, unver, notes = [], [], [], []
    lines = ex.get("lines") or []
    sub, tax, tot, rate = ex.get("subtotal"), ex.get("tax"), ex.get("total"), ex.get("tax_rate")
    for i, l in enumerate(lines, 1):
        q, u, a = l.get("qty"), l.get("unit"), l.get("amount")
        name = (l.get("desc") or f"line {i}")[:28]
        if q is None or u is None or a is None:
            unver.append(f"line {i} ('{name}') has no readable quantity, so quantity × unit price could not be checked")
            rows.append(row(f"Line {i} · {name}", "quantity not read", "qty × unit price", None)); continue
        exp = D(q) * D(u); tol = max(allow, D(q) * per_unit); ok = abs(exp - D(a)) <= tol
        rows.append(row(f"Line {i} · qty × unit price", f"{fmt(D(a))} printed", f"{q:g} × {fmt(D(u))} = {fmt(exp)} (±{fmt(tol)})", ok))
        if not ok: fails.append(f"line {i} '{name}': {q:g} × {fmt(D(u))} = {fmt(exp)}, but the invoice prints {fmt(D(a))}")
    if lines and sub is not None and all(l.get("amount") is not None for l in lines):
        s = sum(D(l["amount"]) for l in lines); ok = abs(s - D(sub)) <= allow
        rows.append(row("Σ line amounts vs subtotal", fmt(s), f"{fmt(D(sub))} (±{fmt(allow)})", ok))
        if not ok: fails.append(f"line items sum to {fmt(s)} but the subtotal is {fmt(D(sub))}")
    elif lines and sub is None: unver.append("the subtotal was not found, so the line items could not be reconciled to it")
    if not lines: rows.append(row("Line items", "none itemised", "line-level checks not applicable", None))
    if sub is None: unver.append("the subtotal (net amount) was not found")
    if tax is None: unver.append("the tax amount was not found")
    if sub is not None and tax is not None and rate is not None:
        exp = (D(sub) * D(rate) / 100).quantize(Decimal("0.01"), ROUND_HALF_UP); ok = abs(exp - D(tax)) <= allow
        rows.append(row(f"{rate:g}% tax", fmt(D(tax)), f"{fmt(exp)} (±{fmt(allow)})", ok))
        if not ok: fails.append(f"{rate:g}% tax on {fmt(D(sub))} should be {fmt(exp)} but the invoice shows {fmt(D(tax))}")
    elif sub is not None and tax is not None:
        notes.append("the tax rate is not printed, so the tax amount was not cross-checked against a rate")
        rows.append(row("Tax rate", "not printed", "tax amount not cross-checked", None))
    if tot is None: unver.append("the invoice total was not found")
    if None not in (sub, tax, tot):
        exp = D(sub) + D(tax); ok = abs(exp - D(tot)) <= allow
        rows.append(row("Subtotal + tax vs total", fmt(D(tot)), f"{fmt(exp)} (±{fmt(allow)})", ok))
        if not ok: fails.append(f"subtotal + tax = {fmt(exp)} but the total is {fmt(D(tot))}")
    for nm, v in (("subtotal", sub), ("total", tot)):
        if v is not None and D(v) <= 0: fails.append(f"the {nm} {fmt(D(v))} is not a positive amount (credit notes need manual handling)")
    if fails:
        extra = f" Not verified either: {'; '.join(unver)}." if unver else ""
        return "fail_hold", "; ".join(fails) + "." + extra, rows
    if unver:
        return "unverified", f"Could not verify: {'; '.join(unver)}. A check that could not run is not counted as passed, and nothing is assumed.", rows
    base = ("Each line (qty × unit price), the line amounts → subtotal, the tax, and subtotal + tax → total all reconcile" if lines
            else "Subtotal + tax → total reconciles. Not applicable: the invoice has no itemised lines, so line-level checks did not run")
    return "pass", f"{base} (rounding allowance {fmt(allow)}){' — note: ' + '; '.join(notes) if notes else ''}.", rows

# --- vendor identity ---
CONFUSABLE = {"0": "0", "O": "0", "D": "0", "Q": "0", "1": "1", "I": "1", "L": "1", "2": "2", "Z": "2",
              "5": "5", "S": "5", "8": "8", "B": "8", "6": "6", "G": "6"}

def confusable_diff(a, b):
    """Differences between two strings if every one is a pair OCR commonly confuses (0/O, 1/I, 5/S, ...); otherwise None."""
    if len(a) != len(b): return None
    d = [(x, y) for x, y in zip(a, b) if x != y]
    if not d or len(d) > 3: return None
    return d if all(CONFUSABLE.get(x) and CONFUSABLE.get(x) == CONFUSABLE.get(y) for x, y in d) else None

def name_match(name):
    best, score = None, 0.0
    if name:
        for k, v in VENDORS.items():
            s = difflib.SequenceMatcher(None, v["name"].lower(), name.lower()).ratio()
            if s > score: best, score = k, s
    return best, score

def resolve_vendor(ex, ocr_used, po_owner):
    """Identity from GSTIN and name. Missing/unreadable identifiers are told apart from contradictory ones:
      exact      GSTIN equals a master GSTIN and the name agrees
      recovered  GSTIN absent/garbled by OCR, accepted only with the explicit corroboration policy (RULES) - flagged as a note
      conflict   a different well-formed GSTIN (or a GSTIN/name pointing at different vendors): needs review, never auto-resolved
      uncertain  identity cannot be established from the evidence: needs review
      unknown / unapproved  not in the approved master: rejected"""
    g = (ex.get("gstin") or "").strip().upper() or None
    name = ex.get("vendor")
    fuzzy, need = RULES["fuzzy_vendor_name"], RULES["ocr_identity_min_corroborations"]
    by_g = next((k for k, v in VENDORS.items() if v["gstin"] == g), None) if g else None
    nvid, score = name_match(name)
    name_ok = bool(nvid) and score >= fuzzy

    def rows(vid):
        v = VENDORS[vid]
        return [row("GSTIN", g, v["gstin"], (g == v["gstin"]) if g else None), row("Name", name, v["name"], (name == v["name"]) or None),
                row("Approved vendor", "—", "yes", bool(v["approved"]))]

    def corroborations(vid):
        v, c = VENDORS[vid], []
        if ex.get("bank") and ex["bank"] == v["bank"]: c.append("the bank account matches the master")
        if ex.get("ifsc") and ex["ifsc"] == v["ifsc"]: c.append("the IFSC matches the master")
        if po_owner == vid: c.append("the PO reference belongs to this vendor")
        return c

    def out(kind, vid, status, detail, rws, **extra):
        return {"kind": kind, "vid": vid, "status": status, "detail": detail, "rows": rws, "extra": {"identity": kind, **extra}}

    def finish(kind, vid, detail, status="pass", **extra):
        if not VENDORS[vid]["approved"]:
            return out("unapproved", vid, "fail_reject", f"{VENDORS[vid]['name']} is not an approved vendor.", [row("Approved", "—", "yes required", False)], vendor_id=vid)
        return out(kind, vid, status, detail, rows(vid), vendor_id=vid, **extra)

    if by_g:
        v = VENDORS[by_g]
        sc = difflib.SequenceMatcher(None, v["name"].lower(), (name or "").lower()).ratio() if name else 1.0
        if name_ok and nvid != by_g:
            return out("conflict", None, "fail_hold", f"Vendor identity conflict: GSTIN {g} belongs to {v['name']} ({by_g}), but the invoice is issued in the name of "
                       f"'{name}', which matches {VENDORS[nvid]['name']} ({nvid}) at {score:.0%}. The identifiers point at different vendors.", rows(by_g), candidate=nvid)
        if sc < 0.6:
            return out("conflict", None, "fail_hold", f"Vendor identity conflict: GSTIN {g} belongs to {v['name']} ({by_g}), but the invoice names '{name}' ({sc:.0%} similar).",
                       rows(by_g), candidate=by_g)
        return finish("exact", by_g, f"{v['name']} ({by_g}) approved — GSTIN exact match.")
    if g and not name_ok:
        return out("unknown", None, "fail_reject", "Vendor not found in approved vendor master.",
                   [row("GSTIN", g, "no match in master", False), row("Name", name, "no match in master", False)])
    if not name_ok:
        return out("unknown", None, "fail_reject", "Vendor not found in approved vendor master.",
                   [row("GSTIN", g, "no match in master", False), row("Name", name, "no match in master", False)])
    master = VENDORS[nvid]["gstin"]
    corro = corroborations(nvid)
    if g:
        diff = confusable_diff(g, master) if ocr_used else None
        if diff:
            pairs = ", ".join(f"'{x}' read for '{y}'" for x, y in diff)
            if len(corro) + 1 >= need:
                return finish("recovered", nvid, f"{VENDORS[nvid]['name']} ({nvid}) accepted on OCR recovery, not as an exact identity match: the GSTIN was read as {g}, "
                              f"which differs from the master GSTIN {master} only in characters OCR confuses ({pairs}); the name matches {score:.0%}; "
                              f"corroborated by: {', '.join(corro) or 'nothing further'} (policy: at least {need} independent signals).", "warn", corroboration=corro)
            return out("uncertain", None, "unverified", f"Vendor identity could not be confirmed: the OCR-read GSTIN {g} differs from {VENDORS[nvid]['name']}'s master GSTIN {master} only in "
                       f"confusable characters ({pairs}), but only {len(corro) + 1} of the required {need} independent signals agree ({', '.join(corro) or 'none besides the GSTIN pattern'}).",
                       rows(nvid), candidate=nvid)
        if ocr_used and not GSTIN_RE.match(g):
            if len(corro) >= need:
                return finish("recovered", nvid, f"{VENDORS[nvid]['name']} ({nvid}) accepted on OCR recovery, not as an exact identity match: the OCR-read GSTIN '{g}' is not a valid number; "
                              f"the name matches {score:.0%}; corroborated by: {', '.join(corro)} (policy: at least {need} independent signals).", "warn", corroboration=corro)
            return out("uncertain", None, "unverified", f"Vendor identity could not be confirmed: the OCR-read GSTIN '{g}' is unreadable and only {len(corro)} of the required {need} "
                       f"independent signals agree with {VENDORS[nvid]['name']} ({', '.join(corro) or 'none'}).", rows(nvid), candidate=nvid)
        why = "is not a valid GSTIN" if not GSTIN_RE.match(g) else f"is a different, well-formed registration number from the {master} on file"
        return out("conflict", None, "fail_hold", f"Vendor identity conflict: the invoice names {VENDORS[nvid]['name']} ({score:.0%} match) but its GSTIN {g} {why}. "
                   "This is a contradiction, not a missing value: a conflicting identifier is never resolved automatically, so a person has to confirm which registration is right.", rows(nvid), candidate=nvid)
    if len(corro) >= need:
        return finish("recovered", nvid, f"{VENDORS[nvid]['name']} ({nvid}) accepted without a GSTIN, not as an exact identity match: no GSTIN could be read; the name matches {score:.0%}; "
                      f"corroborated by: {', '.join(corro)} (policy: at least {need} independent signals).", "warn", corroboration=corro)
    return out("uncertain", None, "unverified", f"Vendor identity could not be confirmed: no GSTIN could be read, the name matches {VENDORS[nvid]['name']} at {score:.0%}, "
               f"and only {len(corro)} of the required {need} independent signals agree ({', '.join(corro) or 'none'}).", rows(nvid), candidate=nvid)

# --- duplicates and resubmissions ---
def norm_inv(s): return re.sub(r"\s+", "", s or "").upper()

DIFF_FIELDS = (("vendor", "vendor name"), ("gstin", "GSTIN"), ("invoice_date", "invoice date"), ("po_ref", "PO reference"), ("subtotal", "net amount"),
               ("tax", "tax"), ("total", "total"), ("bank", "bank account"), ("ifsc", "IFSC"))

def _norm(v):
    return round(float(v), 2) if isinstance(v, (int, float)) and not isinstance(v, bool) else (re.sub(r"\s+", " ", v).strip().upper() if isinstance(v, str) else v)

def diff_extracted(old, new):
    ch = []
    for k, label in DIFF_FIELDS:
        if _norm(old.get(k)) != _norm(new.get(k)): ch.append({"field": label, "before": old.get(k), "after": new.get(k)})
    sig = lambda e: [(_norm(l.get("desc")), _norm(l.get("qty")), _norm(l.get("unit")), _norm(l.get("amount"))) for l in (e.get("lines") or [])]
    if sig(old) != sig(new): ch.append({"field": "line items", "before": f"{len(old.get('lines') or [])} line(s)", "after": f"{len(new.get('lines') or [])} line(s)"})
    return ch

def classify_submission(runs, vid, ex, file_hash):
    """Compare with earlier approved/held runs (same vendor + invoice number, or byte-identical file).
    -> dict(relation, of, changes)  relation: None | duplicate_of_approved | unchanged_resubmission | corrected_resubmission"""
    key = norm_inv(ex.get("invoice_no")); prior = []
    for r in runs:
        if r.get("error") or r.get("is_duplicate") or r.get("decision") not in ("APPROVE", "HOLD"): continue
        pe = r.get("extracted") or {}
        same_no = bool(vid and key and (r.get("vendor_id") or r.get("vendor_candidate")) == vid and norm_inv(pe.get("invoice_no")) == key)
        same_file = bool(file_hash) and r.get("file_hash") == file_hash
        if same_no or same_file: prior.append((r, same_no, same_file))
    ref = lambda r: {"id": r["id"], "decision": r["decision"], "ts": r["ts"], "filename": r["filename"], "invoice_no": (r.get("extracted") or {}).get("invoice_no"), "headline": r.get("headline")}
    approved = [p for p in prior if p[0]["decision"] == "APPROVE"]
    if approved:
        return {"relation": "duplicate_of_approved", "of": [ref(p[0]) for p in approved], "changes": [], "by": "same invoice number from this vendor" if approved[-1][1] else "byte-identical file"}
    if not prior: return {"relation": None, "of": [], "changes": []}
    last = prior[-1]
    changes = [] if last[2] else diff_extracted(last[0].get("extracted") or {}, ex)
    return {"relation": "corrected_resubmission" if changes else "unchanged_resubmission", "of": [ref(p[0]) for p in prior], "changes": changes}

# --- purchase orders ---
def po_consumed(po_id, runs):
    """Net value already approved against a PO: any opening 'billed' figure in the register plus every approved run in history."""
    return D(POS[po_id].get("billed") or 0) + sum(D((r.get("extracted") or {}).get("subtotal") or 0) for r in runs
                                                    if r.get("po") == po_id and r.get("decision") == "APPROVE" and not r.get("error"))

def f2(x): return float(x) if x is not None else None

def run_checks(runs, ex, file_hash, ocr_used):
    """Yield (stage_key, status, title, detail, extra). status: pass | warn | fail_hold | fail_reject | unverified | info"""
    ctx = {}
    po_ref = ex.get("po_ref"); po_owner = POS[po_ref]["vendor"] if po_ref in POS else None

    # 1. completeness and date validity
    missing = [k for k in RULES["required_fields"] if not ex.get(k)]
    dt, dnote, dprob = (read_date(ex["invoice_date"]) if ex.get("invoice_date") else (None, None, None))
    if dt: ex["invoice_date_iso"] = dt.date().isoformat()
    issues = ([f"Missing: {', '.join(m.replace('_', ' ') for m in missing)}"] if missing else []) + ([dprob] if dprob else [])
    if issues:
        yield ("fields", "fail_hold", "Required fields", ". ".join(issues) + ". Nothing is guessed or defaulted.",
               {"missing": missing, "date_problem": dprob, "compare": FIELDROWS(ex, False if dprob else None)})
    else:
        yield ("fields", "pass", "Required fields", "Vendor, invoice number, date and total all present" + (f"; date {dnote}" if dnote else "") + ".", {"compare": FIELDROWS(ex)})

    # 2. vendor identity
    ident = resolve_vendor(ex, ocr_used, po_owner)
    vid = ident["vid"]; ctx["vendor_id"] = vid
    yield ("vendor", ident["status"], "Vendor master", ident["detail"], {**ident["extra"], "compare": ident["rows"]})

    # 3. duplicates / resubmissions
    sub = classify_submission(runs, vid, ex, file_hash)
    ctx["related"] = None if sub["relation"] is None else {"relation": sub["relation"], "of": sub["of"], "changes": sub["changes"]}
    rel = sub["relation"]
    if rel == "duplicate_of_approved":
        r = sub["of"][-1]
        yield ("duplicate", "fail_reject", "Duplicate detection",
               f"Already approved: {r['invoice_no']} on {r['ts'][:16].replace('T', ' ')} ({sub['by']}; run {r['id']}). Approving it again would pay it twice.",
               {"dup_of": r["id"], "compare": [row("Invoice no", ex.get("invoice_no"), r["invoice_no"], False), row("Earlier run", "this run", f"{r['filename']} · {r['decision']}", False)]})
    elif rel == "unchanged_resubmission":
        r = sub["of"][-1]
        yield ("duplicate", "fail_hold", "Duplicate detection",
               f"Resubmitted without changes: {r['invoice_no'] or 'this invoice'} is already on hold (run {r['id']}, {r['ts'][:16].replace('T', ' ')}). Nothing new to evaluate — "
               "it stays on hold until a corrected invoice arrives. It is not a paid duplicate, so it is not rejected.",
               {"related_to": r["id"], "compare": [row("Invoice no", ex.get("invoice_no"), r["invoice_no"], True), row("Earlier run", "this run", f"{r['filename']} · {r['decision']}", None)]})
    elif rel == "corrected_resubmission":
        r = sub["of"][-1]
        shown = lambda v: "not stated" if v is None or v == "" else str(v)          # never show a raw None to a finance reader
        what = "; ".join(f"{c['field']}: {shown(c['before'])} → {shown(c['after'])}" for c in sub["changes"][:6])
        yield ("duplicate", "warn", "Duplicate detection",
               f"Corrected resubmission of held invoice {r['invoice_no']} (run {r['id']}, {r['ts'][:16].replace('T', ' ')}). Changed: {what}. It is evaluated from scratch below; "
               "the earlier held submission stays in history as the audit trail and cannot be approved separately.",
               {"related_to": r["id"], "compare": [row(c["field"].capitalize(), shown(c["after"]), f"{shown(c['before'])} (earlier, held)", None) for c in sub["changes"][:8]]
                + [row("Earlier run", "this run", f"{r['filename']} · {r['decision']}", None)]})
    else:
        yield ("duplicate", "pass", "Duplicate detection", f"No earlier approved or held submission of invoice no {ex.get('invoice_no') or '—'} from this vendor, and the file is new.",
               {"compare": [row("Invoice no", ex.get("invoice_no"), "no earlier match", True), row("File hash", file_hash, "not seen before", True)]})

    # 4. arithmetic
    st, detail, rws = check_arithmetic(ex)
    yield ("math", st, "Arithmetic integrity", detail, {"compare": rws})

    # 5. PO match
    po_id, lines = ex.get("po_ref"), ex.get("lines") or []
    if po_id and po_id not in POS:
        yield ("po", "fail_hold", "PO matching", f"{po_id} does not exist in the PO register.", {"compare": [row("PO reference", po_id, "not in PO register", False)]})
        po_id = None
    elif po_id:
        po = POS[po_id]
        if not vid:
            yield ("po", "info", "PO matching", f"Skipped — {po_id} is in the register, but the vendor identity is unresolved, so ownership was not checked.", {})
            po_id = None
        elif po["vendor"] != vid:
            yield ("po", "fail_hold", "PO matching", f"{po_id} belongs to {VENDORS[po['vendor']]['name']}, not this vendor.", {"compare": [row("PO vendor", VENDORS[vid]["name"], VENDORS[po["vendor"]]["name"], False)]})
            po_id = None
        elif po["status"] != "open":
            yield ("po", "fail_hold", "PO matching", f"{po_id} is {po['status']}.", {"compare": [row("PO status", "needs open", po["status"], False)]}); po_id = None
        else:
            yield ("po", "pass", "PO matching", f"Explicit reference {po_id} found: {po['desc']}.", {"po": po_id, "compare": [row("PO reference", po_id, "exists in register", True),
                   row("PO vendor", VENDORS[vid]["name"], VENDORS[po["vendor"]]["name"], True), row("PO status", "needs open", po["status"], True)]})
    elif not vid:
        yield ("po", "info", "PO matching", "Skipped — no PO reference printed and the vendor identity is unresolved, so no PO can be inferred.", {})
    else:
        # implied PO: the unique open PO of this vendor whose REMAINING value matches the invoice net within tolerance
        cands, tol = [], RULES["price_tolerance_pct"]
        if ex.get("subtotal"):
            for pid, po in POS.items():
                if po["vendor"] == vid and po["status"] == "open":
                    rem = D(po["net"]) - po_consumed(pid, runs)
                    if rem > 0 and abs((D(ex["subtotal"]) - rem) / rem * 100) <= D(tol):
                        sim = max((difflib.SequenceMatcher(None, l["desc"].lower(), pl[0].lower()).ratio() for l in lines for pl in po["lines"]), default=0)
                        cands.append((pid, sim))
        if len(cands) == 1:
            po_id = cands[0][0]; rem = D(POS[po_id]["net"]) - po_consumed(po_id, runs)
            yield ("po", "warn", "PO matching", f"No PO reference printed. Inferred {po_id} ({POS[po_id]['desc']}): the only open PO for this vendor whose remaining value matches the invoice net "
                   f"{fmt(ex['subtotal'])}, line description {cands[0][1]:.0%} similar. Flagged for audit.", {"po": po_id, "inferred": True, "compare": [row("PO reference", "not printed", po_id + " (inferred)", None),
                   row("Invoice net", fmt(ex["subtotal"]), fmt(rem) + " remaining", True)]})
        else:
            why = "no open PO for this vendor matches the amount" if not cands else f"{len(cands)} open POs could match — ambiguous"
            yield ("po", "fail_hold", "PO matching", f"No PO reference printed and {why}. Cannot match safely.", {"compare": [row("PO reference", "not printed", f"{len(cands)} candidate PO(s)", False)]})
            po_id = None
    ctx["po"] = po_id

    # 6. amounts against the PO balance
    if po_id and rel == "duplicate_of_approved":
        yield ("amount", "info", "Amount & tolerance", f"Skipped — this is a duplicate of an invoice that was already approved; its amount was counted against {po_id} "
               "when the original was approved, so it is not counted a second time.", {"kind": "duplicate"})
    elif po_id:
        po = POS[po_id]; consumed = po_consumed(po_id, runs); po_net = D(po["net"]); rem = po_net - consumed
        tol = D(RULES["price_tolerance_pct"]); issues, notes, arows, kind = [], [], [], None
        for l in lines:
            best = max(po["lines"], key=lambda pl: difflib.SequenceMatcher(None, l["desc"].lower(), pl[0].lower()).ratio(), default=None)
            if best and difflib.SequenceMatcher(None, l["desc"].lower(), best[0].lower()).ratio() >= 0.75 and l.get("unit") and "milestone" not in l["desc"].lower():
                v = (D(l["unit"]) - D(best[2])) / D(best[2]) * 100
                arows.append(row(f"Unit price · {l['desc'][:28]}", fmt(D(l["unit"])), f"{fmt(D(best[2]))} (PO, ±{tol}%)", abs(v) <= tol))
                if abs(v) > tol: issues.append(f"unit price of '{l['desc']}' is {fmt(D(l['unit']))} vs PO {fmt(D(best[2]))} ({v:+.1f}%, tolerance ±{tol}%)"); kind = "variance"
                elif v != 0: notes.append(f"unit price {v:+.2f}% vs PO")
        net = ex.get("subtotal")
        if net is None:
            yield ("amount", "unverified", "Amount & tolerance", f"Not verified: the invoice's net amount (subtotal) was not found, so it cannot be compared with the {po_id} balance "
                   f"({fmt(rem)} remaining). Nothing is assumed from the total.", {"kind": "unverified", "po_net": f2(po_net), "remaining": f2(rem), "compare": [row("Invoice net vs PO remaining", "not found", fmt(rem), None)] + arows})
        else:
            net = D(net); limit = rem * (1 + tol / 100)
            if rem <= 0:
                over = f"; over-approved by {fmt(-rem)}" if rem < 0 else ""
                issues.append(f"{po_id} has no remaining value ({fmt(consumed)} of {fmt(po_net)} already approved{over}), so a further invoice of {fmt(net)} cannot be approved against it — "
                              "it needs a PO amendment or a different PO"); kind = "exhausted"
                arows.append(row("Invoice net vs PO remaining", fmt(net), fmt(rem), False))
            elif net <= 0:
                issues.append(f"the invoice net {fmt(net)} is not a positive amount"); kind = "variance"; arows.append(row("Invoice net vs PO remaining", fmt(net), fmt(rem), False))
            elif net > limit:
                v = (net - rem) / rem * 100
                issues.append(f"invoice net {fmt(net)} exceeds remaining PO value {fmt(rem)} by {v:+.1f}% (tolerance ±{tol}%, at most {fmt(limit)})"); kind = "variance"
                arows.append(row("Invoice net vs PO remaining", fmt(net), fmt(rem), False))
            else:
                arows.append(row("Invoice net vs PO remaining", fmt(net), fmt(rem), True))
                if net > rem: notes.append(f"invoice net is {(net - rem) / rem * 100:+.2f}% over the remaining PO value {fmt(rem)} — inside ±{tol}% tolerance; the PO has no value left afterwards")
                elif net < rem: notes.append(f"partial billing: {fmt(net)} of {fmt(rem)} remaining ({net / po_net:.0%} of PO); {fmt(rem - net)} stays open")
            if issues: yield ("amount", "fail_hold", "Amount & tolerance", "; ".join(issues) + ".", {"kind": kind, "po_net": f2(po_net), "remaining": f2(rem), "compare": arows})
            else:
                tail = "; ".join(notes) if notes else f"exact match to PO {fmt(po_net)}"
                yield ("amount", "pass", "Amount & tolerance", f"Within policy — {tail}. Already billed on PO: {fmt(consumed)}.", {"remaining": f2(rem), "compare": arows})
    else:
        yield ("amount", "info", "Amount & tolerance", "Skipped — no verified PO to compare against.", {})

    # 7. bank
    if vid:
        v = VENDORS[vid]
        if not ex.get("bank"):
            yield ("bank", "warn", "Bank details", "No bank details on invoice; any payment would use the vendor master record.", {"compare": [row("Account", "not on invoice", v["bank"], None)]})
        elif ex["bank"] != v["bank"] or (ex.get("ifsc") and ex["ifsc"] != v["ifsc"]):
            yield ("bank", "fail_hold", "Bank details", f"Account on invoice {ex['bank']} ({ex.get('ifsc')}) differs from master {v['bank']} ({v['ifsc']}). "
                   "Classic payment-redirection pattern — verify by call-back to a known vendor contact before any payment.",
                   {"compare": [row("Account no", ex["bank"], v["bank"], ex["bank"] == v["bank"]), row("IFSC", ex.get("ifsc"), v["ifsc"], ex.get("ifsc") == v["ifsc"])]})
        else:
            yield ("bank", "pass", "Bank details", "Account and IFSC match the vendor master.", {"compare": [row("Account no", ex["bank"], v["bank"], True), row("IFSC", ex.get("ifsc"), v["ifsc"], True)]})
    else:
        yield ("bank", "info", "Bank details", "Skipped — vendor identity unresolved.", {})
    yield ("_ctx", "info", "", "", ctx)

# ---------- decision ----------
STAGES = [("ingest", "Ingest"), ("extract", "Extract"), ("fields", "Required fields"), ("vendor", "Vendor master"),
          ("duplicate", "Duplicate detection"), ("math", "Arithmetic integrity"), ("po", "PO matching"),
          ("amount", "Amount & tolerance"), ("bank", "Bank details"), ("decide", "Decision")]
HOLD_STATUSES = ("fail_hold", "unverified")
REQUIRED_STAGES = ("fields", "vendor", "duplicate", "math", "po", "amount", "bank")   # every one must have actually run and passed
HOLD_PRIORITY = ("duplicate", "vendor", "bank", "fields", "math", "amount", "po")

def decide(checks, ex, ctx):
    """Returns (decision, headline, reasoning, recommended next step). Next steps are recommendations: this system records the
    decision; it does not send payments, e-mails or vendor messages. An approval needs EVERY required check to have run and passed."""
    by = {c["key"]: c for c in checks}
    rej = [c for c in checks if c["status"] == "fail_reject"]
    hold = [c for c in checks if c["status"] in HOLD_STATUSES]
    warn = [c for c in checks if c["status"] == "warn"]
    if rej:
        c = rej[0]
        if c["key"] == "duplicate":
            return ("REJECT", "Duplicate invoice — do not pay twice", c["detail"],
                    "Recommended: do not pay this invoice again, and let the vendor know it was already processed (no message is sent automatically).")
        return ("REJECT", "Vendor not approved", c["detail"], "Recommended: do not pay; route the vendor to onboarding before any payment is considered.")
    not_run = [k for k in REQUIRED_STAGES if by.get(k, {}).get("status") not in ("pass", "warn") and by.get(k) not in hold]
    if hold or not_run:
        top = next((k for k in HOLD_PRIORITY if any(c["key"] == k for c in hold)), None)
        h = by.get(top) or {}
        if top == "duplicate": head, act = "Already on hold — resubmitted without changes", "Recommended: wait for the vendor's corrected invoice; nothing new has arrived to review."
        elif top == "vendor":
            head, act = "Vendor identity needs review", "Recommended: confirm the vendor's GSTIN and registration with the vendor master and the vendor (using contact details from the master, not the invoice) before any payment."
        elif top == "bank":
            head, act = "Bank details changed — verify before paying", "Recommended: hold payment. Call the vendor on the number in the vendor master (not the one on the invoice) and have the change approved by two people."
        elif top == "fields":
            x = h.get("extra", {}); need = [m.replace("_", " ") for m in x.get("missing", [])] + (["a valid invoice date"] if x.get("date_problem") else [])
            head = "Invoice date is not valid — vendor follow-up needed" if x.get("date_problem") and not x.get("missing") else "Incomplete invoice — vendor follow-up needed"
            act = "Recommended: ask the vendor to resend the invoice with: " + ", ".join(need) + "."
        elif top == "math":
            head = "Amounts could not be verified — review needed" if h["status"] == "unverified" else "Invoice arithmetic does not reconcile — review needed"
            act = "Recommended: ask the vendor for a corrected invoice, or have AP confirm the figures against the original document."
        elif top == "amount":
            kind = h.get("extra", {}).get("kind")
            if kind == "exhausted": head, act = "PO has no remaining value — buyer review needed", "Recommended: ask the PO owner whether to amend the PO or match a different one before this invoice is paid."
            elif kind == "unverified": head, act = "Amounts could not be verified — review needed", "Recommended: have AP confirm the invoice net amount against the original document, then resubmit."
            else: head, act = "Price/amount variance beyond tolerance — buyer approval needed", "Recommended: ask the PO owner to approve the variance, or request a credit note / corrected invoice from the vendor."
        elif top == "po": head, act = "Cannot match to a PO — manual match needed", "Recommended: ask AP to confirm the PO with the requester, or request a PO reference from the vendor."
        else: head, act = "Needs human review", "Recommended: route to an AP reviewer — a required check did not run."
        body = " ".join(f"• {c['title']}: {c['detail']}" for c in hold)
        if not_run and not hold: body = "These required checks did not complete, so approval is not allowed: " + ", ".join(dict(STAGES)[k] for k in not_run) + "."
        return "HOLD", head, body, act
    if ctx.get("po"):
        parts = ["All checks passed"]
        if warn: parts.append("with notes: " + " ".join(f"{c['title']} — {c['detail'].rstrip()}" if c['detail'].rstrip().endswith(".") else f"{c['title']} — {c['detail']}." for c in warn).rstrip("."))
        return ("APPROVE", f"Approved against {ctx['po']}", " ".join(parts) + f". Total {fmt(D(ex['total']))} is eligible for payment processing; this system has not scheduled or sent any payment.",
                "Recommended: schedule payment per the vendor's terms. The PO balance now reflects this approval.")
    return "HOLD", "Needs human review", "No PO could be verified.", "Recommended: route to an AP reviewer."

# ---------- run orchestration ----------
def stage_step(key, status, title, detail, extra=None, prev=None):
    now = time.time()
    return {"key": key, "status": status, "title": title, "detail": detail, "extra": extra or {},
            "t": datetime.now().isoformat(timespec="milliseconds"), "ms": int((now - (prev or now)) * 1000)}

def step(run, key, status, title, detail, extra=None, wait=True):
    s = stage_step(key, status, title, detail, extra, run.get("_last"))
    run["stages"].append(s)
    if wait and stage_delay() > 0: time.sleep(stage_delay())
    run["_last"] = time.time()
    return s

def execute(run):
    tok = _WS.set(run.get("_ws"))                  # the run thread works inside its owner's workspace
    try: _execute(run)
    finally: _WS.reset(tok)

def llm_via(llm):
    failed = [a["provider"] for a in llm.get("attempts", []) if a["outcome"] != "ok"]
    return (llm.get("provider") or "?") + (f" after {', '.join(failed)} failed" if failed else "")

def _execute(run):
    committed = False
    try:
        data, _ = read_attachment(run["attachment_id"])
        run["file_hash"] = sha_hex(data)[:16]
        run["current"] = "ingest"; run["_last"] = time.time()
        step(run, "ingest", "pass", "Ingest", f"Received {run['filename']} ({len(data) / 1024:.1f} KB), sha256 {run['file_hash']}.")
        run["current"] = "extract"
        with tempfile.TemporaryDirectory() as td:           # extractors want a path: give them a private copy of the verified bytes
            p = os.path.join(td, "doc.pdf")
            with open(p, "wb") as f: f.write(data)
            text, info = extract_text(p)
        ocr = bool(info["ocr_pages"])
        if ocr:
            step(run, "extract_note", "info", "Extract",
                 f"{'All' if len(info['ocr_pages']) == info['pages'] else str(len(info['ocr_pages'])) + ' of ' + str(info['pages'])} page(s) have no text layer — reading with OCR ({info['engine']}).", wait=False)
        ex = parse_invoice(text)
        llm = llm_enrich(ex, text)
        run["extracted"] = ex; run["raw_text"] = text; run["ocr"] = ocr
        run["extraction"] = {**info, "llm": llm}
        found = sum(1 for k in ("vendor", "gstin", "invoice_no", "invoice_date", "po_ref", "subtotal", "tax", "total", "bank", "ifsc") if ex.get(k))
        detail = (f"{'OCR' if ocr else 'Text layer'}: read {found}/10 header fields and {len(ex['lines'])} line item(s) from {info['pages']} page(s)."
                  + (" OCR output is not trusted blindly — values are re-validated below." if ocr else ""))
        if llm["used"]:
            detail += (f" LLM ({llm_via(llm)}) filled gaps ({', '.join(llm['filled']) or 'nothing new'}); values that are not grounded in the document text are discarded"
                       + (f" (discarded: {', '.join(llm['rejected'])})" if llm["rejected"] else "") + ". Business rules below still validate everything it supplied.")
        elif llm["configured"]: detail += " Rules read everything needed, so the LLM was not called."
        if llm["error"]: detail += f" LLM unavailable or unusable ({llm['error']}); continued with rules only."
        step(run, "extract", "warn" if (ocr or llm["used"]) else "pass", "Extract", detail)

        hook("before_critical", run)
        with exclusive_txn() as c:                          # ---- critical section: history read -> checks -> decision -> commit ----
            runs = history(c)
            checks, ctx, new_steps, prev = [], {}, [], time.time()
            for key, status, title, det, extra in run_checks(runs, ex, run["file_hash"], ocr):
                if key == "_ctx": ctx = extra; continue
                s = stage_step(key, status, title, det, extra, prev); prev = time.time()
                new_steps.append(s); checks.append(s)
            dec, head, body, action = decide(checks, ex, ctx)
            vstage = next((s for s in checks if s["key"] == "vendor"), {"extra": {}})
            final = dict(decision=dec, headline=head, reasoning=body, action=action, vendor_id=ctx.get("vendor_id"),
                         vendor_candidate=vstage["extra"].get("candidate"), po=ctx.get("po"), related=ctx.get("related"),
                         is_duplicate=any(s["key"] == "duplicate" and s["status"] == "fail_reject" for s in checks))
            if ctx.get("po"):
                po = POS[ctx["po"]]; billed = po_consumed(ctx["po"], runs)
                final["po_info"] = {"id": ctx["po"], "desc": po["desc"], "net": po["net"], "tax": po["tax"], "billed": float(billed),
                                    "remaining": float(D(po["net"]) - billed), "lines": po["lines"], "inferred": not ex.get("po_ref")}
            if ctx.get("vendor_id"): final["vendor_info"] = {"id": ctx["vendor_id"], **VENDORS[ctx["vendor_id"]]}
            new_steps.append(stage_step("decide", {"APPROVE": "pass", "HOLD": "warn", "REJECT": "fail_reject"}[dec], "Decision", f"{dec}: {head}", {}, prev))
            record = {**public(run), **final, "stages": list(run["stages"]) + new_steps, "status": "done", "current": None}
            hook("in_critical", run)
            persist_run(c, record)
        committed = True
        hook("after_commit", run)
        for s in new_steps:                                 # presentation only: the decision above is already committed
            run["current"] = s["key"]
            if s["key"] == "decide": run.update(final)
            run["stages"].append(s)
            if s["key"] != "decide" and stage_delay() > 0: time.sleep(stage_delay())
    except Exception as e:                                  # never leave a run hanging
        if committed: return _finish(run)
        failed = run.get("current") or "ingest"
        run.setdefault("extracted", {})
        msg = f"{type(e).__name__}: {e}"
        run.update(decision="HOLD", headline="Processing failed — nothing was decided automatically",
                   reasoning=f"The pipeline stopped at '{failed}' ({msg}). No payment decision was made.",
                   action="Retry the run. If it fails again, the file is probably corrupt or not an invoice: ask the vendor to resend.",
                   is_duplicate=False, error=msg, failed_stage=failed)
        step(run, failed, "error", dict(STAGES).get(failed, failed), f"Failed: {msg}", wait=False)
        run.setdefault("file_hash", "")
        try:
            with exclusive_txn() as c: persist_run(c, {**public(run), "status": "done", "current": None})
        except Exception as e2: print("could not persist failed run", run["id"], e2, flush=True)
    finally:
        _finish(run)

def _finish(run):
    run["status"] = "done"; run["current"] = None; run["_done"] = time.time()

def new_run_id():
    while True:
        rid = uuid.uuid4().hex[:8]
        if rid in RUNS: continue
        with conn() as c:
            if not c.execute("SELECT 1 FROM runs WHERE id=?", (rid,)).fetchone(): return rid

def start_run(filename, attachment_id, retry_of=None, up_hash=None):
    purge_runs()
    rid = new_run_id()
    run = {"id": rid, "ver": SCHEMA_VERSION, "ts": datetime.now().isoformat(timespec="seconds"), "filename": safe_name(filename), "status": "running",
           "stages": [], "current": "ingest", "attachment_id": attachment_id, "retry_of": retry_of, "decision": None, "headline": "",
           "file_hash": "", "extracted": {}, "_up": up_hash, "_ws": _WS.get()}
    RUNS[rid] = run
    threading.Thread(target=execute, args=(run,), daemon=True).start()
    return rid

# ---------- http ----------
LEGACY_NOTE = ("Recorded by an earlier version of this pipeline. Where it says an invoice was “released to payment run”, it means the invoice was approved and "
               "eligible for payment processing — this system never released, scheduled or sent a payment.")

def snapshot(obj):
    """JSON-safe copy of a run that another thread may still be appending to."""
    for _ in range(8):
        try: return json.loads(json.dumps(obj))
        except RuntimeError: time.sleep(0.005)
    return json.loads(json.dumps(obj))

def supersession(runs):
    """Derived (never stored): a held run is superseded by the latest later submission that corrects or repeats it."""
    out = {}
    for r in sorted(runs, key=lambda x: x["ts"]):
        rel = r.get("related") or {}
        if rel.get("relation") in ("corrected_resubmission", "unchanged_resubmission"):
            for o in rel.get("of", []):
                if o.get("decision") == "HOLD": out[o["id"]] = r["id"]
    return out

WS_COOKIE = "ap_ws"
NO_WS_PATHS = ("/api/config", "/api/samples")        # need no private data, so they never create a workspace

class H(BaseHTTPRequestHandler):
    timeout = 60                  # a stalled or trickling client cannot hold a thread forever
    _new_ws = None

    def log_message(self, *a): pass

    def client_ip(self):
        if os.environ.get("AP_TRUST_PROXY") == "1":                      # behind Render's proxy the real client is the first X-Forwarded-For hop
            fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            if fwd: return fwd[:64]
        return self.client_address[0]

    def secure(self): return os.environ.get("AP_COOKIE_SECURE") == "1" or self.headers.get("X-Forwarded-Proto") == "https"

    def send(self, code, body, ctype="application/json", headers=None):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b))); self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("X-Frame-Options", "SAMEORIGIN"); self.send_header("Referrer-Policy", "no-referrer")
        if self._new_ws:
            ttl = _int_env("AP_WORKSPACE_TTL_HOURS", 24) * 3600
            self.send_header("Set-Cookie", f"{WS_COOKIE}={self._new_ws}; Path=/; Max-Age={ttl}; HttpOnly; SameSite=Lax" + ("; Secure" if self.secure() else ""))
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)
        if (self.command != "GET" or code >= 400) and os.environ.get("AP_LOG_REQUESTS", "1") != "0":                           # one line per write or error (never per poll), for the host's log view
            ws = _WS.get(); print(f"{datetime.now():%H:%M:%S} {self.client_ip()} {self.command} {urlparse(self.path).path} -> {code}" + (f" ws={ws[:6]}" if ws else ""), flush=True)

    def fail(self, code, msg): self.send(code, {"error": msg})

    def _dispatch(self, fn):
        self._new_ws = None; tok = None
        try:
            path = unquote(urlparse(self.path).path)
            if workspaces_enabled() and path.startswith("/api/") and path not in NO_WS_PATHS:
                m = re.search(r"(?:^|;\s*)%s=([0-9a-f]{32})(?:;|$)" % WS_COOKIE, self.headers.get("Cookie") or "")
                ws = m.group(1) if m else None
                if not ws or not os.path.isdir(ws_dir(ws)):                  # no cookie, or an expired/unknown id: a fresh private workspace (an unknown id grants nothing)
                    lim = _int_env("AP_RATE_NEW_WS_PER_10MIN", 30)         # one address cannot fill the workspace quota by discarding its cookie
                    if lim and not RATE.allow(("ws", self.client_ip()), lim, 600): return self.fail(429, "Too many new sessions from this address. Please wait a few minutes.")
                    try: ws = create_workspace()
                    except WorkspaceBusy: return self.fail(503, "The demo is at capacity right now. Please try again in a few minutes.")
                    self._new_ws = ws
                touch_ws(ws); tok = _WS.set(ws)
            fn()
        except (BrokenPipeError, ConnectionResetError): pass
        except Exception as e:
            try: self.fail(500, f"Internal error ({type(e).__name__}).")
            except Exception: pass
        finally:
            if tok is not None: _WS.reset(tok)

    def do_GET(self): self._dispatch(self._get)
    def do_POST(self): self._dispatch(self._post)

    def _get(self):
        p = unquote(urlparse(self.path).path)
        if p in ("/", "/index.html"):
            return self.send(200, read_bytes(os.path.join(BASE, "static", "index.html")), "text/html; charset=utf-8")
        if p == "/healthz": return self.send(200, b"ok", "text/plain")
        if p == "/api/samples":
            return self.send(200, sorted(f for f in os.listdir(INV_DIR) if f.endswith(".pdf")))
        if p == "/api/config":
            return self.send(200, {"rules": RULES, "stages": STAGES, "vendors": VENDORS, "pos": POS, "reset_enabled": reset_enabled(),
                                   "workspaces": workspaces_enabled(), "workspace_ttl_hours": _int_env("AP_WORKSPACE_TTL_HOURS", 24),
                                   "limits": {"max_upload_mb": MAX_UPLOAD // (1024 * 1024)},
                                   "extraction": {"llm_configured": llm_configured(), "llm_mode": llm_mode(), "llm_model": llm_model(), "llm_providers": active_providers(), "ocr_engines": ocr_engines()}})
        if p == "/api/dashboard":
            runs = load_runs(); sup = supersession(runs)
            for r in runs: r.pop("raw_text", None); r["superseded_by"] = sup.get(r["id"])
            return self.send(200, {"runs": runs, "billed": {pid: float(po_consumed(pid, runs)) for pid in POS}, "pos": POS})
        m = re.fullmatch(r"/api/run/([0-9a-f]{8})/document", p)
        if m:
            state, aid, note = attachment_state(m[1])
            if state is None: return self.fail(404, "Run not found.")
            try:
                if state != "ok": raise AttachmentUnavailable(note or "The original document is not available.")
                data, name = read_attachment(aid)
            except AttachmentUnavailable as e:
                return self.send(410, {"error": f"The original document for this run is no longer available. {e}", "unavailable": True})
            return self.send(200, data, "application/pdf", {"Content-Disposition": f'inline; filename="{name}"'})
        m = re.fullmatch(r"/api/run/([^/]+)", p)
        if m:
            rid = m[1]; r = own_run(rid)
            d = snapshot(public(r)) if r else get_run_row(rid)
            if not d: return self.fail(404, "not found")
            if d.get("status") == "done" or not r: d["document"] = document_info(rid)
            else: d["document"] = {"available": True, "url": f"/api/run/{rid}/document", "reason": None}
            d["superseded_by"] = supersession(load_runs()).get(rid)
            if d.get("ver") != SCHEMA_VERSION and d.get("decision"): d["notes"] = [LEGACY_NOTE]
            return self.send(200, d)
        self.fail(404, "not found")

    def _drain(self, n):
        left = min(n, 64 * 1024 * 1024)
        while left > 0:
            chunk = self.rfile.read(min(left, 1 << 20))
            if not chunk: break
            left -= len(chunk)

    def _admit(self, nbytes=0):
        """Public-demo limits: requests per minute per client, concurrent runs, runs and megabytes per workspace. Off for a local single-user server."""
        lim = _int_env("AP_RATE_RUNS_PER_MIN", 30 if workspaces_enabled() else 0)
        if lim and not RATE.allow(("run", self.client_ip()), lim):
            self.fail(429, "Too many invoices submitted in a short time. Please wait a minute and try again."); return False
        if not workspaces_enabled(): return True
        if sum(1 for r in list(RUNS.values()) if r.get("status") == "running") >= _int_env("AP_MAX_ACTIVE_RUNS", 8):
            self.fail(503, "The demo is busy processing other invoices. Please try again in a few seconds."); return False
        with conn() as c: n = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        if n >= _int_env("AP_MAX_RUNS_PER_WS", 100):
            self.fail(429, f"This workspace already holds {n} runs. Use Clear history to start fresh."); return False
        try: used = sum(e.stat().st_size for e in os.scandir(uploads_dir()) if e.is_file())
        except OSError: used = 0
        if used + nbytes > _int_env("AP_MAX_WS_MB", 60) * 1024 * 1024:
            self.fail(413, "This workspace has reached its storage limit. Use Clear history, or use a new browser session."); return False
        return True

    def _post(self):
        p = urlparse(self.path).path
        try: n = int(self.headers.get("Content-Length") or 0)
        except ValueError: return self.fail(400, "Bad Content-Length header.")
        if n < 0: return self.fail(400, "Bad Content-Length header.")
        limit = MAX_UPLOAD if p == "/api/upload" else MAX_JSON_BODY
        if n > limit:
            self._drain(n)
            return self.fail(413, "PDF is larger than 10 MB." if p == "/api/upload" else "Request body is too large.")
        body = self.rfile.read(n)
        if p == "/api/process_sample":
            try: name = os.path.basename(json.loads(body)["name"])
            except Exception: return self.fail(400, 'Expected JSON like {"name": "01_acme_clean.pdf"}.')
            fp = os.path.join(INV_DIR, name)
            if not name.lower().endswith(".pdf") or not os.path.isfile(fp): return self.fail(404, "Unknown sample invoice.")
            if not self._admit(os.path.getsize(fp)): return
            aid = store_attachment(read_bytes(fp), name, "sample")
            return self.send(200, {"id": start_run(name, aid)})
        if p.startswith("/api/retry/"):
            rid = p.rsplit("/", 1)[-1]
            old = own_run(rid) or get_run_row(rid)
            if not old: return self.fail(404, "Run not found.")
            if not old.get("error"):
                return self.fail(409, "Only runs that failed during processing can be retried. This run already has a decision; upload a corrected invoice instead.")
            state, aid, note = attachment_state(rid)
            try:
                if state != "ok": raise AttachmentUnavailable(note or "not available")
                read_attachment(aid)
            except AttachmentUnavailable:
                return self.fail(404, "The original document is no longer available, so this run cannot be retried.")
            if not self._admit(0): return
            return self.send(200, {"id": start_run(old["filename"], aid, retry_of=old["id"])})
        if p == "/api/upload":
            name = safe_name(unquote(self.headers.get("X-Filename", "upload.pdf")))
            if not body: return self.fail(400, "The file is empty.")
            if not name.lower().endswith(".pdf") or not body.startswith(b"%PDF"):
                return self.fail(400, "That file is not a PDF. Upload an invoice as a .pdf file.")
            h = sha_hex(body)[:16]
            with UP_LOCK:     # double-click guard: the same file, already running, is not started twice
                live = next((r for r in list(RUNS.values()) if r["status"] == "running" and r["filename"] == name and r.get("_up") == h and r.get("_ws") == _WS.get()), None)
                if live: return self.send(200, {"id": live["id"], "reused": True})
                if not self._admit(len(body)): return
                aid = store_attachment(body, name, "upload")
                rid = start_run(name, aid, up_hash=h)
            return self.send(200, {"id": rid})
        if p == "/api/reset":
            if not reset_enabled():
                return self.fail(403, "History reset is disabled on this server, so saved runs cannot be erased. (Local development only: set AP_ALLOW_RESET=1.)")
            mine = _WS.get()
            with exclusive_txn() as c:
                if not mine:     # a consistent safety copy first, next to the database (never in a served folder); a throw-away visitor workspace needs none
                    bdir = os.path.join(os.path.dirname(os.path.abspath(db_path())), "reset-backups"); os.makedirs(bdir, exist_ok=True)
                    src = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True); dst = sqlite3.connect(os.path.join(bdir, f"runs-{datetime.now():%Y%m%d-%H%M%S-%f}.db"))
                    src.backup(dst); dst.close(); src.close()
                c.execute("DELETE FROM runs"); c.execute("DELETE FROM run_attachments")
                if mine: c.execute("DELETE FROM attachments")
            if mine: shutil.rmtree(uploads_dir(), ignore_errors=True)    # frees the workspace's storage allowance; the history is gone, so are its documents
            for k in [k for k, v in list(RUNS.items()) if v.get("_ws") == mine]: RUNS.pop(k, None)    # only this visitor's runs
            return self.send(200, {"ok": True})
        self.fail(404, "not found")

def make_server(host, port):
    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    return srv

if __name__ == "__main__":
    init_store()
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8765"))
    if workspaces_enabled(): start_janitor()
    print(f"AP pipeline on http://localhost:{port}   (history reset {'ENABLED' if reset_enabled() else 'disabled'}; "
          f"{'private per-visitor workspaces' if workspaces_enabled() else 'single shared dataset'}; LLM providers: {', '.join(active_providers()) or 'none (rules only)'})", flush=True)
    make_server(host, port).serve_forever()
