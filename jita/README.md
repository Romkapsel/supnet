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

- **Regel 5:** implementert som «S2B/dag × fyllingstid ≥ min_qty» + «bfs_trades ≥ terskel». Spec-ens «S2B ≥ antall / fyllingstid» er trivielt sann fordi antall allerede er begrenset av S2B × fyllingstid.
- **Dager til fylling:** «enheter foran deg» = enhetene innenfor 1 % (bid_qty_1pct / ask_qty_1pct), ikke 0 – konkurrentene på toppen legger seg over deg igjen.
- **Regel 1x (ny):** margin > `max_margin` (200 %) forkastes – da er «toppbudet» et 0,01-ISK-bud, ikke et marked.
- **Forfilter:** i tillegg til spread ≥ 5 % kreves ikke-ekskludert vare og ≥ 3 ordrer per side (15 000 → 7 000 varer/time).
- **ETag** brukes ikke på full-hentingen (412 sider × body ville gitt 50 MB cache); brukes på watchlist og historikk.
- **Regel 9/1x-avslag lagres ikke** i `candidates` (kan aldri bli «nesten»).
- **20-min-flyt** brukes bare når ≥ 3 timer er dekket; ellers timestall.
- **Plan B for cron:** pg_cron-jobben `jita-vakt` kl. :23 og :40 starter timesjobben via `/api/scan?fallback=1` hvis den er > 50 min gammel – databasen er nå primær klokke, GitHubs egen cron bare bonus.
- **Overbuds-vakt (spec 2.4):** kjøres i times- og watchlist-jobben for alle åpne kjøpsordrer i `decisions`, per vare mot din høyeste egen pris. Gebyrformel fra CCP: `broker × (P2 − P1) × antall` (ved økning) `+ (50 % − 6 % × Advanced Broker Relations) × broker × P2 × antall`. Spec-en hadde 10 %/nivå for ABR – rettet til 6 %. Råd: ENDRE bare hvis muren over deg er > 5 dagers flyt *og* forventet netto neste 24 t > 2 × gebyr; TREKK hvis marginen ved ny pris < terskel; ellers HOLD. Lagres i `jita.alerts` (kind `overbid` / `overbid_cleared`), varsles på Discord ved endring.

## Fase 2 – EVE-innlogging (fra 16. sept 2026)

- App registrert på developers.eveonline.com (callback `https://jita-eve.vercel.app/api/sso`). Vercel env: `EVE_CLIENT_ID`, `EVE_CLIENT_SECRET`.
- `lib/eve.js`: SSO (authorization code + signert `state`), token-refresh, `syncCharacter()` – wallet → `profile.cash_isk`, skills/standings → profil, ordrer → `my_orders` (+ `decisions` automatisk via `order_id`), transaksjoner → `my_transactions` (lukker beslutninger når solgt), hangar → `my_assets`. Varsler: ulistet lager og utløp < 24 t (`alerts`, Discord hvis `DISCORD_WEBHOOK` er satt i Vercel).
- Kjøres av pg_cron `jita-eve-sync` kl. :50 og manuelt fra Profil («Oppdater fra EVE nå»). Refresh-token ligger kun i `jita.sso_tokens`.
