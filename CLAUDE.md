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
Eierens verktøy for å tjene ISK i EVE: markedshandel i Jita 4-4, produksjon i Ylandoki og mining.

- Eget Vercel-prosjekt `jita-eve` (rot `jita/`) → https://jita-eve.vercel.app. Familiesiden er urørt.
- **`jita/README.md` er driftsdokumentet – les den før du endrer noe i `jita/`.** Der ligger avvik fra
  spec, alle designvalg med begrunnelse, og en feillogg over hver bom som er gjort. Spec: `jita/jita-spec.md`.
- Datalager: Supabase «Supnet», skjema `jita`. Robot i GitHub Actions (`.github/workflows/jita.yml`);
  pg_cron er primærklokke og starter jobbene via `/api/scan?fallback=1&job=…`.
- Sider: `index.html` (Topp 10 + «Å gjøre»), `type.html`, `results.html`, `settings.html`, `industry.html`.
- Regler og terskler: `jita/sql/002_judge.sql` (`jita.judge_rows`) + `jita.profile.thresholds`.
  Industri: `005_industry.sql` + `industry_profile.thresholds`. Mining: `006_mining.sql`. Vern: `007_order_guard.sql`.

### Robotens jobber og hvem som kjører hva
| jobb | klokke | gjør |
|---|---|---|
| `hourly` (Actions) | :23 | ordrebok → `type_hourly` → `jita.judge()` → varsler (`ingest_orders.py`) |
| `watchlist` | :05/:25/:45 | lett oppdatering av varer du følger |
| `history` | :29 | ESI-historikk, roterer over forfilteret |
| `industry` | 05:40 UTC daglig | `ingest_industry.py` + `ingest_mining.py` (kjører alle tre testsuitene først) |
| `apiprobe` / `probe` / `seed` | manuelt | spør levende API / eksterne kilder / seed av varetyper |
| pg_cron `jita-vakt*` | hver time | plan B: starter Actions-jobben via `/api/scan?fallback=1&job=…` hvis siste kjøring er for gammel (industri/mining: 20 t) |
| pg_cron `jita-eve-sync` / `-light` | :50 / :10,:30 | henter wallet, ordrer, hangar, transaksjoner og skills fra EVE |
| pg_cron `jita-cleanup` / `-vacuum` / `-vacuum-full` | :55 / 05:20 / 1. kl. 04:35 | rydding og komprimering |

Deploy skjer med `.github/workflows/jita-deploy.yml` (hemmeligheten `VERCEL_TOKEN`). Manuelt, fra en maskin
med nett: `cd jita && vercel --prod --yes`. Formlene ligger i `scripts/industry.py` og `scripts/mining.py`,
brief for industri i `jita/industri-brief.md`.

### Slik jobber eieren
Eieren har **veldig lave tekniske ferdigheter** og vil gjøre minimalt selv: du kjører migrasjoner, jobber
og deploy. Svar på norsk. Ønsket er **en anbefaling, ikke en meny** – «hvis jeg skulle velge, tar jeg denne,
fordi …». Sidene skal peke på én ting å gjøre, med tallene bak. Be om merge med en gang når PR-en er grønn,
det er mønsteret så langt. Og: ingen skal måtte inn i «Avansert» for å få lista til å virke.

### Arbeidsflyt (viktig – dette har kostet tid)
1. Utvikling på den tildelte grenen, PR mot `main`, **squash merge**. `jita-deploy.yml` deployer automatisk.
2. **Start alltid grenen på nytt fra `origin/main` før en ny PR** (`git checkout -B <gren> origin/main` +
   cherry-pick). Squash-mergen skriver om historikken, så en gren som ligger på forrige commit gir
   «merge conflicts» selv om filene er identiske.
3. **Filene er CRLF** (`index.html`, `settings.html`, `sql/*.sql`). Rediger slik: les binært, `\r\n → \n`,
   endre, skriv tilbake som CRLF. Ellers blir diffen hele filen. Python `open()` i tekstmodus ødelegger dette.
4. Test alltid før push: `python jita/scripts/test_industry.py` (118), `test_mining.py` (58),
   `test_advice.py` (45, inkl. ord-for-ord-paritet mot `lib/advice.js` via `advice_probe.mjs`),
   `node --check` på `api/[action].js` og på `<script type="module">` i HTML-filene.
5. **`lib/*.js` speiler `scripts/*.py`.** Endrer du én, endre begge – tekstene skal være ord for ord like,
   ellers sier Discord-varselet og siden ulike ting. `test_advice.py` håndhever det for rådene.

### Containeren kommer ikke ut på nettet
`jita-eve.vercel.app`, ESI, Fuzzwork, everef og `api.vercel.com` er blokkert av egress-policyen.
- **Spør det levende API-et** med Actions-jobben `apiprobe` (`workflow_dispatch`, `job=apiprobe`) – den
  curler `/api/industry` med `JITA_PIN` og skriver ut nøkkeltallene. Bygget 25. sept nettopp fordi
  gjetting kostet to runder.
- Deploy verifiseres med `jita-deploy.yml`-kjøringen og `mcp__Vercel__web_fetch_vercel_url` mot statiske
  filer. Databasen leses direkte med Supabase-MCP (prosjekt `vnbjpgolxmzruhfxepts`).
- **Ikke kall `/api/*` uten PIN.** Fem feil låser innlogging **globalt** i 15 min, og sperretiden dobles.
  PIN-en ligger i Vercel-miljøvariabelen `JITA_PIN`; jeg får ikke lese den. Låsen nullstilles med
  `update jita.auth_lock set failures = 0, rounds = 0, locked_until = null where id = 1`.

### Fallgruver som har kostet tid (alle er i feilloggen i README)
- **Aldri sammenlign en timestamp som har vært innom JS.** `run_at` har mikrosekunder, JS-datoer bare
  millisekunder → `where run_at = ${dato}` traff ingenting, og siden viste «0 forslag» uten feilmelding.
  Gjør sammenligningen i SQL: `where run_at = (select max(run_at) from …)`.
- **psycopg2 gir `numeric` som `Decimal`.** Decimal og float kan sammenlignes, men ikke regnes med om
  hverandre. Konverter ved grensen – det krasjet timesjobben 27. sept.
- **Ikke kjør hele `002_judge.sql`** for å oppdatere én funksjon: filen reschedulerer pg_cron-jobber med
  PIN-plassholderen `0000`. Oppdater funksjonen alene (f.eks. `pg_get_functiondef` + `replace` + `execute`
  i en `do`-blokk, med `raise exception` hvis mønsteret ikke finnes).
- **Probe kilder før du gjetter på adresser** (`scripts/probe_sources.py`, Actions-jobb `probe`).
- **`vacuum analyze` gir ikke plass tilbake til disken** – bare `vacuum (full, analyze) <tabell>`, én
  tabell per kall, utenfor transaksjon.

### Databasen har et hardt tak
Supabase **gratisplan: 500 MB**. Over det blir basen skrivebeskyttet, og da stopper også familiens andre
apper i samme prosjekt (`okonomi`, `hanna`, `hund`, `finn`, `warera`, `wow_ah`). 27. sept: 439 → 285 MB
etter `vacuum full` + strammere oppbevaring. `jita-cleanup` går hver time (:55), full vacuum den 1. i
måneden. Roboten varsler over 400 MB og skriker over 450 MB.

### Tilstandssjekk – kjør denne først i en ny økt
```sql
select round(pg_database_size(current_database())/1048576.0,1) as db_mb,
 (select json_agg(row_to_json(p)) from (select positions, min_qty, reserve_share, cash_isk,
    round((jita.effective_profile()->>'capital_isk')::numeric) as kapital, thresholds
    from jita.profile where id=1) p) as profil,
 (select json_agg(row_to_json(r)) from (select job, max(run_at) siste, bool_and(ok) ok
    from jita.robot_runs where run_at > now() - interval '2 days' group by job) r) as roboten,
 (select count(*) from jita.candidates where run_at=(select max(run_at) from jita.candidates) and passed) as passerer,
 (select count(*) from jita.my_orders where state='open') as apne_ordrer;
```

### Der ting står nå (27. sept 2026)
- Kapital ~52 mill. ISK, hvorav nesten alt bundet i ordrer og lager. 12 posisjoner à ~3,3 mill.
- Pengene tjenes på commodities/loot (0,90 margin), implanter (0,97) og moduler (0,80). Malm (25) og
  skillbøker (16) er ekskludert via regel `9k` fordi de var de eneste kategoriene i minus.
- **Største lekkasje: omprising.** 11.–27. sept gikk 10,2 mill. til broker-gebyr, ~6 mill. av det til å
  følge prisen. Vernet i `guard_advice()` svarer nå LA STÅ (12 t karantene, 15 % gebyrtak) eller DUMP
  (etter 3 endringer, selg til budet). Gebyrvakt på forsiden stopper hånden når dagens gebyr løper.
- Eieren må selv: trene Accounting mot 5 (~2 mill. spart per 14 dager), så Broker Relations 5, og holde
  seg til én omprising per ordre per dag.
- Industri-fanen: ti blueprints rangert på margin ved ME 0 med faste regler i `NYBEGYNNER`
  (`scripts/industry.py`), **ikke** koblet til tersklene.

