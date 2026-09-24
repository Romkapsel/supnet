# Jita – drift

Station-trading-verktøy for EVE Online (Jita 4-4). Spesifikasjon: `jita-spec.md`.
Nettside: eget Vercel-prosjekt med rot `jita/` (egen PIN i `login.html`).

## Hva som kjører hvor

| Del | Hvor | Hva |
|---|---|---|
| Robot | GitHub Actions (`.github/workflows/jita.yml`) | `hourly` (:23 hver time), `watchlist` (:05/:25/:45), `history` (04:30 UTC), `seed` (manuelt) |
| Database | Supabase «Supnet», skjema `jita` | `sql/001_schema.sql` (tabeller, rydding), `sql/002_judge.sql` (dommeren) |
| Nettside + API | Vercel | `jita/*.html`, `jita/api/[action].js` (server-side PIN-sjekk) |
| Keepalive | `.github/workflows/keepalive.yml` | commit den 1. hver måned så cron ikke slås av |

## Hemmeligheter

**GitHub → repo → Settings → Secrets and variables → Actions:**
- `SUPABASE_DB_URL` – `postgresql://postgres.<ref>:<passord>@aws-1-eu-central-1.pooler.supabase.com:5432/postgres?sslmode=require` (session-pooler, port 5432)
- `DISCORD_WEBHOOK` – valgfri; uten den logges varsler bare i jobb-loggen

**Vercel → prosjektet «jita» → Settings → Environment Variables:**
- `SUPABASE_DB_URL` – samme som over, men port **6543** (transaction-pooler)
- `JITA_PIN` – 6-sifret PIN som login.html sjekker mot (5 feil → sperre 15 min, dobles). Endres du den: oppdater også pg_cron-jobbene `jita-vakt` og `jita-eve-sync`
- `GITHUB_REPO` – `Romkapsel/supnet`
- `GITHUB_TOKEN` – fine-grained token med *Contents: Read and write* på repoet (for «Scan nå»)

Lokalt: `.env` i rotmappen med `SUPABASE_DB_URL=…` (ignorert av git).

## Første gang

1. `python jita/scripts/seed_types.py` (eller Actions → jita → Run workflow → job `seed`). ~3 min.
2. Kjør timesjobben to ganger med > 5 min mellom (Actions → Run workflow → `hourly`) for å få første diff.
3. Sjekk: Actions-loggen, og i Supabase SQL:
   ```sql
   select * from jita.robot_runs order by run_at desc limit 5;
   select count(*) from jita.candidates where run_at = (select max(run_at) from jita.candidates) and passed;
   ```
4. Etter ~24 timer har regel 5 (flyt) nok data til at varer kan passere.

## Lokalt

```
pip install -r jita/scripts/requirements.txt
set SUPABASE_DB_URL=...      (eller les fra .env)
set SNAPSHOT_DIR=jita/snapshots
python jita/scripts/ingest_orders.py
python jita/scripts/test_judge.py     # SQL-dommer = Python-formler?
```

## Feilsøking

- Status-boksen nederst på /jita/ blir rød hvis timesjobben er > 2 t gammel eller feilet.
- Discord får melding ved feil, < 95 % sider, ny vare i topp 3, watchlist-endring, DB > 350 MB.
- 429 fra ESI: roboten venter `Retry-After` og prøver igjen (maks 3). Hyppig 429 = delt runner-IP; se spec del 9.
- `X-Compatibility-Date` er satt til 2026-09-14 i `common.py`; oppdater ved ESI-endringer.

## Avvik fra spec (bevisste, sept 2026)

- **Regel 1b (endret 16. sept):** «forventet fortjeneste per posisjon ≥ `min_position_profit_share` × kapital» (1 %) i stedet for «netto/enhet ≥ kapital/1000». Den gamle stoppet Amarr Shuttle (6k × 100 stk) så snart kapitalen passerte ~6 mill. `min_net_per_unit` beregnes fortsatt (vises), men brukes ikke som regel.
- **Regel 2 og 4 (endret 16. sept):** absolutt terskel *og* `wall_days` (2): mur/klump stopper bare hvis den tar > 2 dager å tømme med dagens flyt. Tracking Speed Script (klump 500, liftes 447/dag) og Light Neutron Blaster (mur, dumpes 972/dag) ble feilaktig stoppet av absolutte tall.
- **Score (17. sept):** `antall × netto / (1 + dager)` × historie × etterspørsel × rulleblad × krig × **momentum** (vol5/vol20, 0,7–1,3) × **stabilitet** (dagsintervall > 20 % straffes, gulv 0,7). Faktorene ligger i `candidates.factors` og vises på kortene. v3 brukte flyt i stedet for antall.
- **Regel 5:** implementert som «S2B/dag × fyllingstid ≥ min_qty» + «bfs_trades ≥ terskel». Spec-ens «S2B ≥ antall / fyllingstid» er trivielt sann fordi antall allerede er begrenset av S2B × fyllingstid.
- **Dager til fylling:** «enheter foran deg» = enhetene innenfor 1 % (bid_qty_1pct / ask_qty_1pct), ikke 0 – konkurrentene på toppen legger seg over deg igjen.
- **Regel 1x (ny):** margin > `max_margin` (200 %) forkastes – da er «toppbudet» et 0,01-ISK-bud, ikke et marked.
- **Forfilter:** i tillegg til spread ≥ 5 % kreves ikke-ekskludert vare og ≥ 3 ordrer per side (15 000 → 7 000 varer/time).
- **ETag** brukes ikke på full-hentingen (412 sider × body ville gitt 50 MB cache); brukes på watchlist og historikk.
- **Regel 9/1x-avslag lagres ikke** i `candidates` (kan aldri bli «nesten»).
- **20-min-flyt** brukes bare når ≥ 3 timer er dekket; ellers timestall.
- **Regel 5t** passerer også hvis ESI-historikkens `order_count` (5-dagers snitt, hele The Forge) er ≥ 3 × terskelen – timesdiffen teller hendelser, ikke handler, og undervurderer likvide varer.
- **Plan B for cron:** pg_cron-jobben `jita-vakt` kl. :23 og :40 starter timesjobben via `/api/scan?fallback=1` hvis den er > 50 min gammel – databasen er nå primær klokke, GitHubs egen cron bare bonus.
- **Overbuds-vakt (spec 2.4):** kjøres i times- og watchlist-jobben for alle åpne kjøpsordrer i `decisions`, per vare mot din høyeste egen pris. Gebyrformel fra CCP: `broker × (P2 − P1) × antall` (ved økning) `+ (50 % − 6 % × Advanced Broker Relations) × broker × P2 × antall`. Spec-en hadde 10 %/nivå for ABR – rettet til 6 %. Råd: ENDRE bare hvis muren over deg er > 5 dagers flyt *og* forventet netto neste 24 t > 2 × gebyr; TREKK hvis marginen ved ny pris < terskel; ellers HOLD. Lagres i `jita.alerts` (kind `overbid` / `overbid_cleared`), varsles på Discord ved endring.

## Fase 2 – EVE-innlogging (fra 16. sept 2026)

- App registrert på developers.eveonline.com (callback `https://jita-eve.vercel.app/api/sso`). Vercel env: `EVE_CLIENT_ID`, `EVE_CLIENT_SECRET`.
- `lib/eve.js`: SSO (authorization code + signert `state`), token-refresh, `syncCharacter()` – wallet → `profile.cash_isk`, skills/standings → profil, ordrer → `my_orders` (+ `decisions` automatisk via `order_id`), transaksjoner → `my_transactions` (lukker beslutninger når solgt), hangar → `my_assets`. Varsler: ulistet lager og utløp < 24 t (`alerts`, Discord hvis `DISCORD_WEBHOOK` er satt i Vercel).
- Kjøres av pg_cron `jita-eve-sync` kl. :50 (full) og `jita-eve-light` kl. :10/:30 (bare wallet + ordrer, `?light=1`; ESI-cache på ordrer er 20 min) og manuelt fra Profil («Oppdater fra EVE nå»). Refresh-token ligger kun i `jita.sso_tokens`.

## Minne og struktur (16. sept 2026)

- **Rulleblad per vare** (`jita.type_memory`, `jita.refresh_type_memory()`): fra egne transaksjoner, beslutninger og varsler – runder, realisert margin, snitt dager kjøp→salg, overbud/undercut. Dom: `god` (×1,2), `ok` (×1), `treg`/`svak` (×0,6), `krangel` (×0,75, ≥ 1,5 varsler per runde). Faktoren ganges inn i score; teksten «Erfaring: …» legges til i begrunnelsen. Oppdateres ved hver EVE-synk og daglig 05:10 UTC. Vises på vare-siden.
- **NPC-seedet** (`jita.types.npc_seeded`, `npc_seed_price`): salgsordrer med ≥ 365 dagers varighet finnes bare fra NPC. Timesjobben flagger (1 828 varer 16. sept). Regel `9n` stopper dem med mindre `allow_npc_seeded` er på. Eksempel: Oceanic Command Center (NPC 81 336, Jita 96k) – uendelig tilbud, klump av selgere.
- Neste lag (ikke bygget): zKillboard som etterspørselssignal per varetype; patch-notes via Claude API.

## Optimaliseringsrunde (16. sept 2026, kveld)

- **Krig-indeks:** `type_flow_hourly.bid_mods/ask_mods` = prisendringer på samme ordre innenfor 1 % av toppen, per time. Dommer: regel `10` ved ≥ 3 × `war_mods_per_hour` (4), myk straff `min(1, terskel/mods_per_hour)` i score, «priskrig» i svakhet-teksten.
- **Målt broker-sats:** `profile.broker_fee_measured` settes ved EVE-synk (journalens `brokers_fee` matchet mot ordre i samme sekund, median av topp 3 siste 30 d). `profile_calc` bruker override > målt > formel.
- **Flyt-tak:** s2b/bfs begrenses av `history_daily.volume` (5-dagers snitt).
- **Beste klokkeslett:** `bestHours()` i API – topp 3 timer for dumping/lifting siste 14 d, vist i Topp 10.
- **Skatt per salg:** journalpost `id = journal_ref_id + 1`.
- Forsøk på parallelle spørringer mot Supavisor ble forkastet: flere tilkoblinger kostet mer enn de sparte, og kø > pool henger.

## Blokk 1.4 (16. sept 2026) – driftsrettelser
Se spec del 9.3. Kort: rydding 3 d/30 d/14 d + rettet `type_daily`-rulling; EVE-synk i deler med feil per del; historikk hver time (400 varer, `history_fetched_at`); DB-størrelse og cron-status i robot-boksen; forklaring når porteføljen er tom.

## «Å gjøre» (17. sept 2026)
Regnes **live** i `buildTodo()` fra `my_orders` (EVE) mot siste `type_hourly`, ikke fra lagrede varsler (som ble stående etter at ordren var endret). Robotens eksakte mur-tall (`alerts`, < 3 t gamle og med samme pris/toppbud) brukes når de finnes, ellers anslag fra ordreboken. Rådlogikken er speilet i `lib/advice.js` (= `common.py`). Bare klare verb: HEV, SENK, TREKK, SELG, KJØP, ØK, RELIST – HOLD vises i beholdningen. Reserve senket til 10 % (karakteren er ren trader; cash trengs bare til gebyrer og én ny posisjon).

## Industri – produksjon (steg 1, 24. sept 2026)

Egen fane `/jita/industry.html`. Rangerer hvilke T1-produkter det er verdt å produsere i Ylandoki
(system 30001395) og selge i Jita 4-4. Brief: `industri-brief.md`.

| Del | Hvor | Hva |
|---|---|---|
| Jobb | GitHub Actions `jita` → job `industry` (05:40 UTC daglig) | `scripts/ingest_industry.py` |
| Formler | `scripts/industry.py` | ME/TE-runding, EIV, jobbavgift, netto, score, dom, porteføljevelger |
| Test | `scripts/test_industry.py` | 37 sjekker mot tall regnet for hånd – kjøres først i Actions-jobben |
| Database | `sql/005_industry.sql` | `industry_profile`, `blueprints`, `blueprint_materials`, `market_quotes`, `market_prices`, `industry_systems`, `industry_candidates` |
| API | `api/[action].js` | `industry` (GET rangering + portefølje, POST innstillinger), `industry_type?id=` (én vare) |
| Klokke | pg_cron `jita-industri-rydd` (06:20) og `jita-industri-vakt` (07:10) | rydding, og reservestart hvis jobben ikke har kjørt på 26 t |
| Deploy | `.github/workflows/jita-deploy.yml` | Vercel-deploy ved push til main som rører `jita/`, og manuelt (production/preview). Krever hemmeligheten `VERCEL_TOKEN`; org- og prosjekt-ID står i fila. `cd jita && vercel --prod --yes` virker fortsatt som før. |

Kjør manuelt: knappen «Kjør industri-jobben nå» i fanen, eller Actions → jita → Run workflow → `industry`.
Jobben laster også opp topp 30 som CSV-artifact.

### Slik regnes det
1. **Oppskrifter** hentes fra `sde.hoboleaks.space/tq/blueprints.json` (hele SDE-en i CCPs eget format,
   11,5 MB, én nedlasting), med EVE Refs bulkpakke `data.everef.net/reference-data/reference-data-latest.tar.xz`
   som reserve. Lagres i `jita.blueprints` og hentes på nytt når de er > 7 dager gamle.
   Parseren tåler begge formene (`typeID` og `type_id`), og `test_industry.py` sjekker det.
2. **Materialmengde** per jobb: `max(runs, ceil(round(runs × grunnmengde × (1 − ME/100), 2)))`.
   Mengde 1 reduseres aldri. NPC-stasjon har ingen material- eller tidsbonus.
3. **Jobbavgift** = EIV × (systemets manufacturing cost index + facility tax 0,25 % + SCC 4 %), der
   EIV = grunnmengdene (før ME) × `adjusted_price` fra ESI `/markets/prices/`.
4. **Tid per run** = base × (1 − TE/100) × (1 − 0,04 × Industry) × (1 − 0,03 × Advanced Industry).
5. **Materialpriser** fra Fuzzwork-aggregat for Jita 4-4: høyeste buy (du legger kjøpsordre, + broker fee)
   eller laveste sell (instant) – valgbart i innstillingene.
6. **Salg** ett tick under laveste ask, minus broker + skatt fra `jita.profile` (målt sats slår formelen).
7. **Batchen dimensjoneres av både tid og kapital:** antall runs = min(det slotten rekker på
   `batch_days`, det budsjettet tåler, BPC-grensen). Budsjett per jobb = min(`max_capital_per_job`,
   kapital × `capital_share_per_job`) og dekker materialer *og* jobbavgift.
8. **Realistisk ISK/dag/slot** = netto × det minste av tre tak: hva slotten rekker, 10 % av
   dagsvolumet, og **kapital-omløpet** – hvor mange enheter kapitalen rekker å finansiere per døgn,
   regnet som batchen delt på (produksjonstid + tid å selge unna). Hvilket tak som binder vises som
   `slot` / `marked` / `omløp` i fanen, sammen med potensialet uten omløpstaket.
9. **Score** = ISK/dag/slot × likviditet × konkurranse × stabilitet × trend (faktorene vises i fanen).

### Momentvernet (24. sept 2026)
Tre lag hindrer forslag i markeder uten flyt – det hjelper ikke med 200 skip hvis markedet tar 2 i uka:
1. **Batchen begrenses av markedet:** antall enheter ≤ `volume_share` × dagsvolum × `max_sell_days`
   (10 % × volum × 5 dager). Taket regnes om når historikken er hentet, så batchen krymper til det
   markedet faktisk spiser. Regel `i12` forkaster varer der selv minste batch er for stor.
2. **Handler per dag, ikke bare volum:** `min_trades_per_day` (3) mot ESI-historikkens `order_count`.
   Et dagsvolum på 500 kan være én stor ordre; antall handler viser om varen flyter. Regel `i11`.
   Samme tall trekker ned likviditetsfaktoren i scoren, uansett hvor stort volumet ser ut.
3. **Kapital-omløpet** (se punkt 8 over) straffer alt som tar lang tid å selge unna.

### Valg og avvik fra briefen (bevisste)
- **EVE Ref sitt kost-API brukes ikke per vare.** 1 200+ kall per kjøring er ufint mot en gratis tjeneste, og
  vi trenger egne materialpriser uansett (briefen vil ha Jita buy-pris). Vi henter derfor oppskriftene i
  én nedlasting og regner EIV/avgift/tid selv. `--verify N` kryssjekker de N beste mot kost-API-et og
  logger avviket – bruk den når noe ser rart ut.
- **Salgsgebyr er ikke 5 %,** men broker + skatt fra profilen (nå 1,8 % + 7,5 % = 9,3 %). Kan overstyres i fanen.
- **Trinnvis berikelse** for å holde ESI-bruken nede: alle produkter får kostnad/margin (trinn A),
  de 400 beste får historikk (trinn B), de 120 beste får antall selgere og BPO-pris (trinn C).
  Varer uten dagsvolum kan derfor ikke passere – de mangler data (regel `i7`).
- **NPC-BPO** avgjøres som i timesjobben: en salgsordre med ≥ 365 dagers varighet finnes bare fra NPC.
  Mangler vi en slik ordre, vises varen med merket «ikke NPC-BPO» i stedet for å skjules.
- **Exordium** er aldri aktuelt: produksjon og salg er låst til Ylandoki og Jita (briefens straffeavgifter
  gjelder ikke der vi står).
- **Kapital-omløpet er lagt til** (24. sept, etter første kjøring): uten det ble dyre varer
  urealistisk høyt rangert – 42 mill. ISK/dag på en vare som koster 2 mill. per stk krever at
  55 mill. ISK går gjennom materialene hvert døgn, med 8 mill. i kassa. Taket ligger alltid litt
  under de to andre, fordi batchen også må selges før pengene er tilbake.
- **Egne mineraler er ikke gratis** – materialer verdsettes alltid til markedspris, også det du miner selv.
- Regler: `i1` margin, `i1x` urealistisk margin, `i2` dagsvolum, `i3`/`i3b` tynt marked, `i4` dyr BPO,
  `i5` kapital per jobb (slår bare til når én enkelt run sprenger budsjettet), `i6` prisfall 30 d,
  `i7` mangler data, `i8` nedbetalingstid, `i9` pristopp, `i10` blueprinten finnes ikke på markedet.
- **«NPC-selgd BPO»** avgjøres av om blueprinten finnes som markedsvare i `jita.types` (og ikke er en
  T2-blueprint). Uten det kan du ikke kjøpe den – de varene forkastes med `i10` i stedet for å skjules.
- **BPO-pris** hentes fra Fuzzwork for Jita, Amarr, Dodixie, Rens og Hek, og laveste sell brukes.
  NPC-seedede BPO-er ligger spredt i empire; et oppslag bare mot The Forge fant pris på 54 av 120
  og ingen av de beste.

### Feillogg
- **24. sept 2026, andre kjøring (1 147 vurdert, 9 passerte) hadde tre feil:** batchene ble dimensjonert
  bare etter tid, så forslagene bandt 11–18 mill. ISK per jobb mot en kapital på 8 mill.; BPO-prisen var
  null for alle de beste (region-oppslag mot The Forge); og to varer uten kjøpbar blueprint
  (SCARAB Breacher Pod M, Interdiction Nullifier II – CCP setter ikke metaGroup på dem, så
  `seed_types.py` regner dem som T1) lå øverst. Rettet med kapitaltak på batchen, BPO-priser fra fem
  handelsknuter, og regel `i10`.
- **24. sept 2026, første kjøring feilet:** `ref-data.everef.net/blueprints` gir bare en liste med
  5 082 ID-er (detaljene ligger på `/blueprints/<id>`, altså 5 082 kall), `sde.everef.net` finnes ikke, og
  Fuzzwork-dumpene ligger i `dump/latest/csv/` med datostemplede filnavn – ikke `dump/latest/<tabell>.csv.bz2`.
  Rettet ved å bytte til Hoboleaks + EVE Refs bulkpakke. `scripts/probe_sources.py` (Actions-jobb `probe`,
  bare manuell) viser hvilke kilder som svarer og hvilken form svaret har – bruk den før du gjetter på adresser.

## Mining (steg 2, 24. sept 2026)

Egen seksjon nederst i Industri-fanen. Svarer på: hvilken malm gir mest ISK per time der du miner,
og er det best å refine den eller selge den som den er?

| Del | Hvor | Hva |
|---|---|---|
| Jobb | Samme Actions-jobb som industri (`industry`) | `scripts/ingest_mining.py`, eget innslag i `robot_runs` (`mining`) |
| Formler | `scripts/mining.py` | refine-verdi, salgsvei, ISK/time, dom |
| Test | `scripts/test_mining.py` | 29 sjekker uten nett/database |
| Database | `sql/006_mining.sql` | `mining_profile`, `ore_yields`, `mining_candidates`, `cleanup_mining()` + pg_cron `jita-mining-rydd` (06:25) |
| API | `api/[action].js` | `mining` (GET + POST innstillinger) |

### Slik regnes det
1. **Refine-utbytte** fra `sde.hoboleaks.space/tq/typematerials.json` (`{typeID: {materials: [{typeID, quantity}]}}`),
   batch-størrelsen (`portionSize`) fra EVE Refs bulkpakke. Hentes på nytt når utbyttene er > 7 dager gamle.
2. **Salgsvei per vare** – for hvert mineral og for malmen selv velges den beste av:
   salgsordre `(laveste ask − tick) × (1 − broker − skatt)` eller dumping `høyeste bud × (1 − skatt)`
   (ingen broker fee når du selger til et bud).
3. **Refinet verdi** = Σ(mengde × `reprocess_yield` × netto) / batch-størrelse. Standard utbytte 52 %
   (NPC-stasjon 50 % med skills) – sett det høyere om du refiner i struktur med rigger.
4. **Beste vei** = høyeste ISK per m3 av refine og rå-salg. `refine_premium` viser hvor mye mer refine gir;
   er den negativ, selg malmen som den er.
5. **ISK/time** = beste ISK per m3 × `m3_per_hour`.
6. **Komprimering er en salgsvei, ikke en egen rad.** Du miner rå malm; komprimering skjer etterpå og
   endrer bare volumet. Verdien av den komprimerte varen regnes derfor per m3 **rå** malm, med en
   omregningsfaktor hentet fra utbyttedataene (samme mineralinnhold gir forholdet, f.eks. 100:1 for
   Veldspar) – ikke gjettet.

### Hva som filtreres bort
- `m1` malmgruppen finnes ikke der du miner (`available_groups` i profilen – standard 0.8 Lonetrek:
  Veldspar, Scordite, Pyroxeres, Plagioclase, Omber, Kernite. Rediger i fanen.)
- `m2` ingen pris i Jita, `m4` mangler refine-utbytte.
- `m3` for tynt **eller ukjent** marked for det du faktisk selger (rå eller komprimert vare).
  Manglende omsetningstall forkaster også – en pris uten omsetning bak er én tilfeldig ordre.
  Refine-veien er upåvirket, fordi mineralene alltid flyter.

### «Verdt å mine selv?»
Fanen viser hvilke mineraler produksjonsforslagene dine faktisk spiser (mengde og hva de koster i Jita),
og hvilken av malmene der du miner som gir mest av hvert mineral. Egne mineraler regnes fortsatt til
markedspris i produksjonsdelen – det du sparer, er kjøpesummen, og det du bruker, er tid (se ISK/time).

### Ikke bygget ennå
- Belt-sammensetning per system (hva som faktisk finnes i beltene rundt Ylandoki hentes ikke fra spillet –
  derfor er `available_groups` en liste du styrer selv).
- Is og gass er med i tabellen når de har utbytte og pris, men reglene er laget for malm.
- Varsel på Discord ved nye varer i topp 3 (i dag varsles bare margin som faller under terskel på varer du eier).
