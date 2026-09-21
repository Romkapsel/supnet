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

## Gjennomgang 21. sept 2026
- **DB 463 MB** etter 6 dager tross grønn ryddejobb: anslagene var fortsatt for rause (dommeren lagrer 200 rader 4×/t). Ny oppbevaring: type_hourly 2 d, fills 1 d, flow 10 d, candidates 3 d (ikke-passed bare 6 t), history 60 d; `VACUUM FULL` kjørt manuelt (→ 240 MB) og nattlig `jita-vacuum` 05:20.
- **Resultat 7 d:** +12,8 mill netto (308 salg, 41 mill omsetning). Kapital 4,5 → 28,8 mill. Accounting IV tjener seg inn på 41 dager – anbefalt.
- **Kalibrering:** EVE-opprettede beslutninger manglet `predicted_days` → lære-sløyfen samlet ingen data. Nå fylles den fra siste dom ved ordrelegging. De to første datapunktene: faktisk fylling 0,32 × spådd (vi er for pessimistiske; små tall, vent).
- **Varselstøy:** 104 overbud-varsler på 3 dager (1-ISK-hakk hvert 20. min). Nå: ny post bare ved annet råd, toppbud flyttet > 2 %, eller > 2 t siden sist.

## Strukturmarkeder (21. sept 2026) – største datafeil så langt
- ESIs regionsordrebok har bare NPC-stasjoner. Kjøpsordrer i Perimeter-strukturene (TTT `1042508032148`, 0.0% Neutral States Market HQ `1044752365771`, repro rig) med rekkevidde ≥ 1 hopp dekker Jita og lå langt over Jita-budet (Datacore: 25 000 i 4-4 vs 69 110 i struktur → «163 % margin»). Nå: `scripts/structures.py` henter ~30 000 kjøpsordrer fra `jita.structures` hver time med karakterens token (`/api/token`, GitHub-secret `JITA_PIN`). Oppdagelse daglig fra EVE Ref (`structures-latest.v2.json`), maks 2 hopp. Krever SSO-scope `esi-universe.read_structures.v1` og markedstilgang (403 → strukturen skrus av).
- `/route/` finnes ikke under `X-Compatibility-Date` (404) → seed ga 99 hopp for 87 av 88 systemer, så «N hopp»-rekkevidde aldri dekket Jita. `Esi.get(..., legacy=True)` bruker gammel sti; tabellen er reparert.
- Nye scopes 21. sept: `esi-universe.read_structures.v1`, `esi-location.read_location.v1`, `esi-skills.read_skillqueue.v1` (de to siste ikke tatt i bruk ennå).
