-- Jita – vern mot for hyppige ordreendringer (27. sept 2026). Kjøres én gang mot Supabase «Supnet».
-- Idempotent. Rører ingen eksisterende data.
--
-- Bakgrunnen: 11.–27. sept gikk 10,2 mill. til broker-gebyr, og ~6 mill. av det var omprising.
-- 27. sept ble alle 17 ordrer endret i løpet av fire minutter (08:19–08:23) til 698k i gebyr,
-- mot 429k i salg samme dag. «Å gjøre» skal derfor kunne si «la den stå» med tall bak.
--
-- EVE flytter `issued` når du endrer prisen på en ordre, så jita.my_orders.issued ER tidspunktet
-- for siste endring. Denne tabellen er for ANTALLET: hvor mange ganger ordren er endret, og hva
-- endringene har kostet, slik at et vern kan si «denne ordren har alt spist X av fortjenesten».

create schema if not exists jita;

create table if not exists jita.order_changes (
  order_id bigint not null,
  changed_at timestamptz not null default now(),
  type_id int not null,
  is_buy boolean not null,
  old_price numeric not null,
  new_price numeric not null,
  qty_remain int,
  fee_est numeric,                        -- anslag: modify_fee() med profilens broker-sats
  primary key (order_id, changed_at)
);
create index if not exists order_changes_order on jita.order_changes (order_id, changed_at desc);
create index if not exists order_changes_type on jita.order_changes (type_id, changed_at desc);

-- Rydding: 90 dager er nok til å se et mønster
create or replace function jita.cleanup_order_changes() returns void language plpgsql as $$
begin
  delete from jita.order_changes where changed_at < now() - interval '90 days';
end $$;

grant all on all tables in schema jita to service_role;
do $$ begin
  if not exists (select 1 from pg_class where relname = 'order_changes' and relrowsecurity) then
    alter table jita.order_changes enable row level security;
  end if;
end $$;

-- Tersklene for vernet ligger i jita.profile.thresholds:
--   relist_cooldown_h   (12) – en ordre som ble endret for under 12 t siden skal stå i fred
--   relist_fee_share    (0,15) – endringene på én ordre får koste maks 15 % av posisjonens fortjeneste
--   dump_after_changes  (3) – etter så mange endringer er svaret å selge til budet, ikke følge ned igjen
--   daily_fee_share     (0,10) – dagens broker-gebyr over 10 % av dagens salg gir stopp-varsel
update jita.profile set thresholds = thresholds || '{
    "relist_cooldown_h": 12,
    "relist_fee_share": 0.15,
    "dump_after_changes": 3,
    "daily_fee_share": 0.10
  }'::jsonb where id = 1;
