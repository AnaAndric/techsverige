"""
Rekryteringssystem (ATS) – Teamtailor, Lever, Greenhouse m.fl.
===============================================================

Många medlemmar visar sina annonser i ett rekryteringssystem på en annan domän
(t.ex. bolaget.teamtailor.com) eller på en egen underdomän som drivs av systemet.
Skriptet följer bara sådana länkar när de hittas på medlemmens egen webbplats.

För system med öppna flöden (RSS/JSON) hämtas annonserna därifrån – det är både
snabbare och säkrare än att läsa HTML. Övriga system läses som vanliga sidor.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from html import unescape
from urllib.parse import urlparse, quote

# värd (eller del av värd) -> systemets namn
ATS_HOSTS = {
    "teamtailor.com": "Teamtailor", "varbi.com": "Varbi", "reachmee.com": "ReachMee",
    "jobylon.com": "Jobylon", "hr-manager.net": "HR Manager", "hrmanager.se": "HR Manager",
    "recman.no": "Recman", "recman.se": "Recman", "myworkdayjobs.com": "Workday",
    "myworkdaysite.com": "Workday", "lever.co": "Lever", "greenhouse.io": "Greenhouse",
    "ashbyhq.com": "Ashby", "smartrecruiters.com": "SmartRecruiters", "recruitee.com": "Recruitee",
    "jobs.personio.de": "Personio", "jobs.personio.com": "Personio", "emply.com": "Emply",
    "workable.com": "Workable", "breezy.hr": "Breezy", "successfactors.com": "SuccessFactors",
    "successfactors.eu": "SuccessFactors", "icims.com": "iCIMS", "taleo.net": "Taleo",
    "softgarden.io": "softgarden", "webcruiter.com": "Webcruiter", "jobteaser.com": "JobTeaser",
    "talentech.com": "Talentech", "career.jobvite.com": "Jobvite", "jobs.jobvite.com": "Jobvite",
    "oraclecloud.com": "Oracle", "amazon.jobs": "Amazon Jobs", "eightfold.ai": "Eightfold",
    "njoyn.com": "Njoyn", "bamboohr.com": "BambooHR", "onecruiter.com": "Onecruiter",
    "workspacerecruit.com": "Workspace Recruit", "weselect.com": "Weselect", "jobbet.se": "Jobbet",
    "rippling.com": "Rippling", "zohorecruit.eu": "Zoho Recruit", "zohorecruit.com": "Zoho Recruit",
    "recman.io": "Recman", "pinpointhq.com": "Pinpoint", "phenompeople.com": "Phenom",
}
# system där startsidan listar annonser från alla kunder – är aldrig ett bolags karriärsida
SHARED_ROOTS = {"apply.workspacerecruit.com"}
# kännetecken i HTML för system som körs på bolagets egen domän
ATS_MARKERS = {"teamtailor-cdn.com": "Teamtailor", "teamtailor.com/": "Teamtailor",
               "varbi.com": "Varbi", "reachmee.com": "ReachMee", "jobylon.com": "Jobylon",
               "pcsxconfig": "Eightfold", "static.vscdn.net": "Eightfold"}
# Eightfold körs ofta på bolagets egen domän, t.ex. jobs.ericsson.com/careers?domain=ericsson.com
EIGHTFOLD_RE = re.compile(r"^/careers(/job/\d+[^/]*)?/?$")


def ats_of_url(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    for h, name in ATS_HOSTS.items():
        if host == h or host.endswith("." + h):
            return name
    p = urlparse(url)
    if EIGHTFOLD_RE.match(p.path) and re.search(r"(^|&)domain=", p.query):
        return "Eightfold"
    if (host == "capgemini.com" or host.endswith(".capgemini.com")) and "/job-search" in p.path:
        return "Capgemini"   # egen jobbsök med öppet API
    return None


# Workspace Recruit länkar annonser via app.-värden, som spärrar allt i robots.txt –
# samma annons finns publikt på apply.workspacerecruit.com/job/<id>
WSR_AD_RE = re.compile(r"^https?://app\.workspacerecruit\.com/api/v1/ad/(\d+)/?$", re.I)


def public_job_url(url: str) -> str:
    m = WSR_AD_RE.match(url)
    return f"https://apply.workspacerecruit.com/job/{m.group(1)}" if m else url


def is_shared_root(url: str) -> bool:
    p = urlparse(url)
    return p.netloc.lower() in SHARED_ROOTS and p.path in ("", "/")


def ats_of_html(html: str) -> str | None:
    low = html[:400_000].lower()
    for marker, name in ATS_MARKERS.items():
        if marker in low:
            return name
    return None


def _text(html: str, limit: int = 6000) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", s)
    s = unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()[:limit]


def _job(title, url, location="", country="", published=None, deadline=None, description="",
         employment_type=None) -> dict:
    return {"title": " ".join(str(title or "").split()), "url": url, "location": location or "",
            "country": country or "", "published": published, "deadline": deadline,
            "description": description or "", "employment_type": employment_type}


# ---------------------------------------------------------------------------
# Adaptrar. Var och en får http-klienten och en URL som pekar på systemet och
# returnerar en lista med annonser, eller None om URL:en inte gick att använda.
# ---------------------------------------------------------------------------
def teamtailor(http, url: str, norm_date) -> list[dict] | None:
    p = urlparse(url)
    _, status, body = http.get(f"{p.scheme}://{p.netloc}/jobs.rss")
    if status != 200 or "<rss" not in body[:500]:
        return None
    try:
        root = ET.fromstring(body.encode("utf-8"))
    except ET.ParseError:
        return None
    ns = {"tt": "https://teamtailor.com/locations"}
    out = []
    for it in root.iter("item"):
        cities, countries = [], []
        for loc in it.findall(".//tt:location", ns):
            city = (loc.findtext("tt:city", "", ns) or loc.findtext("tt:name", "", ns) or "").strip()
            cities.append(city)
            countries.append((loc.findtext("tt:country", "", ns) or "").strip())
        remote = (it.findtext("remoteStatus") or "").strip()
        location = ", ".join(dict.fromkeys(c for c in cities if c))
        if remote == "fully":
            location = (location + ", Distans").strip(", ")
        # flera orter: Sverige räcker om någon av dem ligger i Sverige
        country = next((c for c in countries if c.lower() in ("sweden", "sverige")), countries[0] if countries else "")
        out.append(_job(it.findtext("title"), (it.findtext("link") or "").strip(), location, country,
                        norm_date(it.findtext("pubDate")), None, _text(it.findtext("description") or "")))
    return out


def lever(http, url: str, norm_date) -> list[dict] | None:
    slug = urlparse(url).path.strip("/").split("/")[0]
    if not slug:
        return None
    data = http.get_json(f"https://api.lever.co/v0/postings/{quote(slug)}?mode=json")
    if not isinstance(data, list):
        return None
    out = []
    for j in data:
        cat = j.get("categories") or {}
        out.append(_job(j.get("text"), j.get("hostedUrl"), cat.get("location") or "", j.get("country") or "",
                        norm_date(j.get("createdAt")), None, (j.get("descriptionPlain") or "")[:6000],
                        cat.get("commitment")))
    return out


def greenhouse(http, url: str, norm_date) -> list[dict] | None:
    p = urlparse(url)
    parts = p.path.strip("/").split("/")
    token = parts[0] if parts and parts[0] not in ("embed", "") else dict(
        x.split("=", 1) for x in p.query.split("&") if "=" in x).get("for", "")
    if not token:
        return None
    data = http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{quote(token)}/jobs?content=true")
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    return [_job(j.get("title"), j.get("absolute_url"), (j.get("location") or {}).get("name", ""), "",
                 norm_date(j.get("first_published") or j.get("updated_at")), None,
                 _text(unescape(j.get("content") or ""))) for j in data["jobs"]]


def ashby(http, url: str, norm_date) -> list[dict] | None:
    name = urlparse(url).path.strip("/").split("/")[0]
    if not name:
        return None
    data = http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{quote(name)}")
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    out = []
    for j in data["jobs"]:
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        out.append(_job(j.get("title"), j.get("jobUrl"), j.get("location") or addr.get("addressLocality", ""),
                        addr.get("addressCountry", ""), norm_date(j.get("publishedAt")), None,
                        (j.get("descriptionPlain") or "")[:6000], j.get("employmentType")))
    return out


def smartrecruiters(http, url: str, norm_date) -> list[dict] | None:
    company = urlparse(url).path.strip("/").split("/")[0]
    if not company:
        return None
    data = http.get_json(f"https://api.smartrecruiters.com/v1/companies/{quote(company)}/postings?limit=100")
    if not isinstance(data, dict) or "content" not in data:
        return None
    out = []
    for j in data["content"]:
        loc = j.get("location") or {}
        out.append(_job(j.get("name"), f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}",
                        loc.get("city") or "", loc.get("country") or "", norm_date(j.get("releasedDate"))))
    return out


def recruitee(http, url: str, norm_date) -> list[dict] | None:
    host = urlparse(url).netloc
    data = http.get_json(f"https://{host}/api/offers")
    if not isinstance(data, dict) or "offers" not in data:
        return None
    return [_job(j.get("title"), j.get("careers_url"), j.get("city") or j.get("location") or "",
                 j.get("country") or "", norm_date(j.get("published_at")), None,
                 _text(j.get("description") or "")) for j in data["offers"]]


def workday(http, url: str, norm_date) -> list[dict] | None:
    """Workday listar ofta tusentals tjänster världen över – vi söker bara på Sverige
    och hämtar detaljer bara för tjänster vars ort kan ligga i Sverige."""
    p = urlparse(url)
    tenant = p.netloc.split(".")[0]
    segs = [s for s in p.path.split("/") if s and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", s)]
    if not segs:
        return None
    site = segs[0]
    api = f"https://{p.netloc}/wday/cxs/{tenant}/{site}"
    listed, seen = [], set()
    for term in ("Sweden", "Sverige"):
        for offset in range(0, 200, 20):
            data = http.get_json(f"{api}/jobs", {"appliedFacets": {}, "limit": 20, "offset": offset,
                                                 "searchText": term})
            if not isinstance(data, dict):
                break
            posts = data.get("jobPostings") or []
            for j in posts:
                path = j.get("externalPath") or ""
                if path and path not in seen:
                    seen.add(path)
                    listed.append(j)
            if len(posts) < 20:
                break
    if not listed and not seen:
        return None
    out = []
    for j in listed:
        path = j["externalPath"]
        # sökvägen börjar med orten, t.ex. /job/Stockholm/… eller /job/Sweden-Remote/…
        where = f"{j.get('locationsText', '')} {path.split('/')[2] if path.count('/') > 2 else ''}"
        if not re.search(r"swed|sverige|stockholm|g(ö|o)teborg|malm(ö|o)|kista|solna|link(ö|o)ping|"
                         r"uppsala|lund|v(ä|a)ster(å|a)s|ume(å|a)|lule(å|a)|\d+ locations", where, re.I):
            continue
        detail = http.get_json(f"{api}{path}") or {}
        info = detail.get("jobPostingInfo") or {}
        country = ((info.get("country") or {}).get("descriptor") or "")
        out.append(_job(j.get("title"), info.get("externalUrl") or f"https://{p.netloc}/{site}{path}",
                        info.get("location") or j.get("locationsText") or "", country,
                        norm_date(info.get("startDate") or j.get("postedOn")),
                        norm_date(info.get("endDate")), _text(info.get("jobDescription") or "")))
    return out


def eightfold(http, url: str, norm_date) -> list[dict] | None:
    """Eightfold (PCSX) – söker på Sverige och hämtar beskrivningen för varje tjänst,
    eftersom själva annonssidorna bara visar innehåll med JavaScript."""
    p = urlparse(url)
    q = dict(x.split("=", 1) for x in p.query.split("&") if "=" in x)
    domain = q.get("domain") or ".".join(p.netloc.split(".")[-2:])
    base = f"https://{p.netloc}/api/pcsx"
    listed, seen = [], set()
    for start in range(0, 500, 10):
        data = http.get_json(f"{base}/search?domain={quote(domain)}&location=Sweden&start={start}")
        if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
            break
        posts = data["data"].get("positions") or []
        for j in posts:
            if j.get("id") and j["id"] not in seen:
                seen.add(j["id"])
                listed.append(j)
        if len(posts) < 10 or len(seen) >= (data["data"].get("count") or 0):
            break
    if not seen:
        return None
    out = []
    for j in listed:
        locs = j.get("locations") or []
        # "Stockholm,Stockholm,Sweden" – Sverige räcker om någon av orterna ligger i Sverige
        swe = [l for l in locs if l.lower().endswith(("sweden", "sverige"))]
        loc = (swe or locs or [""])[0]
        parts = [x.strip() for x in loc.split(",") if x.strip()]
        if not swe and j.get("workLocationOption") != "remote":
            continue
        detail = http.get_json(f"{base}/position_details?position_id={j['id']}&domain={quote(domain)}") or {}
        info = detail.get("data") if isinstance(detail.get("data"), dict) else {}
        location = parts[0] if parts else ""
        if j.get("workLocationOption") == "remote":
            location = (location + ", Distans").strip(", ")
        out.append(_job(j.get("name"), f"https://{p.netloc}{j.get('positionUrl')}?domain={domain}",
                        location, parts[-1] if parts else "", norm_date(j.get("postedTs")), None,
                        _text(info.get("jobDescription") or "")))
    return out


def njoyn(http, url: str, norm_date) -> list[dict] | None:
    """Njoyn (t.ex. CGI) – sökformuläret skickas med land = Sverige, 50 träffar per sida.
    Listan saknar beskrivning, så annonssidan hämtas för varje svensk tjänst."""
    p = urlparse(url)
    q = {k.lower(): v for k, v in (x.split("=", 1) for x in p.query.split("&") if "=" in x)}
    clid = q.get("clid")
    if not clid:
        return None
    lang = q.get("lang", "1")
    base = f"https://{p.netloc}{p.path or '/corp/xweb/xweb.asp'}"
    out, seen = [], set()
    for pn in range(1, 11):
        form = (f"Inp_country=SE&inp_Country_CGI=SE&pn={pn}&searchFilled=1&Inp_Keywords=&inp_City="
                f"&inp_Province=&Inp_XWEB_JobListing_CustomFields=").encode()
        _, status, body = http.get(f"{base}?NTKN=c&clid={clid}&Page=joblisting&lang={lang}", data=form,
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        if status != 200:
            break
        rows = re.findall(r"(?s)<tr HasMultipleLocations[^>]*>\s*<td><a href='([^']+)'>[^<]*</a></td>\s*"
                          r"<td>(.*?)</td>\s*<td>.*?</td>\s*<td>(.*?)</td>\s*<td name='CountryCell'>(.*?)</td>", body)
        new = 0
        for href, title, city, country in rows:
            link = f"https://{p.netloc}{p.path.rsplit('/', 1)[0]}/{unescape(href)}"
            if link in seen:
                continue
            seen.add(link)
            new += 1
            text = ""
            if re.search(r"(?i)swed|sverige", country):   # annonstexten bara för svenska tjänster
                _, st, page = http.get(link)
                i = page.find("job-posting-wrapper")
                text = _text(page[i:] if i >= 0 else page) if st == 200 else ""
            out.append(_job(_text(title), link, _text(city), _text(country), description=text))
        if len(rows) < 50 or not new:
            break
    return out if seen else None


def _qs(url: str) -> dict:
    return {k.lower(): v for k, v in (x.split("=", 1) for x in urlparse(url).query.split("&") if "=" in x)}


def _ms_date(value, norm_date):
    """'/Date(1790000000000+0200)/' -> YYYY-MM-DD (negativa värden = inget datum)."""
    m = re.search(r"Date\((\d+)", str(value or ""))
    return norm_date(int(m.group(1))) if m else None


def hrmanager(http, url: str, norm_date) -> list[dict] | None:
    customer = _qs(url).get("customer")
    if not customer:
        return None
    data = http.get_json(f"https://api.hr-manager.net/JobPortal.svc/{quote(customer)}/PositionList/json/")
    if not isinstance(data, dict) or not isinstance(data.get("Items"), list):
        return None
    out = []
    for j in data["Items"]:
        where = j.get("WorkPlace") or ", ".join(x.get("Name", "") for x in j.get("PositionLocationMultiSelection") or [])
        parts = [x.strip() for x in where.split(",") if x.strip()]
        # "Malmö, Sverige" – sista delen är land bara om det ser ut som ett land
        country = parts.pop() if parts and re.fullmatch(r"(?i)sverige|sweden|norge|norway|danmark|denmark|"
                                                        r"finland|suomi|island|iceland|.*land", parts[-1]) else ""
        out.append(_job(j.get("Name"), j.get("AdvertisementUrl") or j.get("ApplicationFormUrl"),
                        ", ".join(parts), country,
                        _ms_date(j.get("Published") or j.get("Created"), norm_date),
                        _ms_date(j.get("ApplicationDue"), norm_date), _text(j.get("ShortDescription") or "")))
    return out


def varbi(http, url: str, norm_date) -> list[dict] | None:
    """Varbi har ett RSS-flöde per kund med hela annonstexten (men utan ort)."""
    host = urlparse(url).netloc
    _, status, body = http.get(f"https://{host}/se/what:rssfeed/")
    if status != 200 or "<rss" not in body[:1000]:
        return None
    try:
        root = ET.fromstring(body.encode("utf-8"))
    except ET.ParseError:
        return None
    return [_job(it.findtext("title"), (it.findtext("link") or "").strip(), "", "",
                 norm_date(it.findtext("pubDate")), None, _text(it.findtext("description") or ""))
            for it in root.iter("item")]


def reachmee(http, url: str, norm_date) -> list[dict] | None:
    """ReachMee saknar flöde – listan på .../ext/{iid}/{kund}/main är en vanlig HTML-tabell."""
    p = urlparse(url)
    m = re.match(r"/ext/(I\d+)/(\d+)/", p.path)
    q = _qs(url)
    if not m or not q.get("validator"):
        return None
    base = f"https://{p.netloc}/ext/{m.group(1)}/{m.group(2)}"
    site, lang = q.get("site", "1"), q.get("lang", "SE")
    _, status, body = http.get(f"{base}/main?site={site}&validator={q['validator']}&lang={lang}")
    if status != 200 or "jobsTable" not in body:
        return None
    table = body[body.find("jobsTable"):]
    out = []
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", table[: table.find("</table>")]):
        cells = re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)
        a = re.search(r"(?is)<a[^>]+href=[\"']([^\"']*job_id=(\d+)[^\"']*)[\"'][^>]*>(.*?)</a>", row)
        if not a or len(cells) < 2:
            continue
        out.append(_job(_text(a.group(3)), f"{base}/job?site={site}&lang={lang}&validator={q['validator']}"
                        f"&job_id={a.group(2)}", _text(cells[1]), "", None,
                        norm_date(_text(cells[2])) if len(cells) > 2 else None))
    return out


def bamboohr(http, url: str, norm_date) -> list[dict] | None:
    host = urlparse(url).netloc
    data = http.get_json(f"https://{host}/careers/list")
    if not isinstance(data, dict) or not isinstance(data.get("result"), list):
        return None
    out = []
    for j in data["result"]:
        d = ((http.get_json(f"https://{host}/careers/{j.get('id')}/detail") or {}).get("result") or {}).get("jobOpening") or {}
        loc = d.get("location") or j.get("location") or {}
        country = (d.get("atsLocation") or j.get("atsLocation") or {}).get("country") or loc.get("addressCountry") or ""
        out.append(_job(j.get("jobOpeningName"), f"https://{host}/careers/{j.get('id')}", loc.get("city") or "",
                        country, norm_date(d.get("datePosted")), None, _text(d.get("description") or ""),
                        j.get("employmentStatusLabel")))
    return out


def oracle(http, url: str, norm_date) -> list[dict] | None:
    """Oracle Recruiting Cloud – listan filtreras på land SE, detaljer hämtas bara för dem."""
    p = urlparse(url)
    m = re.search(r"/sites/([A-Za-z0-9_]+)", p.path + "#" + p.fragment)
    site = m.group(1) if m else "CX_1"
    api = f"https://{p.netloc}/hcmRestApi/resources/latest"
    out, seen = [], set()
    for offset in range(0, 2000, 200):
        data = http.get_json(f"{api}/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations"
                             f"&finder=findReqs;siteNumber={site},limit=200,offset={offset}")
        items = (data or {}).get("items") or []
        if not items:
            break
        reqs = items[0].get("requisitionList") or []
        for j in reqs:
            if j.get("Id") in seen:
                continue
            seen.add(j.get("Id"))
            countries = [j.get("PrimaryLocationCountry")] + [x.get("CountryCode") for x in j.get("secondaryLocations") or []]
            if "SE" not in countries:
                continue
            d = http.get_json(f'{api}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true'
                              f'&finder=ById;Id="{j["Id"]}",siteNumber={site}') or {}
            d = (d.get("items") or [{}])[0]
            text = " ".join(_text(d.get(k) or "") for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
                                                             "ExternalQualificationsStr"))
            out.append(_job(j.get("Title"), f"https://{p.netloc}/hcmUI/CandidateExperience/sv/sites/{site}/job/{j['Id']}",
                            (j.get("PrimaryLocation") or "").split(",")[0], "Sweden", norm_date(j.get("PostedDate")),
                            norm_date(d.get("ExternalPostedEndDate")), text.strip()))
        if len(reqs) < 200:
            break
    return out if seen else None


def capgemini(http, url: str, norm_date) -> list[dict] | None:
    """Capgemini (inkl. Sogeti, Invent, Engineering) – eget API bakom jobbsöken på capgemini.com.
    brand=... i karriärsidans adress (kommaseparerat) begränsar till vissa varumärken."""
    from urllib.parse import unquote_plus
    q = _qs(url)
    country = q.get("country_code", "se-en")
    brands = {unquote_plus(b).strip().lower() for b in q.get("brand", "").split(",") if b.strip()}
    data = http.get_json(f"https://cg-jobstream-api.azurewebsites.net/api/job-search?country_code={quote(country)}&size=1000")
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return None
    out = []
    for j in data["data"]:
        if brands and (j.get("brand") or "").lower() not in brands:
            continue
        out.append(_job(j.get("title"), j.get("apply_job_url"), j.get("location") or "", "Sweden" if country.startswith("se") else "",
                        norm_date(j.get("indexed_at")), None,
                        (j.get("description_stripped") or _text(j.get("description") or ""))[:6000],
                        j.get("contract_type")))
    return out


ADAPTERS = {"Teamtailor": teamtailor, "Lever": lever, "Greenhouse": greenhouse, "Ashby": ashby,
            "SmartRecruiters": smartrecruiters, "Recruitee": recruitee, "Workday": workday,
            "Eightfold": eightfold, "Njoyn": njoyn, "HR Manager": hrmanager, "Varbi": varbi,
            "ReachMee": reachmee, "BambooHR": bamboohr, "Oracle": oracle, "Capgemini": capgemini}


def fetch_feed(http, ats: str, url: str, norm_date) -> list[dict] | None:
    fn = ADAPTERS.get(ats)
    if not fn:
        return None
    try:
        return fn(http, url, norm_date)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, ET.ParseError):
        return None
