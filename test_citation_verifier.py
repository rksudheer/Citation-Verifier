"""Tests for citation_verifier_app.py. No network needed: lookups are replaced with fake data.

Run:  python -m unittest -v
"""
import http.client
import io
import json
import threading
import unittest
import zipfile
from unittest import mock

import citation_verifier_app as cv


def record(title, first, year, authors, years=None):
    return {"title": title, "first_author": first, "year": year, "years": years or [year], "authors": authors}


class TestCompare(unittest.TestCase):
    TITLE = "Laser and scanning electron microscopy evaluation of residual deposits on root surfaces"
    REC = record(TITLE, "Alder", 2024, ["Alder", "Brook", "Clay", "Dunn"], years=[2024, 2025])

    def test_exact_reference_matches_without_notes(self):
        ref = "Alder A, Brook B, Clay C, Dunn D. %s. J Test. 2025;96:1-10." % self.TITLE
        status, score, notes = cv.compare(ref, self.REC)
        self.assertEqual(status, "MATCH")
        self.assertEqual(notes, [])

    def test_abbreviation_is_flagged_for_review_with_the_differing_words(self):
        ref = "Alder A, Brook B, Clay C, Dunn D. Laser and SEM evaluation of residual deposits on root surfaces. J Test. 2025."
        status, score, notes = cv.compare(ref, self.REC)
        self.assertEqual(status, "REVIEW")
        self.assertTrue(any("scanning electron microscopy" in n and "sem" in n for n in notes))

    def test_one_word_swap_in_a_long_title_still_matches_but_is_noted(self):
        ref = "Alder A, Brook B, Clay C, Dunn D. %s. J Test. 2025." % self.TITLE.replace("residual", "absent")
        status, score, notes = cv.compare(ref, self.REC)
        self.assertEqual(status, "MATCH")
        self.assertTrue(any("residual" in n and "absent" in n for n in notes))

    def test_author_order_difference_is_noted(self):
        ref = "Alder A, Brook B, Dunn D, Clay C. %s. J Test. 2025." % self.TITLE
        notes = cv.compare(ref, self.REC)[2]
        self.assertTrue(any(n.startswith("author order differs") for n in notes))

    def test_ampersand_for_and_is_not_a_difference(self):
        rec = record("Role of inflammasomes in disease and therapeutics", "Alder", 2020, ["Alder"])
        ref = "Alder A. Role of inflammasomes in disease & therapeutics. J Test. 2020."
        self.assertEqual(cv.compare(ref, rec)[2], [])

    def test_any_known_year_is_accepted(self):
        ref = "Alder A. %s. J Test. 2025." % self.TITLE
        self.assertEqual(cv.compare(ref, self.REC)[0], "MATCH")
        self.assertEqual(cv.compare(ref.replace("2025", "2019"), self.REC)[0], "REVIEW")


def fake_get(url, email=""):
    def cr(title, family, year, doi):
        return {"message": {"title": [title], "author": [{"family": family}], "issued": {"date-parts": [[year]]},
                            "container-title": ["J Test"], "DOI": doi}}
    if "api.crossref.org/works/10.1000/real" in url:
        return cr("Biofilm effects on inflammasome activation in gingival cells", "Alder", 2021, "10.1000/real")
    if "api.crossref.org/works/10.1000/other" in url:
        return cr("Soil fungi in tropical forests", "Smith", 2015, "10.1000/other")
    if "api.crossref.org/works/10.1000/retr" in url:
        return cr("Plaque bacteria and a withdrawn claim", "Lee", 2019, "10.1000/retr")
    if "api.openalex.org/works/doi:10.1000/retr" in url:
        return {"is_retracted": True}
    if "api.openalex.org/works/doi:" in url:
        return {"is_retracted": False}
    if "api.crossref.org/works?query" in url:
        return {"message": {"items": []}}
    if "api.openalex.org/works?search" in url:
        return {"results": []}
    return None  # unknown DOI / handle


class TestCheck(unittest.TestCase):
    def run_check(self, ref):
        with mock.patch.object(cv, "get", fake_get):
            return cv.check(ref, "")

    def test_correct_reference(self):
        r = self.run_check("Alder A. Biofilm effects on inflammasome activation in gingival cells. J Test. 2021. doi:10.1000/real")
        self.assertEqual(r["status"], "MATCH")

    def test_real_doi_with_invented_title(self):
        r = self.run_check("Alder A. A completely invented title about caries vaccines. J Test. 2021. doi:10.1000/other")
        self.assertEqual(r["status"], "MISMATCH")

    def test_doi_that_does_not_exist(self):
        r = self.run_check("Lee J. Something invented. J Test. 2020. doi:10.1000/nope")
        self.assertEqual(r["status"], "NOT FOUND")
        self.assertTrue(any("does not exist" in n for n in r["notes"]))

    def test_retraction_is_reported(self):
        r = self.run_check("Lee J. Plaque bacteria and a withdrawn claim. J Test. 2019. doi:10.1000/retr")
        self.assertTrue(r["retracted"])


def make_docx(paragraphs, extra=""):
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join('<w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p>' % t for t in paragraphs)
    xml = '<?xml version="1.0"?>%s<w:document %s><w:body>%s</w:body></w:document>' % (extra, ns, body)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


class TestDocx(unittest.TestCase):
    def test_references_are_taken_from_after_the_last_heading(self):
        data = make_docx(["Title", "References", "Body text.", "References",
                          "Alder A. A reference long enough to count. J Test. 2020.", "Acknowledgements",
                          "Thanks to everyone who helped with this work."])
        refs, how = cv.extract_refs(cv.docx_paragraphs(data))
        self.assertEqual(len(refs), 1)
        self.assertIn("Alder", refs[0])

    def test_fallback_when_there_is_no_heading(self):
        data = make_docx(["Text.", "Alder A. Paper. doi:10.1000/abc123", "More text."])
        refs, how = cv.extract_refs(cv.docx_paragraphs(data))
        self.assertEqual(len(refs), 1)

    def test_files_with_entity_declarations_are_refused(self):
        data = make_docx(["x"], extra='<!DOCTYPE d [<!ENTITY a "aaaa">]>')
        with self.assertRaises(ValueError):
            cv.docx_paragraphs(data)

    def test_non_docx_is_refused(self):
        with self.assertRaises(Exception):
            cv.docx_paragraphs(b"not a docx")


class TestServer(unittest.TestCase):
    def setUp(self):
        self.srv = cv.ThreadingHTTPServer(("127.0.0.1", 0), cv.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def request(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def test_page_loads_for_local_requests(self):
        status, data = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Citation Verifier", data)

    def test_foreign_host_header_is_refused(self):
        self.assertEqual(self.request("GET", "/", {"Host": "evil.example"})[0], 403)

    def test_foreign_origin_is_refused(self):
        status, _ = self.request("POST", "/api/check", {"Origin": "http://evil.example"}, b'{"refs": []}')
        self.assertEqual(status, 403)

    def test_unreadable_docx_gives_an_error_message(self):
        status, data = self.request("POST", "/api/docx", {}, b"nope")
        self.assertEqual(status, 200)
        self.assertIn("error", json.loads(data))


if __name__ == "__main__":
    unittest.main()
