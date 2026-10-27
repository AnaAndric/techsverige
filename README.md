# Juniora jobb hos TechSveriges medlemmar

En enkel webbapp som listar internships, traineeprogram, examensarbeten, sommarjobb,
studentjobb och juniora tjänster hos TechSveriges medlemsföretag – plus ett skript som
uppdaterar listan automatiskt den 1:a varje månad.

```
scraper/scrape.py            skriptet (Python 3.9+, Playwright + Anthropic SDK)
scraper/ai.py                frågorna till Claude Haiku (karriärsida, annonser, junior eller inte)
scraper/ats.py               flöden från rekryteringssystem (Teamtailor, Lever, Workday …)
docs/index.html              appen (öppna i webbläsaren eller publicera med GitHub Pages)
docs/data.js                 data som appen läser – skrivs över av skriptet
docs/jobs.json, jobs.csv     samma data som JSON och CSV (Excel-vänlig, semikolon)
data/members.json            senast hämtade medlemslista (används om hämtningen misslyckas)
data/run_log.json            körlogg: vilka företag saknade karriärsida, fel osv.
data/ai_cache.json           sparade AI-svar (samma innehåll -> inget nytt anrop)
data/annonssidor_cache.json  sparade annonssidor med ETag/Last-Modified
.github/workflows/…          månadsschemat
tests/test_offline.py        test mot en påhittad lokal sajt
```

`docs/data.js` innehåller **exempeldata** (påhittade företag) tills skriptet körts första gången.
Appen visar en gul banderoll så länge det är exempeldata.

## Kom igång (en gång)

1. Skapa ett repo på GitHub och ladda upp hela mappen.
2. **Settings → Secrets and variables → Actions**: lägg till `ANTHROPIC_API_KEY`
   (utan nyckel körs skriptet med enbart regler, vilket ger betydligt sämre träffar).
3. **Settings → Pages**: Source = *Deploy from a branch*, Branch = `main`, mapp = `/docs`.
   Appen hamnar på `https://<konto>.github.io/<repo>/`.
4. **Actions → Uppdatera juniora jobb → Run workflow** för en första körning
   (tar ca 20–60 min för hela medlemslistan).

Därefter körs den automatiskt kl. 07 den 1:a varje månad och appen uppdateras av sig själv.

## Köra lokalt

```bash
pip install -r scraper/requirements.txt
python -m playwright install chromium   # kan hoppas över, se nedan
export ANTHROPIC_API_KEY=...             # PowerShell: $env:ANTHROPIC_API_KEY="..."
python scraper/scrape.py --limit 30      # snabbtest på 30 medlemmar
python scraper/scrape.py                 # alla
```

Öppna sedan `docs/index.html`.

**Om `playwright install` får timeout** (vanligt på jobbnätverk som blockerar nedladdningen):
hoppa över det steget. Skriptet använder då automatiskt Microsoft Edge eller Google Chrome
som redan finns på datorn. Vill du tvinga fram en viss: `$env:BROWSER_CHANNEL="msedge"` (PowerShell).

Har du medlemslistan internt (t.ex. export från CRM) kan du hoppa över webbskrapningen
av listan: `python scraper/scrape.py --members-file medlemmar.csv`
(kolumner `name;domain;orgnr`, komma eller semikolon).

Utan GitHub: schemalägg samma kommando med cron (`0 7 1 * *`) eller Windows Schemaläggaren.

## Register över karriärsidor – `data/karriarsidor.csv`

Varje körning sparar alla medlemsföretag med länk till deras karriärsida, så att skriptet
inte behöver leta upp sidan nästa gång. Filen öppnas direkt i Excel (semikolon, å/ä/ö fungerar)
och kopieras även till `docs/` så att appen länkar till den.

| Kolumn | Betydelse |
|---|---|
| Företag, Webbplats, Org.nr | från medlemslistan |
| Karriärsida | länk(ar) som skrapas – flera skiljs med ` \| ` |
| Källa | *Automatiskt* (hittad av skriptet) eller *Manuellt* (inlagd av er) |
| Jobbsystem | rekryteringssystem som annonserna ligger i, t.ex. Teamtailor |
| Status | OK, *Ingen karriärsida hittad*, *Webbplatsen svarar inte* … |
| Juniora annonser | antal aktuella träffar vid senaste körningen |
| Senast skrapad | när sidan senast lästes |
| Senast ändrad | senaste datum då annonserna på sidan ändrades |
| Ändrad vid senaste körning | Ja/Nej |
| Bekräftad | datum då AI senast bekräftade att sidan är en karriärsida |
| Fingeravtryck, ETag, Last-Modified | tekniska värden för snabbkollen – rör inte |

**Lägga in en länk för hand:** klistra in adressen i *Karriärsida* (och vid behov *Webbplats*)
för ett företag där skriptet inte hittade något, töm *Källa* och spara. Skriptet räknar den
som manuell och skriver aldrig över den.

### Snabbkoll – bara ändrade sidor skrapas
Vid nästa körning frågar skriptet först varje sparad karriärsida om den ändrats
(`If-None-Match`/`If-Modified-Since`). Svarar servern *oförändrad*, eller är sidans
fingeravtryck detsamma (annonslänkar och strukturerad jobbdata – inte nyheter, cookies
eller sidfot), återanvänds förra körningens annonser från `data/annonser_cache.json` och
inga fler sidor hämtas för det företaget. Bara ändrade eller nya sidor skrapas fullt ut,
vilket gör månadskörningen betydligt snabbare.

Vill du tvinga fram en fullständig skrapning: `python scraper/scrape.py --full`

## Hur skriptet hittar annonserna

1. **Medlemmar** – listsidan på techsverige.se visar en bokstav i taget (med JavaScript), så skriptet
   klickar igenom alla bokstäver i en headless webbläsare (namn, domän, org.nr).
2. **Karriärsidan** – tas från registret om den är bekräftad de senaste 90 dagarna (eller inlagd för hand).
   Annars samlas kandidater från startsidans länkar, `sitemap.xml` och vanliga adresser (`/karriar`,
   `/careers` …) och **Claude Haiku** väljer den sida som troligast listar lediga tjänster.
3. **Rekryteringssystem** – länkar medlemmens webbplats till t.ex. Teamtailor, Varbi, ReachMee, Workday,
   Lever eller Greenhouse följs. Har systemet ett öppet flöde (Teamtailor-RSS, Lever/Greenhouse/Ashby/
   SmartRecruiters/Recruitee-API, Workday, Eightfold) hämtas annonserna därifrån; Workday och Eightfold
   söks bara på Sverige. Är den sparade karriärsidan ett sådant flöde läses det direkt, även om
   bolagets startsida blockerar automatiska besök (t.ex. Ericsson: `jobs.ericsson.com/careers?domain=ericsson.com`).
   Jobbsajter som Platsbanken, LinkedIn och Indeed används inte.
4. **Karriärsidan läses** – strukturerad data (schema.org/JobPosting) plockas direkt. Haiku pekar ut
   vilka länkar som är enskilda annonser, sållar dem på titeln (junior / oklart / inte junior) och säger
   om man behöver gå vidare (t.ex. "Lediga tjänster" under "Arbeta hos oss"). Sidor som bara visar
   innehåll med JavaScript öppnas i en headless webbläsare.
5. **Annonserna bedöms** – juniora och oklara annonser (max 30 per företag) läses och Haiku avgör om
   tjänsten är junior (även "Software Engineer, 0–2 års erfarenhet"), typ, ort, land, sista
   ansökningsdag och startperiod, och skriver en kort motivering (`reason` i datan). Annonser vars
   period redan passerat ("praktik hösten 2024") tas bort.
6. **Bara Sverige** – tjänster med ort/land utomlands tas bort.
7. Dubbletter slås ihop, utgångna annonser tas bort, och nya sedan förra körningen märks **NY**.

Utan `ANTHROPIC_API_KEY` (eller med `--no-ai`) används de gamla reglerna: nyckelord i länktexten.

### Vad AI kostar
Modellen är Claude Haiku (`claude-haiku-5-5`). Varje svar sparas i `data/ai_cache.json` med en nyckel
som bygger på exakt det innehåll som skickades – oförändrade sidor kostar alltså inget nästa gång.
Annonssidor hämtas med `If-None-Match`/`If-Modified-Since` och läses inte om när servern svarar
*oförändrad*. Körloggen (`data/run_log.json` → `meta.ai`) visar antal anrop och tokens.

Skriptet följer `robots.txt`, håller en paus per webbplats och läser bara publika sidor.

## Kända begränsningar

- Webbplatser som blockerar automatiska besök hoppas över (status *Webbplatsen blockerar automatiska
  besök* i registret) – lägg då in karriärsidan för hand om den går att läsa.
- Rekryteringssystem som bara visar annonser i en inbäddad ruta med inloggning/cookie-krav kan missas.
- Per företag läses högst 30 annonser utan tydlig junior-titel; stora koncerner kan ha fler.
- AI kan ta fel. Motiveringen (`reason`) gör det lätt att se varför en annons kom med.
- Publiceringsdatum saknas ofta på företagens egna sidor; appen visar då "–".

## Varumärke

Appen använder TechSveriges palett (grafisk profil 2.3): Svart `#1E1E1E`, Grön `#DCFE8C`,
Varmgrå `#DCDCBE`, Mörkgrå `#968C78`, Vit `#FFFFFF`, Blå `#008CFF`, Röd `#FF6E3C`.
Värdena ligger som variabler överst i `docs/index.html`. Grön, blå och röd används som ytor
och markeringar, inte som text på vitt, eftersom kontrasten då blir för låg. Typsnittet är
tills vidare Inter – byt `--font` om profilen anger ett annat.
