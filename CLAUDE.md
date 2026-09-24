# Supnet – Familie Oltedal

## Prosjektbeskrivelse
En PWA (Progressive Web App) familieside for familien Oltedal. Appen er på norsk og kjøres lokalt/hostet som statiske HTML-filer.

## Struktur
- `index.html` – Innloggingsside med brukervalg og PIN-kode
- `dashboard.html` – Hoveddashboard med vær, kalender og meldingstavle
- `manifest.json` – PWA-manifest (navn, ikoner, tema)
- `sw.js` – Service worker for offline-støtte og caching
- `icons/` – App-ikoner (icon-192.png, icon-512.png) – mangler i mappen ennå

## Brukere
| Bruker  | Emoji | PIN  |
|---------|-------|------|
| Daniel  | 🦒    | 0000 |
| Hanna   | 🦁    | 0000 |
| Frida   | 🦊    | 0000 |
| Magnus  | 🐧    | 0000 |

## Teknisk
- Rent HTML/CSS/JavaScript – ingen rammeverk, ingen build-steg
- Font: Nunito (Google Fonts)
- Vær-API: api.met.no (Yr) – koordinater satt til Fagertunveien, Jar (lat: 59.9324, lon: 10.6045)
- Data (kalender, meldinger) lagres kun i minnet – ikke persistent mellom refreshes ennå
- Service worker cacher: `/`, `index.html`, `dashboard.html`, `manifest.json`, og Nunito-fonten
- Vær-API-kall caches ikke (alltid ferske data)

## Designsystem
- Bakgrunn: `#0f1117`
- Kort: `#1a1d27`
- Aksent: `#f0c040` (gul)
- Tekst: `#e8e8f0`
- Dempet tekst: `#666880`
- Border-radius: `20px`
- Bruker-farge (standard): `#3a7bd5` (blå)

## Jita (EVE Online station trading) – egen app i `jita/`
- Eget Vercel-prosjekt `jita-eve` (rot `jita/`, deploy `cd jita && vercel --prod --yes`) → https://jita-eve.vercel.app. Familiesiden er urørt.
- Spesifikasjon: `jita/jita-spec.md` (v3 + rettelser). Drift, hemmeligheter, avvik fra spec og feillogg: `jita/README.md` – **les den før du endrer noe i jita/**.
- Datalager: Supabase «Supnet», skjema `jita`. Robot i GitHub Actions (`.github/workflows/jita.yml`), men pg_cron er primær klokke (`jita-vakt*` starter jobbene via `/api/scan?fallback=1&job=…`). EVE-synk (`jita-eve-sync`) hver time via `/api/character`.
- Regler og terskler ligger i `jita/sql/002_judge.sql` (`jita.judge_rows`) og `jita.profile.thresholds`. Endre dem bare med begrunnelse i README «Avvik fra spec».
- **Industri-fanen** (`jita/industry.html`): produksjonsmarginer for T1-varer i Ylandoki → Jita. Formler i `jita/scripts/industry.py` (testet av `test_industry.py`), robot i `jita/scripts/ingest_industry.py` (Actions-jobb `industry`, daglig 05:40 UTC), tabeller i `jita/sql/005_industry.sql`. Brief: `jita/industri-brief.md`.
- **Mining-seksjonen** (nederst i samme fane): ISK per time per malm, refine mot rå-salg, og hvilke mineraler produksjonsforslagene spiser. Formler i `jita/scripts/mining.py` (testet av `test_mining.py`), robot i `jita/scripts/ingest_mining.py` (samme Actions-jobb), tabeller i `jita/sql/006_mining.sql`.
- Deploy til Vercel: `.github/workflows/jita-deploy.yml` (krever hemmeligheten `VERCEL_TOKEN`).
