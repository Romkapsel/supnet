# Jita – handelsverktøy for EVE Online på Supnet

**Status:** spesifikasjon v3, 14. september 2026, klar for bygging. Erstatter v2. Lagres i `C:\Users\Daniel\Documents\Supnet\jita\jita-spec.md`.

Del 1–3 er skrevet for Daniel. Del 4–9 er teknisk og skrevet for Claude/Claude Code – Daniel trenger ikke lese dem, men del 8 og 9 forklarer *hvorfor* ting er som de er.

**Endringer i v3 (etter grundig gjennomgang av v2 mot ESI-dokumentasjon og Supabase/GitHub-vilkår):**

- **Snapshot flyttet fra Supabase Storage til GitHub Actions cache.** v2 ville lastet ned 10–15 MB × 24/døgn = 7–11 GB/mnd fra Storage; Supabase Free har 10 GB egress totalt (5 cached + 5 uncached). Actions cache er gratis (10 GB per repo). Storage-bucket er fjernet.
- **Lagring i Postgres kraftig kuttet.** `fills` skrives bare for varer som passerer forfilteret + watchlist, og beholdes 7 dager. Ny liten tabell `type_flow_hourly` (timesaggregat per vare) beholdes 90 dager. `candidates` lagrer bare passed + de 200 beste ikke-passed per kjøring. v2 ville gitt titalls millioner rader.
- **Regel 9 omskrevet.** Navnetesten «slutter på " I"» ville ha ekskludert Tracking Speed Script, ammo, skyttler, droner. Nå brukes dogma-attributtene `metaGroupID` (1692) og `metaLevel` (633) fra ESI, med nytt profilflagg `allow_faction`.
- **Kjøpsordrer med rekkevidde er med.** Kjøpsordrer i andre stasjoner med `range = region` (og ordrer i Jita-systemet) konkurrerer om samme selgere som deg. Salgsordrer er stasjonsbundne og filtreres som før.
- **Cron flyttet fra :07 til :23** (09:07 UTC = 11:07 norsk tid = midt i downtime). `/status/` sjekkes først. `snapshot_at` = ESI `Last-Modified`, flyt normaliseres på faktiske timer mellom snapshots (GitHub-cron er ofte forsinket).
- **ESI rate-limiting håndtert:** `/markets/{region}/orders` er i en rate-limit-gruppe (12 000 tokens/15 min, per IP for uautentiserte kall) siden 24. feb 2026 → koden må håndtere 429 + `Retry-After`. Historikk-endepunktet har 300 forespørsler/min/IP. Historikk hentes bare for varer som passerer regel 1–4.
- **Sidekonsistens:** alle sider i en full henting må ha samme `Last-Modified`, ellers hentes avvikende sider på nytt.
- **Keepalive-jobb:** GitHub slår av scheduled workflows i offentlige repo etter 60 dager uten commits. En månedlig jobb gjør en triviell commit.
- **Token for `repository_dispatch` trenger `Contents: write`**, ikke Actions (blokk 0 rettet).
- **Regel 5 snudd:** ratio BfS/S2B er nå *myk*; hardt krav er at innflyten faktisk kan fylle deg (`s2b ≥ antall / fyllingstid`) og at varen liftes (`bfs_trades ≥ 10`).
- **«gone»-vekt avhenger av plassering** (nær toppen = sannsynligvis fylt, dypt i boka = sannsynligvis kansellert).
- **Regel 7 bootstrappes** fra `history_daily` de første 7 dagene.
- **Dommeren er en SQL-funksjon** i Postgres, ikke Python. Da kan settings-siden re-dømme øyeblikkelig («hva om jeg hadde 30 mill?») uten å vente på GitHub.
- **Ny fase 1b (blokk 1.3):** «Jeg tok denne»-knapp + `decisions`-tabell (lære-sløyfe og enkel P&L før SSO), Discord-varsler fra roboten (inkl. robotfeil), robotstatus-boks, skill-ROI-panel, porteføljeforslag, `ignore`-status, flyt per klokkeslett.
- **Tabell 3.3 rettet** til det formelen faktisk gir (break-even 12,9 % nå, ikke 13,5), og `min_qty` gjort til profilparameter.
- **Modify-gebyr** har nå en formel i regel 2.4.
- Del 9 er ny: punkt-for-punkt-verifikasjon av v3 mot v2 og en ny risikovurdering.

---

# Del 1 – Pitchen

Daniel logger inn på Supnet, åpner «Jita», og ser en topp-10-liste over varer å handle i Jita 4-4. Hver rad sier: **vare – kjøp X stk på Y – selg på Z – forventet fortjeneste – dager til fylling – én setning med hvorfor.** Under lista: varer som *nesten* ble med, og hvorfor de falt ut. Nederst: robotens status (siste kjøring, alt i orden?).

Det verktøyet gjør er den jobben som i dag tar Adam4EVE, tjue klikk i spillklienten, EVE Tycoon og en samtale med Claude: finne varer med bred spread, sjekke at køen på kjøpssiden er kort, at selgerne ikke er klumpet, at varen faktisk liftes fra selgersiden (ikke bare dumpes), og regne ut pris, antall og fortjeneste med Daniels faktiske gebyrer og kapital.

Verktøyet skal **vokse med Daniel**. Det som er en god vare med 4 mill i kapital og 7,5 % salgsskatt er ikke det samme som med 100 mill og Accounting V. Derfor er alle regler styrt av en profil (kapital, skills, standings) som Daniel oppdaterer selv, eller som hentes automatisk når EVE-innlogging er på. Endrer Daniel profilen, dømmes lista på nytt med én gang.

Verktøyet skal også **lære av Daniel**. Når han faktisk legger en ordre, trykker han «Jeg tok denne». Da vet verktøyet hva det spådde (dager til fylling) og kan senere sammenlignes med hva som skjedde. Det er slik tersklene kalibreres – ikke ved gjetting.

Verktøyet handler **aldri** selv. Det leser markedet og gir råd. Ordrer legges i spillet.

Plattform: Supnet (Supabase + Vercel) med GitHub Actions som «robot» i bakgrunnen. Kostnad: 0 kr/mnd på gratisnivåene. Claude Pro (som Daniel har) dekker byggingen i Claude Code. Varsler går til en Discord-kanal Daniel oppretter (gratis).

---

# Del 2 – Reglene («dommeren»)

Dette er regelsettet som ble brukt manuelt 13.–14. september, og som verktøyet kjører på alle varer i Jita hver time. Alle terskler er parametere i profilen (del 3), ikke hardkodet.

## 2.1 Harde krav – alle må bestå

| # | Regel | Hvorfor | Standardverdi (4 mill-profil) |
|---|---|---|---|
| 1 | Netto margin etter *alle* gebyrer ≥ terskel | Gebyrene er 10–15 %; alt under er tap | ≥ 10 % |
| 2 | Enheter på toppbudet ≤ terskel. **Toppbudet regnes over alle kjøpsordrer som dekker Jita 4-4** (stasjon, system, region-rekkevidde), ikke bare de som ligger i 4-4 | Store toppbud = mur, du står bakerst i ukevis (Inertial Stabs: 3 900 stk). En region-ordre i Perimeter er like mye mur | ≤ 100 |
| 3 | Antall budgivere innenfor 1 % under toppbudet ≤ terskel (samme rekkevidde-regel) | Mange aktører = du overbys hele dagen | ≤ 3 |
| 4 | Selgerenheter innenfor 1 % over laveste ask ≤ terskel (bare Jita 4-4 – salgsordrer er stasjonsbundne) | Klumpede selgere = du undercuttes i minutter | ≤ 300 |
| 5 | **Innflyt kan fylle deg:** dumpet (S2B) per dag ≥ anbefalt antall / ønsket fyllingstid. **Og varen liftes:** antall lift-handler (BfS) siste 24 t ≥ minimum | Innflyt var flaskehalsen i uke 1, ikke kapital. Varen må også være noe folk *kjøper* | bfs_trades ≥ 10 |
| 5b | Forhold BfS/S2B – **myk regel** | Mer lifting enn dumping er sunt, men høy innflyt skal ikke straffes hardt | ratio < 1 → score × ratio |
| 6 | Historisk dagssnitt (1d/5d/20d) nær ask, ikke nær bid – **myk regel** | Tycoon-sjekken: handelen skjer på selgersiden. ESI-historikk er for hele The Forge, ikke bare Jita 4-4 | pos ≥ 0,7 ellers score × pos/0,7 |
| 7 | 7-dagers salgspris ikke falt > 15 %, kjøpspris ikke steget > 25 %. De første 7 dagene brukes `history_daily` (regionsnitt) som erstatning | Kollaps eller noen som presser marginen | – |
| 8 | Kjøpspris ≤ maks pris for profilen | Posisjonsstørrelse må passe kapitalen | beregnes fra del 3.2 (nå ≈ 24 000) |
| 9 | Varetype (fra dogma, ikke navn): `is_excluded = false`, `is_meta = false`; `is_t2` krever `allow_t2`; `is_faction` krever `allow_faction` | Meta (navngitte T1-varianter) er loot og dumpes; blueprint/mutaplasmid/filament/skin/apparel har rare markeder | allow_t2 = false, allow_faction = false |

## 2.2 Rangering av dem som består

```
score = netto per enhet × min(dumpet/dag, liftet/dag) / (1 + dager til fylling)
        × min(1, hist_pos / 0,7)          (regel 6, myk)
        × min(1, BfS/S2B)                 (regel 5b, myk)
```
Altså: **forventet ISK per dag for posisjonen, straffet av ventetid og av svake tegn.** Dette rangerer Tracking Speed Script (18k netto, mye flyt, kort kø) over Small Thermal (10k netto, lite flyt) – som stemmer med erfaringen.

## 2.3 Anbefalingen per vare

- **Kjøpspris** = ett tick over toppbudet (4 signifikante siffer), der toppbudet er beste bud over alle ordrer som dekker Jita 4-4.
- **Salgspris** = ett tick under laveste ask i Jita 4-4.
- **Antall** = minste av: (a) posisjonsbudsjett / kjøpspris, (b) forventet innflyt × ønsket fyllingstid (3–5 dager). Aldri mer enn (b) – det er dét som gir døde køer.
- **Dager til fylling (kjøp)** = (enheter foran deg på ≥ din pris + ditt antall) / dumpet per dag.
- **Dager til fylling (salg)** = (enheter under din salgspris + ditt antall) / liftet per dag.
- **Forventet fortjeneste** = antall × netto per enhet.
- **Begrunnelse** = én setning generert fra tallene («Dør på toppen (27 stk), ~43/dag inn, 480/dag ut. Svakhet: tregt inn.»). Svakhet nevnes når hist_pos < 0,4, ratio < 1, eller dager til fylling > fyllingstid.

## 2.4 Regler for eksisterende posisjoner (fase 2, når EVE-innlogging er på)

- **Modify-gebyr** (relist): ≈ 0,5 × broker-sats × (1 − 0,10 × Advanced Broker Relations) × gjenværende ordreverdi, pluss broker-sats × prisøkning × antall hvis prisen settes opp. Kalibreres mot tallet spillet viser i Modify-dialogen.
- **Modify:** anbefal *bare* hvis forventet ekstra fylling neste 24 t × netto > 2 × modify-gebyr, og ordren over deg tilsvarer > 5 dagers flyt. Ellers «la ligge».
- **Trend mot deg:** lager > 10 stk og ask falt > 5 % på 48 t, eller selgerklyngen tredoblet → varsel «vurder å følge ned én gang».
- **Ulistet lager:** vare i hangar uten salgsordre → varsel med foreslått pris.
- **Utløp < 24 t** → «legg ut på nytt på 90 dager»-liste med ny pris.
- **Trekk:** kjøpsordre der dager til fylling > 14 og det finnes en kandidat med score > 2× → anbefal å flytte kapitalen.

## 2.5 Lære-sløyfen (fase 1b, før EVE-innlogging)

- «Jeg tok denne» på en kandidat lagrer: vare, side, pris, antall, dato og verktøyets *spådde* dager til fylling.
- «Fylt» / «Solgt» lagrer faktisk dato og pris.
- Verktøyet viser spådd vs. faktisk per beslutning og et løpende snitt (over-/undervurderer vi innflyt?). Det er datagrunnlaget for å justere gone-vekt og regel-6-terskel i fase 3 – og en enkel P&L.

---

# Del 3 – Skalering: profilen

Alt over styres av én profil. Daniel fyller inn tre ting: **kapital, skills, standings.** Resten regnes ut.

## 3.1 Profilfelt

| Felt | Nå (sept 2026) | Kilde |
|---|---|---|
| Kapital i arbeid (ISK) | ~4,5 mill | manuelt / EVE-innlogging |
| Broker Relations | IV | manuelt / skills-API |
| Accounting | 0 | manuelt / skills-API |
| Advanced Broker Relations | 0 | manuelt / skills-API |
| Trade / Retail / Wholesale / Tycoon | IV / III→IV / 0 / 0 | manuelt / skills-API |
| Standing Caldari Navy / Caldari State (umodifisert) | ukjent | standings-API / Market Orders-vinduet |
| Antall posisjoner du vil drive | 6–8 | manuelt |
| Ønsket fyllingstid | 3–5 dager | manuelt |
| Reserveandel | 25 % | manuelt |
| Minste fornuftige antall per posisjon (`min_qty`) | 20 | manuelt (senkes til 5 ved store varer) |
| allow_t2 / allow_faction | av / av | manuelt |

## 3.2 Det som regnes ut fra profilen

- **Broker's fee** = 3 % − 0,3 pp × Broker Relations − 0,03 pp × faction-standing (Caldari State) − 0,02 pp × corp-standing (Caldari Navy), gulv 1 %. Bare *umodifiserte* standings teller (Connections/Diplomacy hjelper ikke). Nå 2,1 %.
- **Sales tax** = 7,5 % × (1 − 0,11 × Accounting). Nå 7,5 %; med Accounting IV 4,2 %; V 3,375 %.
- **Break-even spread** = (1 + broker) / (1 − broker − tax) − 1. Nå 12,9 %; Accounting IV + BR V 7,6 %; Accounting V + BR V 6,7 %; maks skills + standings 5,6 %.
- **Ordreplasser** = 5 + 4×Trade + 8×Retail + 16×Wholesale + 32×Tycoon.
- **Posisjonsbudsjett** = kapital × (1 − reserve) / antall posisjoner.
- **Maks kjøpspris** = posisjonsbudsjett / `min_qty`. Nå: 4,5 mill × 0,75 / 7 / 20 ≈ 24 000.
- **Minimum netto per enhet** = kapital / 1 000 (nå 4 500), så verktøyet slutter å foreslå 5k-fortjenester når du har 100 mill.
- **Skill-ROI** (fase 1b): med siste 7 dagers realisert/forventet fortjeneste → hvor mange dager før Accounting-boka (6,5 mill) eller BR V tjener seg inn, gitt at de senker gebyrene X pp på din omsetning.

## 3.3 Hvordan bildet endrer seg med kapital (regnet med formlene over)

| Profil | 4 mill (nå) | 30 mill | 100 mill | 500 mill |
|---|---|---|---|---|
| Skills antatt | BR IV | Acc IV, BR IV | Acc V, BR V | + standings |
| Break-even spread | 12,9 % | 8,3 % | 6,7 % | ~5,6 % |
| Posisjoner / min_qty | 7 / 20 | 12 / 20 | 20 / 5 | 35 / 5 |
| Maks kjøpspris | ~24k | ~94k | ~750k | ~2,1 mill |
| Typiske varer | T1-moduler, scripts, skyttler | T1-rigger, mid-moduler, T1-frigatt/destroyer-skrog | T1-cruiser-skrog, T2-moduler, større rigger | T2-skip, faction-moduler, capital-komponenter |
| Flyt som betyr noe | 20–100 stk/dag | 10–50 stk/dag | 5–30 stk/dag | 1–10 stk/dag |
| Neste steg utenfor Jita | – | – | Amarr som hub nr. 2 (hauling) | flere huber, alt-karakterer |

Verktøyet må derfor kunne: (1) kjøre samme regler på *alle* varetyper inkl. skip, T2 og faction, ikke bare moduler; (2) skru terskler per profil; (3) senere legge til flere stasjoner/regioner som parameter. Ingen av disse krever ombygging – bare at reglene er parametere fra dag én, som de er i del 2.

---

# Del 4 – Arkitektur (teknisk)

```
GitHub Actions (offentlig repo)             Supabase «Supnet» (Frankfurt)      Vercel (Supnet)
 cron 23 * * * *  (time, UTC)                skjema jita:                        ├─ /jita/index.html      topp 10 + «nesten» + status
 ├─ [cache restore prev_snapshot]             types, systems, fills (7 d),       ├─ /jita/type.html       ordrebok + kalkulator + «Jeg tok denne»
 ├─ ingest_orders.py  ─┐                      type_flow_hourly (90 d),           ├─ /jita/settings.html   profil → re-døm nå
 ├─ compute_metrics.py ┼─ service key ─►      type_hourly (7 d) → type_daily,    ├─ /jita/decisions.html  lære-sløyfe / P&L
 ├─ select jita.judge() ┘                     history_daily, profile,            ├─ /jita/portfolio.html  (fase 2)
 └─ [cache save prev_snapshot]                candidates, watchlist, decisions,  ├─ /api/jita/scan        trigger workflow (PIN + sperre)
 cron 5,25,45 * * * * (watchlist, lett)       robot_runs, my_orders (f2),        ├─ /api/jita/rejudge     kjør jita.judge() (PIN)
 └─ ingest_watchlist.py → judge()             my_transactions, pnl, alerts       ├─ /api/jita/sso         EVE SSO (fase 2)
 cron 30 4 * * * daglig                      funksjoner: jita.judge(),           └─ /api/jita/character   hent egne data (fase 2)
 └─ ingest_history.py                         jita.judge_preview(jsonb)
 cron månedlig: keepalive commit              pg_cron: rydding
 «Scan nå» ◄── repository_dispatch ◄──
 Discord webhook ◄── varsler + robotfeil
```

**Hvorfor GitHub Actions og ikke Vercel/Supabase-funksjoner:** hver time hentes 300–400 sider ordrer (~350 000 rader) og differes mot forrige time. Det tar 1–3 minutter. Vercel Hobby tillater én cron/dag og korte kjøretider; Supabase Edge Functions har korte tidsgrenser. Actions tåler timer, og repoet trengs uansett for Vercel-deploy.

**Repoet må være offentlig.** Actions-minutter er ubegrenset for offentlige repo; private har 2 000 min/mnd, og 3 min × 24 × 30 ≈ 2 200 min. All kode er ufarlig å vise; alt hemmelig (Supabase service-key, GitHub-token, Discord-webhook, EVE SSO-secret) ligger i GitHub Secrets / Vercel-miljøvariabler og aldri i koden. `.gitignore` skal dekke `.env*`, `.etag.json`, `*.json.gz` og lokale snapshots.

**Keepalive:** GitHub deaktiverer scheduled workflows i offentlige repo automatisk etter 60 dager uten commits. `keepalive.yml` kjører den 1. hver måned og committer `jita/.keepalive` med dato (`permissions: contents: write`).

**Snapshot-lagring (endret i v3):** forrige snapshot ligger i **GitHub Actions cache**, ikke i Supabase. Jobben kjører `actions/cache/restore` med `restore-keys: jita-snapshot-` (gir nyeste treff), og `actions/cache/save` med nøkkel `jita-snapshot-${{ github.run_id }}` (cache-nøkler er uforanderlige, derfor unik per kjøring). Watchlist-jobben bruker `jita-watchlist-`. Cache er gratis, 10 GB per repo, LRU-utkastet; en oppføring som brukes hver time forsvinner ikke. Supabase ser dermed bare aggregater, aldri rådata, og egress holder seg på noen hundre MB/mnd.

**Lagringsprinsipp i Postgres:** rådata går *ikke* inn. Roboten gjør diffen i minnet og skriver:
- `type_hourly` for varer som passerer et billig forfilter: begge sider har ordrer *og* brutto spread ≥ 5 % (typisk 3–5k varer/time). Beholdes 7 dager, rulles til `type_daily`.
- `type_flow_hourly`: én rad per vare per time med summert BfS/S2B (qty + antall handler) – for *samme* forfilter-sett pluss watchlist. Beholdes 90 dager. Dette er grunnlaget for 24 t-flyt og flyt-per-klokkeslett.
- `fills`: enkelthendelser, bare for forfilter-sett + watchlist, beholdes 7 dager (til feilsøking og til type.html).
- `candidates`: bare passed + de 200 beste ikke-passed per kjøring, 30 dager.
Anslag: type_hourly 7 d ≈ 100–150 MB, flow 90 d ≈ 100 MB, fills 7 d ≈ 50 MB, resten småtterier. Godt under 500 MB. Roboten logger `pg_database_size` til `robot_runs` så det er synlig.

**Oppløsning:** timesdiff undervurderer flyt på varer som fylles på under en time (Tracking Speed Script gikk på < 1 t). Derfor kjører en lett jobb tre ganger i timen som *bare* henter `/markets/10000002/orders/?type_id=…` for varer i `jita.watchlist` (status `follow`) og for siste kjørings topp 20 kandidater (én side per vare, < 30 s totalt). Disse fills merkes `resolution = 20` og brukes foran timestallene der de finnes. **Den lette jobben skriver bare flyt og kjører `jita.judge()` på nytt – den henter ikke ny ordrebok for hele markedet, så `type_hourly` er uendret mellom timesjobbene.**

**Datakilde:** ESI (EVEs åpne API). Region The Forge = `10000002`, Jita 4-4 = `location_id 60003760`, Jita-systemet = `30000142`. Offentlige endepunkter uten innlogging: `/markets/{region}/orders/` (300 s cache, paginert, bruk ETag), `/markets/{region}/history/?type_id=` (daglig, 13 mnd), `/universe/types/{id}` (inkl. `dogma_attributes`), `/route/{a}/{b}/` (antall hopp), `/status/`. **Alle kall sender header `X-Compatibility-Date: 2026-09-14`** (ESI bruker datobasert versjonering; datoen må ikke ligge i framtida). User-Agent: `Supnet-Jita/0.3 (daniel@…; +https://supnet…)` – CCP ber eksplisitt om beskrivende User-Agent.

**Rate-limiting (nytt i v3):** `/markets/{region}/orders` er i rate-limit-gruppen «market-order» siden 24. februar 2026: 12 000 tokens per 15-minutters flytende vindu, 2 tokens per 2xx, 1 per 304, 5 per 4xx. For uautentiserte kall er bøtta per kilde-IP – og GitHub-runnere deler IP-er, så andre kan ha brukt av bøtta. Koden skal: lese `X-Ratelimit-Remaining` og bremse under 2 000; ved 429 vente `Retry-After` sekunder og prøve igjen (maks 3 ganger); alltid bruke `If-None-Match` (ETag) for å få billige 304. Én full kjøring koster ~700–800 tokens. Historikk-endepunktet har en egen, udokumentert grense på 300 forespørsler per minutt per IP → maks 4 parallelle med 1 s pause. Det gamle feillimit-systemet (`X-ESI-Error-Limit-Remain`, 100 feil/min → 420) gjelder fortsatt for andre ruter: under 20 → vent til `X-ESI-Error-Limit-Reset`.

**Sidekonsistens (nytt i v3):** de 300–400 sidene hentes over 30–60 s. Ruller cachen midt i, mangler ordrer på grensen mellom sider og blir feilaktig «gone». Alle sider i én henting skal ha identisk `Last-Modified`; sider som avviker hentes på nytt (maks 2 runder). `snapshot_at` settes til denne `Last-Modified`, ikke til kjøretidspunktet. Flyt per dag regnes som fills / (timer mellom snapshot_at og forrige snapshot_at) × 24 – GitHub-cron er ofte 5–30 min forsinket og hopper over kjøringer under høy last. Skriv ALDRI fills eller nytt snapshot hvis under 95 % av sidene kom.

**Downtime:** daglig downtime ~11:00–11:15 norsk tid = 09:00–09:15 UTC. Alle cron-tider er UTC. Timesjobben kjører :23; roboten kaller `/status/` først og avbryter (uten feil-varsel) hvis serveren er nede. UI viser en advarsel mellom 10:50 og 11:20 norsk tid: «Ikke legg ordrer nå.»

**Kjøpsordrer med rekkevidde (nytt i v3):** en selger i Jita 4-4 som velger «Immediate» treffer den høyest prisede kjøpsordren *hvis rekkevidde dekker 4-4* – uansett hvor ordren står. Snapshotet beholder derfor for kjøpsordrer: `location_id`, `system_id`, `range`. En kjøpsordre «dekker Jita 4-4» hvis: `range = 'region'`; eller `range = 'station'` og `location_id = 60003760`; eller `range = 'solarsystem'` og `system_id = 30000142`; eller `range` er et tall N og `jita.systems.jumps_from_jita ≤ N` for ordrens system. `jita.systems` fylles én gang av `seed_types.py` via `/universe/regions/10000002/` → constellations → systems → `/route/30000142/{system}/`. Salgsordrer: bare `location_id = 60003760`. `min_volume > 1` på kjøpsordrer ignoreres i fase 1 (sjelden på T1-varer).

**Innlogging (fase 2):** EVE SSO (OAuth2, PKCE) via Vercel-funksjon. Scopes: `esi-markets.read_character_orders.v1`, `esi-wallet.read_character_wallet.v1`, `esi-assets.read_assets.v1`, `esi-markets.structure_markets.v1`, `esi-skills.read_skills.v1`, `esi-characters.read_standings.v1`. Refresh-token kun server-side i Supabase (samme regel som Anthropic-nøkkelen). Perimeter TTT er en spillerstruktur og finnes ikke i offentlige data – krever innlogget karakter med docking-tilgang (`/markets/structures/{id}/`).

**Sikkerhet:** siden er bak Supnets eksisterende PIN-innlogging. `/api/jita/scan` og `/api/jita/rejudge` verifiserer PIN-sesjonen server-side og svarer 401 uten. Scan har i tillegg sperre i `jita.profile.last_manual_scan` – < 10 min siden → «vent N min» uten å trigge (ESI-cachen er 5 min, så hyppigere gir ingenting nytt). Supabase RLS: lesetilgang for innlogget bruker; skriving til `profile`, `watchlist`, `decisions` fra Vercel-funksjoner med service-key; alt annet skrives bare fra Actions. Discord-webhook ligger som GitHub Secret.

**Varsler (fase 1b):** roboten poster til Discord-webhooken når: (a) en kjøring feiler eller henter < 95 % av sidene; (b) en vare kommer inn i topp 3 som ikke var der forrige kjøring; (c) en watchlist-vare skifter mellom passed/ikke passed; (d) DB-størrelse > 350 MB. Maks 5 meldinger per kjøring. Dette erstatter Web Push i fase 3 for det meste.

---

# Del 5 – Datamodell

```sql
create schema if not exists jita;

create table jita.types (
  type_id int primary key, name text not null,
  group_id int, group_name text, category_id int, market_group_path text,
  meta_group_id int, meta_level int, published boolean,
  is_t1 boolean, is_meta boolean, is_t2 boolean, is_faction boolean,
  is_ship boolean, is_excluded boolean,
  updated_at timestamptz default now()
);
-- Klassifisering fra dogma (attributt 1692 metaGroupID, 633 metaLevel):
--   is_excluded : category_id in (blueprint 9, skin 91, apparel 30) eller group i mutaplasmid/filament-grupper
--   is_meta     : meta_group_id = 1 and meta_level > 0     (Compact, Enduring, Ample, 'Arbalest' …)
--   is_t2       : meta_group_id in (2, 14)                 (Tech II, Tech III)
--   is_faction  : meta_group_id in (3, 4, 5, 6, 52)        (storyline, faction, officer, deadspace, structure faction)
--   is_t1       : not is_excluded and not is_meta and not is_t2 and not is_faction
--                 (dekker T1-moduler, ammo, scripts, droner, skyttler, rigger, skrog)
--   is_ship     : category_id = 6
-- Navneordlista fra v2 brukes bare som reserve når dogma mangler.

create table jita.systems (                  -- for rekkevidde på kjøpsordrer
  system_id int primary key, name text, jumps_from_jita int
);

-- Ingen orders_raw-tabell og ingen Storage-bucket (v3): rådata ligger som prev_snapshot.json.gz i GitHub Actions cache.
-- Formatet i fila: {"snapshot_at": "<Last-Modified>", "orders": [[order_id, type_id, is_buy, price, volume_remain, issued, duration, location_id, system_id, range], ...]}
-- Salgsordrer: bare location_id = 60003760. Kjøpsordrer: alle i The Forge (rekkevidde avgjøres ved beregning).

create table jita.fills (                    -- enkelthendelser, bare forfilter-sett + watchlist, 7 d
  observed_at timestamptz not null, order_id bigint not null,
  type_id int not null, is_buy boolean not null,   -- is_buy=true: dumpet (S2B); false: liftet (BfS)
  price numeric not null, qty int not null,
  kind text not null check (kind in ('partial','gone')),
  weight numeric not null default 1,         -- partial=1; gone: 0.8 nær toppen, 0.5 midt, 0.2 dypt
  resolution int not null default 60,        -- 60 = timesjobb, 20 = watchlist-jobb
  primary key (observed_at, order_id)
);
create index on jita.fills (type_id, observed_at);

create table jita.type_flow_hourly (         -- timesaggregat av fills, 90 d
  type_id int not null, hour timestamptz not null, resolution int not null,
  bfs_qty numeric, bfs_trades int, s2b_qty numeric, s2b_trades int,
  hours_covered numeric,                     -- faktisk tid mellom snapshots (normalisering)
  primary key (type_id, hour, resolution)
);

create table jita.type_hourly (              -- ordrebok-øyeblikksbilde, forfilter-sett, 7 d
  type_id int not null, snapshot_at timestamptz not null,
  best_bid numeric, best_ask numeric,        -- best_bid over alle ordrer som dekker Jita 4-4
  bid_top_qty int, bid_orders_1pct int, bid_qty_1pct int,
  ask_orders_1pct int, ask_qty_1pct int, ask_qty_3pct int,
  bid_floor_price numeric, bid_floor_qty int,
  primary key (type_id, snapshot_at)
);
-- 24 t-flyt hentes fra type_flow_hourly ved dømming, ikke lagret her (v3).

create table jita.type_daily (               -- type_hourly eldre enn 7 d rulles hit av pg_cron
  type_id int not null, date date not null,
  best_bid_avg numeric, best_ask_avg numeric, bfs_qty numeric, s2b_qty numeric,
  bid_top_qty_avg int, ask_qty_1pct_avg int,
  primary key (type_id, date)
);

create table jita.history_daily (
  type_id int not null, date date not null,
  average numeric, highest numeric, lowest numeric, volume bigint, order_count int,
  primary key (type_id, date)
);

create table jita.profile (                  -- én rad, id = 1
  id int primary key default 1,
  capital_isk numeric, broker_relations int, accounting int, adv_broker_relations int,
  trade int, retail int, wholesale int, tycoon int,
  standing_corp numeric, standing_faction numeric,
  broker_fee_override numeric, sales_tax_override numeric,
  positions int default 7, target_fill_days numeric default 4, reserve_share numeric default 0.25,
  min_qty int default 20,
  allow_t2 boolean default false, allow_faction boolean default false,
  thresholds jsonb,                          -- alle terskler fra del 2.1
  last_manual_scan timestamptz,
  updated_at timestamptz default now()
);

create table jita.candidates (               -- dommerens output; bare passed + 200 beste ikke-passed, 30 d
  run_at timestamptz not null, type_id int not null,
  passed boolean, failed_rules text[],
  buy_price numeric, sell_price numeric, qty int,
  net_per_unit numeric, margin numeric, expected_profit numeric,
  days_to_fill_buy numeric, days_to_fill_sell numeric,
  hist_pos numeric, flow_ratio numeric, score numeric,
  reason text,
  primary key (run_at, type_id)
);

create table jita.watchlist (
  type_id int primary key,
  status text not null default 'follow' check (status in ('follow','ignore')),
  note text, updated_at timestamptz default now()
);

create table jita.decisions (                -- lære-sløyfen (fase 1b)
  id bigserial primary key, created_at timestamptz default now(),
  type_id int not null, side text check (side in ('buy','sell')),
  price numeric, qty int,
  predicted_days numeric, predicted_net_per_unit numeric,
  filled_at timestamptz, filled_qty int, sell_price numeric, closed_at timestamptz,
  note text
);

create table jita.robot_runs (               -- status-boksen
  run_at timestamptz primary key, job text, snapshot_at timestamptz,
  pages_total int, pages_ok int, orders_count int,
  ratelimit_remaining int, duration_s numeric, db_bytes bigint,
  ok boolean, message text
);

-- fase 2
create table jita.my_orders (order_id bigint primary key, type_id int, is_buy boolean, price numeric,
  volume_remain int, volume_total int, issued timestamptz, duration int, state text,
  first_seen timestamptz, last_seen timestamptz);
create table jita.my_transactions (transaction_id bigint primary key, date timestamptz, type_id int,
  is_buy boolean, unit_price numeric, quantity int, location_id bigint, journal_ref_id bigint);
create table jita.pnl (sell_transaction_id bigint primary key, type_id int, quantity int,
  buy_cost numeric, sell_net numeric, profit numeric, margin numeric, matched_buy_ids bigint[]);
create table jita.alerts (id bigserial primary key, created_at timestamptz default now(), kind text,
  type_id int, order_id bigint, payload jsonb, seen boolean default false);
```

Rydding (pg_cron, daglig 05:00 UTC): `type_hourly` > 7 d aggregeres til `type_daily` og slettes; `fills` > 7 d slettes; `type_flow_hourly` > 90 d slettes; `candidates` > 30 d slettes; `robot_runs` > 90 d slettes. Ingen orders_raw å rydde.

**Dommeren som SQL:** `jita.judge()` leser `profile`, nyeste `type_hourly` per vare, summerer `type_flow_hourly` siste 24 t (resolution 20 foretrekkes der den finnes, normalisert på `hours_covered`), leser `history_daily` for regel 6/7, kjører reglene i del 2.1, regner anbefaling (del 2.3) og skriver `candidates` med nytt `run_at`. `jita.judge_preview(p jsonb)` gjør det samme med en profil sendt inn som JSON og *returnerer* radene uten å skrive – brukes av settings-siden for «hva om». Reason-setningen bygges med `format()`. Alle terskler leses fra `profile.thresholds`.

---

# Del 6 – Formler (referanse for koden; SQL-funksjonen skal gi identiske tall)

```python
import math

def tick(p):                    # 4 signifikante siffer
    return 10 ** (math.floor(math.log10(p)) - 3)
def over(p):  return p + tick(p)
def under(p): return p - tick(p)

def fees(profile):
    # Broker: 3 % - 0,3 pp/nivå BR - 0,03 pp/faction-standing - 0,02 pp/corp-standing, gulv 1 %
    broker = profile.broker_fee_override or max(0.01,
              0.03 - 0.003 * profile.broker_relations
                   - 0.0003 * max(0, profile.standing_faction or 0)
                   - 0.0002 * max(0, profile.standing_corp or 0))
    tax    = profile.sales_tax_override  or 0.075 * (1 - 0.11 * profile.accounting)
    return broker, tax

def break_even(broker, tax):
    return (1 + broker) / (1 - broker - tax) - 1

def max_buy_price(profile):
    budget = profile.capital_isk * (1 - profile.reserve_share) / profile.positions
    return budget / profile.min_qty

def min_net_per_unit(profile):
    return profile.capital_isk / 1000

def economics(best_bid, best_ask, broker, tax):
    buy  = over(best_bid);  sell = under(best_ask)
    inn  = buy * (1 + broker)
    ut   = sell * (1 - broker - tax)
    return buy, sell, ut - inn, (ut - inn) / inn

def qty_recommendation(profile, buy_price, s2b_per_day):
    budget = profile.capital_isk * (1 - profile.reserve_share) / profile.positions
    by_capital = budget // buy_price
    by_flow    = s2b_per_day * profile.target_fill_days
    return int(max(1, min(by_capital, by_flow)))

def days_to_fill(units_ahead, my_qty, flow_per_day):
    return (units_ahead + my_qty) / max(flow_per_day, 0.1)

def flow_per_day(sum_qty_24h, hours_covered):
    return sum_qty_24h / max(hours_covered, 1) * 24     # normalisert på faktisk dekket tid

def score(net_per_unit, s2b, bfs, days, hist_pos):
    base  = net_per_unit * min(s2b, bfs) / (1 + days)
    ratio = bfs / max(s2b, 0.1)
    return base * min(1.0, max(hist_pos, 0) / 0.7) * min(1.0, ratio)

def gone_weight(order_price, best_price, is_buy):
    # hvor nær toppen lå ordren da den forsvant?
    d = (best_price - order_price) / best_price if is_buy else (order_price - best_price) / best_price
    if d <= 0.01: return 0.8      # lå på/nær toppen: sannsynligvis fylt
    if d <= 0.05: return 0.5
    return 0.2                    # dypt i boka: sannsynligvis kansellert
```

**Flyt fra diff (ingest_orders.py):** for hver ordre i forrige snapshot – finnes nå med lavere `volume_remain` → `partial` med differansen, vekt 1; finnes ikke og ikke utløpt (`issued + duration` > nå) → `gone` med resten, vekt fra `gone_weight` regnet mot *forrige* snapshots beste pris; ikke funnet og utløpt → ingenting. Nye ordrer = ny konkurranse (påvirker type_hourly, ikke fills). Prisendring med samme order_id = modify, ikke fill. Etter diffen summeres fills per (type, time, resolution) til `type_flow_hourly` med `hours_covered` = timer mellom de to snapshot_at.

**Historikk-sjekk (regel 6, myk):** `pos = (avg − bid) / (ask − bid)` for 1d, 5d og 20d-vinduer av `history_daily.average`; `hist_pos = min(pos_1d, pos_5d, pos_20d)`. Varen består regel 6 uansett, men score skaleres med `min(1, hist_pos/0,7)`, og `hist_pos < 0,4` vises som svakhet i reason. Kalibrering: Relic Analyzer I bør havne høyt, Auto Targeting System I lavt.

**Regel 7 (trend):** fra dag 8 brukes `type_daily.best_ask_avg` / `best_bid_avg`; før det `history_daily.lowest` (proxy for ask) og `highest` (proxy for bid) over 7 dager.

**Flyt-tall som brukes i regel 5 og score:** hvis varen har `resolution = 20`-rader siste 24 t, brukes de; ellers timestallene. Aldri summen av begge.

---

# Del 7 – Faser og Claude Code-blokker

Alle blokker limes inn i Claude Code i Supnet-mappen, én om gangen. Etter hver blokk: Daniel sier hva han ser, neste blokk skrives.

## Fase 0 – Repo (½ kveld)

**Blokk 0 – GitHub-repo og Vercel-kobling:**

```
Supnet ligger i C:\Users\Daniel\Documents\Supnet og deployes til Vercel, men har ikke noe
GitHub-repo ennå. Jeg er ikke programmerer – forklar kort, ett steg om gangen, og vent på
meg mellom stegene der jeg må gjøre noe i nettleseren.

1. Sjekk at det finnes .gitignore som dekker .env*, .etag.json, node_modules, *.json.gz og
   eventuelle nøkkelfiler. Søk gjennom mappen etter ting som ser ut som nøkler eller
   passord (sk_, service_role, eyJ…, PIN, webhook) og si fra FØR vi committer noe – repoet
   skal være OFFENTLIG.
2. git init, første commit.
3. Be meg opprette et offentlig repo "supnet" på github.com (fortell hva jeg skal trykke),
   koble remote, push.
4. Be meg koble repoet til det eksisterende Vercel-prosjektet (Vercel → Settings → Git),
   og verifiser at neste push gir en deploy.
5. Be meg lage en fine-grained GitHub-token med Contents: read/write på repoet (det er
   Contents, ikke Actions, som trengs for repository_dispatch), og legge den som
   GITHUB_TOKEN i Vercel-miljøvariabler. Legg SUPABASE_URL og SUPABASE_SERVICE_KEY som
   GitHub Secrets (fortell hvor).
6. Be meg lage en Discord-server/kanal med en webhook (fortell hvor i Discord), og legg
   URL-en som GitHub Secret DISCORD_WEBHOOK.
7. Lag .github/workflows/keepalive.yml: kjører "0 6 1 * *", committer jita/.keepalive med
   dagens dato (permissions: contents: write). Forklar meg hvorfor (60-dagersregelen).
```

## Fase 1 – Robot + topp 10 (2–3 kvelder)

**Blokk 1.1 – database og robot** (ingenting synlig ennå):

```
Vi bygger et EVE Online station-trading-verktøy kalt "Jita" i Supnet. Les CLAUDE.md og
/jita/jita-spec.md først (del 4–6 er spesifikasjonen). Plattform: Supabase (Supnet,
Frankfurt) + Vercel + GitHub Actions. Jeg er ikke programmerer – forklar kort hva du gjør
underveis, og spør før du velger noe som ikke står i spesifikasjonen.

1. Lag /jita/sql/001_schema.sql med skjema jita og ALLE tabellene i del 5 (ingen
   orders_raw, ingen Storage-bucket), pg_cron-rydding som beskrevet, og RLS som i del 4.
   Kjør den mot Supabase via MCP hvis tilgjengelig, ellers gi meg SQL-en jeg skal lime inn.
2. Lag /jita/scripts/common.py med tick/over/under, fees (full standings-formel),
   break_even, max_buy_price (med min_qty), min_net_per_unit, economics,
   qty_recommendation, days_to_fill, flow_per_day, score, gone_weight fra del 6.
   ESI-klient som: alltid sender X-Compatibility-Date: 2026-09-14 og User-Agent
   "Supnet-Jita/0.3 (…)", bruker ETag/If-None-Match med cache i .etag.json, leser
   X-Ratelimit-Remaining og bremser under 2000, håndterer 429 med Retry-After (maks 3
   forsøk), og respekterer X-ESI-Error-Limit-Remain (< 20 → vent til reset). Discord-
   funksjon notify(text) som poster til DISCORD_WEBHOOK (maks 5 per kjøring). Supabase-
   klient som leser SUPABASE_URL og SUPABASE_SERVICE_KEY fra miljøet. Funksjon som
   skriver en rad til jita.robot_runs (inkl. pg_database_size) ved slutten av hver jobb,
   og varsler Discord hvis ok=false.
3. Lag /jita/scripts/seed_types.py: fyll jita.types fra
   /markets/10000002/types/ og /universe/types/{id} (parallelt, 10 om gangen, ETag).
   Klassifiser med dogma_attributes 1692 (metaGroupID) og 633 (metaLevel) nøyaktig som
   kommentaren i del 5 sier; bruk navneordlista (Compact, Enduring, Ample, Prototype,
   Scoped, Restrained, Upgraded, Modulated, Limited, Experimental, 'Arbalest', 'Malkuth',
   'Regulated', 'Allotek', 'Kindred') bare som reserve når attributtene mangler.
   Fyll jita.systems: alle systemer i The Forge og antall hopp fra Jita via /route/.
   Skriv ut en kontroll: Tracking Speed Script, Amarr Shuttle, Damage Control I skal være
   is_t1=true; Damage Control II is_t2; 'Compact' noe is_meta; en Blueprint is_excluded.
4. Lag /jita/scripts/ingest_orders.py: les prev_snapshot.json.gz fra stien i miljøvariabel
   SNAPSHOT_DIR (restaurert av Actions cache; kan mangle første gang). Sjekk /status/
   først – avbryt stille hvis nede. Hent alle sider av
   /markets/10000002/orders/?order_type=all (les X-Pages, 8 parallelle). Verifiser at alle
   sider har samme Last-Modified; hent avvikende på nytt (maks 2 runder). Behold
   salgsordrer bare for location_id=60003760 og kjøpsordrer for hele regionen (med
   location_id, system_id, range). snapshot_at = Last-Modified. Diff i minnet mot forrige
   snapshot som i del 6 (partial/gone med gone_weight), skriv jita.fills (resolution=60)
   BARE for varer i forfilter-settet (regnes i steg 5, samme kjøring) og watchlist, og
   jita.type_flow_hourly med hours_covered. Skriv ALDRI fills eller nytt snapshot hvis
   under 95 % av sidene kom – varsle Discord i stedet. Lagre nytt snapshot til
   SNAPSHOT_DIR (Actions cache lagrer det). Rådata skal ikke inn i Postgres.
5. Lag /jita/scripts/compute_metrics.py: fyll jita.type_hourly for nyeste snapshot etter
   del 5 – bare varer med ordrer på begge sider og brutto spread ≥ 5 %. best_bid,
   bid_top_qty, bid_orders_1pct, bid_qty_1pct regnes over kjøpsordrer som DEKKER Jita 4-4
   etter rekkevidde-regelen i del 4 (bruk jita.systems). Ask-tallene bare fra 4-4.
   Snapshot og forfilter-sett sendes videre i minnet fra steg 4 (samme kjøring).
5b. Lag /jita/scripts/ingest_watchlist.py: for type_id i jita.watchlist (status='follow')
   + topp 20 fra siste candidates, hent /markets/10000002/orders/?type_id=… (én side per
   vare, 8 parallelle), diff mot prev_snapshot_watchlist.json.gz i SNAPSHOT_DIR, skriv
   fills og type_flow_hourly med resolution=20. Skal IKKE røre type_hourly.
6. Lag /jita/scripts/ingest_history.py: for type_id som passerte regel 1–4 i siste
   candidates-kjøring (passed eller failed_rules bare inneholder 5–9) pluss watchlist,
   hent /markets/10000002/history/?type_id=… og skriv siste 30 dager til
   jita.history_daily. Maks 4 parallelle med 1 s pause (300/min-grensen), ETag.
7. Lag /jita/sql/002_judge.sql: funksjonene jita.judge() og jita.judge_preview(jsonb)
   som beskrevet i del 5 – reglene i del 2.1 (5b og 6 som score-faktorer, 7 med
   bootstrap fra history_daily, 8 mot max_buy_price, 9 via is_*-flaggene og
   allow_t2/allow_faction), anbefaling i del 2.3, resolution=20-flyt foretrukket,
   score som i del 6, reason bygd med format() inkl. svakheter. Skriv bare passed + 200
   beste ikke-passed. Lag en liten test i /jita/scripts/test_judge.py som regner samme
   vare i Python og SQL og krever likhet på 4 desimaler.
8. Lag .github/workflows/jita.yml med tre jobber, alle UTC:
   - cron "23 * * * *": actions/cache/restore (restore-keys jita-snapshot-) →
     ingest_orders → compute_metrics → psql "select jita.judge()" → actions/cache/save
     (key jita-snapshot-${{ github.run_id }}).
   - cron "5,25,45 * * * *": cache restore/save på jita-watchlist- → ingest_watchlist →
     select jita.judge().
   - cron "30 4 * * *": ingest_history.
   workflow_dispatch og repository_dispatch (type "jita-scan") kjører timesjobben.
   Secrets: SUPABASE_URL, SUPABASE_SERVICE_KEY, DISCORD_WEBHOOK. Timeout 15 min, egen
   concurrency-gruppe per jobb (cancel-in-progress: false).
9. Lag /jita/README.md: hvordan legge inn secrets i GitHub, hvordan kjøre første gang
   (workflow_dispatch to ganger med > 5 min mellom for å få første diff), hvordan sjekke
   at det funker (SQL: select * from jita.robot_runs order by run_at desc limit 5;
   select count(*) from jita.candidates where passed).
```

**Blokk 1.2 – siden** (etter at første kandidatliste ligger i databasen):

```
Lag /jita/index.html i Supnets stil (#0f1117, #1a1d27, #f0c040, Nunito), bak eksisterende
PIN-innlogging, mobil først. Innhold:
- Topp: kapital fra jita.profile, gebyrsatser, break-even, siste kjøring, knapp "Scan nå"
  som kaller /api/jita/scan. Vercel-funksjonen SKAL sjekke PIN-sesjonen server-side (samme
  som resten av Supnet) og svare 401 uten, sjekke jita.profile.last_manual_scan og svare
  "vent N min" hvis < 10 min siden, ellers sende repository_dispatch "jita-scan" til
  GitHub med token fra miljøvariabel GITHUB_TOKEN, oppdatere last_manual_scan og vise
  "kjører… ~3 min". Advarsel "Downtime – ikke legg ordrer nå" mellom 10:50 og 11:20
  norsk tid.
- Liste "Topp 10": fra jita.candidates nyeste run_at, passed=true, sortert på score.
  Per rad: navn, "Kjøp N stk à Y", "Selg på Z", forventet fortjeneste, dager til fylling
  kjøp/salg, margin, reason. Klikk → /jita/type.html?id=…
- Liste "Nesten": 10 med høyest score blant passed=false, med failed_rules som tekst
  ("Toppbud 785 stk", "Selgere klumpet", "For lite innflyt").
- Robotstatus-boks nederst: fra jita.robot_runs – siste kjøring per jobb, ok/feil,
  andel sider hentet, ratelimit igjen, DB-størrelse i MB. Rødt hvis siste timesjobb er
  > 2 t gammel eller feilet.
- /jita/settings.html: skjema for jita.profile (kapital, skills, standings, posisjoner,
  fyllingstid, reserve, min_qty, allow_t2, allow_faction) og tersklene. Lagre → vis
  beregnede gebyrer, break-even, ordreplasser, maks kjøpspris og min netto/enhet, og kall
  /api/jita/rejudge (PIN-sjekket, kjører select jita.judge()) så lista er ny med én gang.
  "Hva om"-knapp: samme skjema, men kaller jita.judge_preview(jsonb) og viser topp 10
  uten å lagre.
- Watchlist: knapper på type.html "Følg" (status follow → 20-min-oppløsning) og "Ignorer"
  (status ignore, med notat) → jita.watchlist. Ignorerte varer vises aldri i listene.
- /jita/type.html: ordreboken fra siste snapshot (kjøp/selg side om side, kjøpsordrer
  markert med rekkevidde/stasjon), sparkline bid/ask 7 d, BfS/S2B per dag, historikk,
  og kalkulator "X stk på pris Y" → dager til fylling og netto.
```

## Fase 1b – Lære-sløyfe og varsler (1 kveld)

**Blokk 1.3:**

```
Utvid Jita med lære-sløyfen (spec del 2.5) og de små hjelperne:
1. På type.html og hver rad i topp 10: knapp "Jeg tok denne" → skriver jita.decisions
   (type, side=buy, pris, antall, predicted_days og predicted_net_per_unit fra kandidaten).
   Skriving via Vercel-funksjon /api/jita/decision med PIN-sjekk.
2. /jita/decisions.html: åpne beslutninger med knapper "Fylt" (dato + antall) og "Solgt"
   (pris + dato). Viser per rad: spådd vs. faktisk dager, netto spådd vs. realisert.
   Øverst: løpende snitt "vi spår innflyt X % for høyt/lavt" og sum realisert fortjeneste
   siste 7/30 dager.
3. Skill-ROI-boks på settings.html: med sum omsetning siste 7 dager (fra decisions) →
   dager til tilbakebetaling for Accounting-boka (6,5 mill) og Broker Relations V, gitt
   gebyrbesparelsen fra del 3.2.
4. "Forslag til porteføljen" under topp 10: velg grådig fra kandidatlista de N
   (profile.positions) varene med høyest score som til sammen passer kapitalen (sum
   qty×buy_price ≤ kapital × (1−reserve)) og der maks 2 er fra samme market_group_path
   på nivå 2. Vis sum forventet fortjeneste per dag.
5. type.html: liten graf "flyt per klokkeslett" (BfS og S2B summert per time på døgnet
   siste 14 dager fra type_flow_hourly), norsk tid.
6. Discord-varsler i roboten (del 4, Varsler): feil, ny i topp 3, watchlist skifter status,
   DB > 350 MB.
```

## Fase 2 – Daniel i systemet (2 kvelder)
EVE SSO via Vercel, henting av egne ordrer/transaksjoner/hangar hver time, FIFO-fortjeneste, kapital i arbeid, automatisk gebyr fra skills og standings, TTT-ordrer inn i køberegningen, portfolio-side. `decisions` matches automatisk mot `my_orders` slik at lære-sløyfen blir manuell-fri.

## Fase 3 – Rådgiveren (1–2 kvelder)
Reglene i del 2.4 som varsler på dashboardet og i Discord: ulistet lager, utløp, undercut, trend mot deg, flytt kapital. Web Push via Supnets PWA hvis Discord ikke holder. Kalibrering av gone-vekter og regel 5b/6-terskler mot `decisions` og egne faktiske fyllinger.

## Fase 4 – Skalering
Flere stasjoner/regioner som parameter (Amarr først), hub-til-hub-differ, ukesrapport via Claude API, backtesting av tersklene mot `type_daily` + `history_daily`.

---

# Del 8 – Avgjørelser fra gjennomgangene (14. sept 2026)

Fra v2-gjennomgangen (fortsatt gjeldende):
1. **Terskler i del 2.1:** beholdes som standard. Regel 6 er myk og kalibreres mot Relic Analyzer I (høyt) og Auto Targeting System I (lavt). Justeres i settings.
2. **Skip og T2:** skip er med fra dag én (styrt av maks kjøpspris). T2 slippes inn med `allow_t2` – av til Accounting er kjøpt. (v3: faction/deadspace har eget flagg `allow_faction`.)
3. **reason:** regler først. Claude API vurderes i fase 4.
4. **Repo:** offentlig, opprettes i blokk 0 før blokk 1.1.
5. **Sikkerhet:** scan-endepunktet krever PIN-sesjon og har 10-minutters sperre.

Fra v3-gjennomgangen:
6. **gone-vekt:** ikke flat 0,5 lenger, men 0,8/0,5/0,2 etter avstand fra toppen. Kalibreres i fase 3 mot `decisions`.
7. **Oppløsning:** time for hele markedet, 20 min for watchlist + topp 20 fra dag én – men den lette jobben rører ikke `type_hourly`.
8. **Lagring:** snapshot i Actions cache; ingen rådata og ingen Storage i Supabase; små aggregater i tabeller med korte retention-tider.
9. **Dommeren i SQL**, slik at profilendringer gir ny liste med én gang. Python-formlene i del 6 er fasit; testen i blokk 1.1 steg 7 sikrer at SQL og Python er enige.
10. **Regel 5:** innflyt-krav er hardt, ratio er mykt.
11. **Varsler:** Discord-webhook fra fase 1b. Web Push utsatt til det eventuelt trengs.
12. **Lære-sløyfe** fra fase 1b, manuell til fase 2 automatiserer den.

Kjente begrensninger som ikke skal overraske: offentlige data skiller ikke fylt fra kansellert (gone-vekt er et estimat); ESI-historikk er regionsnitt, ikke Jita 4-4 alene; TTT-konkurransen mangler til fase 2; timesdiff undervurderer flyt på varer utenfor watchlist; GitHub-cron kan komme 5–30 min for sent eller hoppe over en kjøring (derfor status-boks og normalisering på faktisk tid); rate-limit-bøtta for uautentiserte ESI-kall deles med andre på samme runner-IP (derfor 429-håndtering); verktøyet er bare så godt som rutinen med å åpne det.

---

# Del 9 – Verifikasjon av v3 mot v2, og ny vurdering

## 9.1 Hva som skjedde med hvert element i v2

| v2-element | Status i v3 |
|---|---|
| Pitch (login → Scan → topp 10 med pris/antall/fortjeneste/dager/reason), «nesten»-liste | Beholdt; status-boks og lære-sløyfe lagt til |
| Verktøyet handler aldri selv | Beholdt |
| Regel 1, 2, 3, 4, 8 | Beholdt; 2 og 3 regnes nå over kjøpsordrer med rekkevidde |
| Regel 5 (BfS/S2B ≥ 2, ≥ 10 handler) | **Endret:** ratio myk (5b), hardt krav på innflyt + lift-handler |
| Regel 6 myk | Beholdt |
| Regel 7 | Beholdt; bootstrap fra history_daily lagt til |
| Regel 9 via is_t1/is_ship/is_excluded/is_meta + allow_t2 | **Endret:** dogma-basert, `is_ship` er ikke lenger et krav (alt T1 er inne), `allow_faction` nytt |
| Score-formel | Beholdt; to myke faktorer eksplisitt i formelen |
| Anbefaling (2.3) | Beholdt |
| Regler for posisjoner (2.4) | Beholdt; modify-gebyr har fått formel |
| Profil (3.1) og beregninger (3.2) | Beholdt; `min_qty`, `allow_faction`, break-even-formel og skill-ROI lagt til |
| Tabell 3.3 | **Rettet** til formelens tall; 100/500-mill-kolonnene bruker min_qty 5 |
| GitHub Actions som robot, offentlig repo | Beholdt; keepalive lagt til |
| Snapshot i Supabase Storage, diff i minnet | **Endret:** Actions cache. Diff i minnet beholdt |
| Rådata ikke i Postgres | Beholdt og strammet: fills bare forfilter+watchlist, 7 d |
| type_hourly med bfs/s2b-24h-kolonner | **Endret:** flyt flyttet til `type_flow_hourly`, type_hourly er bare ordrebok |
| pg_cron-rydding | Beholdt; nye tabeller lagt til, fills 90 d → 7 d |
| 20-min watchlist-jobb | Beholdt; presisert at den ikke rører type_hourly |
| ESI: compat-date-header, User-Agent, X-ESI-Error-Limit | Beholdt; rate-limit-gruppe (429/Retry-After), historikk 300/min, sidekonsistens, /status/ lagt til |
| 95 %-regelen | Beholdt; varsler nå Discord |
| Cron :07 | **Endret:** :23 (downtime); watchlist 5,25,45 |
| SSO-scopes, TTT i fase 2 | Beholdt |
| PIN-sjekk og 10-min sperre på scan | Beholdt; rejudge- og decision-endepunkt med samme sjekk |
| Storage-bucket privat | **Fjernet** (ingen bucket) |
| Blokk 0 (steg 1–5) | Beholdt; token-rettighet rettet; steg 6–7 (Discord, keepalive) lagt til |
| Blokk 1.1 (steg 1–9) | Beholdt struktur; steg 4/5/6/7/8 omskrevet for cache, rekkevidde, rate-limit, SQL-dommer |
| Blokk 1.2 | Beholdt; status-boks, rejudge, hva-om, ignorer, downtime-advarsel lagt til |
| Faser 2–4 | Beholdt; fase 1b lagt inn før fase 2 |
| Del 8-avgjørelser 1–8 | Alle beholdt (nr. 3 gone-vekt og 7 lagring er oppdatert, ikke reversert) |
| Kjente begrensninger | Beholdt; to nye (cron-forsinkelse, delt rate-limit-IP) |

Ingenting fra v2 er tapt. Fire ting er *endret i innhold* (regel 5, regel 9, snapshot-lagring, cron-tid); resten er tillegg eller innstramming.

## 9.2 Ny vurdering etter omskrivingen

**Det som nå er robust:** lagring holder seg under gratisgrensene med god margin (snapshot utenfor Supabase, korte retention-tider, størrelse logges); ESI-bruken er innenfor rate-limit med ~700 tokens per kjøring og tåler 429; feil blir synlige (status-boks + Discord) i stedet for stille; klassifiseringen av varer bygger på spilldata, ikke navnegjetting; kjøpssiden regnes slik spillet faktisk matcher ordrer.

**Gjenstående risiko, i prioritert rekkefølge:**
1. **Dommeren i SQL er mer kode enn Python-varianten** og vanskeligere for Daniel å lese. Testen (Python = SQL) er derfor obligatorisk, ikke valgfri. Om SQL-varianten blir tung, er fallback: Python-dommer i Actions + en enkel `judge_preview` i SQL bare for hva-om. Avgjøres i blokk 1.1 steg 7 – Claude Code skal si fra hvis det blir uforholdsmessig.
2. **gone-vektene er fortsatt gjetninger** (0,8/0,5/0,2). De er bedre begrunnet enn flat 0,5, men det er `decisions`-dataene som skal avgjøre. Ikke juster dem manuelt før det finnes minst ~20 avsluttede beslutninger.
3. **Rekkevidde-regelen mangler `min_volume`** og kjenner ikke kjøpsordrer i strukturer. Begge er sjeldne for T1-varer under 25k; blir relevant først ved større varer (fase 2/4).
4. **Første uke er blind på regel 7 og halvblind på regel 6** (regionsnitt). Vent med å stole på «trend mot deg» før `type_daily` har 7 dager.
5. **Delt IP i Actions.** Hvis 429 blir hyppig, er løsningen å sende et EVE-token (fase 2) slik at bøtta blir `IP:applicationID` – eller kjøre roboten på en egen liten maskin. Ikke sannsynlig ved ~700 tokens per time, men mulig.
6. **Tidsestimatene i del 7** (2–3 kvelder for fase 1) er optimistiske gitt at v3 har mer i blokk 1.1. Regn med 3–4 kvelder, og at blokk 1.1 kanskje må deles i 1.1a (steg 1–3) og 1.1b (steg 4–9) hvis Claude Code-økten blir lang.

**Første ting å gjøre:** blokk 0. Ingenting i v3 endrer det.
