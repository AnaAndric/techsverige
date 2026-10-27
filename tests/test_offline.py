"""Offline-test: en påhittad företagssajt serveras lokalt och skrapas.
Kör:  python tests/test_offline.py
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))
import scrape  # noqa: E402
import datetime
scrape.TODAY = datetime.date(2026, 10, 12)  # fast datum så att testet inte åldras

HOME = """<html><body><nav><a href="/tjanster">Våra tjänster</a>
<a href="/karriar">Karriär</a><a href="https://linkedin.com/company/x">LinkedIn</a></nav></body></html>"""

CAREER = """<html><head><script src="https://assets.teamtailor-cdn.com/x.js"></script>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"JobPosting",
 "title":"Trainee – Cloud & Infrastructure 2027","datePosted":"2026-09-20","validThrough":"2026-11-15T23:59",
 "url":"/jobs/1-trainee","jobLocation":{"@type":"Place","address":{"addressLocality":"Göteborg"}}},
 {"@type":"JobPosting","title":"Senior Backend Engineer","datePosted":"2026-09-01"}]}</script></head>
<body><h2>Lediga tjänster</h2>
<a href="/jobs/2-exjobb">Examensarbete: AI för nätplanering</a>
<a href="/jobs/3-junior">Junior Frontend Developer   Stockholm</a>
<a href="/students">Students</a>
<a href="/jobs/4-old">Sommarjobb 2025 – support</a>
<a href="/jobs/5-pm">Product Manager</a>
<a href="https://jobs.example-ats.com/acme/99">Trainee på extern jobbsajt</a>
<a href="/jobs/9">OneExample Augusta Spring 2027 Intern - May 2027 Grads</a></body></html>"""

JOB_EXJOBB = """<html><body><h1>Examensarbete: AI för nätplanering</h1>
<p>Plats: Linköping</p><p>Sista ansökningsdag: 30 november 2026</p></body></html>"""

JOB_JUNIOR = """<html><head><script type="application/ld+json">{"@type":"JobPosting","title":"Junior Frontend Developer",
"datePosted":"2026-10-01","validThrough":"2026-10-31","jobLocation":[{"address":{"addressLocality":"Stockholm"}}],
"jobLocationType":"TELECOMMUTE"}</script></head><body></body></html>"""

JOB_US = """<html lang="en"><head><title>Spring 2027 Intern, Augusta, Georgia, United States | Careers</title></head>
<body><b>279781</b><p>Date: 08/31/2026</p><p>Intern/Co-Op</p><h1>OneExample Augusta Spring 2027 Intern - May 2027 Grads</h1>
<p>Augusta, Georgia, United States</p><a href="#">Apply Now</a></body></html>"""

JOB_OLD = """<html><body><p>Ansök senast 2025-05-01</p></body></html>"""

ROUTES = {"/": HOME, "/karriar": CAREER, "/jobs/2-exjobb": JOB_EXJOBB, "/jobs/3-junior": JOB_JUNIOR,
          "/jobs/4-old": JOB_OLD, "/jobs/9": JOB_US,
          "/jobs/8": "<html lang='sv'><body><h1>Sommarpraktik 2027</h1><p>Ort: Stockholm</p></body></html>", "/robots.txt": "User-agent: *\nDisallow: /privat\n"}


REQUESTS: list[str] = []


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = ROUTES.get(self.path.rstrip("/") or "/")
        if body is None:
            self.send_response(404); self.end_headers(); return
        body = body.replace("HOST", self.headers["Host"]).encode()
        import hashlib
        etag = '"' + hashlib.md5(body).hexdigest() + '"'
        REQUESTS.append(self.path)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304); self.send_header("ETag", etag); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("ETag", etag)
        self.end_headers(); self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_dates_and_classify():
    assert scrape.norm_date("31 oktober 2026") == "2026-10-31"
    assert scrape.norm_date("October 3, 2026") == "2026-10-03"
    assert scrape.norm_date("31.10.2026") == "2026-10-31"
    assert scrape.norm_date(1790000000000) is not None
    assert scrape.classify("Summer Intern – Data") == "Praktik/Internship"
    assert scrape.classify("Internal Sales Manager") is None
    assert scrape.classify("International Account Manager") is None
    assert scrape.classify("Master Thesis: Edge AI") == "Examensarbete"
    assert scrape.classify("Graduate Programme 2027") == "Trainee"
    assert scrape.classify("Praktisk IT-tekniker") is None
    assert scrape.classify("Anything", ["INTERN"]) == "Praktik/Internship"
    assert scrape.norm_date("Date: 08/31/2026") == "2026-08-31"      # amerikanskt format
    assert scrape.norm_date("05/10/2026") == "2026-10-05"            # svenskt/europeiskt
    assert scrape.sweden_verdict("Augusta, Georgia, United States") is False
    assert scrape.sweden_verdict("Kista") is True
    assert scrape.sweden_verdict("Stockholm, Oslo") is True
    assert scrape.sweden_verdict("Göteborg", "SE") is True
    assert scrape.sweden_verdict("Bengaluru") is False
    assert scrape.sweden_verdict("Hybrid") is None
    page = scrape.parse_page(JOB_US)
    assert scrape.guess_location(page) == "Augusta, Georgia, United States"


def test_site():
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"127.0.0.1:{srv.server_port}"
    http = scrape.Http(per_host_delay=0)
    s = scrape.CompanyScraper(http, {"name": "Exempelbolaget AB", "domain": f"http://{host}", "orgnr": ""})
    jobs = scrape.finalize(s.run(), previous=[])
    srv.shutdown()
    for j in jobs:
        print(f"  {j['type']:<20} {j['title']:<42} {j['location']:<20} pub={j['published']} sista={j['deadline']} [{j['source']}]")
    titles = {j["title"]: j for j in jobs}
    assert "Trainee – Cloud & Infrastructure 2027" in titles
    assert titles["Trainee – Cloud & Infrastructure 2027"]["location"] == "Göteborg"
    assert titles["Examensarbete: AI för nätplanering"]["deadline"] == "2026-11-30"
    assert titles["Junior Frontend Developer"]["location"] == "Stockholm, Distans"
    assert "Trainee på extern jobbsajt" not in titles, "bara medlemmens egen webbplats"
    assert not any("Augusta" in t for t in titles), "tjänster utanför Sverige ska bort"
    assert any("utanför Sverige" in n for n in s.notes), s.notes
    assert all(j["source"] == "Webbplats" for j in jobs)
    assert titles["Examensarbete: AI för nätplanering"]["location"] == "Linköping"
    assert not any("Senior" in t or "Head" in t or "Product" in t or t == "Students" for t in titles)
    assert not any("2025" in t for t in titles), "utgången annons ska bort"


def test_register(tmp=None):
    """Körning 1 hittar och sparar karriärsidan, körning 2 använder den direkt.
    En länk som lagts in för hand (även i Excel-kodning) skrivs aldrig över."""
    import tempfile
    tmp = Path(tmp or tempfile.mkdtemp())
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    site = f"http://127.0.0.1:{srv.server_port}"
    (tmp / "m.csv").write_text(f"name;domain;orgnr\nExempelbolaget AB;{site};\nÅkerö Data AB;;\n", "utf-8")
    args = ["--members-file", str(tmp / "m.csv"), "--out", str(tmp / "out"), "--data-dir", str(tmp / "data"),
            "--no-ai"]
    assert scrape.main(args) == 0
    reg = scrape.load_register(tmp / "data" / "karriarsidor.csv")
    row = reg[scrape._reg_key("Exempelbolaget AB")]
    assert row["Karriärsida"].endswith("/karriar") and row["Källa"] == "Automatiskt", row
    assert "Webbplats saknas" in reg[scrape._reg_key("Åkerö Data AB")]["Status"]
    assert (tmp / "out" / "karriarsidor.csv").exists()

    # Någon har öppnat filen i Excel, lagt in en egen länk och sparat (cp1252)
    text = (tmp / "data" / "karriarsidor.csv").read_text("utf-8-sig").replace(
        f"{site}/karriar;Automatiskt", f"{site}/karriar;")
    (tmp / "data" / "karriarsidor.csv").write_bytes(text.encode("cp1252"))
    assert scrape.main(args) == 0
    reg = scrape.load_register(tmp / "data" / "karriarsidor.csv")
    row = reg[scrape._reg_key("Exempelbolaget AB")]
    assert row["Källa"] == "Manuellt" and row["Status"] == "OK", row
    assert row["Juniora annonser"] == "3", row

    # Körning 3: inget har ändrats -> bara karriärsidan kollas (304), annonserna återanvänds
    REQUESTS.clear()
    assert scrape.main(args) == 0
    assert [r for r in REQUESTS if r != "/robots.txt"] == ["/karriar"], REQUESTS
    reg = scrape.load_register(tmp / "data" / "karriarsidor.csv")
    row = reg[scrape._reg_key("Exempelbolaget AB")]
    assert row["Ändrad vid senaste körning"] == "Nej" and row["Juniora annonser"] == "3", row
    jobs = json.loads((tmp / "out" / "jobs.json").read_text("utf-8"))["jobs"]
    assert len(jobs) == 3

    # Körning 4: en ny annons på karriärsidan -> upptäcks och skrapas
    ROUTES["/karriar"] = CAREER.replace("</body>", '<a href="/jobs/8">Sommarpraktik 2027</a></body>')
    REQUESTS.clear()
    assert scrape.main(args) == 0
    reg = scrape.load_register(tmp / "data" / "karriarsidor.csv")
    row = reg[scrape._reg_key("Exempelbolaget AB")]
    assert row["Ändrad vid senaste körning"] == "Ja" and row["Juniora annonser"] == "4", row
    ROUTES["/karriar"] = CAREER
    srv.shutdown()

if __name__ == "__main__":
    test_dates_and_classify()
    test_site()
    test_register()
    print("OK – alla tester gick igenom")
