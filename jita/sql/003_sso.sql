-- Jita – fase 2: EVE SSO og karakterdata (spec del 4 «Innlogging», 2.4)

create table if not exists jita.sso_tokens (        -- refresh-token KUN her (server-side), aldri i klienten
  character_id bigint primary key,
  character_name text,
  refresh_token text not null,
  access_token text,
  expires_at timestamptz,
  scopes text,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  last_sync timestamptz,
  last_error text
);
create table if not exists jita.my_assets (          -- hangar i Jita 4-4 (og andre stasjoner)
  item_id bigint primary key,
  type_id int, quantity bigint, location_id bigint, location_flag text,
  seen_at timestamptz default now()
);
alter table jita.my_orders add column if not exists escrow numeric, add column if not exists location_id bigint,
  add column if not exists range text, add column if not exists region_id int, add column if not exists character_id bigint;
alter table jita.decisions add column if not exists order_id bigint, add column if not exists source text default 'manual';
create index if not exists decisions_order on jita.decisions (order_id);
alter table jita.my_transactions add column if not exists character_id bigint, add column if not exists client_id bigint;

do $$ declare t text; begin
  for t in select tablename from pg_tables where schemaname = 'jita' and tablename in ('sso_tokens','my_assets') loop
    execute format('alter table jita.%I enable row level security', t);
  end loop; end $$;
grant all on all tables in schema jita to service_role;

-- Synk fra EVE hver time kl. :50 (Vercel-funksjonen henter og skriver; pg_cron trigger). PIN som i jita-vakt.
select cron.unschedule(jobid) from cron.job where jobname = 'jita-eve-sync';
select cron.schedule('jita-eve-sync', '50 * * * *',
  $$select net.http_post(url := 'https://jita-eve.vercel.app/api/character',
      headers := '{"x-jita-pin": "<PIN>", "Content-Type": "application/json"}'::jsonb, body := '{}'::jsonb)$$);

-- Wallet-journal: faktiske gebyrer (brokers_fee, transaction_tax) m.m.
create table if not exists jita.my_journal (
  id bigint primary key, date timestamptz, ref_type text, amount numeric, balance numeric,
  description text, context_id bigint, context_id_type text, character_id bigint
);
create index if not exists my_journal_date on jita.my_journal (date);
alter table jita.my_journal enable row level security;
grant all on all tables in schema jita to service_role;
