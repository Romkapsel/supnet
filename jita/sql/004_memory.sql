-- Jita – varens «rulleblad» (lag 1) og strukturelle flagg (lag 2)

-- NPC-seedet: salgsordrer med 365 dagers varighet i regionen = uendelig tilbud, prislokk. Settes av timesjobben.
alter table jita.types add column if not exists npc_seeded boolean default false;
alter table jita.types add column if not exists npc_seed_price numeric;

-- Rulleblad per vare fra egne transaksjoner, beslutninger og varsler. Regnes daglig (og ved synk).
create table if not exists jita.type_memory (
  type_id int primary key,
  rounds int,                    -- antall salgsmatcher (FIFO)
  sold_qty bigint,
  realized_margin numeric,       -- (salg − kost − skatt − broker) / kost
  avg_hold_days numeric,         -- fra kjøp til salg
  overbid_count int,             -- ganger overbudt (alerts)
  undercut_count int,
  fill_ratio numeric,            -- faktisk / spådd fyllingstid (beslutninger med begge)
  last_traded timestamptz,
  verdict text,                  -- 'god' | 'ok' | 'treg' | 'krangel' | null
  factor numeric default 1,      -- multiplikator på score i dommeren (0,5–1,3)
  updated_at timestamptz default now()
);
alter table jita.type_memory enable row level security;
grant all on all tables in schema jita to service_role;

create or replace function jita.refresh_type_memory() returns int language plpgsql as $$
declare n int;
begin
  -- FIFO per vare i SQL er tungt; vi bruker en enklere, robust tilnærming:
  -- snitt kjøpspris og snitt salgspris per vare, hold-tid = snitt(salgsdato) − snitt(kjøpsdato) for perioder med begge.
  with b as (
    select type_id, sum(quantity) q, sum(unit_price*quantity)/sum(quantity) p, min(date) first_buy, max(date) last_buy
    from jita.my_transactions where is_buy group by type_id),
  s as (
    select type_id, sum(quantity) q, sum(unit_price*quantity)/sum(quantity) p, count(*) rounds, max(date) last_sell,
           sum(quantity * extract(epoch from date)) / sum(quantity) as w_sell_t
    from jita.my_transactions where not is_buy group by type_id),
  bw as (
    select type_id, sum(quantity * extract(epoch from date)) / sum(quantity) as w_buy_t from jita.my_transactions where is_buy group by type_id),
  al as (
    select type_id,
           count(*) filter (where kind = 'overbid') as ob,
           count(*) filter (where kind = 'undercut') as uc
    from jita.alerts group by type_id),
  dec as (
    select type_id, avg(extract(epoch from (filled_at - created_at)) / 86400 / nullif(predicted_days, 0)) as fr
    from jita.decisions where filled_at is not null and predicted_days > 0 group by type_id),
  pr as (select c.broker, c.tax from jita.profile_calc(jita.effective_profile()) c),
  m as (
    select coalesce(s.type_id, b.type_id) type_id,
           coalesce(s.rounds, 0) rounds, coalesce(s.q, 0) sold_qty,
           case when s.p is not null and b.p is not null and b.p > 0
                then (s.p * (1 - pr.broker - pr.tax) - b.p * (1 + pr.broker)) / (b.p * (1 + pr.broker)) end as realized_margin,
           case when s.w_sell_t is not null and bw.w_buy_t is not null then greatest(0, (s.w_sell_t - bw.w_buy_t) / 86400) end as avg_hold_days,
           coalesce(al.ob, 0) ob, coalesce(al.uc, 0) uc, dec.fr,
           greatest(s.last_sell, b.last_buy) last_traded
    from b full join s using (type_id) left join bw using (type_id) left join al using (type_id) left join dec using (type_id), pr)
  insert into jita.type_memory (type_id, rounds, sold_qty, realized_margin, avg_hold_days, overbid_count, undercut_count, fill_ratio, last_traded, verdict, factor, updated_at)
  select type_id, rounds, sold_qty, realized_margin, avg_hold_days, ob, uc, fr, last_traded,
         case when rounds >= 2 and (ob + uc)::numeric / greatest(rounds, 1) >= 1.5 then 'krangel'
              when rounds >= 2 and realized_margin >= 0.15 and coalesce(avg_hold_days, 0) <= 4 then 'god'
              when rounds >= 1 and coalesce(avg_hold_days, 0) > 7 then 'treg'
              when rounds >= 1 and realized_margin < 0.05 then 'svak'
              when rounds >= 1 then 'ok' end,
         case when rounds >= 2 and (ob + uc)::numeric / greatest(rounds, 1) >= 1.5 then 0.75
              when rounds >= 2 and realized_margin >= 0.15 and coalesce(avg_hold_days, 0) <= 4 then 1.2
              when rounds >= 1 and coalesce(avg_hold_days, 0) > 7 then 0.6
              when rounds >= 1 and realized_margin < 0.05 then 0.6
              else 1.0 end,
         now()
  from m
  on conflict (type_id) do update set rounds = excluded.rounds, sold_qty = excluded.sold_qty, realized_margin = excluded.realized_margin,
    avg_hold_days = excluded.avg_hold_days, overbid_count = excluded.overbid_count, undercut_count = excluded.undercut_count,
    fill_ratio = excluded.fill_ratio, last_traded = excluded.last_traded, verdict = excluded.verdict, factor = excluded.factor, updated_at = now();
  get diagnostics n = row_count;
  return n;
end $$;

-- daglig oppfrisking sammen med ryddingen
select cron.unschedule(jobid) from cron.job where jobname = 'jita-memory';
select cron.schedule('jita-memory', '10 5 * * *', 'select jita.refresh_type_memory()');
