"""OCR safety on a small host: bounded memory, one OCR at a time, an out-of-memory kill cannot take the web server down.
(Found on the free Render instance: the scanned sample peaked at ~900 MB in the web-server process and the whole instance was killed.)"""
import importlib.util, os, subprocess, sys, tempfile, threading, time, unittest
from types import SimpleNamespace
from unittest import mock
import fitz
from helpers import AppTestCase, ROOT, make_invoice, sample_bytes

HAS_RAPID = importlib.util.find_spec("rapidocr_onnxruntime") is not None


def image_only_pdf(pages):
    doc = fitz.open()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40), False); pix.clear_with(200)
    for _ in range(pages): doc.new_page(width=300, height=300).insert_image(fitz.Rect(10, 10, 200, 200), pixmap=pix)
    return doc.tobytes()


class OcrLimits(AppTestCase):
    def tmp_pdf(self, data):
        f = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False); f.write(data); f.close(); self.addCleanup(os.unlink, f.name); return f.name

    def test_render_resolution_is_capped_for_oversized_pages(self):
        r = lambda w, h: SimpleNamespace(width=w, height=h); dpi = self.server.ocr_render_dpi
        self.assertEqual(dpi(r(595, 842)), 200)                               # A4: unchanged
        self.assertEqual(dpi(r(3000, 4000)), 43)                              # poster: the bitmap stays at <= 2400 px on its long side
        self.assertLessEqual(4000 / 72 * dpi(r(3000, 4000)), 2401)
        self.assertGreaterEqual(dpi(r(1e6, 1e6)), 30); self.assertEqual(dpi(r(0, 0)), 200)

    def test_too_many_scanned_pages_are_refused_before_any_ocr_runs(self):
        os.environ["AP_OCR_MAX_PAGES"] = "2"; self.addCleanup(os.environ.pop, "AP_OCR_MAX_PAGES", None)
        with mock.patch.object(self.server, "ocr_png", side_effect=AssertionError("OCR must not start")):
            with self.assertRaises(RuntimeError) as cm: self.server.extract_text(self.tmp_pdf(image_only_pdf(3)))
        self.assertIn("3 pages", str(cm.exception)); self.assertIn("at most 2 scanned pages", str(cm.exception))

    def test_only_one_ocr_runs_at_a_time(self):
        live, peak, lock = [0], [0], threading.Lock()
        def fake(png, engine):
            with lock: live[0] += 1; peak[0] = max(peak[0], live[0])
            time.sleep(0.05)
            with lock: live[0] -= 1
            return "text"
        with mock.patch.object(self.server, "_ocr_run", fake):
            ts = [threading.Thread(target=self.server.ocr_png, args=(b"x", "rapidocr")) for _ in range(6)]; [t.start() for t in ts]; [t.join(10) for t in ts]
        self.assertEqual(peak[0], 1)

    def test_a_killed_ocr_worker_fails_the_run_politely_and_the_server_stays_up(self):
        os.environ["AP_OCR_ENGINE"] = "rapidocr"; self.addCleanup(os.environ.pop, "AP_OCR_ENGINE", None)
        killed = subprocess.CompletedProcess([], -9, "", "")
        with mock.patch.object(self.server, "ocr_engines", lambda: ["rapidocr"]), mock.patch.object(self.server.subprocess, "run", return_value=killed):
            r = self.app.submit(make_invoice(invoice_no="INV-OOM-0001", as_scan=True), "scan.pdf")
        self.assertEqual(r["status"], "done"); self.assertIn("OCR worker was killed", r["error"]); self.assertEqual(r["decision"], "HOLD")   # a processing failure is held for a retry, never approved
        self.assertEqual(self.app.get("/api/config")[0], 200)                         # still serving
        self.assertEqual(self.app.post(f"/api/retry/{r['id']}")[0], 200)              # and the run can be retried

    def test_secrets_are_not_handed_to_the_ocr_worker(self):
        seen = {}
        def fake(cmd, **kw): seen.update(kw["env"]); return subprocess.CompletedProcess(cmd, 0, "line one", "")
        os.environ.update(SARVAM_API_KEY="s-secret", GEMINI_API_KEY="g-secret", SOME_TOKEN="t-secret"); self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("SARVAM_API_KEY", "GEMINI_API_KEY", "SOME_TOKEN")])
        with mock.patch.object(self.server.subprocess, "run", fake): self.assertEqual(self.server._ocr_run(b"png", "rapidocr"), "line one")
        self.assertFalse([k for k in seen if "KEY" in k or "TOKEN" in k or "SECRET" in k]); self.assertIn("PATH", seen)


@unittest.skipUnless(HAS_RAPID, "rapidocr is not installed")
class RapidOcrWorker(AppTestCase):
    def test_worker_memory_stays_far_below_a_512mb_host(self):
        code = ("import resource,subprocess,sys,platform;"
                f"subprocess.run([sys.executable,{os.path.join(ROOT, 'ocr_worker.py')!r},{os.path.join(ROOT, 'invoices', '02_brightline_scanned.pdf')!r}],capture_output=True);"
                "print(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss/(1048576 if platform.system()=='Darwin' else 1024))")
        pdf = fitz.open(os.path.join(ROOT, "invoices", "02_brightline_scanned.pdf")); png = os.path.join(tempfile.mkdtemp(prefix="ap-ocr-"), "p.png"); self.addCleanup(__import__("shutil").rmtree, os.path.dirname(png), True)
        open(png, "wb").write(pdf[0].get_pixmap(dpi=self.server.ocr_render_dpi(pdf[0].rect)).tobytes("png"))
        code = code.replace(repr(os.path.join(ROOT, "invoices", "02_brightline_scanned.pdf")), repr(png))
        mb = float(subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300).stdout.strip())
        print(f"\n[OCR worker peak: {mb:.0f} MB]"); self.assertLess(mb, 450, "the OCR worker must fit beside the web server on a 512 MB host (library default was ~900 MB)")

    def test_scanned_sample_reads_the_same_fields_with_rapidocr(self):
        os.environ["AP_OCR_ENGINE"] = "rapidocr"; self.addCleanup(os.environ.pop, "AP_OCR_ENGINE", None)
        with mock.patch.object(self.server, "ocr_engines", lambda: ["rapidocr"]): r = self.app.submit_sample("02_brightline_scanned.pdf")
        ex = r["extracted"]
        self.assertEqual((ex["invoice_no"], ex["gstin"], ex["po_ref"], ex["total"], ex["bank"], ex["ifsc"]), ("BL/2026/1187", "27AAFFB5678K1Z2", "PO-2026-1002", 100654.0, "60110045678901", "ICIC0004567"))
        self.assertEqual(r["extraction"]["engine"], "rapidocr"); self.assertEqual(r["decision"], "APPROVE")


if __name__ == "__main__":
    unittest.main()
