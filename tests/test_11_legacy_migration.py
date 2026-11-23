"""Legacy history (written by the previous version) is preserved byte for byte. Each old run is linked to its original document only
when that document is provably recoverable (checksum equals the hash recorded on the run); an overwritten original is reported as
unavailable - never replaced by an unrelated document."""
import hashlib, json, os, shutil, sqlite3, tempfile, unittest
from helpers import InProcessApp, make_invoice, sample_bytes, INVOICES

LEGACY_DDL = """CREATE TABLE runs(id TEXT PRIMARY KEY, ts TEXT, filename TEXT, file_hash TEXT, vendor_id TEXT,
        invoice_no TEXT, po TEXT, net REAL, total REAL, decision TEXT, headline TEXT, body TEXT)"""
h16 = lambda b: hashlib.sha256(b).hexdigest()[:16]


def legacy_body(rid, ts, filename, data, path, invoice_no, decision="APPROVE"):
    return {"id": rid, "ts": ts, "filename": filename, "status": "done", "stages": [{"key": "ingest", "status": "pass", "title": "Ingest", "detail": "x", "extra": {}, "t": ts, "ms": 1}],
            "current": None, "path": path, "retry_of": None, "decision": decision, "headline": "Approved against PO-2026-1001", "file_hash": h16(data), "_last": 1.0, "_up": "abc",
            "reasoning": "All checks passed. Total ₹153,400.00 released to payment run.", "action": "Schedule payment per terms; log PO consumption.", "vendor_id": "V001",
            "po": "PO-2026-1001", "is_duplicate": False, "ocr": False, "raw_text": "Acme Industrial Supplies Pvt Ltd",
            "extracted": {"vendor": "Acme Industrial Supplies Pvt Ltd", "invoice_no": invoice_no, "subtotal": 130000.0, "tax": 23400.0, "total": 153400.0, "lines": []}}


class LegacyMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ap-legacy-"); self.addCleanup(shutil.rmtree, self.tmp, True)
        self.up = os.path.join(self.tmp, "uploads"); os.makedirs(self.up); self.db = os.path.join(self.tmp, "runs.db")
        sample01, sample02 = sample_bytes("01_acme_clean.pdf"), sample_bytes("02_brightline_scanned.pdf")
        first, second = make_invoice(invoice_no="INV-OLD-1"), make_invoice(invoice_no="INV-OLD-2")
        other = make_invoice(invoice_no="INV-OLD-3")
        put = lambda name, data: open(os.path.join(self.up, name), "wb").write(data)
        put("01_acme_clean.pdf", sample01)                      # A: intact legacy upload
        put("invoice.pdf", second)                              # B: two runs share this path; only the second still exists
        put("collide.pdf", sample01)                            # D: the stored file is something else entirely (a bundled sample)
        self.docs = {"A": sample01, "B2": second, "E": sample02}
        P = lambda n: os.path.join(self.up, n)
        rows = [legacy_body("aaaaaaaa", "2026-11-23T10:00:00", "01_acme_clean.pdf", sample01, P("01_acme_clean.pdf"), "INV-ACM-0421"),
                legacy_body("bbbbbbb1", "2026-11-23T10:01:00", "invoice.pdf", first, P("invoice.pdf"), "INV-OLD-1", "HOLD"),
                legacy_body("bbbbbbb2", "2026-11-23T10:02:00", "invoice.pdf", second, P("invoice.pdf"), "INV-OLD-2", "HOLD"),
                legacy_body("cccccccc", "2026-11-23T10:03:00", "gone.pdf", other, P("gone.pdf"), "INV-OLD-3", "HOLD"),
                legacy_body("dddddddd", "2026-11-23T10:04:00", "collide.pdf", other, P("collide.pdf"), "INV-OLD-4", "HOLD"),
                legacy_body("eeeeeeee", "2026-11-23T10:05:00", "02_brightline_scanned.pdf", sample02, P("02_brightline_scanned.pdf"), "BL/2026/1187", "HOLD")]
        with sqlite3.connect(self.db) as c:
            c.execute(LEGACY_DDL)
            for b in rows:
                c.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (b["id"], b["ts"], b["filename"], b["file_hash"], b["vendor_id"], b["extracted"]["invoice_no"], b["po"], 130000.0, 153400.0, b["decision"], b["headline"], json.dumps(b)))
        self.before_rows = sqlite3.connect(self.db).execute("SELECT * FROM runs ORDER BY id").fetchall()
        self.before_files = {n: hashlib.sha256(open(P(n), "rb").read()).hexdigest() for n in os.listdir(self.up)}
        self.app = InProcessApp(env={"AP_DB": self.db, "AP_UPLOADS": self.up}); self.addCleanup(self.app.close)

    def doc(self, rid):
        s, h, b = self.app.document(rid); return s, b

    def test_history_rows_and_original_files_are_untouched(self):
        self.assertEqual(sqlite3.connect(self.db).execute("SELECT * FROM runs ORDER BY id").fetchall(), self.before_rows)
        for n, digest in self.before_files.items(): self.assertEqual(hashlib.sha256(open(os.path.join(self.up, n), "rb").read()).hexdigest(), digest, n)
        self.assertEqual({r["id"] for r in self.app.runs()}, {"aaaaaaaa", "bbbbbbb1", "bbbbbbb2", "cccccccc", "dddddddd", "eeeeeeee"})

    def test_recoverable_documents_are_linked_and_served(self):
        self.assertEqual(self.doc("aaaaaaaa"), (200, self.docs["A"]))
        self.assertEqual(self.doc("bbbbbbb2"), (200, self.docs["B2"]))
        self.assertEqual(self.doc("eeeeeeee"), (200, self.docs["E"]))                            # upload gone, but the bundled sample matches the recorded checksum

    def test_overwritten_originals_are_reported_unavailable_not_replaced(self):
        s, b = self.doc("bbbbbbb1")                                                              # shared path, now holds the SECOND invoice
        self.assertEqual(s, 410); self.assertIn(b"overwritten by a later upload", b); self.assertNotEqual(b, self.docs["B2"])
        s, b = self.doc("dddddddd"); self.assertEqual(s, 410)                                    # filename of a sample, but different content than recorded
        info = self.app.get("/api/run/bbbbbbb1")[1]["document"]; self.assertFalse(info["available"]); self.assertIn("overwritten", info["reason"])

    def test_missing_originals_are_reported_unavailable(self):
        s, b = self.doc("cccccccc"); self.assertEqual(s, 410); self.assertIn(b"was not kept", b)

    def test_migration_is_idempotent_and_never_rewrites_runs(self):
        with sqlite3.connect(self.db) as c: n1 = c.execute("SELECT COUNT(*) FROM attachments").fetchone()[0], c.execute("SELECT COUNT(*) FROM run_attachments").fetchone()[0]
        self.app.reopen(); self.app.reopen()
        with sqlite3.connect(self.db) as c: n2 = c.execute("SELECT COUNT(*) FROM attachments").fetchone()[0], c.execute("SELECT COUNT(*) FROM run_attachments").fetchone()[0]
        self.assertEqual(n1, n2); self.assertEqual(n1, (3, 6))
        self.assertEqual(sqlite3.connect(self.db).execute("SELECT * FROM runs ORDER BY id").fetchall(), self.before_rows)

    def test_old_wording_is_kept_but_explained(self):
        r = self.app.get("/api/run/aaaaaaaa")[1]
        self.assertIn("released to payment run", r["reasoning"])                                  # the stored explanation is preserved verbatim
        self.assertIn("never released, scheduled or sent a payment", r["notes"][0])
        self.assertNotIn("ver", r)

    def test_legacy_history_still_drives_duplicate_and_po_logic(self):
        r = self.app.submit_sample("01_acme_clean.pdf")                                           # INV-ACM-0421 was approved by the previous version
        self.assertEqual(r["decision"], "REJECT"); self.assertEqual(r["related"]["of"][0]["id"], "aaaaaaaa")
        self.assertEqual(self.app.get("/api/dashboard")[1]["billed"]["PO-2026-1001"], 130000.0)


if __name__ == "__main__":
    unittest.main()
