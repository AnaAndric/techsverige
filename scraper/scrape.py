#!/usr/bin/env python3
"""
TechSverige – juniora tjänster hos medlemsföretagen
====================================================

Hämtar internships, traineeprogram, examensarbeten, sommarjobb och andra
juniora tjänster som TechSveriges medlemsföretag annonserar, och skriver en
tabell som webbappen i ../docs visar.

Flöde
-----
1. Medlemslistan hämtas från techsverige.se/om-oss-2/vara-medlemmar/
   (sidan renderas med JavaScript och visar en bokstav i taget, därför
   Playwright). Kan ersättas med en egen fil:  --members-file medlemmar.csv
2. För varje medlem med webbplats:
   - karriärsidan tas från registret (data/karriarsidor.csv) om den är
     bekräftad, annars letas den upp: länkar på startsidan, sitemap.xml och
     vanliga adresser. Claude Haiku väljer bland kandidaterna (se ai.py).
   - karriärsidan läses: strukturerad data (schema.org/JobPosting), flöden från
     rekryteringssystem (Teamtailor, Lever, Workday … se ats.py) och länkar.
     Haiku pekar ut vilka länkar som är annonser och sållar på titeln.
   - varje möjlig junior annons läses och Haiku avgör om den är junior, vilken
     typ den är, ort/land och sista ansökningsdag.
   Sidor som bara visar innehåll med JavaScript öppnas i en headless webbläsare.
   Endast medlemmens egen webbplats och rekryteringssystem som den länkar till
   används – inga jobbsajter som Platsbanken eller LinkedIn.
3. Resultatet skrivs till docs/data.js (läses av appen) och docs/jobs.json.
   Annonser vars sista ansökningsdag passerat tas bort.
   "Ny" = annonsen fanns inte vid förra körningen.

Kör:  python scraper/scrape.py              (allt)
      python scraper/scrape.py --limit 25   (snabbtest på 25 medlemmar)
      python scraper/scrape.py --no-ai      (bara regler, utan Claude)

AI kräver miljövariabeln ANTHROPIC_API_KEY. Utan den körs regelversionen.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import gzip
import hashlib
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import unicodedata
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib import robotparser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse, urlunparse, parse_qsl, quote
from urllib.request import Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ats  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MEMBERS_URL = "https://techsverige.se/om-oss-2/vara-medlemmar/"
USER_AGENT = ("Mozilla/5.0 (compatible; TechSverige-JuniorJobs/1.1; "
              "+https://techsverige.se)")
TODAY = dt.date.today()
RECONFIRM_DAYS = 90   # automatiskt hittade karriärsidor bekräftas på nytt efter så här många dagar

log = logging.getLogger("scrape")

# ---------------------------------------------------------------------------
# Klassning – vilken typ av junior tjänst är det? (används utan AI och som
# snabb förhandskoll). Ordningen spelar roll: första träffen vinner.
# ---------------------------------------------------------------------------
TYPE_RULES: list[tuple[str, re.Pattern]] = [
    ("Examensarbete", re.compile(
        r"\b(examensarbete\w*|exjobb\w*|ex-jobb\w*|(master|bachelor)'?s?\s+thesis|"
        r"thesis(\s+work|\s+project)?|degree\s+project|kandidatarbete)\b", re.I)),
    ("Trainee", re.compile(
        r"\b(trainee\w*|graduate\s+(program+e?|scheme|trainee)|graduateprogram\w*)\b", re.I)),
    ("Praktik/Internship", re.compile(
        # "intern" på svenska betyder oftast "intern länk", "intern kommunikatör" osv.
        r"\b(intern(?!\s+(länk|kommunik\w*|revis\w*|service|support|it|rekrytering|säljare|sälj))|"
        r"interns|internship\w*|praktik|praktikant\w*|praktikplats\w*|"
        r"sommarpraktik\w*|lia|lia-praktik|working\s+student|werkstudent\w*|co-?op)\b", re.I)),
    ("Sommarjobb", re.compile(
        r"\b(sommarjobb\w*|sommarvikari\w*|feriejobb\w*|summer\s+(job|jobs|worker|workers|student|students))\b",
        re.I)),
    ("Junior", re.compile(
        r"\b(junior|jr\.?|entry[\s-]level|nyexaminerad\w*|nyutexaminerad\w*|new\s+grads?|"
        r"(recent\s+)?graduates?|early\s+careers?|first\s+job|förstajobb)\b", re.I)),
    ("Studentjobb", re.compile(
        r"\b(studentjobb\w*|studentmedarbetare|studentassistent\w*|studentambassad\w*|"
        r"student\s+(assistant|worker|ambassador|job|position)s?|extrajobb)\b", re.I)),
]

# Navigeringslänkar ("Students", "Graduates") är inga annonser
GENERIC_NAV_RE = re.compile(
    r"^(our |våra |for |för )?(graduates?|students?|studenter|internships?|interns|trainees?|"
    r"praktik|examensarbeten?|exjobb|thesis|early careers?|junior|student(er)? och (nyexade|graduates))$", re.I)

NOT_JOB_RE = re.compile(  # länktexter som matchar men inte är annonser
    r"\b(läs mer om|read more about|our (graduates|interns) say|blogg?|blog|story|stories|"
    r"case|webinar|event|intern länk|från praktik till)\b", re.I)

# Sökvägar som nästan aldrig är karriärsidor, även om ett nyckelord råkar finnas med
NOT_CAREER_PATH_RE = re.compile(
    r"/(nyheter|news|blogg?|blog|press|pressmeddelanden?|kundcase|kundcase|case|cases|customer-stories|"
    r"webinar|webinars|event|events|evenemang|artiklar|articles?|insights|stories|kunskapsbank|"
    r"investerare|investors?|tag|category|kategori)(/|$)", re.I)


def classify(title: str, employment_type: str | list | None = None) -> str | None:
    text = " ".join((title or "").split())
    for label, rx in TYPE_RULES:
        if rx.search(text):
            return label
    et = " ".join(employment_type) if isinstance(employment_type, list) else (employment_type or "")
    if "INTERN" in et.upper():
        return "Praktik/Internship"
    return None


# ---------------------------------------------------------------------------
# HTTP (endast standardbiblioteket) med artig takt per värd + robots.txt
# ---------------------------------------------------------------------------
def to_uri(url: str) -> str:
    """https://exempel.se/karriär -> https://exempel.se/karri%C3%A4r (urllib klarar bara ASCII)."""
    try:
        url.encode("ascii")
        return url
    except UnicodeEncodeError:
        pass
    p = urlparse(url)
    host = p.netloc.encode("idna").decode("ascii") if p.netloc else ""
    safe = "/%:@!$&'()*+,;=-._~"
    return urlunparse((p.scheme, host, quote(p.path, safe=safe), p.params,
                       quote(p.query, safe=safe + "?"), quote(p.fragment, safe=safe)))


class Http:
    def __init__(self, per_host_delay: float = 0.6, timeout: float = 15.0):
        self.opener = build_opener()
        self.timeout = timeout
        self.per_host_delay = per_host_delay
        self._last: dict[str, float] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self.robots_txt: dict[str, str] = {}
        self._lock = threading.Lock()
        self._tl = threading.local()   # svarshuvuden för senaste anropet i tråden

    def _throttle(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._last.get(host, 0) + self.per_host_delay - now
            self._last[host] = max(now, self._last.get(host, 0) + self.per_host_delay)
        if wait > 0:
            time.sleep(wait)

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        with self._lock:
            known = origin in self._robots
            rp = self._robots.get(origin)
        if not known:
            rp = robotparser.RobotFileParser()
            body = ""
            try:
                _, status, body = self._fetch(origin + "/robots.txt", max_bytes=500_000)
                body = body if status == 200 else ""
                rp.parse(body.splitlines())
            except Exception:
                rp = None  # robots.txt saknas/går ej att läsa -> tillåt
            with self._lock:
                self._robots[origin] = rp
                self.robots_txt[origin] = body or ""
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    def _fetch(self, url, data=None, headers=None, max_bytes=3_000_000):
        h = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate",
             "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
             "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"}
        h.update(headers or {})
        url = to_uri(url)
        self._throttle(urlparse(url).netloc)
        req = Request(url, data=data, headers=h)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                raw = r.read(max_bytes)
                enc = (r.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip":
                    raw = gzip.decompress(raw)
                elif enc == "deflate":
                    raw = zlib.decompress(raw)
                charset = r.headers.get_content_charset() or "utf-8"
                self._tl.headers = r.headers
                try:
                    text = raw.decode(charset, errors="replace")
                except LookupError:
                    text = raw.decode("utf-8", errors="replace")
                return r.geturl(), r.status, text
        except HTTPError as e:
            self._tl.headers = e.headers
            return url, e.code, ""

    def get(self, url: str, check_robots: bool = True, **kw) -> tuple[str, int, str]:
        if check_robots and not self.allowed(url):
            log.debug("robots.txt stoppar %s", url)
            return url, 999, ""
        return self._fetch(url, **kw)

    def get_conditional(self, url: str, etag: str = "", last_modified: str = ""):
        """GET med If-None-Match/If-Modified-Since. Status 304 = oförändrad sedan sist."""
        h = {}
        if etag:
            h["If-None-Match"] = etag
        if last_modified:
            h["If-Modified-Since"] = last_modified
        self._tl.headers = None
        final, status, body = self.get(url, headers=h)
        hd = getattr(self._tl, "headers", None)
        return (final, status, body,
                (hd.get("ETag") or "") if hd else "", (hd.get("Last-Modified") or "") if hd else "")

    def get_json(self, url: str, data: dict | None = None, **kw):
        body = json.dumps(data).encode() if data is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            _, status, text = self._fetch(url, data=body, headers=headers, **kw)
        except (URLError, TimeoutError, OSError, ValueError):
            return None
        if status != 200 or not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None


# ---------------------------------------------------------------------------
# Headless webbläsare för sidor som bara visar innehåll med JavaScript.
# Playwright är inte trådsäkert – varje webbläsare lever i en egen tråd och
# tar emot uppdrag via en kö.
# ---------------------------------------------------------------------------
class Renderer:
    def __init__(self, workers: int = 2):
        self.q: queue.Queue = queue.Queue()
        self.ok = True
        self.count = 0
        self.threads = [threading.Thread(target=self._loop, daemon=True) for _ in range(workers)]
        for t in self.threads:
            t.start()

    def _loop(self):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = launch_browser(pw)
                ctx = browser.new_context(user_agent=USER_AGENT, locale="sv-SE")
                while True:
                    item = self.q.get()
                    if item is None:
                        break
                    url, fut = item
                    page = ctx.new_page()
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=25_000)
                        try:
                            page.wait_for_load_state("networkidle", timeout=7_000)
                        except Exception:
                            pass
                        if page.locator("a[href]").count() < 15:
                            dismiss_consent(page)   # vissa sidor visar inget innehåll före samtycke
                            try:   # vänta tills innehållet dykt upp
                                page.wait_for_function("document.querySelectorAll('a[href]').length >= 15",
                                                       timeout=5_000)
                            except Exception:
                                pass
                        frames = [f.url for f in page.frames[1:] if f.url.startswith("http")]
                        fut.set_result((page.url, page.content(), frames))
                        self.count += 1
                    except Exception as e:
                        log.debug("rendering misslyckades %s: %s", url, str(e).splitlines()[0][:120])
                        fut.set_result((url, "", []))
                    finally:
                        page.close()
                browser.close()
        except Exception as e:
            log.warning("Webbläsaren gick inte att starta (%s) – sidor med JavaScript läses utan den", e)
            self.ok = False
            while True:  # svara tomt på allt som ligger i kön
                item = self.q.get()
                if item is None:
                    break
                item[1].set_result((item[0], "", []))

    def render(self, url: str) -> tuple[str, str, list[str]]:
        if not self.ok:
            return url, "", []
        fut: cf.Future = cf.Future()
        self.q.put((url, fut))
        try:
            return fut.result(timeout=90)
        except cf.TimeoutError:
            return url, "", []

    def close(self):
        for _ in self.threads:
            self.q.put(None)


# ---------------------------------------------------------------------------
# HTML-parsning
# ---------------------------------------------------------------------------
class PageParser(HTMLParser):
    """Samlar länkar (href + text), JSON-LD-block, iframes och synlig text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.jsonld: list[str] = []
        self.feeds: list[str] = []
        self.iframes: list[str] = []
        self.text_parts: list[str] = []
        self.title = ""
        self.lang = ""
        self.microdata: list[str] = []   # itemprop addressLocality/addressCountry …
        self._itemprop: str | None = None
        self._a_href: str | None = None
        self._a_label = ""
        self._a_text: list[str] = []
        self._in_jsonld = False
        self._skip = 0
        self._in_title = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html" and a.get("lang"):
            self.lang = a["lang"].lower()
        ip = (a.get("itemprop") or "").lower()
        if ip in ("addresslocality", "addressregion", "addresscountry", "joblocation"):
            if a.get("content"):
                self.microdata.append(a["content"])
            else:
                self._itemprop = ip
        if tag == "a" and a.get("href"):
            # aria-label/title används bara om länken saknar egen text
            self._a_href, self._a_text = a["href"], []
            self._a_label = a.get("aria-label") or a.get("title") or ""
        elif tag == "iframe" and a.get("src"):
            self.iframes.append(a["src"])
        elif tag == "script":
            if (a.get("type") or "").lower() == "application/ld+json":
                self._in_jsonld, self._buf = True, []
            else:
                self._skip += 1
        elif tag in ("style", "noscript", "svg"):
            self._skip += 1
        elif tag == "link" and "alternate" in (a.get("rel") or "") and "rss" in (a.get("type") or ""):
            if a.get("href"):
                self.feeds.append(a["href"])
        elif tag == "title":
            self._in_title = True
        elif tag in ("br", "p", "div", "li", "h1", "h2", "h3", "h4", "tr", "dd", "dt"):
            self.text_parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "dd", "dt",
                   "section", "header", "footer", "button", "title"):
            self.text_parts.append("\n")
        if tag == "a" and self._a_href is not None:
            text = " ".join(" ".join(self._a_text).split()) or " ".join(self._a_label.split())
            self.links.append((self._a_href, text))
            self._a_href = None
        elif tag == "script":
            if self._in_jsonld:
                self.jsonld.append("".join(self._buf))
                self._in_jsonld = False
            elif self._skip:
                self._skip -= 1
        elif tag in ("style", "noscript", "svg") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._itemprop and data.strip():
            self.microdata.append(data.strip())
            self._itemprop = None
        if self._in_jsonld:
            self._buf.append(data)
            return
        if self._skip:
            return
        if self._in_title:
            self.title += data
        if self._a_href is not None:
            self._a_text.append(data)
        self.text_parts.append(data)

    @property
    def text(self) -> str:
        t = re.sub(r"[ \t\r\f\v]+", " ", "".join(self.text_parts))
        return re.sub(r"\n\s*\n+", "\n", t)


def parse_page(html_text: str) -> PageParser:
    p = PageParser()
    try:
        p.feed(html_text)
        p.close()
    except Exception:  # trasig HTML ska inte stoppa körningen
        pass
    return p


def thin(page: PageParser) -> bool:
    """Ser sidan ut att behöva JavaScript för att visa sitt innehåll?"""
    return len(page.links) < 15 or len(page.text.strip()) < 800


# ---------------------------------------------------------------------------
# Datum
# ---------------------------------------------------------------------------
MONTHS = {
    "jan": 1, "januari": 1, "january": 1, "feb": 2, "februari": 2, "february": 2,
    "mar": 3, "mars": 3, "march": 3, "apr": 4, "april": 4, "maj": 5, "may": 5,
    "jun": 6, "juni": 6, "june": 6, "jul": 7, "juli": 7, "july": 7,
    "aug": 8, "augusti": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "okt": 10, "oct": 10, "oktober": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_MONTH_RX = "|".join(sorted(MONTHS, key=len, reverse=True))


def _mk(y, m, d) -> str | None:
    try:
        return dt.date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def norm_date(value) -> str | None:
    """Gör om nästan vad som helst till YYYY-MM-DD."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):  # epoch (s eller ms)
        v = value / 1000 if value > 1e11 else value
        return dt.datetime.fromtimestamp(v, dt.timezone.utc).date().isoformat()
    s = str(value).strip()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return _mk(*m.groups())
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b", s)  # 31/10/2026, 31.10.2026, 08/31/2026
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), m.group(3)
        if b > 12 and a <= 12:            # amerikanskt format MM/DD/YYYY
            return _mk(y, a, b)
        return _mk(y, b, a)
    m = re.search(rf"\b(\d{{1,2}})(?:e|a|st|nd|rd|th)?\.?\s+({_MONTH_RX})\.?,?\s+(\d{{4}})\b", s, re.I)
    if m:
        return _mk(m.group(3), MONTHS[m.group(2).lower()], m.group(1))
    m = re.search(rf"\b({_MONTH_RX})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", s, re.I)
    if m:
        return _mk(m.group(3), MONTHS[m.group(1).lower()], m.group(2))
    low = s.lower()  # Workday: "Posted Today", "Posted 3 Days Ago", "Posted 30+ Days Ago"
    if "today" in low or "idag" in low:
        return TODAY.isoformat()
    if "yesterday" in low or "igår" in low:
        return (TODAY - dt.timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s+(days?|dagar)\s+(ago|sedan)", low)
    if m:
        return (TODAY - dt.timedelta(days=int(m.group(1)))).isoformat()
    try:
        return parsedate_to_datetime(s).date().isoformat()  # RFC 822 (RSS)
    except (TypeError, ValueError, IndexError):
        return None


DEADLINE_RX = re.compile(
    r"(sista\s+ansökningsdag(en)?|sista\s+dag\s+att\s+ansöka|ansök\s+senast|ansökan\s+senast|"
    r"sök\s+senast|application\s+deadline|apply\s+(by|before)|deadline|last\s+day\s+to\s+apply|"
    r"closing\s+date)\s*(är|is|:)?\s*[:\-–]?\s*(?P<d>[^\n]{4,40})", re.I)


LOCATION_RX = re.compile(
    r"(?:^|\n)\s*(?:plats|ort|arbetsort|placering|location|placeringsort|office)\s*:\s*"
    r"(?P<loc>[A-ZÅÄÖ][\wÅÄÖåäöéü .,/&-]{1,50})", re.I)


def deadline_from_text(text: str) -> str | None:
    for m in DEADLINE_RX.finditer(text):
        d = norm_date(m.group("d"))
        if d:
            return d
    return None


# ---------------------------------------------------------------------------
# schema.org/JobPosting (JSON-LD)
# ---------------------------------------------------------------------------
def _walk_jsonld(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from _walk_jsonld(x)
    elif isinstance(obj, dict):
        t = obj.get("@type")
        types = t if isinstance(t, list) else [t]
        if "JobPosting" in types:
            yield obj
        for k in ("@graph", "itemListElement", "item", "mainEntity"):
            if k in obj:
                yield from _walk_jsonld(obj[k])


def _country_of(job: dict) -> str:
    locs = job.get("jobLocation") or []
    for loc in (locs if isinstance(locs, list) else [locs]):
        addr = (loc or {}).get("address") if isinstance(loc, dict) else None
        if isinstance(addr, dict) and addr.get("addressCountry"):
            c = addr["addressCountry"]
            return str(c.get("name", "") if isinstance(c, dict) else c).strip()
    return ""


def _location_str(job: dict) -> str:
    parts: list[str] = []
    locs = job.get("jobLocation") or []
    for loc in (locs if isinstance(locs, list) else [locs]):
        if not isinstance(loc, dict):
            continue
        addr = loc.get("address") or {}
        if isinstance(addr, str):
            parts.append(addr)
        elif isinstance(addr, dict):
            city = addr.get("addressLocality") or addr.get("addressRegion") or ""
            if city:
                parts.append(city)
            elif addr.get("addressCountry"):
                c = addr["addressCountry"]
                parts.append(c.get("name", "") if isinstance(c, dict) else str(c))
    if str(job.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        parts.append("Distans")
    seen, out = set(), []
    for p in (x.strip() for x in parts):
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return ", ".join(out)


def jobpostings_from_jsonld(blocks: list[str], page_url: str) -> list[dict]:
    out = []
    for raw in blocks:
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            try:  # vissa sidor har kontrolltecken/radbrytningar i strängar
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", raw.strip()))
            except json.JSONDecodeError:
                continue
        for job in _walk_jsonld(data):
            title = unescape(str(job.get("title") or job.get("name") or "")).strip()
            if not title:
                continue
            desc = re.sub(r"<[^>]+>", " ", unescape(str(job.get("description") or "")))
            out.append({
                "title": title,
                "url": urljoin(page_url, job.get("url") or page_url),
                "published": norm_date(job.get("datePosted")),
                "deadline": norm_date(job.get("validThrough")),
                "location": _location_str(job),
                "country": _country_of(job),
                "employment_type": job.get("employmentType"),
                "description": " ".join(desc.split())[:6000],
            })
    return out


# ---------------------------------------------------------------------------
# Karriärsidor
# ---------------------------------------------------------------------------
CAREER_RE = re.compile(
    r"(karri[aä]r|career|/jobs?\b|\bjobs?\b|jobb|lediga|jobba|join[-_ ]?(us|the[-_ ]?team|our[-_ ]?team)|"
    r"work[-_ ]?(with|at|for)[-_ ]?us|arbeta[-_ ]?(hos|med|p[aå])|vacanc|open[-_ ]?(positions|roles)|"
    r"\bstudents?\b|\bgraduates?\b|trainee|\bpraktik\b|internship|early[-_ ]?careers?|"
    r"examensarbete|exjobb|thesis)", re.I)
SKIP_HOSTS = ("linkedin.", "facebook.", "twitter.", "x.com", "instagram.", "youtube.",
              "tiktok.", "glassdoor.", "google.", "apple.com", "microsoft.com/store",
              "arbetsformedlingen.", "indeed.", "monster.")
SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|zip|docx?|xlsx?|pptx?|mp4|mp3|ics)(\?|$)", re.I)
GUESS_PATHS = ["/karriar", "/karriär", "/careers", "/career", "/jobs", "/jobb",
               "/lediga-tjanster", "/en/careers", "/sv/karriar", "/om-oss/karriar", "/jobba-hos-oss"]
ABOUT_RE = re.compile(r"(^|/|\s)(om[-_ ]?oss|about[-_ ]?us|about|vi|företaget|company|who[-_ ]?we[-_ ]?are)(/|$|\s)", re.I)
CAREER_PATH_RE = re.compile(  # strängare variant för URL:er i sitemap.xml
    r"(karri[aä]r|career|/jobs?(/|$|-)|/jobb|lediga|jobba|join-?us|work-?(with|at|for)-?us|"
    r"arbeta-?(hos|med|pa)|vacanc|open-?positions|trainee|/praktik|internship|student|graduate|"
    r"examensarbete|exjobb|thesis)", re.I)


CAREER_HOST_RE = re.compile(r"^(careers?|karri[aä]r|jobs?|jobb|jobba|join|rekrytering|work)\.", re.I)


def clean_url(u: str) -> str:
    p = urlparse(u)
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "source", "ref"))]
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", urlencode(q), ""))


def host_of(u: str) -> str:
    h = urlparse(u).netloc.lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def site_of(host: str) -> str:
    parts = host.split(".")
    n = 3 if len(parts) > 2 and parts[-2] in ("co", "com", "org", "net", "ac", "gov") else 2
    return ".".join(parts[-n:])


def same_brand(a: str, b: str) -> bool:
    """Samma varumärke på en annan toppdomän, t.ex. advania.com och advania.se."""
    ba, bb = site_of(host_of(a)).split(".")[0], site_of(host_of(b)).split(".")[0]
    return len(ba) > 3 and ba == bb


def same_site(a: str, b: str) -> bool:
    ha, hb = host_of(a), host_of(b)
    return ha == hb or site_of(ha) == site_of(hb)


# ---------------------------------------------------------------------------
# Bara tjänster i Sverige
# ---------------------------------------------------------------------------
SV_PLACES = """sverige sweden stockholm göteborg goteborg gothenburg malmö malmo uppsala
linköping linkoping västerås vasteras örebro orebro norrköping norrkoping helsingborg jönköping jonkoping
umeå umea lund borås boras sundsvall gävle gavle eskilstuna södertälje sodertalje karlstad täby taby växjö vaxjo
halmstad luleå lulea trollhättan östersund ostersund borlänge falun kalmar skövde skovde kristianstad karlskrona
skellefteå skelleftea uddevalla varberg nyköping nykoping motala landskrona trelleborg örnsköldsvik ornskoldsvik
visby lidköping piteå pitea kiruna solna sundbyberg kista nacka huddinge sollentuna lidingö danderyd järfälla
haninge botkyrka upplands väsby mölndal molndal kungsbacka partille lerum alingsås ängelholm angelholm hässleholm
enköping enkoping sigtuna märsta arlanda norrtälje vänersborg katrineholm sandviken hudiksvall söderhamn
kramfors härnösand mora ludvika avesta köping kungälv kungalv karlskoga mariestad falkenberg ystad simrishamn
oskarshamn västervik värnamo vaxholm strängnäs nynäshamn tumba bromma solna""".split()
_two_word = ["upplands väsby"]
SV_RE = re.compile(r"\b(" + "|".join(sorted(set(SV_PLACES) - {"upplands", "väsby"} | set(_two_word), key=len, reverse=True)) + r")\b", re.I)
FOREIGN_RE = re.compile(
    r"\b(united states|usa|u\.s\.a?|united kingdom|uk|england|scotland|ireland|norway|norge|denmark|danmark|"
    r"finland|iceland|island|germany|tyskland|deutschland|netherlands|nederländerna|holland|belgium|belgien|"
    r"france|frankrike|spain|spanien|portugal|italy|italien|switzerland|schweiz|austria|österrike|poland|polen|"
    r"czech|tjeckien|slovakia|hungary|ungern|romania|rumänien|bulgaria|greece|grekland|croatia|serbia|estonia|"
    r"estland|latvia|lettland|lithuania|litauen|ukraine|turkey|turkiet|israel|egypt|morocco|south africa|india|"
    r"indien|pakistan|bangladesh|sri lanka|china|kina|japan|korea|singapore|malaysia|indonesia|philippines|"
    r"filippinerna|vietnam|thailand|australia|australien|new zealand|canada|kanada|mexico|brazil|brasilien|"
    r"argentina|chile|colombia|peru|"
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|"
    r"illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
    r"mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|"
    r"north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|"
    r"utah|vermont|virginia|washington|wisconsin|wyoming|"
    r"london|manchester|dublin|oslo|bergen|copenhagen|köpenhamn|aarhus|helsinki|helsingfors|tampere|espoo|"
    r"berlin|munich|münchen|hamburg|frankfurt|amsterdam|rotterdam|brussels|paris|madrid|barcelona|lisbon|"
    r"milan|rome|zurich|vienna|warsaw|warszawa|krakow|kraków|wroclaw|gdansk|prague|budapest|bucharest|"
    r"tallinn|riga|vilnius|kyiv|kiev|bangalore|bengaluru|hyderabad|pune|chennai|mumbai|delhi|noida|gurgaon|"
    r"shanghai|beijing|tokyo|toronto|montreal|vancouver|chicago|boston|seattle|austin|atlanta|san francisco|"
    r"los angeles|dallas|houston|denver|augusta|emea|apac|latam|americas)\b", re.I)


def sweden_verdict(location: str, country: str = "") -> bool | None:
    """True = Sverige, False = utomlands, None = går inte att avgöra."""
    c = (country or "").strip().lower()
    if c in ("se", "swe", "sweden", "sverige"):
        return True
    loc = location or ""
    if SV_RE.search(loc):
        return True
    if (c and len(c) >= 2) or FOREIGN_RE.search(loc):
        return False
    return None


def guess_location(page: "PageParser") -> str:
    """Ort när annonsen saknar strukturerad data och etikett ("Plats:").
    Letar efter en kort rad som ser ut som en ort, t.ex. 'Augusta, Georgia, United States'."""
    if page.microdata:
        return ", ".join(dict.fromkeys(page.microdata))
    lines = [ln.strip(" \t·|•–-") for ln in page.text.split("\n")]
    for ln in lines[:400]:
        if not (2 < len(ln) <= 80) or ln.count(",") > 4 or ln.endswith((".", ":", "?", "!")):
            continue
        parts = [x.strip() for x in ln.split(",")]
        if any(len(x) > 40 or len(x.split()) > 4 for x in parts):
            continue
        if SV_RE.fullmatch(parts[0]) or SV_RE.fullmatch(parts[-1]) or FOREIGN_RE.fullmatch(parts[-1]) \
                or FOREIGN_RE.fullmatch(parts[0]):
            return ln
    for piece in re.split(r"\s[|–-]\s|,\s", page.title):  # <title> innehåller ofta orten
        piece = piece.strip()
        if piece and (SV_RE.fullmatch(piece) or FOREIGN_RE.fullmatch(piece)):
            return piece
    return ""


JOBLINK_RE = re.compile(r"(job|jobb|position|annons|vacanc|opening|tjanst|tjänst|career|karri|apply|ansok|ansök)", re.I)


def page_fingerprint(page: PageParser) -> str:
    """Fingeravtryck av det som spelar roll på en karriärsida: annonslänkar och
    strukturerad jobbdata. Nyheter, cookies, datum i sidfoten osv. påverkar inte."""
    parts = sorted({f"{href.strip()}|{' '.join(text.split())}" for href, text in page.links
                    if classify(text) or JOBLINK_RE.search(href)})
    parts += sorted(re.sub(r"\s+", " ", b).strip() for b in page.jsonld if "JobPosting" in b)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def feed_fingerprint(feed: list[dict]) -> str:
    parts = sorted(f"{j['url']}|{j['title']}" for j in feed)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Cache för annonssidor: URL -> ETag/Last-Modified + texten. Vid nästa körning
# frågar vi servern om sidan ändrats; svarar den 304 används den sparade texten
# (och därmed det sparade AI-svaret) utan att något behöver läsas om.
# ---------------------------------------------------------------------------
class PageCache:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        try:
            self.data: dict = json.loads(path.read_text("utf-8")) if path.exists() else {}
        except json.JSONDecodeError:
            self.data = {}
        self.hits = 0

    def get(self, url: str) -> dict | None:
        with self._lock:
            return self.data.get(clean_url(url))

    def put(self, url: str, entry: dict) -> None:
        entry["checked"] = TODAY.isoformat()
        with self._lock:
            self.data[clean_url(url)] = entry

    def save(self) -> None:
        cutoff = (TODAY - dt.timedelta(days=120)).isoformat()
        with self._lock:
            keep = {k: v for k, v in self.data.items() if v.get("checked", "") >= cutoff}
        self.path.write_text(json.dumps(keep, ensure_ascii=False, separators=(",", ":")), "utf-8")


class CompanyScraper:
    MAX_CAREER_PAGES = 5
    MAX_JOB_LINKS = 25
    MAX_ENRICH = 40
    MAX_DETAIL = 30      # annonser per företag som läses och bedöms av AI
    MAX_FEED_DETAIL = 100  # annonser med text från ett flöde behöver inte hämtas – högre tak

    def __init__(self, http: Http, member: dict, known: dict | None = None,
                 cached_postings: list[dict] | None = None, force_full: bool = False,
                 ai=None, renderer: Renderer | None = None, pagecache: PageCache | None = None):
        known = known or {}
        self.http = http
        self.m = member
        self.ai = ai
        self.renderer = renderer
        self.pagecache = pagecache
        self.known_pages = [u for u in known.get("pages", []) if not ats.is_shared_root(u)]  # från karriarsidor.csv
        self.known_fp = known.get("fingerprints", [])
        self.known_etag = known.get("etags", [])
        self.known_lm = known.get("lastmods", [])
        self.known_manual = known.get("manual", False)
        self.known_confirmed = known.get("confirmed", "")
        self.known_ats = known.get("ats", "")
        self.known_total = known.get("open_total")
        self.cached_postings = cached_postings
        self.force_full = force_full
        self.career_pages: list[str] = []      # karriärsidor som faktiskt gick att läsa
        self.fingerprints: list[str] = []
        self.etags: list[str] = []
        self.lastmods: list[str] = []
        self.used_known = False
        self.confirmed = False                 # AI (eller en människa) har bekräftat karriärsidan
        self.ats_name = ""
        self.unchanged = False                 # True = sidan oförändrad, gamla annonser återanvändes
        self.blocked = False
        self.meta: dict[str, tuple[str, str]] = {}   # url -> (ETag, Last-Modified)
        self.postings: list[dict] = []
        self.cands: dict[str, dict] = {}       # möjliga annonser (AI-läget)
        self.notes: list[str] = []
        self.visited: set[str] = set()
        self.cache: dict[str, tuple] = {}
        self.home = ""
        self.last_status = 0
        self.fallback_done = False
        self.crawl_more: list[str] = []
        self.guessed_site = ""
        self.open_total: int | None = None    # alla lediga tjänster i Sverige (ungefärligt)
        self.searched = False
        self.search_careers = ""
        self.found_by_search = False

    # -- hjälp -------------------------------------------------------------
    def fetch(self, url: str) -> tuple[str, PageParser | None, str]:
        key = clean_url(url)
        if key in self.cache:
            return self.cache[key]
        self.visited.add(key)
        result = (url, None, "")
        try:
            self.http._tl.headers = None
            final, status, body = self.http.get(url)
            self.last_status = status
            hd = getattr(self.http._tl, "headers", None)
            if hd:
                self.meta[key] = (hd.get("ETag") or "", hd.get("Last-Modified") or "")
            if status == 200 and body:
                result = (final, parse_page(body), body)
                self.visited.add(clean_url(final))
        except (URLError, TimeoutError, OSError, ValueError, EOFError, zlib.error) as e:   # även trasig gzip
            self.last_status = -1
            log.debug("%s: %s", url, e)
        self.cache[key] = result
        return result

    def render(self, url: str) -> tuple[str, PageParser | None, str]:
        """Öppnar sidan i webbläsaren. iframes (ofta rekryteringssystem) läggs till som länkar."""
        if not self.renderer or not self.http.allowed(url):
            return url, None, ""
        final, html, frames = self.renderer.render(url)
        if not html:
            return url, None, ""
        page = parse_page(html)
        page.links += [(f, "(inbäddad sida)") for f in frames]
        self.cache[clean_url(url)] = (final, page, html)
        return final, page, html

    def fetch_or_render(self, url: str) -> tuple[str, PageParser | None, str, bool]:
        final, page, body = self.fetch(url)
        if self.renderer and (page is None and self.last_status in (403, 429, -1) or page is not None and thin(page)):
            f2, p2, b2 = self.render(url)
            if p2 is not None and (page is None or len(p2.links) > len(page.links)
                                   or len(p2.text) > len(page.text) * 1.3):
                return f2, p2, b2, True
        return final, page, body, False

    def ok_target(self, url: str) -> bool:
        if self.search_careers and same_site(url, self.search_careers):
            return True   # karriärsajt som webbsökningen hittat (t.ex. aderian.se för bybrick.se)
        """Länkar som får följas: medlemmens egen webbplats, ett rekryteringssystem eller en
        karriärsajt på en närliggande domän (t.ex. addsecuregroup.com -> career.addsecure.com)."""
        if not url.startswith("http") or SKIP_EXT.search(url) or any(s in url.lower() for s in SKIP_HOSTS):
            return False
        return (same_site(url, self.home) or same_brand(url, self.home) or ats.ats_of_url(url) is not None
                or bool(CAREER_HOST_RE.match(host_of(url))))

    def add(self, title, url, source, published=None, deadline=None, location="",
            employment_type=None, typ=None, country=""):
        if self.home and url and not self.ok_target(url):
            return  # annonsen ligger inte på medlemmens egen webbplats
        typ = typ or classify(title, employment_type)
        if not typ or NOT_JOB_RE.search(title) or GENERIC_NAV_RE.match(title.strip()) or len(title) > 160:
            return
        self.postings.append({
            "company": self.m["name"], "orgnr": self.m.get("orgnr", ""),
            "type": typ, "title": " ".join(title.split()), "url": url,
            "published": published, "deadline": deadline,
            "location": location or "", "source": source, "_country": country or "",
        })

    def add_cand(self, title, url, source, triage=None, **kw):
        url = ats.public_job_url(url) if url else url
        if not url or not title or not self.ok_target(url):
            return
        key = clean_url(url)
        c = self.cands.get(key)
        if c is None:
            c = self.cands[key] = {"title": " ".join(str(title).split()), "url": url, "source": source,
                                   "triage": triage, "location": "", "country": "", "published": None,
                                   "deadline": None, "description": "", "employment_type": None}
        for k, v in kw.items():
            if v and not c.get(k):
                c[k] = v
        if triage and (c["triage"] in (None, "unclear")):
            c["triage"] = triage

    # -- huvudflöde --------------------------------------------------------
    def quick_check(self) -> bool:
        """Snabbkoll: har de sparade karriärsidorna ändrats sedan förra skrapningen?
        Returnerar True om allt är oförändrat (då återanvänds förra körningens annonser)."""
        if self.force_full or not self.known_pages or self.cached_postings is None \
                or len(self.known_fp) != len(self.known_pages):
            return False
        if self.known_ats in ats.ADAPTERS or any(ats.ats_of_url(u) in ats.ADAPTERS for u in self.known_pages):
            return False   # flöden är billiga att läsa – hämta alltid färskt
        if self.ai and not (self.known_manual or self.fresh_confirmation()):
            return False   # osäker karriärsida – leta på nytt
        fps, etags, lms = [], [], []
        for i, url in enumerate(self.known_pages):
            etag = self.known_etag[i] if i < len(self.known_etag) else ""
            lm = self.known_lm[i] if i < len(self.known_lm) else ""
            try:
                final, status, body, new_etag, new_lm = self.http.get_conditional(url, etag, lm)
            except (URLError, TimeoutError, OSError, ValueError):
                return False
            if status == 304:
                fps.append(self.known_fp[i])
            elif status == 200 and body:
                page = parse_page(body)
                self.cache[clean_url(url)] = (final, page, body)  # behövs om sidan har ändrats
                self.meta[clean_url(url)] = (new_etag, new_lm)
                fps.append(page_fingerprint(page))
            else:
                return False
            etags.append(new_etag or etag)
            lms.append(new_lm or lm)
        self.fingerprints, self.etags, self.lastmods = fps, etags, lms
        if fps == self.known_fp:
            self.unchanged = True
            self.used_known = True
            self.confirmed = bool(self.known_confirmed) or self.known_manual
            self.ats_name = self.known_ats
            self.open_total = self.known_total
            self.career_pages = list(self.known_pages)
            self.postings = [dict(p) for p in self.cached_postings]
            dom = self.m.get("domain") or self.known_pages[0]
            self.home = dom if dom.startswith("http") else "https://" + dom
            self.keep_swedish()   # även sparade annonser filtreras på Sverige
            return True
        return False

    def fresh_confirmation(self) -> bool:
        try:
            return (TODAY - dt.date.fromisoformat(self.known_confirmed)).days <= RECONFIRM_DAYS
        except ValueError:
            return False

    def open_home(self) -> tuple[str, PageParser | None, str]:
        domain = (self.m.get("domain") or "").strip() or self.known_pages[0]
        if not domain.startswith("http"):
            domain = "https://" + domain.lstrip("/")
        p = urlparse(domain)
        host = re.sub(r"^w{2,3}\.", "", p.netloc.lower())   # även felstavat "ww.", "Www."
        path = p.path if p.path not in ("", "/") else ""
        statuses = []
        tries = [domain] if p.netloc.lower() == host or p.scheme == "http" else []
        tries += [f"https://{host}{path}", f"https://www.{host}{path}", f"http://www.{host}{path}"]
        for cand in dict.fromkeys(tries):
            url, page, body = self.fetch(cand)
            statuses.append(self.last_status)
            if page:
                if self.renderer and thin(page):   # t.ex. samtyckesvägg eller sida byggd med JavaScript
                    f2, p2, b2 = self.render(url)
                    if p2 is not None and len(p2.links) > len(page.links):
                        return f2, p2, b2
                return url, page, body
        if self.renderer and any(s in (403, 429) for s in statuses):
            url, page, body = self.render(f"https://www.{host}{path}")
            if page and len(page.links) >= 3:   # "Access Denied" o.d. har inga länkar
                return url, page, body
            self.blocked = True
            return "", None, ""
        # Domänen i medlemslistan är ofta gammal – prova företagsnamnet (Alfa eCare AB -> alfaecare.se)
        guess = norm_name(self.m["name"])
        if len(guess) >= 4 and not self.m.get("_no_guess"):
            for cand in (f"https://{guess}.se", f"https://www.{guess}.se", f"https://{guess}.com"):
                if host_of(cand) == host:
                    continue
                url, page, body = self.fetch(cand)
                if page and len(page.links) >= 5 and guess[:6] in re.sub(r"\W", "", (page.title + page.text[:3000]).lower()):
                    self.notes.append(f"webbplatsen i medlemslistan svarar inte – använde {host_of(url)} (kontrollera)")
                    self.guessed_site = host_of(url)
                    return url, page, body
        return "", None, ""

    def run(self) -> list[dict]:
        domain = (self.m.get("domain") or "").strip()
        if not domain and not self.known_pages:
            return []
        if self.quick_check():
            return self.postings
        self.fingerprints = []
        if self.read_known_feeds(domain):
            return self.postings
        home_url, home, home_html = self.open_home()
        if not home and self.ai and not self.blocked:
            home_url, home, home_html = self.search_site(dead=True)
        if not home:
            self.notes.append("webbplatsen blockerar automatiska besök" if self.blocked
                              else "webbplats svarar inte")
            return []
        self.home = home_url
        if self.ai:
            self.run_ai(home_url, home, home_html)
        else:
            self.run_rules(home_url, home)
        self.etags = [self.meta.get(clean_url(u), ("", ""))[0] for u in self.career_pages]
        self.lastmods = [self.meta.get(clean_url(u), ("", ""))[1] for u in self.career_pages]
        self.keep_swedish()
        return self.postings

    # -- utan AI: regler ---------------------------------------------------
    def run_rules(self, home_url: str, home: PageParser) -> None:
        pages_read = 0
        queue_: list[str] = []
        # 1) Sparade karriärsidor från förra gången (eller inlagda för hand)
        for url in self.known_pages:
            final, page, _ = self.fetch(url)
            if page:
                self.used_known = True
                pages_read += 1
                self.career_pages.append(url)
                self.fingerprints.append(page_fingerprint(page))
                self.scan_listing(page, final)
                queue_ += [u for u in self.find_career_links(page, final) if clean_url(u) not in self.visited]
        # 2) Annars: leta upp karriärsidan från startsidan
        if not self.used_known:
            if self.known_pages:
                self.notes.append("sparad karriärsida svarar inte – letade på nytt")
            queue_ = self.find_career_links(home, home_url) or [urljoin(home_url, g) for g in GUESS_PATHS]
        queue_ = list(dict.fromkeys(queue_))
        while queue_ and pages_read < self.MAX_CAREER_PAGES:
            url = queue_.pop(0)
            if clean_url(url) in self.visited or ats.is_shared_root(url):
                continue
            final, page, body = self.fetch(url)
            if not page:
                continue
            pages_read += 1
            if not self.used_known and len(self.career_pages) < 2:
                self.career_pages.append(final)
                self.fingerprints.append(page_fingerprint(page))
            self.scan_listing(page, final)
            # en nivå djupare: "Lediga tjänster", "Open positions" osv.
            for sub in self.find_career_links(page, final):
                if clean_url(sub) not in self.visited and sub not in queue_:
                    queue_.append(sub)
        if pages_read == 0:
            self.notes.append("ingen karriärsida hittad")
        self.enrich()

    def find_career_links(self, page: PageParser, base: str) -> list[str]:
        scored = []
        for href, text in page.links:
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            url = urljoin(base, href)
            if not url.startswith("http") or SKIP_EXT.search(url):
                continue
            if any(s in url.lower() for s in SKIP_HOSTS):
                continue
            if text and classify(text) and not GENERIC_NAV_RE.match(text):
                continue  # en enskild annons, inte en karriärsida
            path = urlparse(url).path
            if NOT_CAREER_PATH_RE.search(path):
                continue
            hay = f"{path} {text}"
            if not CAREER_RE.search(hay):
                continue
            if not same_site(url, self.home or base):
                continue  # bara medlemmens egen webbplats
            score = 0
            score += 3 if re.search(r"karri[aä]r|career", hay, re.I) else 0
            score += 2 if re.search(r"lediga|open|vacanc|jobs|jobb", hay, re.I) else 0
            score += 2 if re.search(r"student|graduate|trainee|praktik|intern|early", hay, re.I) else 0
            score -= path.count("/") * 0.2
            scored.append((score, url))
        scored.sort(key=lambda x: -x[0])
        return list(dict.fromkeys(u for _, u in scored))[: self.MAX_CAREER_PAGES + 2]

    def scan_listing(self, page: PageParser, page_url: str) -> None:
        for job in jobpostings_from_jsonld(page.jsonld, page_url):
            self.add(job["title"], job["url"], "Webbplats", job["published"], job["deadline"],
                     job["location"], job["employment_type"], country=job["country"])
        n = 0
        for href, text in page.links:
            if n >= self.MAX_JOB_LINKS:
                break
            if not text or len(text) < 4 or not classify(text):
                continue
            url = urljoin(page_url, href)
            if not url.startswith("http") or SKIP_EXT.search(url) or any(s in url.lower() for s in SKIP_HOSTS):
                continue
            if clean_url(url) == clean_url(page_url) or not same_site(url, self.home or page_url):
                continue
            n += 1
            title = re.split(r"\s{2,}|\s[|•·–-]\s", text)[0]
            self.add(title, url, "Webbplats")

    # -- med AI -------------------------------------------------------------
    def link_table(self, page: PageParser, base: str, limit: int = 200) -> list[tuple[int, str, str]]:
        out, seen = [], set()
        for href, text in page.links:
            if href.startswith(("mailto:", "tel:", "javascript:", "#")):
                continue
            url = urljoin(base, href.strip())
            key = clean_url(url)
            if key in seen or key == clean_url(base) or not self.ok_target(url):
                continue
            seen.add(key)
            out.append((len(out), text[:120], url))
            if len(out) >= limit:
                break
        return out

    def sitemap_urls(self, home_url: str) -> list[str]:
        """Karriärliknande sidor ur sitemap.xml (robots.txt pekar ofta ut den)."""
        p = urlparse(home_url)
        origin = f"{p.scheme}://{p.netloc}"
        self.http.allowed(origin + "/")
        maps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", self.http.robots_txt.get(origin, ""))
        maps = maps or [origin + "/sitemap.xml", origin + "/sitemap_index.xml"]
        found, fetched = [], 0
        todo = list(dict.fromkeys(maps))[:3]
        while todo and fetched < 6:
            sm = todo.pop(0)
            fetched += 1
            try:
                _, status, body = self.http.get(sm)
            except (URLError, TimeoutError, OSError, ValueError):
                continue
            if status != 200 or "<loc>" not in body:
                continue
            locs = [unescape(x.strip()) for x in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", body)]
            if "<sitemapindex" in body[:2000]:
                # välj del-sitemaps som troligen innehåller sidor (inte produkter/bilder)
                locs.sort(key=lambda u: (not re.search(r"page|sida|career|karri|job", u, re.I), len(u)))
                todo += [u for u in locs if not re.search(r"image|product|post-tag|category|author", u, re.I)][:3]
                continue
            for u in locs:
                path = urlparse(u).path
                if CAREER_PATH_RE.search(path) and not NOT_CAREER_PATH_RE.search(path) and same_site(u, home_url):
                    found.append(u)
        # kortast först: /karriar före /karriar/junior-utvecklare-123
        return sorted(dict.fromkeys(found), key=lambda u: (urlparse(u).path.count("/"), len(u)))[:40]

    def discover(self, home_url: str, home: PageParser) -> list[str]:
        """Kandidater från startsida, sitemap och vanliga adresser -> AI väljer."""
        cands: list[tuple[str, str]] = []   # (text, url)
        all_links = self.link_table(home, home_url, limit=400)
        career = [(t, u) for _, t, u in all_links
                  if (CAREER_RE.search(f"{urlparse(u).path} {t}") or ats.ats_of_url(u))
                  and not NOT_CAREER_PATH_RE.search(urlparse(u).path)]
        cands += career
        cands += [("(sitemap)", u) for u in self.sitemap_urls(home_url)]
        cands += [("(tidigare sparad)", u) for u in self.known_pages]
        if len(cands) < 3:
            for g in GUESS_PATHS:
                url = urljoin(home_url, g)
                final, page, _ = self.fetch(url)
                if page and clean_url(final) != clean_url(home_url):
                    cands.append(("(vanlig adress)", final))
                    break
        if len(cands) < 3:   # inget som liknar en karriärsida – låt AI se hela menyn
            cands += [(t, u) for _, t, u in all_links[:120]]
        table, seen = [], set()
        for text, url in cands:
            k = clean_url(url)
            if k not in seen and k != clean_url(home_url):
                seen.add(k)
                table.append((len(table), text, url))
        log.debug("[%s] startsida %s: %d länkar, %d kandidater", self.m["name"], home_url, len(home.links), len(table))
        if not table:
            self.fallback_done = True
            return self.fallback_pages(home_url, home)
        picks = self.ai.pick_career_pages(self.m["name"], home_url, table[:150])
        log.debug("[%s] %d kandidater, AI valde %s", self.m["name"], len(table),
                  [(table[p["id"]][2], p["kind"]) for p in picks])
        chosen = [table[p["id"]][2] for p in picks]
        if not chosen:
            self.fallback_done = True
            chosen = self.fallback_pages(home_url, home)
        return chosen

    def fallback_pages(self, home_url: str, home: PageParser) -> list[str]:
        """Ingen karriärlänk – prova vanliga adresser och "Om oss" (sidan kan länka vidare)."""
        out = []
        for g in GUESS_PATHS:
            final, page, _ = self.fetch(urljoin(home_url, g))
            if page and clean_url(final) != clean_url(home_url):
                out.append(final)
                break
        all_links = self.link_table(home, home_url, limit=400)
        out += [u for _, t, u in all_links if ABOUT_RE.search(f"{urlparse(u).path} {t}")][:2]
        # global koncernsajt: länken till den svenska sajten
        out += [u for _, t, u in all_links if re.search(r"\b(sverige|sweden|svenska)\b", t, re.I)
                or (host_of(u).endswith(".se") and not same_site(u, home_url))][:1]
        return list(dict.fromkeys(out))

    def run_ai(self, home_url: str, home: PageParser, home_html: str) -> None:
        start: list[str] = []
        if self.crawl_more:
            start, self.crawl_more = self.crawl_more, []
        elif self.known_pages and (self.known_manual or self.fresh_confirmation()):
            start = list(self.known_pages)
            self.used_known = True
            self.confirmed = True
        else:
            # rekryteringssystem som körs direkt på startsidan (t.ex. hela sajten är Teamtailor)
            if ats.ats_of_html(home_html) == "Teamtailor" or ats.ats_of_url(home_url):
                start.append(home_url)
            start += self.discover(home_url, home)
            if self.known_pages and not start:
                self.notes.append("sparad karriärsida verkar inte längre stämma – letade på nytt")
        queue_ = list(dict.fromkeys(start))
        pages_read = 0
        while queue_ and pages_read < self.MAX_CAREER_PAGES:
            url = queue_.pop(0)
            key = clean_url(url)
            if key in self.visited and key not in {clean_url(u) for u in start}:
                continue
            self.visited.add(key)
            if ats.is_shared_root(url):
                continue
            # 1) Rekryteringssystem med öppet flöde?
            name = ats.ats_of_url(url)
            if name in ats.ADAPTERS and self.read_feed(name, url):
                pages_read += 1
                continue
            final, page, body, rendered = self.fetch_or_render(url)
            if not page:
                continue
            pages_read += 1
            name = ats.ats_of_url(final) or ats.ats_of_html(body)
            if name in ("Teamtailor", "Eightfold") and self.read_feed(name, final):
                continue
            if name == "Teamtailor":   # inbäddad lista – flödet ligger på bolagets teamtailor-värd
                tt = next((h for h in dict.fromkeys(re.findall(r"\b([a-z0-9-]+)\.teamtailor\.com", body.lower()))
                           if h not in ("www", "api", "app", "career", "support", "assets", "scripts")), None)
                if tt and self.read_feed(name, f"https://{tt}.teamtailor.com/jobs"):
                    continue
            for job in jobpostings_from_jsonld(page.jsonld, final):
                self.add_cand(job["title"], job["url"], "Webbplats", location=job["location"],
                              country=job["country"], published=job["published"], deadline=job["deadline"],
                              description=job["description"], employment_type=job["employment_type"])
            links = self.link_table(page, final)
            res = self.ai.analyze_page(self.m["name"], final, page.title, page.text, links)
            if res and res.get("listing_loaded_by_script") and not rendered and self.renderer:
                f2, p2, b2 = self.render(final)
                if p2 is not None and len(p2.links) > len(page.links):
                    final, page, body = f2, p2, b2
                    links = self.link_table(page, final)
                    res = self.ai.analyze_page(self.m["name"], final, page.title, page.text, links) or res
            log.debug("[%s] %s%s -> %s, %d annonser %s, följ %s", self.m["name"], final,
                      " (renderad)" if rendered else "", res and res.get("page_kind"),
                      len(res.get("jobs", [])) if res else 0,
                      [(links[j["id"]][1][:40], j["triage"]) for j in (res or {}).get("jobs", [])][:12],
                      [links[i][2] for i in (res or {}).get("follow", [])])
            if res is None:            # AI svarade inte – använd reglerna för just den här sidan
                self.scan_listing_cands(page, final)
                continue
            kind = res.get("page_kind")
            follow = [links[i][2] for i in res.get("follow", [])]
            if kind == "not_careers":
                # fel sida, men den kan peka vidare ("Lediga tjänster" under "Arbeta hos oss")
                queue_ += [u for u in follow if clean_url(u) not in self.visited and u not in queue_]
                continue
            if kind == "single_job_posting":
                self.add_cand(page.title.split("|")[0].strip() or final, final, "Webbplats")
                continue
            self.confirmed = True
            if len(self.career_pages) < 3 and clean_url(final) not in {clean_url(u) for u in self.career_pages}:
                self.career_pages.append(final)
                self.fingerprints.append(page_fingerprint(page))
                if name and not self.ats_name:
                    self.ats_name = name
            for j in res.get("jobs", []):
                _, text, jurl = links[j["id"]]
                self.add_cand(text or jurl, jurl, name or "Webbplats", triage=j["triage"])
            # länkar till rekryteringssystem följs alltid först
            ats_links = [u for _, _, u in links if ats.ats_of_url(u) and clean_url(u) not in self.visited]
            for u in ats_links[:2] + follow:
                if clean_url(u) not in self.visited and u not in queue_:
                    queue_.append(u) if u in follow else queue_.insert(0, u)
        if not self.career_pages and not self.fallback_done and pages_read < self.MAX_CAREER_PAGES + 2:
            self.fallback_done = True
            extra = [u for u in self.fallback_pages(home_url, home) if clean_url(u) not in self.visited]
            if extra:
                log.debug("[%s] inget hittat – provar %s", self.m["name"], extra)
                self.crawl_more = extra
                return self.run_ai(home_url, home, home_html)
        if not self.career_pages and not self.searched:
            self.search_site(dead=False)
            if self.search_careers and clean_url(self.search_careers) not in self.visited:
                self.crawl_more = [self.search_careers]
                return self.run_ai(home_url, home, home_html)
        if not self.career_pages:
            self.notes.append("ingen karriärsida hittad")
        else:
            self.open_total = len(self.cands)
        self.assess()

    def search_site(self, dead: bool) -> tuple[str, PageParser | None, str]:
        """Webbsökning (Claude) efter webbplats och karriärsida. Används bara när medlemslistans
        domän inte svarar eller när ingen karriärsida hittades på vanligt sätt."""
        self.searched = True
        r = self.ai.find_site(self.m["name"], self.m.get("orgnr", ""), self.m.get("domain", ""))
        log.debug("[%s] webbsökning: %s", self.m["name"], r)
        if not r:
            return "", None, ""
        site, careers = r.get("website", "").strip(), r.get("careers_url", "").strip()
        home = ("", None, "")
        if dead and site.startswith("http") and not any(x in site for x in SKIP_HOSTS):
            final, page, body, _ = self.fetch_or_render(site)
            if page and len(page.links) >= 5:
                home = (final, page, body)
                self.home = final
                self.guessed_site = host_of(final)
                self.found_by_search = True
                self.notes.append(f"webbplatsen i medlemslistan svarar inte – hittade {host_of(final)} via webbsökning (kontrollera)")
        if careers.startswith("http") and (self.home or home[0]):
            self.home = self.home or home[0]
            if self.ok_target(careers) or not any(x in careers.lower() for x in SKIP_HOSTS):
                self.search_careers = careers
                self.found_by_search = True
                if dead:
                    self.crawl_more = [careers]
        return home

    def read_known_feeds(self, domain: str) -> bool:
        """Sparade karriärsidor som alla är flöden (t.ex. Eightfold) läses direkt – startsidan
        behövs inte, vilket hjälper när den blockerar automatiska besök (ericsson.com)."""
        if not self.known_pages or not (self.known_manual or self.fresh_confirmation()):
            return False
        if not all(ats.ats_of_url(u) in ats.ADAPTERS for u in self.known_pages):
            return False
        dom = domain or self.known_pages[0]
        self.home = dom if dom.startswith("http") else "https://" + dom
        self.used_known = True
        for u in self.known_pages:
            self.read_feed(ats.ats_of_url(u), u)
        if not self.career_pages:
            self.home = ""
            return False   # flödet svarade inte – leta på vanligt sätt
        self.open_total = len(self.cands)
        if self.ai:
            self.assess()
        else:
            for c in self.cands.values():
                self.add(c["title"], c["url"], c["source"], c["published"], c["deadline"], c["location"],
                         c["employment_type"], country=c["country"])
        self.etags, self.lastmods = [""] * len(self.career_pages), [""] * len(self.career_pages)
        self.keep_swedish()
        return True

    def read_feed(self, name: str, url: str) -> bool:
        feed = ats.fetch_feed(self.http, name, url, norm_date)
        if feed is None:
            return False
        self.ats_name = self.ats_name or name
        self.confirmed = True
        if len(self.career_pages) < 3:
            self.career_pages.append(url)
            self.fingerprints.append(feed_fingerprint(feed))
        dropped = 0
        for j in feed:
            if sweden_verdict(j["location"], j["country"]) is False:
                dropped += 1
                continue
            self.add_cand(j["title"], j["url"], name, location=j["location"], country=j["country"],
                          published=j["published"], deadline=j["deadline"], description=j["description"],
                          employment_type=j["employment_type"])
        if dropped:
            self.notes.append(f"{dropped} tjänster utanför Sverige i {name} hoppades över")
        return True

    def scan_listing_cands(self, page: PageParser, page_url: str) -> None:
        for href, text in page.links:
            if text and classify(text) and not NOT_JOB_RE.search(text):
                self.add_cand(text, urljoin(page_url, href), "Webbplats", triage="junior")

    def job_text(self, url: str) -> tuple[str, PageParser | None, str]:
        """Annonsens text – från cachen om servern säger att sidan inte ändrats (304)."""
        cached = self.pagecache.get(url) if self.pagecache else None
        if cached and (cached.get("etag") or cached.get("lm")):
            try:
                final, status, body, etag, lm = self.http.get_conditional(url, cached.get("etag", ""),
                                                                          cached.get("lm", ""))
            except (URLError, TimeoutError, OSError, ValueError):
                status, body = 0, ""
            if status == 304:
                self.pagecache.hits += 1
                self.pagecache.put(url, cached)
                return cached["text"], None, cached.get("final", url)
            if status == 200 and body:
                page = parse_page(body)
                self.cache[clean_url(url)] = (final, page, body)
                self.meta[clean_url(url)] = (etag, lm)
        final, page, body, _ = self.fetch_or_render(url)
        if not page:
            return "", None, url
        text = page.text
        hints = jobpostings_from_jsonld(page.jsonld, final)
        if hints:   # strukturerad data ger ofta datum/ort som inte syns i texten
            h = hints[0]
            text = (f"[Strukturerad data: titel={h['title']}; ort={h['location']}; land={h['country']}; "
                    f"publicerad={h['published']}; sista dag={h['deadline']}]\n") + text
        if self.pagecache:
            etag, lm = self.meta.get(clean_url(url), ("", ""))
            self.pagecache.put(url, {"etag": etag, "lm": lm, "final": final, "text": text[:15000]})
        return text, page, final

    def assess(self) -> None:
        """Sållning på titel och sedan bedömning av själva annonsen."""
        cands = list(self.cands.values())
        if not cands:
            return
        untriaged = [(i, c["title"], c["location"]) for i, c in enumerate(cands) if not c["triage"]]
        if untriaged:
            verdicts = self.ai.triage_titles(self.m["name"], untriaged)
            for i, v in verdicts.items():
                cands[i]["triage"] = v
        log.debug("[%s] sållning: %s", self.m["name"], [(c["title"][:40], c["triage"]) for c in cands][:40])
        pick = [c for c in cands if c["triage"] == "junior"]
        pick += [c for c in cands if c["triage"] in ("unclear", None)]
        # annonser vars text redan finns (från flöde/API) kostar bara AI-bedömningen
        has_text = [c for c in pick if len(c["description"]) > 300][: self.MAX_FEED_DETAIL]
        needs_fetch = [c for c in pick if len(c["description"]) <= 300][: self.MAX_DETAIL]
        skipped = len(pick) - len(has_text) - len(needs_fetch)
        for_clients = off_topic = 0
        if skipped > 0:
            self.notes.append(f"{skipped} oklara annonser lästes inte (max {self.MAX_DETAIL} per företag)")
        for c in [c for c in pick if c in has_text or c in needs_fetch]:
            final = c["url"]
            if len(c["description"]) > 300:
                text = c["description"]
            else:
                text, _, final = self.job_text(c["url"])
                if not text:
                    continue
            hints = "; ".join(f"{k}={c[k]}" for k in ("location", "country", "published", "deadline") if c[k])
            r = self.ai.classify_job(self.m["name"], c["title"], final, text, hints)
            log.debug("[%s] bedömning %s: %s", self.m["name"], c["title"][:60],
                      r and (r.get("is_job_posting"), r.get("is_junior"), r.get("type"), r.get("country"), r.get("reason")))
            if not r or not r.get("is_job_posting") or not r.get("is_junior") or r.get("type") not in ai_types():
                continue
            if r.get("own_hire") is False:   # rekrytering/bemanning åt kund, annan arbetsgivare
                log.debug("[%s] annan arbetsgivare: %s", self.m["name"], c["url"])
                for_clients += 1
                continue
            if r.get("tech_role") is False:  # t.ex. förskola, vård, butik – inte tech eller angränsande
                log.debug("[%s] utanför tech: %s", self.m["name"], c["url"])
                off_topic += 1
                continue
            if stale(r.get("start", ""), c["deadline"] or norm_date(r.get("deadline"))):
                log.debug("[%s] gammal annons (start %s): %s", self.m["name"], r.get("start"), c["url"])
                continue
            location = c["location"] or r.get("city", "")
            if r.get("remote") and "distans" not in location.lower():
                location = (location + ", Distans").strip(", ")
            self.postings.append({
                "company": self.m["name"], "orgnr": self.m.get("orgnr", ""), "type": r["type"],
                "title": (r.get("title") or c["title"]).strip()[:160], "url": c["url"],
                "published": c["published"] or norm_date(r.get("published")),
                "deadline": c["deadline"] or norm_date(r.get("deadline")),
                "location": location, "source": c["source"], "reason": r.get("reason", ""),
                "_site": site_of(host_of(self.home)),
                "_country": c["country"] or r.get("country", ""),
                "_sv_hint": bool(SV_RE.search(text[:3000])) or "sverige" in text[:3000].lower(),
            })
        if for_clients:
            self.notes.append(f"{for_clients} annonser för andra arbetsgivare (kunder) bortfiltrerade")
        if off_topic:
            self.notes.append(f"{off_topic} annonser utanför tech och angränsande yrken bortfiltrerade")

    # -- berika träffar från annonssidan (regelläget) -----------------------
    def enrich(self) -> None:
        done = 0
        for p in sorted(self.postings, key=lambda x: bool(x["location"])):  # utan ort först
            if done >= self.MAX_ENRICH:
                break
            if p["published"] and p["deadline"] and p["location"]:
                continue
            if not p["url"].startswith("http"):
                continue
            done += 1
            final, page, _ = self.fetch(p["url"])
            if not page:
                continue
            jobs = jobpostings_from_jsonld(page.jsonld, final)
            best = next((j for j in jobs if j["title"].lower() == p["title"].lower()),
                        jobs[0] if len(jobs) == 1 else None)
            if best:
                p["published"] = p["published"] or best["published"]
                p["deadline"] = p["deadline"] or best["deadline"]
                p["location"] = p["location"] or best["location"]
                p["_country"] = p.get("_country") or best.get("country", "")
                if p["source"] == "Webbplats" and len(best["title"]) > 3:
                    p["title"] = best["title"]
            if not p["deadline"]:
                p["deadline"] = deadline_from_text(page.text)
            if not p["location"]:
                m = LOCATION_RX.search(page.text)
                if m:
                    p["location"] = m.group("loc").strip(" .,")
            if not p["location"]:
                p["location"] = guess_location(page)
            p["_sv_hint"] = page.lang.startswith("sv") or bool(SV_RE.search(page.text))

    def keep_swedish(self) -> None:
        """Tar bort tjänster som ligger utomlands. Okänd ort behålls bara om annonsen
        eller webbplatsen pekar på Sverige."""
        se_site = host_of(self.home).endswith(".se")
        kept, dropped = [], 0
        for p in self.postings:
            v = sweden_verdict(p["location"], p.get("_country", ""))
            if v is None:
                v = p.get("_sv_hint", se_site)
            if v:
                kept.append(p)
            else:
                dropped += 1
        if dropped:
            self.notes.append(f"{dropped} tjänster utanför Sverige bortfiltrerade")
        self.postings = kept


def stale(start: str, deadline: str | None) -> bool:
    """Annonser som ligger kvar fast perioden passerat ("praktik hösten 2024")."""
    if deadline and deadline >= TODAY.isoformat():
        return False
    m = re.match(r"(\d{4})-(\d{2})", start or "")
    if not m:
        return False
    months = (TODAY.year - int(m.group(1))) * 12 + TODAY.month - int(m.group(2))
    return months > 3


def ai_types() -> set[str]:
    return {t for t, _ in TYPE_RULES}


# ---------------------------------------------------------------------------
# Namn och nummer
# ---------------------------------------------------------------------------
def norm_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(publ\)|\b(ab|aktiebolag|hb|kb|ek\.? ?for|sverige|sweden|nordic|group|i)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


# ---------------------------------------------------------------------------
# Medlemslistan
# ---------------------------------------------------------------------------
MEMBER_JS = r"""
() => [...document.querySelectorAll('.Member-search__result__item__inner')].map(e => ({
  name: (e.querySelector('h4') || {}).textContent?.trim() || '',
  orgnr: [...e.querySelectorAll('p')].map(p => p.textContent.trim()).find(t => /^\d{6}-?\d{4}$/.test(t)) || '',
  domain: (e.querySelector('a[href]') || {}).textContent?.trim() || ''}))
"""


def launch_browser(pw, headless: bool = True):
    """Playwrights egen Chromium om den finns, annars datorns Edge eller Chrome.
    På jobbdatorer blockeras ofta nedladdningen av Playwrights webbläsare –
    då används den Edge/Chrome som redan är installerad."""
    wanted = os.environ.get("BROWSER_CHANNEL")  # t.ex. msedge eller chrome
    attempts = [wanted] if wanted else [None, "chrome", "msedge"]
    last = None
    for channel in attempts:
        try:
            b = pw.chromium.launch(channel=channel, headless=headless) if channel \
                else pw.chromium.launch(headless=headless)
            log.debug("Webbläsare: %s", channel or "Playwright Chromium")
            return b
        except Exception as e:
            last = e
    raise RuntimeError(f"Hittade ingen webbläsare (Playwright Chromium, Edge eller Chrome): {last}")


# Neka i första hand – skriptet behöver inga kakor
COOKIE_BUTTONS = ["Neka alla", "Avvisa alla", "Deny all", "Reject all", "Godkänn alla", "Acceptera alla",
                  "Tillåt alla", "Accept all", "Allow all cookies", "Allow all", "OK"]


CONSENT_RE = re.compile(r"^(neka|avvisa|deny|reject|decline|godkänn|acceptera|tillåt|accept|allow|agree|ok)\b", re.I)


def dismiss_consent(page) -> None:
    """Klickar bort en cookie-/samtyckesruta (i första hand 'neka'), om det finns en."""
    try:
        buttons = page.get_by_role("button", name=CONSENT_RE)
        n = buttons.count()
        if not n:
            return
        texts = [buttons.nth(i).inner_text(timeout=1000).strip().lower() for i in range(min(n, 8))]
        order = sorted(range(len(texts)), key=lambda i: not re.match(r"neka|avvisa|deny|reject|decline", texts[i]))
        buttons.nth(order[0]).click(timeout=3000)
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass


def fetch_members_playwright(url: str = MEMBERS_URL, limit: int = 0, show: bool = False,
                             debug_dir: Path | None = None) -> list[dict]:
    """Medlemssidan visar en bokstav i taget – klicka igenom alla bokstäver."""
    from playwright.sync_api import sync_playwright  # importeras bara här

    sink: dict[tuple, dict] = {}
    with sync_playwright() as pw:
        browser = launch_browser(pw, headless=not show)
        page = browser.new_page(user_agent=USER_AGENT, locale="sv-SE")
        page.set_default_timeout(15_000)
        log.info("Öppnar medlemssidan …")
        page.goto(url, wait_until="networkidle", timeout=60_000)
        for label in COOKIE_BUTTONS:  # cookie-banner kan dölja listan
            try:
                btn = page.get_by_role("button", name=label, exact=True)
                if btn.count():
                    btn.first.click(timeout=2000)
                    break
            except Exception:
                pass
        letters = page.locator(".Member-search__letter")
        for i in range(letters.count()):
            label = letters.nth(i).inner_text().strip()
            try:
                letters.nth(i).evaluate("e => e.click()")   # fungerar även om något täcker knappen
                page.wait_for_timeout(700)
                rows = page.evaluate(MEMBER_JS)
            except Exception as e:
                log.warning("  bokstaven %s gick inte att läsa (%s)", label, str(e).splitlines()[0][:80])
                continue
            for r in rows:
                if r["name"]:
                    sink[(r["name"], r["orgnr"])] = r
            log.info("  %s: %d medlemmar (totalt %d)", label, len(rows), len(sink))
        if not sink and debug_dir:  # spara vad webbläsaren såg, för felsökning
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / "medlemssida.html").write_text(page.content(), "utf-8")
            page.screenshot(path=str(debug_dir / "medlemssida.png"), full_page=True)
            log.warning("Inga medlemmar hittades – sparade data/medlemssida.html och .png för felsökning")
        browser.close()
    members = list(sink.values())
    log.info("Medlemslista: %d medlemmar (%d med webbplats)", len(members),
             sum(1 for m in members if m["domain"]))
    return members


def load_members_file(path: Path) -> list[dict]:
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text("utf-8"))
    with path.open(encoding="utf-8-sig") as f:
        sample = f.read(2048)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        rows = list(csv.DictReader(f, dialect=dialect))
    out = []
    for r in rows:
        r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
        name = r.get("name") or r.get("namn") or r.get("företag") or ""
        if name:
            out.append({"name": name, "domain": r.get("domain") or r.get("webbplats") or r.get("url") or "",
                        "orgnr": r.get("orgnr") or r.get("organisationsnummer") or ""})
    return out


# ---------------------------------------------------------------------------
# Sammanställning
# ---------------------------------------------------------------------------
def dedupe(postings: list[dict]) -> list[dict]:
    def completeness(p):
        return sum(bool(p.get(k)) for k in ("published", "deadline", "location"))
    postings = sorted(postings, key=lambda p: -completeness(p))
    out, by_url, by_title = [], set(), {}
    for p in postings:
        u = clean_url(p["url"]) if p["url"] else ""
        t = (p.get("_site") or norm_name(p["company"]), re.sub(r"\W+", "", p["title"].lower()))
        if (u and u in by_url):
            continue
        keep = by_title.get(t)
        # samma roll från två källor -> komplettera tomma fält. Två olika annonser med samma
        # titel i samma flöde (t.ex. två team som söker "System Developer") behålls båda.
        if keep and not (u and keep["url"] and keep.get("source") == p.get("source")
                         and keep.get("source") in ats.ADAPTERS):
            for k in ("published", "deadline", "location"):
                keep[k] = keep[k] or p.get(k)
            continue
        by_url.add(u)
        by_title[t] = p
        out.append(p)
    return out


def finalize(postings: list[dict], previous: list[dict]) -> list[dict]:
    first_seen = {clean_url(p["url"]): p.get("first_seen") for p in previous if p.get("url")}
    today = TODAY.isoformat()
    out = []
    for p in dedupe([dict(x) for x in postings]):
        for k in [k for k in p if k.startswith("_")]:
            p.pop(k)
        if p.get("deadline") and p["deadline"] < today:
            continue  # ansökningstiden har gått ut
        if p.get("published") and p["published"] > today:
            p["published"] = None
        fs = first_seen.get(clean_url(p["url"]))
        p["first_seen"] = fs or today
        p["is_new"] = fs is None and bool(previous)
        out.append(p)
    out.sort(key=lambda p: (p.get("published") or p["first_seen"]), reverse=True)
    return out


def company_list(members: list[dict], register: dict, jobs: list[dict]) -> list[dict]:
    """Medlemmar med hittad karriärsida: antal lediga tjänster totalt och antal juniora.
    Systerbolag med samma karriärsida visas en gång."""
    junior: dict[str, int] = {}
    for j in jobs:
        junior[j["company"]] = junior.get(j["company"], 0) + 1
    by_page: dict[str, dict] = {}
    for m in members:
        r = register.get(_reg_key(m["name"])) or {}
        pages = [u for u in re.split(r"[|\s]+", r.get("Karriärsida", "")) if u.startswith("http")]
        if not pages or not r.get("Status", "").startswith("OK"):
            continue
        total = int(r["Lediga tjänster"]) if (r.get("Lediga tjänster") or "").isdigit() else None
        c = {"company": m["name"], "url": pages[0], "open_total": total,
             "junior": junior.get(m["name"], 0), "ats": r.get("Jobbsystem", "")}
        key = clean_url(pages[0])
        old = by_page.get(key)
        if old is None or (c["junior"], c["open_total"] or 0) > (old["junior"], old["open_total"] or 0):
            by_page[key] = c
    return sorted(by_page.values(), key=lambda c: (-c["junior"], -(c["open_total"] or 0), c["company"].lower()))


def write_outputs(out_dir: Path, jobs: list[dict], meta: dict, companies: list[dict] | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "jobs": jobs, "companies": companies or []}
    (out_dir / "jobs.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    (out_dir / "data.js").write_text(
        "/* Genereras av scraper/scrape.py – redigera inte för hand */\nwindow.TS_DATA = "
        + json.dumps(payload, ensure_ascii=False) + ";\n", "utf-8")


# ---------------------------------------------------------------------------
# Register över karriärsidor (data/karriarsidor.csv) – sparas mellan körningar
# ---------------------------------------------------------------------------
REGISTER_COLS = ["Företag", "Webbplats", "Rätt webbplats", "Org.nr", "Karriärsida", "Källa", "Jobbsystem", "Status",
                 "Juniora annonser", "Lediga tjänster", "Senast skrapad", "Senast ändrad", "Ändrad vid senaste körning",
                 "Bekräftad",
                 # tekniska kolumner för snabbkollen – behöver inte röras
                 "Fingeravtryck", "ETag", "Last-Modified"]
MANUAL = "Manuellt"


def _reg_key(name: str) -> str:
    return norm_name(name) or (name or "").strip().lower()


def load_register(path: Path) -> dict[str, dict]:
    """Läser registret. Klarar både UTF-8 och den kodning Excel sparar med i Sverige."""
    if not path.exists():
        return {}
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    lines = text.splitlines()
    if not lines:
        return {}
    delim = ";" if lines[0].count(";") >= lines[0].count(",") else ","
    out = {}
    for row in csv.DictReader(lines, delimiter=delim):
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        name = row.get("Företag", "")
        if not name:
            continue
        # En länk som någon klistrat in själv räknas som manuell och skrivs aldrig över
        if row.get("Karriärsida") and not row.get("Källa"):
            row["Källa"] = MANUAL
        out[_reg_key(name)] = row
    return out


def save_register(path: Path, register: dict[str, dict]) -> None:
    rows = sorted(register.values(), key=lambda r: r.get("Företag", "").lower())
    with path.open("w", newline="", encoding="utf-8-sig") as f:   # BOM -> Excel visar å/ä/ö rätt
        w = csv.DictWriter(f, fieldnames=REGISTER_COLS, delimiter=";", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _split(v: str) -> list[str]:
    return [x.strip() for x in (v or "").split("|")] if (v or "").strip() else []


def known_for(row: dict | None) -> dict:
    if not row:
        return {}
    pages = [u for u in re.split(r"[|\s]+", row.get("Karriärsida", "")) if u.startswith("http")]
    return {"pages": pages, "fingerprints": [x for x in _split(row.get("Fingeravtryck")) if x],
            "etags": _split(row.get("ETag")), "lastmods": _split(row.get("Last-Modified")),
            "manual": row.get("Källa") == MANUAL, "confirmed": row.get("Bekräftad", ""),
            "ats": row.get("Jobbsystem", ""),
            "open_total": int(row["Lediga tjänster"]) if (row.get("Lediga tjänster") or "").isdigit() else None}


def update_register(register: dict, s: "CompanyScraper") -> None:
    key = _reg_key(s.m["name"])
    row = register.get(key, {})
    manual = row.get("Källa") == MANUAL
    today = TODAY.isoformat()
    active = [p for p in s.postings if not (p.get("deadline") and p["deadline"] < today)]
    fp = " | ".join(s.fingerprints)
    changed = bool(fp) and fp != row.get("Fingeravtryck", "")
    member_site = row.get("Webbplats", "") if row.get("Rätt webbplats") else s.m.get("domain", "")
    row.update({"Företag": s.m["name"], "Webbplats": member_site or row.get("Webbplats", ""),
                "Org.nr": s.m.get("orgnr", "") or row.get("Org.nr", ""),
                "Juniora annonser": str(len(active)),
                "Lediga tjänster": "" if s.open_total is None else str(s.open_total),
                "Senast skrapad": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "Ändrad vid senaste körning": "Ja" if changed else ("Nej" if fp else ""),
                "Fingeravtryck": fp, "ETag": " | ".join(s.etags), "Last-Modified": " | ".join(s.lastmods),
                "Jobbsystem": s.ats_name or row.get("Jobbsystem", "") if s.career_pages else ""})
    if changed or (fp and not row.get("Senast ändrad")):
        row["Senast ändrad"] = today
    if s.confirmed and s.ai and not s.unchanged:
        row["Bekräftad"] = today
    row.pop("Senast kontrollerad", None)
    if manual:
        row["Status"] = "OK" if s.used_known else "Sparad länk svarar inte – kontrollera"
    elif s.career_pages:
        row["Karriärsida"] = " | ".join(dict.fromkeys(s.career_pages))
        row["Källa"] = "Automatiskt"
        row["Status"] = (f"OK – webbplatsen i medlemslistan svarar inte, använde {s.guessed_site} (kontrollera)"
                         if s.guessed_site else "OK – hittad via webbsökning (kontrollera)" if s.found_by_search
                         else "OK")
    else:
        row["Karriärsida"] = ""
        row["Källa"] = ""
        row["Bekräftad"] = ""
        row["Status"] = ("Webbplatsen svarar inte" if "webbplats svarar inte" in s.notes
                         else "Webbplatsen blockerar automatiska besök" if s.blocked
                         else "Ingen karriärsida hittad – lägg gärna in länken för hand")
    register[key] = row


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--members-file", type=Path, help="CSV/JSON med name, domain, orgnr (ersätter skrapning av medlemslistan)")
    ap.add_argument("--out", type=Path, default=ROOT / "docs", help="mapp för data.js/jobs.json")
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data", help="cache för medlemslista och körlogg")
    ap.add_argument("--limit", type=int, default=0, help="bara de N första medlemmarna med webbplats (för test)")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--offset", type=int, default=0, help="hoppa över de N första medlemmarna med webbplats")
    ap.add_argument("--full", action="store_true",
                    help="skrapa allt på nytt även om karriärsidorna är oförändrade sedan sist")
    ap.add_argument("--no-ai", action="store_true", help="bara regler, utan Claude (även utan ANTHROPIC_API_KEY)")
    ap.add_argument("--no-browser", action="store_true", help="öppna inga sidor i webbläsaren (JavaScript)")
    ap.add_argument("--show-browser", action="store_true", help="visa webbläsaren medan medlemslistan hämtas")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    a.data_dir.mkdir(parents=True, exist_ok=True)
    cache = a.data_dir / "members.json"
    started = time.time()

    # 1. Medlemmar
    if a.members_file:
        members = load_members_file(a.members_file)
    else:
        try:
            members = fetch_members_playwright(show=a.show_browser, debug_dir=a.data_dir)
        except Exception as e:
            log.error("Kunde inte hämta medlemslistan (%s)", e)
            members = []
        if not members and cache.exists():
            log.warning("Använder sparad medlemslista från förra körningen")
            members = json.loads(cache.read_text("utf-8"))
    if not members:
        log.error("Ingen medlemslista – avbryter. Använd --members-file vid behov.")
        return 1
    if not a.members_file:
        cache.write_text(json.dumps(members, ensure_ascii=False, indent=1), "utf-8")

    # Registret med karriärsidor från tidigare körningar + förra körningens annonser
    reg_path = a.data_dir / "karriarsidor.csv"
    register = load_register(reg_path)
    for m in members:  # webbplats som lagts in för hand i registret
        r = register.get(_reg_key(m["name"]))
        if r and r.get("Rätt webbplats", "").strip() == "-":   # "-" = hoppa över företaget helt
            m["domain"], m["_skip"] = "", True
        elif r and r.get("Rätt webbplats"):          # ersätter medlemslistans (ofta gamla) adress
            m["domain"], m["_no_guess"] = r["Rätt webbplats"], True
        elif r and not m.get("domain") and r.get("Webbplats"):
            m["domain"] = r["Webbplats"]
    has_site = lambda m: not m.get("_skip") and (m.get("domain")
                                                 or known_for(register.get(_reg_key(m["name"]))).get("pages"))
    all_members = list(members)
    if a.limit or a.offset:
        members = [m for m in members if has_site(m)][a.offset: a.offset + a.limit if a.limit else None]

    ai = None
    if not a.no_ai and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from ai import AI
            ai = AI(a.data_dir / "ai_cache.json")
            log.info("AI: %s", ai.model)
        except ImportError as e:
            log.warning("AI avstängd – paketet anthropic saknas (%s)", e)
    elif not a.no_ai:
        log.warning("ANTHROPIC_API_KEY saknas – kör utan AI (bara regler)")
    renderer = Renderer(workers=4) if ai and not a.no_browser else None
    pagecache = PageCache(a.data_dir / "annonssidor_cache.json") if ai else None

    http = Http()
    postings: list[dict] = []
    notes: dict[str, list[str]] = {}
    career_found = 0
    cache_path = a.data_dir / "annonser_cache.json"
    try:
        postings_cache: dict[str, list] = json.loads(cache_path.read_text("utf-8")) if cache_path.exists() else {}
    except json.JSONDecodeError:
        postings_cache = {}
    unchanged = 0
    log.info("Karriärsidor sparade sedan tidigare: %d", sum(1 for r in register.values() if r.get("Karriärsida")))
    # Har stödet för rekryteringssystem utökats, eller bedömningen av annonser ändrats, sedan förra
    # körningen? Då gäller inte de sparade annonserna för oförändrade sidor – skrapa fullt en gång.
    from ai import CLASSIFY_VERSION
    ats_sig = hashlib.sha1(json.dumps([sorted(ats.ATS_HOSTS.items()), sorted(ats.ADAPTERS),
                                       CLASSIFY_VERSION]).encode()).hexdigest()[:12]
    try:
        prev_sig = json.loads((a.data_dir / "run_log.json").read_text("utf-8"))["meta"].get("ats_signature")
    except (OSError, ValueError, KeyError, TypeError):
        prev_sig = None
    if prev_sig != ats_sig and not a.full:
        log.info("Nya rekryteringssystem eller ändrad annonsbedömning sedan förra körningen – "
                 "fullständig skrapning den här gången")
        a.full = True

    # 2. Medlemmarnas webbplatser
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        scrapers = [CompanyScraper(http, m, known_for(register.get(_reg_key(m["name"]))),
                                   postings_cache.get(_reg_key(m["name"])), force_full=a.full,
                                   ai=ai, renderer=renderer, pagecache=pagecache)
                    for m in members if has_site(m)]
        futs = {pool.submit(s.run): s for s in scrapers}
        for i, fut in enumerate(as_completed(futs), 1):
            s = futs[fut]
            try:
                found = fut.result()
                postings.extend(found)
            except Exception as e:
                log.exception("fel hos %s", s.m["name"])
                s.notes.append(f"fel: {e}")
            if s.career_pages:
                career_found += 1
            update_register(register, s)
            postings_cache[_reg_key(s.m["name"])] = s.postings
            unchanged += s.unchanged
            if s.notes:
                notes[s.m["name"]] = s.notes
            log.info("  %s: %d träffar%s%s", s.m["name"], len(s.postings),
                     " (oförändrad sedan sist)" if s.unchanged else "",
                     f" ({', '.join(s.notes)})" if s.notes else "")
            if i % 25 == 0 or i == len(futs):
                log.info("Webbplatser: %d/%d klara, %d träffar hittills", i, len(futs), len(postings))
                save_register(reg_path, register)   # delresultat om körningen avbryts
                if ai:
                    ai.save()
    if renderer:
        renderer.close()

    for m in members:  # medlemmar utan webbplats kommer också med i registret
        key = _reg_key(m["name"])
        if not m.get("domain") and key not in register:
            register[key] = {"Företag": m["name"], "Org.nr": m.get("orgnr", ""),
                             "Status": "Webbplats saknas i medlemslistan – lägg gärna in den för hand"}
    save_register(reg_path, register)
    cache_path.write_text(json.dumps(postings_cache, ensure_ascii=False), "utf-8")
    if ai:
        ai.save()
    if pagecache:
        pagecache.save()
    log.info("Snabbkoll: %d karriärsidor oförändrade sedan sist (återanvände annonserna)", unchanged)
    log.info("Sparade %s (%d företag)", reg_path, len(register))

    # 3. Skriv ut
    prev_file = a.out / "jobs.json"
    previous = []
    if prev_file.exists():
        try:
            prev = json.loads(prev_file.read_text("utf-8"))
            previous = [] if prev.get("meta", {}).get("demo") else prev.get("jobs", [])
        except (json.JSONDecodeError, AttributeError):
            pass
    done = {_reg_key(m["name"]) for m in members}
    for m in all_members:
        key = _reg_key(m["name"])
        if key not in done:
            postings.extend(dict(p) for p in postings_cache.get(key) or [])
    jobs = finalize(postings, previous)
    meta = {
        "updated": dt.datetime.now().astimezone().isoformat(timespec="minutes"),
        "members_total": len(all_members),
        "members_with_website": sum(1 for m in all_members if m.get("domain")),
        "members_with_career_page": sum(1 for m in all_members
                                        if register.get(_reg_key(m["name"]), {}).get("Status", "").startswith("OK")),
        "members_scraped_this_run": len(members),
        "career_pages_unchanged": unchanged,
        "companies_with_jobs": len({j["company"] for j in jobs}),
        "jobs_total": len(jobs),
        "duration_min": round((time.time() - started) / 60, 1),
        "demo": False,
        "ats_signature": ats_sig,
    }
    if ai:
        meta["ai"] = {"model": ai.model, **ai.stats,
                      "pages_rendered": renderer.count if renderer else 0,
                      "job_pages_unchanged": pagecache.hits if pagecache else 0}
    write_outputs(a.out, jobs, meta, company_list(all_members, register, jobs))
    (a.data_dir / "run_log.json").write_text(
        json.dumps({"meta": meta, "notes": notes}, ensure_ascii=False, indent=1), "utf-8")
    log.info("Klart: %d annonser hos %d företag -> %s", len(jobs), meta["companies_with_jobs"], a.out)
    if ai:
        log.info("AI: %d anrop, %d från cache, %d fel, %d in- och %d ut-tokens", ai.stats["calls"],
                 ai.stats["cached"], ai.stats["errors"], ai.stats["input_tokens"], ai.stats["output_tokens"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
