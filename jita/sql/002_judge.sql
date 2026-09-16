-- Jita – dommeren (spec v3, del 2, 5 og 6).
-- Formlene her skal gi IDENTISKE tall som jita/scripts/common.py (test_judge.py sjekker det).
-- Alt regnes i double precision for å matche Python-float.

-- ── Formler ──────────────────────────────────────────────────────────────────
create or replace function jita.tick(p double precision) returns double precision
language sql immutable as $$ select power(10::double precision, floor(log(p)) - 3) $$;

create or replace function jita.gone_weight(order_price double precision, best_price double precision, is_buy boolean)
returns double precision language sql immutable as $$
  select case
    when best_price <= 0 then 0.5
    when (case when is_buy then (best_price - order_price) / best_price else (order_price - best_price) / best_price end) <= 0.01 then 0.8
    when (case when is_buy then (best_price - order_price) / best_price else (order_price - best_price) / best_price end) <= 0.05 then 0.5
    else 0.2 end
$$;

-- Gebyrer m.m. fra en profil som jsonb → én rad med utledede tall
create or replace function jita.profile_calc(p jsonb)
returns table (broker double precision, tax double precision, break_even double precision,
               order_slots int, budget double precision, max_buy_price double precision,
               min_net_per_unit double precision)
language sql immutable as $$
  with v as (
    select coalesce((p->>'broker_fee_override')::float8,
             (p->>'broker_fee_measured')::float8,                      -- målt fra journalen (fase 2) slår formelen
             greatest(0.01, 0.03 - 0.003 * coalesce((p->>'broker_relations')::float8, 0)
                                 - 0.0003 * greatest(0, coalesce((p->>'standing_faction')::float8, 0))
                                 - 0.0002 * greatest(0, coalesce((p->>'standing_corp')::float8, 0)))) as broker,
           coalesce((p->>'sales_tax_override')::float8,
             0.075 * (1 - 0.11 * coalesce((p->>'accounting')::float8, 0))) as tax,
           coalesce((p->>'capital_isk')::float8, 0) as cap,
           coalesce((p->>'reserve_share')::float8, 0.25) as reserve,
           greatest(1, coalesce((p->>'positions')::int, 7)) as positions,
           greatest(1, coalesce((p->>'min_qty')::int, 20)) as min_qty
  )
  select broker, tax,
         (1 + broker) / (1 - broker - tax) - 1,
         5 + 4 * coalesce((p->>'trade')::int, 0) + 8 * coalesce((p->>'retail')::int, 0)
           + 16 * coalesce((p->>'wholesale')::int, 0) + 32 * coalesce((p->>'tycoon')::int, 0),
         cap * (1 - reserve) / positions,
         cap * (1 - reserve) / positions / min_qty,
         cap / 1000
  from v
$$;

-- ── Kjernen: dømmer alle varer i nyeste type_hourly med profilen p ───────────
do $$ begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where n.nspname = 'jita' and t.typname = 'judgement') then
    execute 'create type jita.judgement as (
  type_id int, passed boolean, failed_rules text[],
  buy_price double precision, sell_price double precision, qty bigint,
  net_per_unit double precision, margin double precision, expected_profit double precision,
  days_to_fill_buy double precision, days_to_fill_sell double precision,
  hist_pos double precision, flow_ratio double precision, score double precision,
  reason text,
  -- ekstra kolonner til «nesten»-lista og type-siden
  best_bid double precision, best_ask double precision, bid_top_qty bigint, bid_orders_1pct int,
  ask_qty_1pct bigint, s2b_per_day double precision, bfs_per_day double precision, bfs_trades int,
  name text, market_group_path text
)';
  end if;
end $$;

create or replace function jita.judge_rows(p jsonb)
returns setof jita.judgement
language sql stable as $$
  with pc as (select * from jita.profile_calc(p)),
  th as (select (p->'thresholds') as t),
  snap as (select max(snapshot_at) as at from jita.type_hourly),
  book as (select h.* from jita.type_hourly h, snap where h.snapshot_at = snap.at),
  fl as (
    select f.type_id, f.resolution,
           sum(f.bfs_qty)::float8 bfs_qty, sum(f.bfs_trades)::int bfs_trades,
           sum(f.s2b_qty)::float8 s2b_qty, sum(f.s2b_trades)::int s2b_trades,
           sum(f.hours_covered)::float8 hours, count(*)::int runs,
           sum(coalesce(f.bid_mods, 0) + coalesce(f.ask_mods, 0))::float8 mods
    from jita.type_flow_hourly f
    where f.hour >= now() - interval '25 hours'
    group by f.type_id, f.resolution
  ),
  flow as (select distinct on (type_id) * from fl order by type_id, (resolution = 20 and hours >= 3 and runs >= 12) desc),   -- 20-min-tall bare når de er TETTE (≥ 12 kjøringer/døgn) – glisne målinger lyver
  hist as (
    select hd.type_id,
           avg(hd.average) filter (where hd.date >= current_date - 1)::float8  a1,
           avg(hd.average) filter (where hd.date >= current_date - 5)::float8  a5,
           avg(hd.average) filter (where hd.date >= current_date - 20)::float8 a20,
           avg(hd.order_count) filter (where hd.date >= current_date - 5)::float8 oc5,  -- ekte handler/dag (hele The Forge)
           avg(hd.volume) filter (where hd.date >= current_date - 5)::float8 vol5        -- enheter/dag (tak for flyt-anslag)
    from jita.history_daily hd group by hd.type_id
  ),
  d7 as (
    select coalesce(td.type_id, hd.type_id) type_id,
           coalesce(td.best_ask_avg, hd.lowest)::float8 ask7,
           coalesce(td.best_bid_avg, hd.highest)::float8 bid7
    from (select * from jita.type_daily where date = current_date - 7) td
    full join (select * from jita.history_daily where date = current_date - 7) hd using (type_id)
  ),
  base as (
    select b.type_id, t.name, t.market_group_path,
           b.best_bid::float8 bid, b.best_ask::float8 ask,
           b.bid_top_qty, b.bid_orders_1pct, b.bid_qty_1pct, b.ask_qty_1pct,
           t.is_excluded, t.is_meta, t.is_t2, t.is_faction, t.is_t1, t.npc_seeded,
           mem.verdict as mem_verdict, coalesce(mem.factor, 1)::float8 as mem_factor, mem.rounds as mem_rounds,
           mem.avg_hold_days::float8 as mem_hold, mem.realized_margin::float8 as mem_margin,
           least(coalesce(f.s2b_qty / greatest(f.hours, 1) * 24, 0), coalesce(h.vol5, 1e12))::float8 s2b,   -- tak: regionens dagsvolum
           least(coalesce(f.bfs_qty / greatest(f.hours, 1) * 24, 0), coalesce(h.vol5, 1e12))::float8 bfs,
           coalesce(f.mods / greatest(f.hours, 1), 0)::float8 mods_per_hour,
           coalesce(f.bfs_trades, 0) bfs_trades,
           h.a1, h.a5, h.a20, h.oc5, h.vol5, d.ask7, d.bid7,
           w.status as wl_status
    from book b
    join jita.types t on t.type_id = b.type_id
    left join flow f on f.type_id = b.type_id
    left join hist h on h.type_id = b.type_id
    left join d7 d on d.type_id = b.type_id
    left join jita.watchlist w on w.type_id = b.type_id
    left join jita.type_memory mem on mem.type_id = b.type_id
    where coalesce(w.status, '') <> 'ignore'
  ),
  econ as (
    select base.*, pc.*,
           bid + jita.tick(bid) as buy,
           ask - jita.tick(ask) as sell
    from base, pc
  ),
  e2 as (
    select econ.*,
           (sell * (1 - broker - tax)) - (buy * (1 + broker)) as net,
           ((sell * (1 - broker - tax)) - (buy * (1 + broker))) / (buy * (1 + broker)) as mrg,
           greatest(1, trunc(least(floor(budget / buy), s2b * coalesce((p->>'target_fill_days')::float8, 4))))::bigint as q
    from econ
  ),
  e3 as (
    select e2.*,
           (bid_qty_1pct + q) / greatest(s2b, 0.1) as dfb,   -- «foran deg»: budgiverne innenfor 1 % legger seg over deg igjen
           (ask_qty_1pct + q) / greatest(bfs, 0.1) as dfs,
           bfs / greatest(s2b, 0.1) as ratio,
           case when ask > bid then least(
                  coalesce((coalesce(a1, a5) - bid) / (ask - bid), 9),
                  coalesce((a5 - bid) / (ask - bid), 9),
                  coalesce((a20 - bid) / (ask - bid), 9))
                else null end as hp_raw
    from e2
  ),
  e4 as (
    select e3.*,
           case when hp_raw is null or hp_raw = 9 then null else hp_raw end as hp,
           array_remove(array[
             case when mrg < coalesce((th.t->>'min_margin')::float8, 0.10) then '1' end,
             -- 1b (endret 16. sept 2026): posisjonen må monne – forventet fortjeneste ≥ andel av kapitalen (1 %).
             -- Spec-ens «netto/enhet ≥ kapital/1000» stoppet Amarr Shuttle (6k × 100 stk = 600k) ved 12 mill kapital.
             case when q * net < coalesce((p->>'capital_isk')::float8, 0) * coalesce((th.t->>'min_position_profit_share')::float8, 0.01) then '1b' end,
             case when mrg > coalesce((th.t->>'max_margin')::float8, 2.0) then '1x' end,   -- ingen ekte bud (0,01-ISK-bud o.l.)
             -- 2 og 4 (endret 16. sept 2026): en mur/klump er bare et problem hvis den er over terskelen OG tar > wall_days å tømme med dagens flyt
             case when bid_top_qty > coalesce((th.t->>'max_bid_top_qty')::int, 100)
                   and bid_top_qty / greatest(s2b, 0.1) > coalesce((th.t->>'wall_days')::float8, 2) then '2' end,
             case when bid_orders_1pct > coalesce((th.t->>'max_bid_orders_1pct')::int, 3) then '3' end,
             case when ask_qty_1pct > coalesce((th.t->>'max_ask_qty_1pct')::int, 300)
                   and ask_qty_1pct / greatest(bfs, 0.1) > coalesce((th.t->>'wall_days')::float8, 2) then '4' end,
             case when s2b * coalesce((p->>'target_fill_days')::float8, 4) < greatest(1, coalesce((p->>'min_qty')::int, 20)) then '5' end,
             case when bfs_trades < coalesce((th.t->>'min_bfs_trades')::int, 10)
                   and coalesce(oc5, 0) < 3 * coalesce((th.t->>'min_bfs_trades')::int, 10) then '5t' end,   -- timesdiffen undervurderer; ESI-historikk (regionen) som kryssjekk
             case when ask7 > 0 and (ask7 - ask) / ask7 > coalesce((th.t->>'max_ask_drop_7d')::float8, 0.15) then '7' end,
             case when bid7 > 0 and (bid - bid7) / bid7 > coalesce((th.t->>'max_bid_rise_7d')::float8, 0.25) then '7b' end,
             case when buy > max_buy_price then '8' end,
             case when npc_seeded and not coalesce((p->>'allow_npc_seeded')::boolean, false) then '9n' end,   -- NPC-seedet: uendelig tilbud, prislokk
             case when mods_per_hour >= 3 * coalesce((th.t->>'war_mods_per_hour')::float8, 4) then '10' end,   -- priskrig: ≥ 3× terskel prisendringer/t nær toppen
             case when mods_per_hour >= 3 * coalesce((th.t->>'war_mods_per_hour')::float8, 4) then '10' end,   -- priskrig: ≥ 3× terskel prisendringer/t nær toppen
             case when is_excluded or is_meta
                    or (is_t2 and not coalesce((p->>'allow_t2')::boolean, false))
                    or (is_faction and not coalesce((p->>'allow_faction')::boolean, false)) then '9' end
           ]::text[], null) as failed
    from e3, th
  ),
  e5 as (
    select e4.*,
           net * least(s2b, bfs) / (1 + dfb + dfs)
             * least(1.0, greatest(coalesce(hp, 0.7), 0) / 0.7)
             * least(1.0, ratio)
             * mem_factor                                          -- rulleblad: god 1,2 · ok 1 · treg/svak 0,6 · krangel 0,75
             * least(1.0, coalesce((th.t->>'war_mods_per_hour')::float8, 4) / greatest(mods_per_hour, 0.001)) as sc   -- priskrig: myk straff
    from e4, th
  )
  select type_id, cardinality(failed) = 0 as passed, failed,
         buy, sell, q, net, mrg, q * net,
         dfb, dfs, hp, ratio, sc,
         format('Toppbud %s stk, %s budgivere. ~%s/dag inn, ~%s/dag ut. Netto %s ISK (%s %%).%s',
                bid_top_qty, bid_orders_1pct, round(s2b::numeric), round(bfs::numeric),
                replace(to_char(net, 'FM999,999,999'), ',', ' '), replace(round((mrg * 100)::numeric, 1)::text, '.', ','),
                case when hp < coalesce((th.t->>'hist_pos_weak')::float8, 0.4) or ratio < 1
                       or dfb > coalesce((p->>'target_fill_days')::float8, 4) or dfs > coalesce((p->>'target_fill_days')::float8, 4)
                       or mods_per_hour > coalesce((th.t->>'war_mods_per_hour')::float8, 4)
                  then ' Svakhet: ' || concat_ws(', ',
                         case when mods_per_hour > coalesce((th.t->>'war_mods_per_hour')::float8, 4) then format('priskrig (%s endringer/t)', round(mods_per_hour::numeric, 1)) end,
                         case when hp < coalesce((th.t->>'hist_pos_weak')::float8, 0.4) then 'handles nær bid' end,
                         case when ratio < 1 then 'mer dumping enn lifting' end,
                         case when dfb > coalesce((p->>'target_fill_days')::float8, 4) then 'tregt inn' end,
                         case when dfs > coalesce((p->>'target_fill_days')::float8, 4) then 'tregt ut' end) || '.'
                  else '' end)
           || case when hp is null then ' ⚠ Mangler historikk (regel 6 nøytral, regel 7 ikke vurdert).' else '' end
           || case when mem_rounds >= 1 then format(' Erfaring: %s (%s runder, %s %%, ~%s d).', coalesce(mem_verdict, 'ok'), mem_rounds,
                        round((coalesce(mem_margin, 0) * 100)::numeric), round(coalesce(mem_hold, 0)::numeric, 1)) else '' end as reason,
         bid, ask, bid_top_qty, bid_orders_1pct, ask_qty_1pct, s2b, bfs, bfs_trades, name, market_group_path
  from e5, th
$$;

-- ── Kapital i arbeid = cash + bundet ─────────────────────────────────────────
-- Bundet kapital = escrow i åpne kjøpsordrer + lager til kostpris (hangar + varer i salgsordrer, snitt kjøpspris siste 90 d).
-- Uten EVE-innlogging: gammel tilnærming fra decisions.
create or replace function jita.bound_isk() returns numeric language sql stable as $$
  with esc as (select coalesce(sum(volume_remain * price), 0) as v from jita.my_orders where state = 'open' and is_buy),
  avgbuy as (select type_id, sum(unit_price * quantity) / sum(quantity) as p from jita.my_transactions
             where is_buy and date > now() - interval '90 days' group by type_id),
  stock as (select a.type_id, sum(a.quantity) as n from jita.my_assets a where a.location_flag = 'Hangar' group by a.type_id
            union all
            select o.type_id, sum(o.volume_remain) from jita.my_orders o where o.state = 'open' and not o.is_buy group by o.type_id),
  inv as (select coalesce(sum(s.n * b.p), 0) as v from stock s join avgbuy b using (type_id) join jita.types t using (type_id)
          where not (t.is_ship and s.n = 1) and t.category_id <> 16)
  select case when exists (select 1 from jita.sso_tokens)
              then (select v from esc) + (select v from inv)
              else coalesce((select sum(price * case when filled_at is null then qty else coalesce(filled_qty, qty) end)
                             from jita.decisions where side = 'buy' and closed_at is null), 0) end
$$;
create or replace function jita.effective_profile() returns jsonb language sql stable as $$
  select to_jsonb(p) || jsonb_build_object(
           'capital_isk', coalesce(p.cash_isk, p.capital_isk) + case when p.cash_isk is null then 0 else jita.bound_isk() end,
           'bound_isk', jita.bound_isk())
  from jita.profile p where p.id = 1
$$;

-- ── judge(): dømmer med lagret profil og skriver candidates ──────────────────
create or replace function jita.judge() returns int
language plpgsql as $$
declare
  p jsonb;
  ts timestamptz := now();
  n int;
begin
  p := jita.effective_profile();
  if not exists (select 1 from jita.type_hourly) then
    return 0;
  end if;
  insert into jita.candidates (run_at, type_id, passed, failed_rules, buy_price, sell_price, qty,
    net_per_unit, margin, expected_profit, days_to_fill_buy, days_to_fill_sell,
    hist_pos, flow_ratio, score, s2b_per_day, bfs_per_day, reason)
  select ts, r.type_id, r.passed, r.failed_rules, r.buy_price, r.sell_price, r.qty,
         r.net_per_unit, r.margin, r.expected_profit, r.days_to_fill_buy, r.days_to_fill_sell,
         r.hist_pos, r.flow_ratio, r.score, r.s2b_per_day, r.bfs_per_day, r.reason
  from (
    select *, row_number() over (partition by passed order by cardinality(failed_rules), score desc nulls last) as rn
    from jita.judge_rows(p)
    where passed or not (failed_rules && array['9','9n','1x','10'])   -- regel 9/9n/1x-avslag lagres ikke (kan aldri bli «nesten»)
  ) r
  where r.passed or r.rn <= 200;
  select count(*) into n from jita.candidates c where c.run_at = ts and c.passed;
  return n;
end $$;

-- ── judge_preview(p): «hva om» – returnerer topp 50 uten å skrive ────────────
create or replace function jita.judge_preview(p jsonb)
returns setof jita.judgement
language sql stable as $$
  -- (select …) evalueres én gang; et rent uttrykk ville blitt inlinet per rad i judge_rows (7 000 × bound_isk)
  select r.* from lateral jita.judge_rows((select jita.effective_profile() || coalesce(p, '{}'::jsonb))) r
  order by r.passed desc, r.score desc nulls last
  limit 50
$$;

grant execute on all functions in schema jita to service_role;
