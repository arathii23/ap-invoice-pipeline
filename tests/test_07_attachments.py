"""Defect 7: every run keeps its own immutable document under a unique id. Filenames (repeated, colliding with a bundled sample,
hostile, concurrent) never decide which document a run shows; previews, downloads and retries resolve through the run's attachment."""
import os, re, stat, threading, unittest
from helpers import AppTestCase, make_invoice, sample_bytes, make_po, PO_ID


class Attachments(AppTestCase):
    def doc(self, run):
        s, h, b = self.app.document(run["id"]); return s, b

    def stored_files(self):
        return sorted(os.listdir(self.app.uploads))

    def test_two_different_uploads_named_invoice_pdf_each_show_their_own_document(self):
        p1, p2 = make_invoice(invoice_no="INV-F-0001"), make_invoice(invoice_no="INV-F-0002", date="06 Nov 2026")
        r1, r2 = self.app.submit(p1, "invoice.pdf"), self.app.submit(p2, "invoice.pdf")
        self.assertEqual((r1["filename"], r2["filename"]), ("invoice.pdf", "invoice.pdf"))
        self.assertEqual(self.doc(r1), (200, p1)); self.assertEqual(self.doc(r2), (200, p2))     # the first run does NOT show the second PDF
        s, h, _ = self.app.document(r1["id"]); self.assertEqual(h["Content-Type"], "application/pdf"); self.assertIn("invoice.pdf", h["Content-Disposition"])

    def test_upload_named_like_a_bundled_sample_shows_the_upload_and_the_sample_run_shows_the_sample(self):
        sample, mine = sample_bytes("01_acme_clean.pdf"), make_invoice(invoice_no="INV-F-0003")
        self.assertNotEqual(sample, mine)
        up = self.app.submit(mine, "01_acme_clean.pdf"); smp = self.app.submit_sample("01_acme_clean.pdf")
        self.assertEqual(self.doc(up), (200, mine)); self.assertEqual(self.doc(smp), (200, sample))

    def test_no_route_serves_a_document_by_filename(self):
        self.app.submit(make_invoice(invoice_no="INV-F-0004"), "invoice.pdf")
        for path in ("/pdf/invoice.pdf", "/pdf/01_acme_clean.pdf", "/data/uploads/invoice.pdf", "/invoices/01_acme_clean.pdf"):
            self.assertEqual(self.app.request("GET", path)[0], 404, path)

    def test_concurrent_uploads_with_the_same_filename_do_not_collide(self):
        pdfs = [make_invoice(invoice_no=f"INV-F-{n:04d}", date=f"{n:02d} Nov 2026") for n in range(10, 18)]
        out = [None] * len(pdfs)
        def go(i): out[i] = self.app.submit(pdfs[i], "invoice.pdf")
        ts = [threading.Thread(target=go, args=(i,)) for i in range(len(pdfs))]; [t.start() for t in ts]; [t.join() for t in ts]
        for r, p in zip(out, pdfs): self.assertEqual(self.doc(r), (200, p))
        self.assertEqual(len(set(self.stored_files())), len(pdfs))

    def test_documents_are_still_correct_after_restart(self):
        p1, p2 = make_invoice(invoice_no="INV-F-0005"), make_invoice(invoice_no="INV-F-0006", date="06 Nov 2026")
        r1, r2 = self.app.submit(p1, "invoice.pdf"), self.app.submit(p2, "invoice.pdf")
        self.app.reopen()                                                     # forget in-memory runs, re-open the same DB + files
        self.assertEqual(self.doc(r1), (200, p1)); self.assertEqual(self.doc(r2), (200, p2))
        self.assertEqual({r["id"] for r in self.app.runs()}, {r1["id"], r2["id"]})
        self.assertTrue(self.app.get(f"/api/run/{r1['id']}")[1]["document"]["available"])

    def test_stored_files_use_unique_ids_are_read_only_and_stay_inside_the_upload_folder(self):
        r = self.app.submit(make_invoice(invoice_no="INV-F-0007"), "../../evil name.pdf")
        self.assertEqual(r["filename"], "evil name.pdf")
        names = self.stored_files(); self.assertEqual(len(names), 1); self.assertRegex(names[0], r"^[0-9a-f]{32}\.pdf$")
        self.assertFalse(os.stat(os.path.join(self.app.uploads, names[0])).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        self.assertEqual(os.listdir(os.path.dirname(self.app.uploads)).count("evil name.pdf"), 0)

    def test_a_stored_file_that_no_longer_matches_its_checksum_is_not_served(self):
        r = self.app.submit(make_invoice(invoice_no="INV-F-0008"), "a.pdf")
        path = os.path.join(self.app.uploads, self.stored_files()[0]); os.chmod(path, 0o644)
        with open(path, "ab") as f: f.write(b"tampered")
        s, body = self.app.document(r["id"])[0], self.app.document(r["id"])[2]
        self.assertEqual(s, 410); self.assertIn(b"no longer matches its recorded checksum", body)
        self.assertFalse(self.app.get(f"/api/run/{r['id']}")[1]["document"]["available"])

    def test_a_missing_file_is_reported_unavailable_never_replaced_by_another_document(self):
        keep = make_invoice(invoice_no="INV-F-0009")
        r1 = self.app.submit(make_invoice(invoice_no="INV-F-0010"), "same.pdf"); r2 = self.app.submit(keep, "same.pdf")
        import sqlite3
        with sqlite3.connect(self.app.db) as c: aid = c.execute("SELECT attachment_id FROM run_attachments WHERE run_id=?", (r1["id"],)).fetchone()[0]
        os.remove(os.path.join(self.app.uploads, aid + ".pdf"))
        s, h, b = self.app.document(r1["id"])
        self.assertEqual(s, 410); self.assertIn(b"no longer available", b)
        self.assertEqual(self.doc(r2), (200, keep))
        info = self.app.get(f"/api/run/{r1['id']}")[1]["document"]; self.assertFalse(info["available"]); self.assertTrue(info["reason"])

    def test_retry_reuses_the_runs_own_document_and_only_failed_runs_can_be_retried(self):
        bad = b"%PDF-1.4\nthis is not really a pdf"
        failed = self.app.submit(bad, "invoice.pdf")                                   # processing error
        good = self.app.submit(make_invoice(invoice_no="INV-F-0011"), "invoice.pdf")   # same filename, different document
        self.assertTrue(failed.get("error")); self.assertEqual(good["decision"], "APPROVE")
        s, r = self.app.post(f"/api/retry/{failed['id']}"); self.assertEqual(s, 200)
        retried = self.app.wait(r["id"])
        self.assertEqual(retried["retry_of"], failed["id"]); self.assertEqual(self.doc(retried), (200, bad))      # the failed run's file, not the same-named upload
        self.assertEqual(self.app.post(f"/api/retry/{good['id']}")[0], 409)            # decided runs are not re-run
        self.assertEqual(self.app.post("/api/retry/deadbeef")[0], 404)

    def test_upload_validation(self):
        before = len(self.stored_files()) if os.path.isdir(self.app.uploads) else 0
        self.assertEqual(self.app.upload(b"hello", "notes.txt")[0], 400)
        self.assertEqual(self.app.upload(b"", "empty.pdf")[0], 400)
        self.assertEqual(self.app.upload(b"MZ not a pdf", "fake.pdf")[0], 400)
        s, body = self.app.upload(b"%PDF-1.4" + b"0" * (10 * 1024 * 1024), "huge.pdf")
        self.assertEqual(s, 413); self.assertIn("10 MB", body["error"])
        self.assertEqual(self.app.runs(), [])
        self.assertEqual(len(self.stored_files()) if os.path.isdir(self.app.uploads) else 0, before)


if __name__ == "__main__":
    unittest.main()
