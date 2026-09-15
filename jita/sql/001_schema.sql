-- Jita – skjema (spec v3, del 5)
-- Kjøres én gang mot Supabase «Supnet». Idempotent (create if not exists).

create schema if not exists jita;

-- ── Varetyper (fra ESI /universe/types, klassifisert med dogma) ─────────────
create table if not exists jita.types (
  type_id int primary key,
  name text not null,
  group_id int, group_name text, category_id int, market_group_path text,
  meta_group_id int, meta_level int, published boolean,
  is_t1 boolean, is_meta boolean, is_t2 boolean, is_faction boolean,
  is_ship boolean, is_excluded boolean,
  volume numeric,
  updated_at timestamptz default now()
);
-- Klassifisering fra dogma (attributt 1692 metaGroupID, 633 metaLevel):
--   is_excluded : category_id in (blueprint 9, skin 91, apparel 30) eller group i mutaplasmid/filament-grupper
--   is_meta     : meta_group_id = 1 and meta_level > 0     (Compact, Enduring, Ample, 'Arbalest' …)
--   is_t2       : meta_group_id in (2, 14)                 (Tech II, Tech III)
--   is_faction  : meta_group_id in (3, 4, 5, 6, 52)        (storyline, faction, officer, deadspace, structure faction)
--   is_t1       : not is_excluded and not is_meta and not is_t2 and not is_faction
--   is_ship     : category_id = 6

-- ── Systemer i The Forge med antall hopp fra Jita (rekkevidde på kjøpsordrer) ─
create table if not exists jita.systems (
  system_id int primary key,
  name text,
  jumps_from_jita int
);

-- ── Enkelthendelser (fills), bare forfilter-sett + watchlist, 7 d ────────────
create table if not exists jita.fills (
  observed_at timestamptz not null,
  order_id bigint not null,
  type_id int not null,
  is_buy boolean not null,            -- true: dumpet (S2B); false: liftet (BfS)
  price numeric not null,
  qty int not null,
  kind text not null check (kind in ('partial','gone')),
  weight numeric not null default 1,  -- partial=1; gone: 0.8 nær toppen, 0.5 midt, 0.2 dypt
  resolution int not null default 60, -- 60 = timesjobb, 20 = watchlist-jobb
  primary key (observed_at, order_id)
);
create index if not exists fills_type_time on jita.fills (type_id, observed_at);

-- ── Timesaggregat av fills, 90 d ─────────────────────────────────────────────
create table if not exists jita.type_flow_hourly (
  type_id int not null,
  hour timestamptz not null,
  resolution int not null,
  bfs_qty numeric, bfs_trades int,
  s2b_qty numeric, s2b_trades int,
  hours_covered numeric,              -- faktisk tid mellom snapshots (normalisering)
  primary key (type_id, hour, resolution)
);
create index if not exists flow_hour on jita.type_flow_hourly (hour);

-- ── Ordrebok-øyeblikksbilde, forfilter-sett, 7 d ─────────────────────────────
create table if not exists jita.type_hourly (
  type_id int not null,
  snapshot_at timestamptz not null,
  best_bid numeric, best_ask numeric, -- best_bid over alle ordrer som dekker Jita 4-4
  bid_top_qty int, bid_orders_1pct int, bid_qty_1pct int,
  ask_orders_1pct int, ask_qty_1pct int, ask_qty_3pct int,
  bid_floor_price numeric, bid_floor_qty int,
  primary key (type_id, snapshot_at)
);
create index if not exists type_hourly_snap on jita.type_hourly (snapshot_at);

-- ── Dagsaggregat (type_hourly > 7 d rulles hit) ──────────────────────────────
create table if not exists jita.type_daily (
  type_id int not null,
  date date not null,
  best_bid_avg numeric, best_ask_avg numeric,
  bfs_qty numeric, s2b_qty numeric,
  bid_top_qty_avg int, ask_qty_1pct_avg int,
  primary key (type_id, date)
);

-- ── ESI-historikk (regionsnitt The Forge) ────────────────────────────────────
create table if not exists jita.history_daily (
  type_id int not null,
  date date not null,
  average numeric, highest numeric, lowest numeric,
  volume bigint, order_count int,
  primary key (type_id, date)
);

-- ── Profil (én rad) ──────────────────────────────────────────────────────────
create table if not exists jita.profile (
  id int primary key default 1 check (id = 1),
  capital_isk numeric default 4500000,
  broker_relations int default 4, accounting int default 0, adv_broker_relations int default 0,
  trade int default 4, retail int default 3, wholesale int default 0, tycoon int default 0,
  standing_corp numeric default 0, standing_faction numeric default 0,
  broker_fee_override numeric, sales_tax_override numeric,
  positions int default 7, target_fill_days numeric default 4, reserve_share numeric default 0.25,
  min_qty int default 20,
  allow_t2 boolean default false, allow_faction boolean default false,
  thresholds jsonb default '{
    "min_margin": 0.10,
    "max_bid_top_qty": 100,
    "max_bid_orders_1pct": 3,
    "max_ask_qty_1pct": 300,
    "min_bfs_trades": 10,
    "hist_pos_target": 0.7,
    "hist_pos_weak": 0.4,
    "max_ask_drop_7d": 0.15,
    "max_bid_rise_7d": 0.25,
    "prefilter_spread": 0.05
  }'::jsonb,
  last_manual_scan timestamptz,
  updated_at timestamptz default now()
);
insert into jita.profile (id) values (1) on conflict (id) do nothing;

-- ── Dommerens output ─────────────────────────────────────────────────────────
create table if not exists jita.candidates (
  run_at timestamptz not null,
  type_id int not null,
  passed boolean,
  failed_rules text[],
  buy_price numeric, sell_price numeric, qty int,
  net_per_unit numeric, margin numeric, expected_profit numeric,
  days_to_fill_buy numeric, days_to_fill_sell numeric,
  hist_pos numeric, flow_ratio numeric, score numeric,
  reason text,
  primary key (run_at, type_id)
);
create index if not exists candidates_run on jita.candidates (run_at desc);

-- ── Watchlist ────────────────────────────────────────────────────────────────
create table if not exists jita.watchlist (
  type_id int primary key,
  status text not null default 'follow' check (status in ('follow','ignore')),
  note text,
  updated_at timestamptz default now()
);

-- ── Lære-sløyfen ─────────────────────────────────────────────────────────────
create table if not exists jita.decisions (
  id bigserial primary key,
  created_at timestamptz default now(),
  type_id int not null,
  side text check (side in ('buy','sell')),
  price numeric, qty int,
  predicted_days numeric, predicted_net_per_unit numeric,
  filled_at timestamptz, filled_qty int, sell_price numeric, closed_at timestamptz,
  note text
);

-- ── Robotstatus ──────────────────────────────────────────────────────────────
create table if not exists jita.robot_runs (
  run_at timestamptz primary key,
  job text,
  snapshot_at timestamptz,
  pages_total int, pages_ok int, orders_count int,
  ratelimit_remaining int, duration_s numeric, db_bytes bigint,
  ok boolean, message text
);

-- ── Fase 2 ───────────────────────────────────────────────────────────────────
create table if not exists jita.my_orders (
  order_id bigint primary key, type_id int, is_buy boolean, price numeric,
  volume_remain int, volume_total int, issued timestamptz, duration int, state text,
  first_seen timestamptz, last_seen timestamptz
);
create table if not exists jita.my_transactions (
  transaction_id bigint primary key, date timestamptz, type_id int,
  is_buy boolean, unit_price numeric, quantity int, location_id bigint, journal_ref_id bigint
);
create table if not exists jita.pnl (
  sell_transaction_id bigint primary key, type_id int, quantity int,
  buy_cost numeric, sell_net numeric, profit numeric, margin numeric, matched_buy_ids bigint[]
);
create table if not exists jita.alerts (
  id bigserial primary key, created_at timestamptz default now(), kind text,
  type_id int, order_id bigint, payload jsonb, seen boolean default false
);

-- ── Tilgang ──────────────────────────────────────────────────────────────────
-- Skjemaet nås BARE med service-nøkkelen (roboten i GitHub Actions og Vercel-funksjonene).
-- anon/authenticated får ingenting; sidene går via /api/jita/* som sjekker PIN.
revoke all on schema jita from public, anon, authenticated;
grant usage on schema jita to service_role;
grant all on all tables in schema jita to service_role;
grant all on all sequences in schema jita to service_role;
grant execute on all functions in schema jita to service_role;
alter default privileges in schema jita grant all on tables to service_role;
alter default privileges in schema jita grant all on sequences to service_role;
alter default privileges in schema jita grant execute on functions to service_role;

-- RLS på alle tabeller (service_role går forbi RLS; ingen andre har policy → ingen tilgang)
do $$
declare t text;
begin
  for t in select tablename from pg_tables where schemaname = 'jita' loop
    execute format('alter table jita.%I enable row level security', t);
  end loop;
end $$;

-- ── Rydding (pg_cron, daglig 05:00 UTC) ──────────────────────────────────────
create or replace function jita.cleanup() returns void language plpgsql as $$
begin
  -- type_hourly > 7 d → type_daily
  insert into jita.type_daily (type_id, date, best_bid_avg, best_ask_avg, bfs_qty, s2b_qty, bid_top_qty_avg, ask_qty_1pct_avg)
  select h.type_id, (h.snapshot_at at time zone 'utc')::date,
         avg(h.best_bid), avg(h.best_ask),
         (select coalesce(sum(f.bfs_qty),0) from jita.type_flow_hourly f
           where f.type_id = h.type_id and f.resolution = 60
             and (f.hour at time zone 'utc')::date = (h.snapshot_at at time zone 'utc')::date),
         (select coalesce(sum(f.s2b_qty),0) from jita.type_flow_hourly f
           where f.type_id = h.type_id and f.resolution = 60
             and (f.hour at time zone 'utc')::date = (h.snapshot_at at time zone 'utc')::date),
         avg(h.bid_top_qty)::int, avg(h.ask_qty_1pct)::int
  from jita.type_hourly h
  where h.snapshot_at < now() - interval '7 days'
  group by h.type_id, (h.snapshot_at at time zone 'utc')::date
  on conflict (type_id, date) do update set
    best_bid_avg = excluded.best_bid_avg, best_ask_avg = excluded.best_ask_avg,
    bfs_qty = excluded.bfs_qty, s2b_qty = excluded.s2b_qty,
    bid_top_qty_avg = excluded.bid_top_qty_avg, ask_qty_1pct_avg = excluded.ask_qty_1pct_avg;

  delete from jita.type_hourly where snapshot_at < now() - interval '7 days';
  delete from jita.fills where observed_at < now() - interval '7 days';
  delete from jita.type_flow_hourly where hour < now() - interval '90 days';
  delete from jita.candidates where run_at < now() - interval '30 days';
  delete from jita.robot_runs where run_at < now() - interval '90 days';
  delete from jita.history_daily where date < current_date - 400;
end $$;

select cron.unschedule(jobid) from cron.job where jobname = 'jita-cleanup';
select cron.schedule('jita-cleanup', '0 5 * * *', 'select jita.cleanup()');
