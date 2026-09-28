-- Jita – flyt delt på riktig tid (28. sept 2026). Kjøres én gang mot Supabase «Supnet».
-- Idempotent. Rører ingen eksisterende data.
--
-- Feilen: timesjobben skriver en rad i type_flow_hourly bare for varer som hadde en fylling eller
-- prisendring i intervallet, og hours_covered ligger på den raden. Dommeren delte så summen på
-- sum(hours_covered) PER VARE – altså bare de timene noe skjedde. De rolige timene falt ut av nevneren.
-- 28. sept: de 30 varene som passerte hadde i snitt 18,7 av 24 timer dekket, den dårligste 11,3 –
-- flyten var opptil dobbelt så høy som den egentlig var, og med den antall og dager til fylling.
--
-- Rettelsen: én rad per kjøring (ikke per vare) med tiden kjøringen dekket. Flyt per døgn =
-- sum(qty) / dekket tid for HELE markedet × 24. For 20-min-oppløsningen (få varer) skriver
-- watchlist-jobben i stedet nullrader, så hver vare har sin egen, riktige dekning.

create table if not exists jita.flow_coverage (
  resolution int not null,                 -- 60 = timesjobb, 20 = watchlist-jobb
  hour timestamptz not null,               -- samme timebøtte som type_flow_hourly.hour
  hours_covered numeric not null default 0,
  runs int not null default 0,
  primary key (resolution, hour)
);
alter table jita.flow_coverage enable row level security;

-- Tilbakefylling fra dataene som finnes: i hver timebøtte har minst én vare hatt aktivitet i alle
-- kjøringene, så største hours_covered i bøtta er tiden kjøringene dekket.
insert into jita.flow_coverage (resolution, hour, hours_covered, runs)
select resolution, hour, max(hours_covered), 1
from jita.type_flow_hourly
group by resolution, hour
on conflict (resolution, hour) do nothing;

-- Dekket tid siste 25 t for en oppløsning – samme vindu som dommeren og vaktene bruker.
create or replace function jita.flow_cover(res int) returns double precision
language sql stable as $$
  select coalesce(sum(hours_covered), 0)::float8 from jita.flow_coverage
  where resolution = res and hour >= now() - interval '25 hours'
$$;

-- Rydding: samme oppbevaring som type_flow_hourly (7 d). Egen funksjon, kalt fra jita.cleanup().
create or replace function jita.cleanup_flow_coverage() returns void language sql as $$
  delete from jita.flow_coverage where hour < now() - interval '7 days'
$$;
