"""Defect 9: impossible or unsupported invoice dates are held with a clear explanation; valid supported formats keep working.
Documented interpretation: day first (DD/MM/YYYY), years 2000-2100, and no rule compares a date with today's date."""
import datetime, unittest
from helpers import AppTestCase, make_invoice, make_po, PO_ID

D = datetime.datetime
NOV5 = D(2026, 11, 5)


class DateReading(AppTestCase):
    def test_supported_formats_all_read_as_the_same_day(self):
        for s in ("05 Nov 2026", "5 Nov 2026", "05 November 2026", "05/11/2026", "5/11/2026", "05-11-2026", "05.11.2026", "2026-11-05", "05-Nov-2026", "05/11/26", "  05   Nov  2026 "):
            self.assertEqual(self.server.read_date(s)[0], NOV5, s)

    def test_impossible_dates_are_problems_with_a_reason(self):
        bad = {"31/99/2026": "out of range", "31/04/2026": "does not exist in that month", "29/02/2026": "does not exist in that month",
               "30 Feb 2026": "Feb 2026 has no day 30", "32 Nov 2026": "Nov 2026 has no day 32", "2026-13-01": "no month 13", "2026-02-30": "does not exist",
               "12/31/2026": "no month 31", "00/11/2026": "out of range", "31 Foo 2026": "not a month name", "05/11/1999": "outside the accepted range",
               "05/11/2101": "outside the accepted range", "05/11/70": "outside the accepted range"}
        for s, why in bad.items():
            dt, note, problem = self.server.read_date(s)
            self.assertIsNone(dt, s); self.assertIn(why, problem, s); self.assertIn(s, problem)

    def test_unsupported_formats_are_not_guessed(self):
        for s in ("Nov 5, 2026", "5th November 2026", "20261105", "2026/11/05", "yesterday", "", "  "):
            self.assertIsNone(self.server.read_date(s)[0], s)

    def test_leap_years(self):
        self.assertEqual(self.server.read_date("29 Feb 2028")[0], D(2028, 2, 29)); self.assertIsNone(self.server.read_date("29 Feb 2027")[0])
        self.assertIsNone(self.server.read_date("29/02/2100")[0])                               # 2100 is not a leap year

    def test_ambiguous_numeric_dates_are_read_day_first_and_say_so(self):
        dt, note, _ = self.server.read_date("03/04/2026")
        self.assertEqual(dt, D(2026, 4, 3)); self.assertIn("read day first, as 3 April 2026", note)
        self.assertIsNone(self.server.read_date("13/04/2026")[1]); self.assertIsNone(self.server.read_date("05/05/2026")[1])   # not ambiguous
        self.assertIsNone(self.server.read_date("05 Nov 2026")[1])

    def test_no_rule_compares_dates_with_today(self):
        self.assertIsNotNone(self.server.read_date("01 Jan 2099")[0]); self.assertIsNotNone(self.server.read_date("01 Jan 2001")[0])


class DatesEndToEnd(AppTestCase):
    def setUp(self):
        super().setUp(); self.patch_pos(**{PO_ID: make_po(net=100_000_000)})

    def sub(self, n, date):
        return self.app.submit(make_invoice(invoice_no=f"INV-D-{n:04d}", po=PO_ID, date=date, lines=[("Custom job", 1, 1000.0, 1000.0)]), f"d{n}.pdf")

    def test_reported_case_31_99_2026_is_held_with_a_clear_explanation(self):
        r = self.sub(1, "31/99/2026")
        self.assertEqual(r["decision"], "HOLD", r["reasoning"])
        f = self.stage(r, "fields"); self.assertEqual(f["status"], "fail_hold")
        self.assertIn("“31/99/2026” is not a valid date", f["detail"]); self.assertNotIn("unusual", f["detail"])
        self.assertEqual(r["headline"], "Invoice date is not valid — vendor follow-up needed"); self.assertIn("a valid invoice date", r["action"])
        self.assertFalse(f["extra"]["compare"][2]["ok"])

    def test_every_supported_format_is_approved_and_normalised(self):
        for i, s in enumerate(("05 Nov 2026", "05/11/2026", "05-11-2026", "05.11.2026", "2026-11-05", "05-Nov-2026", "05/11/26")):
            r = self.sub(10 + i, s); self.assertEqual(r["decision"], "APPROVE", (s, r["reasoning"])); self.assertEqual(r["extracted"]["invoice_date_iso"], "2026-11-05")

    def test_ambiguous_date_is_approved_with_the_interpretation_shown(self):
        r = self.sub(30, "03/04/2026"); self.assertEqual(r["decision"], "APPROVE")
        self.assertIn("read day first, as 3 April 2026", self.stage(r, "fields")["detail"]); self.assertEqual(r["extracted"]["invoice_date_iso"], "2026-04-03")

    def test_impossible_and_out_of_range_dates_are_held(self):
        for i, s in enumerate(("31/04/2026", "29/02/2026", "2026-13-01", "12/31/2026", "31 Foo 2026", "05/11/1999")):
            r = self.sub(40 + i, s); self.assertEqual(r["decision"], "HOLD", s); self.assertEqual(self.stage(r, "fields")["status"], "fail_hold", s)
        self.assertEqual([x["decision"] for x in self.app.runs()].count("APPROVE"), 0)

    def test_unsupported_format_that_cannot_be_found_is_a_missing_field(self):
        r = self.app.submit(make_invoice(invoice_no="INV-D-0099", date=None, extra_lines=["Invoice Date: Nov 5, 2026"]), "us.pdf")
        self.assertEqual(r["decision"], "HOLD"); self.assertIn("invoice date", self.stage(r, "fields")["detail"])


if __name__ == "__main__":
    unittest.main()
