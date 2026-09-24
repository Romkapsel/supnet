-- Jita – mining-laget (steg 2). Kjøres én gang mot Supabase «Supnet». Idempotent.
--
-- Svarer på: hvilken malm gir mest ISK per time, og er det best å refine den eller selge den rå?
-- Refine-utbyttene kommer fra SDE (typematerials), prisene fra jita.market_quotes (Jita 4-4),
-- og hva som er tilgjengelig der du er styres av gruppenavn i mining_profile.thresholds.

create schema if not exists jita;

-- ── Mining-profil (én rad) ───────────────────────────────────────────────────
create table if not exists jita.mining_profile (
  id int primary key default 1 check (id = 1),
  reprocess_yield numeric default 0.52,     -- NPC-stasjon 50 % × skills; struktur gir mer
  m3_per_hour numeric default 3000,         -- hva du faktisk henter inn per time (skip + skills)
  jumps_from_jita int default 3,
  thresholds jsonb default '{
    "min_daily_volume": 100,
    "min_trades_per_day": 3,
    "available_groups": ["Veldspar", "Scordite", "Pyroxeres", "Plagioclase", "Omber", "Kernite"]
  }'::jsonb,
  updated_at timestamptz default now()
);
insert into jita.mining_profile (id) values (1) on conflict (id) do nothing;

comment on column jita.mining_profile.thresholds is
  'available_groups = malmgruppene som finnes der du miner (0.8 Lonetrek som standard). Rediger i fanen.';

-- ── Refine-utbytte: hva én batch malm gir av mineraler (fra SDE) ─────────────
create table if not exists jita.ore_yields (
  ore_type_id int not null,
  mineral_type_id int not null,
  quantity numeric not null,                -- per batch, FØR reprocess_yield
  batch_size int not null default 100,      -- portion size (Veldspar 100, komprimert 1 …)
  source text,
  updated_at timestamptz default now(),
  primary key (ore_type_id, mineral_type_id)
);

-- ── Rangeringen roboten skriver ──────────────────────────────────────────────
create table if not exists jita.mining_candidates (
  run_at timestamptz not null,
  ore_type_id int not null,
  volume numeric,                           -- m3 per enhet
  batch_size int,
  -- refine
  refined_value_per_unit numeric,           -- netto etter gebyr og reprocess_yield
  refined_value_per_m3 numeric,
  -- selge rå (eller komprimert)
  raw_net_per_unit numeric,
  raw_net_per_m3 numeric,
  raw_route text,                           -- 'salgsordre' eller 'dumping'
  -- komprimering er en SALGSVEI, ikke en egen rad: du miner rå malm
  compressed_type_id int,
  compression_ratio numeric,                -- rå enheter per komprimert enhet (fra utbyttedata)
  compressed_net_per_unit numeric,
  compressed_net_per_m3 numeric,            -- per m3 RÅ malm minet
  refine_premium numeric,                   -- hvor mye mer refine gir enn å selge varen
  -- dom
  best_route text,                          -- 'refine' / 'raw' / 'compressed'
  best_value_per_m3 numeric,
  isk_per_hour numeric,                     -- best_value_per_m3 × m3_per_hour
  market_type_id int,                       -- varen du faktisk selger på beste vei
  market_daily_volume numeric,              -- og hvordan DET markedet flyter
  market_trades_per_day numeric,
  ore_daily_volume numeric,                 -- markedet for rå malm (til opplysning)
  ore_trades_per_day numeric,
  mineral_mix jsonb,                         -- {mineralnavn: andel av verdien}
  available boolean,                         -- finnes gruppen der du miner?
  failed_rules text[],                       -- m1 … m4, se scripts/mining.py
  score numeric,
  notes text,
  primary key (run_at, ore_type_id)
);
create index if not exists mining_candidates_run on jita.mining_candidates (run_at desc);

create or replace view jita.mining_latest as
select c.*, t.name, t.group_name, k.name as compressed_name
from jita.mining_candidates c
join jita.types t on t.type_id = c.ore_type_id
left join jita.types k on k.type_id = c.compressed_type_id
where c.run_at = (select max(run_at) from jita.mining_candidates);

create or replace function jita.cleanup_mining() returns void language plpgsql as $$
begin
  delete from jita.mining_candidates where run_at < now() - interval '90 days';
end $$;

-- ── Tilgang og RLS (samme regime som 001) ────────────────────────────────────
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

-- Ryddingen henger på den som alt finnes for industri (06:20).
select cron.unschedule(jobid) from cron.job where jobname = 'jita-mining-rydd';
select cron.schedule('jita-mining-rydd', '25 6 * * *', 'select jita.cleanup_mining()');
