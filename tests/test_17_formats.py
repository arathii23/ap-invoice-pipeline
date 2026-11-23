"""Real AP inputs, not only clean PDFs: phone photos and scans as images, forwarded e-mails with attachments, ZIP batches.
Every format is normalised to a PDF and runs through the same pipeline; unsupported formats get a specific, helpful message."""
import email.message, io, json, os, time, unittest, zipfile
import fitz
from PIL import Image
from helpers import AppTestCase, make_invoice, sample_bytes


def as_image(pdf, fmt="JPEG", rotate=0, dpi=170):
    pix = fitz.open(stream=pdf, filetype="pdf")[0].get_pixmap(dpi=dpi)
    im = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    exif = None
    if rotate:                                   # a phone photo: pixels stored sideways, EXIF says how to turn them upright
        im = im.rotate(90, expand=True); exif = Image.Exif(); exif[0x0112] = 6
    b = io.BytesIO(); im.save(b, fmt, **({"exif": exif} if exif is not None else {})); return b.getvalue()


def eml(attachments, subject="Invoice attached", sender="billing@acme.example", inline_logo=False):
    m = email.message.EmailMessage(); m["From"] = sender; m["To"] = "ap@zenith.example"; m["Subject"] = subject; m["Date"] = "Mon, 23 Nov 2026 10:00:00 +0530"
    m.set_content("Dear AP team,\nplease find our invoice attached.\nRegards")
    if inline_logo:
        b = io.BytesIO(); Image.new("RGB", (60, 20), "navy").save(b, "PNG")
        m.add_attachment(b.getvalue(), maintype="image", subtype="png", filename="logo.png", disposition="inline", cid="<logo>")
    for data, name, mt, st in attachments: m.add_attachment(data, maintype=mt, subtype=st, filename=name)
    return m.as_bytes()


def zipped(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files: z.writestr(name, data)
    return b.getvalue()


class Formats(AppTestCase):
    def upload(self, data, name):
        s, r = self.app.upload(data, name); return s, r

    def wait_all(self, ids, timeout=120):
        return [self.app.wait(i, timeout) for i in ids]

    def test_sniffing_recognises_every_format(self):
        sn = self.server.sniff; pdf = make_invoice()
        self.assertEqual(sn(pdf, "x.pdf"), "pdf"); self.assertEqual(sn(as_image(pdf, "JPEG"), "x.jpg"), "jpg"); self.assertEqual(sn(as_image(pdf, "PNG"), "x"), "png")
        self.assertEqual(sn(as_image(pdf, "TIFF"), "x.tif"), "tiff"); self.assertEqual(sn(as_image(pdf, "WEBP"), "x.webp"), "webp")
        self.assertEqual(sn(eml([(pdf, "i.pdf", "application", "pdf")]), "mail.eml"), "eml"); self.assertEqual(sn(zipped([("a.pdf", pdf)]), "a.zip"), "zip")
        self.assertEqual(sn(zipped([("[Content_Types].xml", b"x"), ("word/document.xml", b"x")]), "inv.docx"), "office")
        self.assertEqual(sn(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40, "IMG_0001.HEIC"), "heic")

    def test_phone_photo_is_turned_upright_read_by_ocr_and_approved(self):
        pdf = make_invoice(invoice_no="INV-PHOTO-1"); self.patch_ocr_engine()
        s, r = self.upload(as_image(pdf, "JPEG", rotate=90), "IMG_2041.jpg"); self.assertEqual(s, 200, r)
        run = self.app.wait(r["id"], 120)
        self.assertEqual(run["source"]["format"], "jpg"); self.assertTrue(run["ocr"]); self.assertEqual(run["filename"], "IMG_2041.pdf")
        self.assertEqual(run["extracted"]["invoice_no"], "INV-PHOTO-1"); self.assertEqual(run["decision"], "APPROVE")
        self.assertIn("converted to PDF", self.stage(run, "ingest")["detail"]); self.assertEqual(self.app.document(run["id"])[0], 200)

    def test_png_tiff_and_webp_scans(self):
        self.patch_ocr_engine()
        for n, fmt in enumerate(("PNG", "TIFF", "WEBP"), 1):
            s, r = self.upload(as_image(make_invoice(invoice_no=f"INV-IMG-{n}", po="PO-2026-1001", lines=(("Seal kit SK-11", 1, 2000.0, 2000.0),)), fmt), f"scan{n}.{fmt.lower()}")
            self.assertEqual(s, 200, (fmt, r)); run = self.app.wait(r["id"], 120)
            self.assertEqual(run["extracted"]["invoice_no"], f"INV-IMG-{n}", fmt); self.assertEqual(run["source"]["format"], {"PNG": "png", "TIFF": "tiff", "WEBP": "webp"}[fmt])

    def test_resending_the_same_photo_is_caught_as_a_duplicate(self):
        self.patch_ocr_engine(); img = as_image(make_invoice(invoice_no="INV-PHOTO-2"), "JPEG")
        a = self.app.wait(self.upload(img, "a.jpg")[1]["id"], 120); b = self.app.wait(self.upload(img, "b.jpg")[1]["id"], 120)
        self.assertEqual((a["decision"], b["decision"]), ("APPROVE", "REJECT"))

    def test_forwarded_email_with_two_invoices_and_a_signature_logo(self):
        mail = eml([(sample_bytes("01_acme_clean.pdf"), "INV-ACM-0421.pdf", "application", "pdf"), (sample_bytes("07_greenfield_price_variance.pdf"), "GF-90871.pdf", "application", "pdf")],
                   subject="Invoices for November", inline_logo=True)
        s, r = self.upload(mail, "Invoices for November.eml"); self.assertEqual(s, 200, r)
        self.assertEqual(len(r["ids"]), 2); self.assertEqual(r["container"]["type"], "email"); self.assertEqual(r["container"]["from"], "billing@acme.example")
        self.assertEqual([x["name"] for x in r["skipped"]], ["logo.png"])
        runs = self.wait_all(r["ids"])
        self.assertEqual([x["decision"] for x in runs], ["APPROVE", "HOLD"]); self.assertEqual(runs[0]["source"]["email"]["subject"], "Invoices for November")
        self.assertIn("e-mail “Invoices for November” sent by billing@acme.example", self.stage(runs[0], "ingest")["detail"])

    def test_email_without_an_invoice_attachment_is_explained(self):
        s, r = self.upload(eml([]), "hello.eml"); self.assertEqual(s, 400); self.assertIn("no attachment", r["error"])
        s, r = self.upload(eml([(b"just text", "notes.txt", "text", "plain")]), "notes.eml"); self.assertEqual(s, 400); self.assertIn("No invoice found", r["error"])

    def test_zip_batch_runs_serially_and_reports_what_it_skipped(self):
        z = zipped([("nov/01.pdf", sample_bytes("01_acme_clean.pdf")), ("nov/05.pdf", sample_bytes("05_acme_duplicate_resent.pdf")), ("nov/readme.txt", b"hi"),
                    ("__MACOSX/._01.pdf", b"junk"), ("nov/03.pdf", sample_bytes("03_novatech_milestone1.pdf"))])
        s, r = self.upload(z, "november.zip"); self.assertEqual(s, 200, r); self.assertEqual(len(r["ids"]), 3)
        self.assertEqual([x["name"] for x in r["skipped"]], ["readme.txt"])
        st = self.app.get("/api/runs?ids=" + ",".join(r["ids"]))[1]; self.assertEqual(len(st["runs"]), 3)
        runs = self.wait_all(r["ids"]); self.assertEqual([x["decision"] for x in runs], ["APPROVE", "REJECT", "APPROVE"])     # order kept: 05 is a duplicate of 01
        self.assertTrue(all(x.get("batch") == r["batch"] for x in runs))

    def test_whole_sample_set_as_one_batch(self):
        names = sorted(self.app.get("/api/samples")[1]); self.patch_ocr_engine()
        s, r = self.app.post("/api/process_samples", {"names": names}); self.assertEqual(s, 200, r); self.assertEqual(len(r["ids"]), len(names))
        dec = {x["filename"][:2]: x["decision"] for x in self.wait_all(r["ids"], 240)}
        self.assertEqual(dec["01"], "APPROVE"); self.assertEqual(dec["05"], "REJECT"); self.assertEqual(dec["07"], "HOLD"); self.assertEqual(dec["09"], "HOLD")

    def test_unsupported_formats_get_specific_help(self):
        for data, name, words in ((zipped([("[Content_Types].xml", b"x")]), "invoice.docx", "Office document"),
                                  (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40, "IMG_0001.HEIC", "HEIC"),
                                  (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 50, "mail.msg", "Outlook"),
                                  (b"hello world", "notes.txt", "not a supported file"),
                                  (b"\xff\xd8\xff\xe0" + b"\x00" * 30, "broken.jpg", "could not be read")):
            s, r = self.upload(data, name); self.assertEqual(s, 400, name); self.assertIn(words, r["error"], name)

    def test_zip_bomb_and_oversized_members_are_refused(self):
        big = b"0" * (11 * 1024 * 1024)
        s, r = self.upload(zipped([("huge.pdf", big), ("ok.pdf", sample_bytes("01_acme_clean.pdf"))]), "b.zip"); self.assertEqual(s, 200, r)
        self.assertEqual(r["skipped"][0]["reason"], "larger than 10 MB"); self.wait_all(r["ids"])

    def patch_ocr_engine(self):
        """Use the engine this machine has, so image tests run the real OCR path."""
        pass


if __name__ == "__main__":
    unittest.main()
