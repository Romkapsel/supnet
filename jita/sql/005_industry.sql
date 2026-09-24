-- Jita – industri-marginfinner (steg 1: produksjon). Kjøres én gang mot Supabase «Supnet».
-- Idempotent (create if not exists / create or replace). Rører ikke tabellene til station-trading-delen.
--
-- Kort fortalt: jita.blueprints + jita.blueprint_materials er oppskriftene (fra SDE),
-- jita.market_quotes er Jita-prisene på materialer og produkter, jita.industry_systems er
-- kostnadsindeksen der du produserer, og jita.industry_candidates er rangeringen roboten skriver.

create schema if not exists jita;

-- ── Industri-profil (én rad): hvor du produserer og med hvilke forutsetninger ─
create table if not exists jita.industry_profile (
  id int primary key default 1 check (id = 1),
  system_id int default 30001395,              -- Ylandoki, Lonetrek, 0.8
  system_name text default 'Ylandoki',
  jumps_from_jita int default 3,               -- for hauling-regnestykket
  sell_station_id bigint default 60003760,     -- Jita IV-4
  me int default 10, te int default 20,        -- blueprint research (NPC-BPO → ME 10 / TE 20)
  facility_tax numeric default 0.0025,         -- NPC-stasjon: 0,25 %
  scc_rate numeric default 0.04,               -- SCC-avgift, 4 % av EIV
  industry int default 5,                      -- skills: −4 % tid per nivå
  advanced_industry int default 3,             -- −3 % tid per nivå
  mass_production int default 0,               -- +1 slot per nivå
  adv_mass_production int default 0,           -- +1 slot per nivå
  slots int,                                   -- null = 1 + mass_production + adv_mass_production
  sell_fee_override numeric,                   -- null = broker + skatt fra jita.profile
  material_source text default 'buy' check (material_source in ('buy', 'sell')),
  -- 'buy'  = du legger kjøpsordre i Jita og venter (høyeste buy-pris)
  -- 'sell' = du kjøper instant fra laveste sell-pris
  thresholds jsonb default '{
    "min_margin": 0.10,
    "max_margin": 3.0,
    "min_daily_volume": 20,
    "min_sell_orders": 5,
    "max_spread": 0.60,
    "max_price_drop_30d": 0.15,
    "max_price_spike": 3.0,
    "max_bpo_price": 50000000,
    "max_capital_per_job": 20000000,
    "max_payback_days": 30,
    "volume_share": 0.10,
    "batch_days": 1,
    "capital_share_per_job": 0.5
  }'::jsonb,
  updated_at timestamptz default now()
);
insert into jita.industry_profile (id) values (1) on conflict (id) do nothing;

comment on column jita.industry_profile.thresholds is
  'volume_share = hvor stor del av dagsvolumet du tillater å ta (10 %). batch_days = hvor lang én jobb bør være.';

-- ── Oppskrifter (SDE): blueprint → produkt ───────────────────────────────────
create table if not exists jita.blueprints (
  blueprint_type_id int primary key,
  product_type_id int not null,
  units_per_run int not null default 1,        -- ammo/probes gir mange enheter per run
  base_time_s int,                             -- manufacturing-tid per run, før TE og skills
  max_runs int,                                -- BPC-grense (null for BPO)
  npc_bpo boolean,                             -- selges BPO-en av NPC? (salgsordre med ≥ 365 d varighet)
  bpo_price numeric,                           -- laveste sell for BPO-en
  bpo_price_source text,                       -- 'jita' / 'forge' / 'esi_adjusted'
  bpo_checked_at timestamptz,
  source text,                                 -- hvor oppskriften kom fra (everef / fuzzwork)
  updated_at timestamptz default now()
);
create index if not exists blueprints_product on jita.blueprints (product_type_id);

create table if not exists jita.blueprint_materials (
  blueprint_type_id int not null,
  material_type_id int not null,
  quantity numeric not null,                   -- per run, FØR ME
  primary key (blueprint_type_id, material_type_id)
);

-- ── Jita-priser for materialer og produkter (Fuzzwork-aggregat, station 60003760) ─
-- Egen tabell fordi jita.type_hourly bare dekker forfilter-settet (spread ≥ 5 %),
-- og mineraler har alltid tynn spread og faller derfor utenfor.
create table if not exists jita.market_quotes (
  type_id int primary key,
  buy_max numeric, buy_volume bigint, buy_orders int,
  sell_min numeric, sell_volume bigint, sell_orders int,
  updated_at timestamptz default now()
);

-- ── ESI /markets/prices/ (adjusted price = grunnlaget for EIV og jobbavgiften) ─
create table if not exists jita.market_prices (
  type_id int primary key,
  adjusted_price numeric,
  average_price numeric,
  updated_at timestamptz default now()
);

-- ── ESI /industry/systems/ (kostnadsindekser per system) ─────────────────────
create table if not exists jita.industry_systems (
  system_id int primary key,
  manufacturing numeric,
  copying numeric, invention numeric, reaction numeric,
  updated_at timestamptz default now()
);

-- ── Rangeringen roboten skriver (én rad per produkt per kjøring) ─────────────
create table if not exists jita.industry_candidates (
  run_at timestamptz not null,
  product_type_id int not null,
  blueprint_type_id int,
  runs int, units int, units_per_run int,
  -- kostnad
  material_cost numeric,                       -- hele batchen, med gebyr hvis du kjøper fra sell
  job_cost numeric,                            -- jobbavgift + facility tax + SCC
  eiv numeric,                                 -- estimated item value (grunnlaget for avgiftene)
  total_cost numeric, cost_per_unit numeric,
  -- salg
  sell_price numeric,                          -- Jita sell min (der du legger deg)
  net_per_unit numeric,                        -- etter broker + skatt, minus kostpris
  margin numeric,
  -- tid og tempo
  time_per_run_s numeric, time_per_batch_s numeric,
  units_per_day_slot numeric,                  -- hva én slot rekker per døgn
  daily_volume numeric,                        -- ESI-historikk, 30-dagers snitt
  daily_volume_90d numeric,
  realistic_units_per_day numeric,             -- min(slot-kapasitet, volume_share × dagsvolum)
  isk_per_day_slot numeric,                    -- HOVEDTALLET rangeringen sorteres på
  isk_per_hour_slot numeric,
  -- kapital og risiko
  capital_per_job numeric,                     -- materialkost for én full batch
  bpo_price numeric, payback_days numeric,
  sell_orders int,                             -- konkurrenter i Jita
  price_avg_30d numeric, price_drop_30d numeric, price_volatility numeric,
  m3_in numeric, m3_out numeric,               -- volum inn (materialer) og ut (produkt) per batch
  -- dom
  score numeric,
  passed boolean,
  failed_rules text[],
  reason text,
  factors jsonb,
  primary key (run_at, product_type_id)
);
create index if not exists industry_candidates_run on jita.industry_candidates (run_at desc);
create index if not exists industry_candidates_type on jita.industry_candidates (product_type_id, run_at desc);

-- Siste kjøring, med navn – det API-et leser
create or replace view jita.industry_latest as
select c.*, t.name, t.group_name, t.category_id, t.market_group_path, b.npc_bpo
from jita.industry_candidates c
join jita.types t on t.type_id = c.product_type_id
left join jita.blueprints b on b.blueprint_type_id = c.blueprint_type_id
where c.run_at = (select max(run_at) from jita.industry_candidates);

-- ── Rydding: 90 dager historikk på kandidater, og bare de 300 beste per kjøring ─
create or replace function jita.cleanup_industry() returns void language plpgsql as $$
begin
  delete from jita.industry_candidates where run_at < now() - interval '90 days';
  -- behold alt for siste kjøring, ellers bare de 300 beste (historikk = trend, ikke fullt arkiv)
  delete from jita.industry_candidates c using (
    select run_at, product_type_id,
           row_number() over (partition by run_at order by isk_per_day_slot desc nulls last) rn
    from jita.industry_candidates
    where run_at < (select max(run_at) from jita.industry_candidates)) x
  where c.run_at = x.run_at and c.product_type_id = x.product_type_id and x.rn > 300;
  delete from jita.market_quotes where updated_at < now() - interval '30 days';
end $$;

-- ── Tilgang og RLS (samme regime som 001: bare service_role) ─────────────────
grant all on all tables in schema jita to service_role;
grant all on all sequences in schema jita to service_role;
grant execute on all functions in schema jita to service_role;
do $$
declare t text;
begin
  for t in select tablename from pg_tables where schemaname = 'jita' loop
    execute format('alter table jita.%I enable row level security', t);
  end loop;
end $$;

-- ── Klokke: rydding rett etter industri-jobben, og plan B for selve jobben ───
-- Industri-jobben kjøres daglig av GitHub Actions (05:40 UTC). pg_cron er reserve, som for timesjobben:
-- starter den via Vercel-API-et hvis siste kjøring er mer enn 26 timer gammel.
select cron.unschedule(jobid) from cron.job where jobname = 'jita-industri-rydd';
select cron.schedule('jita-industri-rydd', '20 6 * * *', 'select jita.cleanup_industry()');

select cron.unschedule(jobid) from cron.job where jobname = 'jita-industri-vakt';
select cron.schedule('jita-industri-vakt', '10 7 * * *',
  $$select net.http_post(url := 'https://jita-eve.vercel.app/api/scan?fallback=1&job=industry',
      headers := '{"x-jita-pin": "0000", "Content-Type": "application/json"}'::jsonb, body := '{}'::jsonb)$$);
