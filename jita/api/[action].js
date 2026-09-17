// Jita – Vercel-funksjon for alle /api/* (spec del 4, blokk 1.2 og 1.3). Eget Vercel-prosjekt med rot = jita/.
// Alle kall krever header x-jita-pin = JITA_PIN (server-side PIN-sjekk). Ellers 401.
// Miljøvariabler: SUPABASE_DB_URL (pooler, port 6543), JITA_PIN, GITHUB_TOKEN, GITHUB_REPO.

import postgres from "postgres";
import { authorizeUrl, checkState, completeLogin, syncCharacter, ssoStatus } from "../lib/eve.js";
import { computeResults } from "../lib/pnl.js";
import { overbidAdvice, undercutAdvice, tick as tickOf } from "../lib/advice.js";

// Én tilkobling per kall (serverless): en gjenbrukt tilkobling mot transaction-pooleren hang på kall nr. 2.
function db() {
  return postgres(process.env.SUPABASE_DB_URL, {
    ssl: "require", prepare: false, max: 1, connect_timeout: 10, idle_timeout: 5,   // én tilkobling, sekvensielle spørringer (parallellitet mot pooleren ga ingen gevinst)
  });
}

const RULES = {
  "1": "Margin under terskel", "1b": "Posisjonen monner ikke (< 1 % av kapitalen)", "1x": "Urealistisk spread (ingen ekte bud)", "2": "Toppbud for stort (mur)",
  "3": "For mange budgivere", "4": "Selgere klumpet", "5": "For lite innflyt", "5t": "Liftes for sjelden",
  "7": "Salgspris faller", "7b": "Kjøpspris stiger", "8": "For dyr for profilen", "9": "Feil varetype (meta/T2/faction)", "9n": "NPC-seedet (uendelig tilbud, prislokk)", "10": "Priskrig (mange prisendringer/t)",
};

function json(res, status, body) {
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.status(status).send(JSON.stringify(body));
}

async function readBody(req) {
  if (req.body && typeof req.body === "object") return req.body;
  return new Promise((resolve) => {
    let s = "";
    req.on("data", (c) => (s += c));
    req.on("end", () => { try { resolve(JSON.parse(s || "{}")); } catch { resolve({}); } });
  });
}

import { timingSafeEqual } from "node:crypto";

// PIN-sjekk med sperre: 5 feil → 15 min, dobles for hver runde (30, 60 …). Én global sperre (én bruker).
async function checkPin(q, given) {
  const pin = process.env.JITA_PIN;
  if (!pin) return { status: 500, error: "JITA_PIN mangler i Vercel" };
  const [lock] = await q`select failures, rounds, locked_until from jita.auth_lock where id = 1`;
  if (lock?.locked_until && new Date(lock.locked_until) > new Date()) {
    const min = Math.ceil((new Date(lock.locked_until) - Date.now()) / 60000);
    return { status: 423, error: `Sperret etter for mange feil – prøv igjen om ${min} min` };
  }
  const a = Buffer.from(String(given || "")), b = Buffer.from(pin);
  const ok = a.length === b.length && timingSafeEqual(a, b);
  if (ok) {
    if (lock?.failures) await q`update jita.auth_lock set failures = 0, rounds = 0, locked_until = null where id = 1`;
    return { status: 200 };
  }
  const failures = (lock?.failures || 0) + 1;
  if (failures >= 5) {
    const rounds = (lock?.rounds || 0) + 1;
    const minutes = 15 * 2 ** (rounds - 1);
    await q`update jita.auth_lock set failures = 0, rounds = ${rounds}, locked_until = now() + ${minutes + " minutes"}::interval, last_fail = now() where id = 1`;
    return { status: 423, error: `Sperret i ${minutes} min etter 5 feil` };
  }
  await q`update jita.auth_lock set failures = ${failures}, last_fail = now() where id = 1`;
  return { status: 401, error: `Feil PIN (${5 - failures} forsøk igjen)` };
}

async function discord(text) {
  const hook = process.env.DISCORD_WEBHOOK;
  if (!hook) return;
  await fetch(hook, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: text.slice(0, 1900) }) }).catch(() => {});
}

export default async function handler(req, res) {
  const action = req.query.action;
  const q = db();
  try {
    // SSO-callback kommer fra nettleseren via login.eveonline.com – ingen PIN-header, men signert state
    if (action === "sso" && req.query.code) {
      if (!checkState(req.query.state)) return json(res, 400, { error: "ugyldig state" });
      try {
        const c = await completeLogin(q, req.query.code);
        res.statusCode = 302; res.setHeader("Location", `/settings.html?sso=ok&name=${encodeURIComponent(c.name)}`); return res.end();
      } catch (e) {
        res.statusCode = 302; res.setHeader("Location", `/settings.html?sso=error&msg=${encodeURIComponent(e.message)}`); return res.end();
      }
    }
    const auth = await checkPin(q, req.headers["x-jita-pin"]);
    if (auth.status !== 200) return json(res, auth.status, { error: auth.error });
    switch (action) {
      case "sso": return json(res, 200, { url: authorizeUrl() });
      case "sso_status": return json(res, 200, { eve: await ssoStatus(q) });
      case "results": return json(res, 200, await computeResults(q, await effectiveProfile(q)));
      case "character": {
        const r = await syncCharacter(q, discord);
        if (r.ok) { const n = (await q`select jita.judge() as n`)[0].n; r.passed = n; }
        return json(res, 200, r);
      }
      case "summary": return json(res, 200, await summary(q));
      case "type": return json(res, 200, await typeDetail(q, Number(req.query.id)));
      case "profile":
        if (req.method === "POST") return json(res, 200, await saveProfile(q, await readBody(req)));
        return json(res, 200, await getProfile(q));
      case "preview": return json(res, 200, await preview(q, await readBody(req)));
      case "scan": return json(res, 200, await scan(q, req.query.fallback === "1", ["watchlist", "history"].includes(req.query.job) ? req.query.job : "hourly"));
      case "rejudge": return json(res, 200, { passed: (await q`select jita.judge() as n`)[0].n });
      case "watchlist": return json(res, 200, await watchlist(q, await readBody(req)));
      case "decision": return json(res, 200, await decision(q, await readBody(req)));
      case "decisions": return json(res, 200, await decisions(q));
      case "search": return json(res, 200, await search(q, String(req.query.q || "")));
      default: return json(res, 404, { error: "ukjent handling" });
    }
  } catch (e) {
    console.error(e);
    return json(res, 500, { error: String(e.message || e) });
  } finally {
    await q.end({ timeout: 2 }).catch(() => {});
  }
}

// Profil med kapital i arbeid = cash + bundet (jita.effective_profile) + utledede tall (profile_calc)
async function effectiveProfile(q) {
  const [r] = await q`select x.j as profile, to_jsonb(c) as calc from (select jita.effective_profile() as j) x, lateral jita.profile_calc(x.j) c`;
  return { ...r.profile, ...r.calc };
}

// ── Forside ──────────────────────────────────────────────────────────────────
async function summary(q) {
  const profile = await effectiveProfile(q);
  const [run] = await q`select max(run_at) as run_at from jita.candidates`;
  const runAt = run?.run_at;
  const top = runAt ? await q`
    select c.*, t.name, t.market_group_path from jita.candidates c join jita.types t using (type_id)
    where c.run_at = (select max(run_at) from jita.candidates) and c.passed
      and c.type_id not in (select type_id from jita.decisions where closed_at is null)
    order by c.score desc nulls last limit 10` : [];
  const robot = await q`
    select distinct on (job) job, run_at, snapshot_at, pages_total, pages_ok, orders_count,
           ratelimit_remaining, duration_s, db_bytes, ok, message
    from jita.robot_runs order by job, run_at desc`;
  const [dbinfo] = await q`
    select pg_database_size(current_database())::bigint as db_bytes,
           (select json_agg(x) from (select j.jobname, r.status, r.start_time, left(r.return_message, 80) as msg
              from cron.job j left join lateral (select status, start_time, return_message from cron.job_run_details d where d.jobid = j.jobid order by start_time desc limit 1) r on true
              where j.jobname like 'jita%' order by j.jobname) x) as cron`;
  const [counts] = await q`
    select (select count(*) from jita.candidates where run_at = (select max(run_at) from jita.candidates) and passed) as passed,
           (select count(*) from jita.type_hourly where snapshot_at = (select max(snapshot_at) from jita.type_hourly)) as evaluated,
           (select max(snapshot_at) from jita.type_hourly) as snapshot_at`;
  const open = await q`
    select d.id, d.type_id, t.name, d.side, d.qty, d.price, d.created_at, d.filled_at, d.filled_qty, d.predicted_days, d.predicted_net_per_unit,
           c.sell_price as sell_now, c.buy_price as buy_now, c.passed, c.failed_rules, c.reason, c.net_per_unit as net_now,
           c.days_to_fill_buy, c.days_to_fill_sell, c.s2b_per_day, c.bfs_per_day, c.margin as margin_now,
           h.best_bid, h.best_ask, h.bid_top_qty, h.bid_orders_1pct,
           a.kind as alert_kind, a.payload as alert, a.created_at as alert_at
    from jita.decisions d join jita.types t using (type_id)
    left join lateral (select kind, payload, created_at from jita.alerts
                       where kind in ('overbid', 'overbid_cleared') and (payload->>'decision_id')::bigint = d.id
                       order by created_at desc limit 1) a on true
    left join jita.candidates c on c.type_id = d.type_id and c.run_at = (select max(run_at) from jita.candidates)
    left join jita.type_hourly h on h.type_id = d.type_id and h.snapshot_at = (select max(snapshot_at) from jita.type_hourly)
    where d.closed_at is null order by d.created_at desc`;
  const passedAll = await q`
    select c.type_id, t.name, t.market_group_path, c.score, c.buy_price, c.sell_price, c.net_per_unit, c.s2b_per_day, c.bfs_per_day, c.days_to_fill_buy
    from jita.candidates c join jita.types t using (type_id)
    where c.run_at = (select max(run_at) from jita.candidates) and c.passed order by c.score desc nulls last`;
  const eve = await ssoStatus(q);
  const alerts = await q`select a.kind, a.type_id, t.name, a.payload->>'text' as text, a.created_at
                         from jita.alerts a left join jita.types t using (type_id)
                         where a.created_at > now() - interval '48 hours' and a.kind in ('unlisted','expiry','overbid_cleared')
                         order by a.created_at desc limit 20`;
  const hangar = await q`
    select a.type_id, t.name, sum(a.quantity)::int as qty, h.best_ask, h.best_bid,
           exists (select 1 from jita.my_orders o where o.type_id = a.type_id and o.state = 'open' and not o.is_buy) as listed
    from jita.my_assets a join jita.types t using (type_id)
    left join jita.type_hourly h on h.type_id = a.type_id and h.snapshot_at = (select max(snapshot_at) from jita.type_hourly)
    where a.location_id = 60003760 and not t.is_excluded and not (t.is_ship and a.quantity = 1) and t.category_id <> 16
      and (a.quantity >= 2 or exists (select 1 from jita.my_transactions x where x.type_id = a.type_id and x.is_buy and x.date > now() - interval '60 days'))
    group by a.type_id, t.name, h.best_ask, h.best_bid order by qty desc`;
  const portfolio = buildPortfolio(profile, passedAll, open);
  const todo = await buildTodo(q, profile, portfolio, hangar);
  const timing = await bestHours(q, top.map((c) => c.type_id));
  for (const c of top) c.timing = timing[c.type_id] || null;
  return { profile, eve, alerts, hangar, todo, run_at: runAt, snapshot_at: counts.snapshot_at, top, open, portfolio, robot, counts, db: dbinfo, rules: RULES };
}

// ── «Å gjøre»: alt som krever handling i spillet, regnet LIVE fra dine ordrer (EVE) mot siste ordrebok ─
// Bare klare verb: HEV, SENK, TREKK, SELG, KJØP, ØK, RELIST. HOLD-tilfeller vises i beholdningen, ikke her.
async function buildTodo(q, p, portfolio, hangar) {
  const items = [];
  const tick = tickOf;
  const br = Number(p.broker), tax = Number(p.tax);
  const minMargin = Number(p.thresholds?.min_margin ?? 0.10);
  const JITA = 60003760;

  // Dine åpne ordrer i Jita 4-4 med siste ordrebok, flyt og (om ferskt) robotens eksakte mur-tall
  const mine = await q`
    select o.order_id, o.type_id, t.name, o.is_buy, o.price::float8 as price, o.volume_remain::int as remaining, o.volume_total, o.issued, o.duration,
           h.best_bid::float8 as best_bid, h.best_ask::float8 as best_ask, h.bid_qty_1pct::bigint as bid_qty_1pct, h.ask_qty_1pct::bigint as ask_qty_1pct, h.bid_top_qty::bigint as bid_top_qty,
           c.s2b_per_day::float8 as s2b, c.bfs_per_day::float8 as bfs, c.score::float8 as score, c.days_to_fill_buy::float8 as dtf,
           (select sum(unit_price * quantity) / nullif(sum(quantity), 0) from jita.my_transactions x where x.type_id = o.type_id and x.is_buy and x.date > now() - interval '90 days')::float8 as cost,
           a.payload as alert
    from jita.my_orders o join jita.types t using (type_id)
    left join jita.type_hourly h on h.type_id = o.type_id and h.snapshot_at = (select max(snapshot_at) from jita.type_hourly)
    left join jita.candidates c on c.type_id = o.type_id and c.run_at = (select max(run_at) from jita.candidates)
    left join lateral (select payload from jita.alerts al where al.type_id = o.type_id and al.kind in ('overbid', 'undercut')
                       and al.created_at > now() - interval '3 hours' order by al.created_at desc limit 1) a on true
    where o.state = 'open' and o.location_id = ${JITA}`;

  for (const o of mine) {
    if (!o.best_bid || !o.best_ask || o.remaining <= 0) continue;
    const fresh = o.alert && Math.abs(Number(o.alert.price) - o.price) < 1e-6
      && Math.abs(Number(o.is_buy ? o.alert.best_bid : o.alert.best_ask) - (o.is_buy ? o.best_bid : o.best_ask)) / (o.is_buy ? o.best_bid : o.best_ask) < 0.003;
    if (o.is_buy && o.best_bid > o.price + 1e-9) {
      // mur = enheter over deg; robotens eksakte tall hvis ferskt, ellers anslag fra ordreboken (innenfor 1 %)
      const wall = fresh ? Number(o.alert.wall_qty) : Math.max(0, Number(o.bid_qty_1pct || 0) - (o.price >= o.best_bid * 0.99 ? o.remaining : 0));
      const adv = fresh ? { ...o.alert, action: o.alert.action === "ENDRE" ? "HEV" : o.alert.action } : overbidAdvice(p, o.price, o.remaining, o.best_bid, wall, o.s2b || 0, o.best_ask, minMargin);
      if (adv.action === "HEV") items.push({ kind: "overbid", type_id: o.type_id, name: o.name, action: "HEV",
        title: `Hev kjøpsordren ${o.name} → ${Math.round(adv.new_price).toLocaleString("nb-NO")}`, detail: adv.text, impact: Number(adv.gain_24h || 0), where: "Jita 4-4 (må være dokket)" });
      else if (adv.action === "TREKK") items.push({ kind: "overbid", type_id: o.type_id, name: o.name, action: "TREKK",
        title: `Trekk kjøpsordren ${o.name} (${o.remaining} stk)`, detail: adv.text, impact: o.price * o.remaining * 0.1, where: "Jita 4-4 (må være dokket)" });
    }
    if (!o.is_buy && o.best_ask < o.price - 1e-9) {
      const wall = fresh ? Number(o.alert.wall_qty) : Math.max(0, Number(o.ask_qty_1pct || 0) - (o.price <= o.best_ask * 1.01 ? o.remaining : 0));
      const adv = fresh ? { ...o.alert, action: o.alert.action === "ENDRE" ? "SENK" : o.alert.action } : undercutAdvice(p, o.price, o.remaining, o.best_ask, wall, o.bfs || 0, o.cost, minMargin);
      if (adv.action === "SENK") items.push({ kind: "undercut", type_id: o.type_id, name: o.name, action: "SENK",
        title: `Senk salgsordren ${o.name} → ${Math.round(adv.new_price).toLocaleString("nb-NO")}`, detail: adv.text, impact: Number(adv.gain_24h || 0), where: "Jita 4-4 (må være dokket)" });
    }
    // utløper < 24 t
    const left = new Date(o.issued).getTime() + o.duration * 86400e3 - Date.now();
    if (left < 24 * 3600e3)
      items.push({ kind: "expiry", type_id: o.type_id, name: o.name, action: "RELIST",
        title: `${o.is_buy ? "Kjøpsordre" : "Salgsordre"} ${o.name} utløper om ${Math.max(0, Math.round(left / 3600e3))} t – legg ut på nytt (90 dager)`,
        detail: `${o.remaining} stk à ${o.price.toLocaleString("nb-NO")} · ny ordre koster fullt broker-gebyr, så velg 90 dager`, impact: o.price * o.remaining * 0.1, where: "Jita 4-4 (må være dokket)" });
    // kjøpsordre som sitter fast mens en dobbelt så god kandidat finnes (spec 2.4)
    if (o.is_buy && o.dtf > 14) {
      const [b] = await q`select t.name, c.score::float8 as score from jita.candidates c join jita.types t using (type_id)
        where c.run_at = (select max(run_at) from jita.candidates) and c.passed and c.type_id not in (select type_id from jita.my_orders where state = 'open')
        order by c.score desc nulls last limit 1`;
      if (b && b.score > 2 * (o.score || 0) && !items.some((x) => x.type_id === o.type_id && x.action === "TREKK"))
        items.push({ kind: "move", type_id: o.type_id, name: o.name, action: "TREKK",
          title: `Trekk kjøpsordren ${o.name} (${o.remaining} stk) og flytt kapitalen`,
          detail: `fyllingstid ~${o.dtf.toFixed(0)} d; ${b.name} har over dobbel score. Broker-gebyret er tapt uansett – ${Math.round(o.price * o.remaining).toLocaleString("nb-NO")} ISK frigjøres`,
          impact: o.price * o.remaining * 0.1, where: "Jita 4-4 (må være dokket)" });
    }
  }

  // Ulistet lager → SELG
  for (const h of hangar.filter((h) => !h.listed && h.best_ask)) {
    const sell = Number(h.best_ask) - tick(Number(h.best_ask)), net = sell * (1 - br - tax);
    items.push({ kind: "unlisted", type_id: h.type_id, name: h.name, action: "SELG",
      title: `Legg ut ${h.qty} × ${h.name} à ${Math.round(sell).toLocaleString("nb-NO")}`,
      detail: `ett tick under laveste ask ${Math.round(h.best_ask).toLocaleString("nb-NO")} · netto ~${Math.round(net).toLocaleString("nb-NO")}/stk`,
      impact: net * h.qty, where: "Jita 4-4 (må være dokket)" });
  }

  // Trend mot deg på varer du sitter med og har salgsordre for → SENK (spec 2.4). Uten salgsordre dekkes det av SELG over.
  const trend = await q`
    select o.type_id, t.name, o.price::float8 as my_ask, o.volume_remain::int as remaining,
           now_.best_ask::float8 as ask_now, then_.best_ask::float8 as ask_then, now_.ask_orders_1pct as cl_now, then_.ask_orders_1pct as cl_then
    from jita.my_orders o join jita.types t using (type_id)
    join lateral (select best_ask, ask_orders_1pct from jita.type_hourly where type_id = o.type_id order by snapshot_at desc limit 1) now_ on true
    join lateral (select best_ask, ask_orders_1pct from jita.type_hourly where type_id = o.type_id and snapshot_at <= now() - interval '48 hours' order by snapshot_at desc limit 1) then_ on true
    where o.state = 'open' and not o.is_buy and o.location_id = ${JITA} and o.volume_remain > 10`;
  for (const r of trend) {
    const drop = r.ask_then > 0 ? (r.ask_then - r.ask_now) / r.ask_then : 0;
    const cluster = r.cl_then > 0 && r.cl_now >= 3 * r.cl_then;
    if ((drop > 0.05 || cluster) && r.my_ask > r.ask_now && !items.some((x) => x.type_id === r.type_id && x.action === "SENK")) {
      const target = r.ask_now - tick(r.ask_now);
      items.push({ kind: "trend", type_id: r.type_id, name: r.name, action: "SENK",
        title: `Senk salgsordren ${r.name} → ${Math.round(target).toLocaleString("nb-NO")} (markedet går mot deg)`,
        detail: `${drop > 0.05 ? `laveste ask falt ${(drop * 100).toFixed(1).replace(".", ",")} % på 48 t (${Math.round(r.ask_then).toLocaleString("nb-NO")} → ${Math.round(r.ask_now).toLocaleString("nb-NO")})` : ""}${drop > 0.05 && cluster ? " · " : ""}${cluster ? `selgere innenfor 1 % gikk fra ${r.cl_then} til ${r.cl_now}` : ""} · følg ned én gang, ikke jag`,
        impact: r.remaining * r.ask_now * Math.max(drop, 0.05), where: "Jita 4-4 (må være dokket)" });
    }
  }

  // Porteføljeforslag → KJØP / ØK
  for (const x of portfolio?.picks || []) {
    items.push({ kind: "buy", type_id: x.type_id, name: x.name, action: x.have ? "ØK" : "KJØP",
      title: `${x.have ? `Øk ${x.name} med ${x.qty}` : `Legg inn kjøpsordre ${x.name}: ${x.qty} stk`} à ${Math.round(x.buy_price).toLocaleString("nb-NO")}`,
      detail: `${Math.round(x.cost).toLocaleString("nb-NO")} ISK bundet · forventet +${Math.round(x.expected_profit).toLocaleString("nb-NO")} · fylling ~${x.days_to_fill_buy.toFixed(1)} d · 90 dagers varighet`,
      impact: x.expected_profit, where: "Jita 4-4 (kjøpsordre med rekkevidde «station»)" });
  }
  items.sort((a, b) => b.impact - a.impact);
  return items;
}

// ── Beste tidspunkt (norsk tid) å legge ordrer: når dumping (kjøp) / lifting (salg) topper, siste 14 d ─
async function bestHours(q, typeIds) {
  if (!typeIds.length) return {};
  const rows = await q`
    select type_id, extract(hour from hour at time zone 'Europe/Oslo')::int as h, sum(s2b_qty)::float8 s2b, sum(bfs_qty)::float8 bfs
    from jita.type_flow_hourly where resolution = 60 and hour > now() - interval '14 days' and type_id = any(${typeIds})
    group by type_id, extract(hour from hour at time zone 'Europe/Oslo')`;
  const by = {};
  for (const r of rows) (by[r.type_id] ||= []).push(r);
  const out = {};
  for (const [id, hs] of Object.entries(by)) {
    if (hs.length < 8) continue;                       // for lite data
    const tot = hs.reduce((a, r) => a + r.s2b, 0), totB = hs.reduce((a, r) => a + r.bfs, 0);
    const peak = (k) => hs.slice().sort((a, b) => b[k] - a[k]).slice(0, 3).map((r) => r.h).sort((a, b) => a - b);
    out[id] = { buy_hours: peak('s2b'), sell_hours: peak('bfs'), s2b_share_top3: tot ? hs.slice().sort((a, b) => b.s2b - a.s2b).slice(0, 3).reduce((a, r) => a + r.s2b, 0) / tot : null };
  }
  return out;
}

// ── Porteføljeforslag (spec 1b.4): sysselsett kapitalen der flyten tåler det ──
// Grådig i score-rekkefølge. Per vare: antall = min(flyt-tak = S2B/dag × fyllingstid, maks andel av kapitalen,
// det som er igjen). Maks 2 varer per varegruppe (nivå 2). Varer du allerede holder telles som brukt kapital.
function buildPortfolio(p, cands, open) {
  const th = p.thresholds || {};
  const capital = Number(p.capital_isk), reserve = Number(p.reserve_share ?? 0.25);
  const share = Number(th.max_position_share ?? 0.35), days = Number(p.target_fill_days ?? 4);
  const investable = capital * (1 - reserve);
  const bound = (open || []).reduce((a, o) => a + Number(o.price) * Number(o.filled_qty ?? o.qty), 0);
  const heldQty = {};
  for (const o of open || []) heldQty[o.type_id] = (heldQty[o.type_id] || 0) + Number(o.filled_qty ?? o.qty);
  let left = Math.max(0, investable - bound);
  const groups = {}, picks = [];
  for (const c of cands) {
    const g = (c.market_group_path || "").split(" > ").slice(0, 2).join(" > ");
    const have = heldQty[c.type_id] || 0;
    if (!have && (groups[g] || 0) >= 2) continue;
    const buy = Number(c.buy_price);
    const byFlow = Math.floor(Number(c.s2b_per_day || 0) * days);
    const byShare = Math.floor(capital * share / buy);
    const byLeft = Math.floor(left / buy);
    const potential = Math.min(byFlow, byShare);            // det varen tåler totalt
    const qty = Math.min(potential - have, byLeft);          // det du kan legge til nå
    if (qty < 1) continue;
    const limit = potential - have === byFlow - have ? "flyt" : qty === byLeft ? "kapital" : "andel";
    picks.push({ type_id: c.type_id, name: c.name, qty, have, buy_price: buy, sell_price: Number(c.sell_price),
      net_per_unit: Number(c.net_per_unit), expected_profit: qty * Number(c.net_per_unit),
      cost: qty * buy, limit, days_to_fill_buy: (have + qty) / Math.max(Number(c.s2b_per_day || 0), 0.1),
      profit_per_day: qty * Number(c.net_per_unit) / (1 + Math.max(Number(c.days_to_fill_buy || 0), 0.1)) });
    if (!have) groups[g] = (groups[g] || 0) + 1;
    left -= qty * buy;
    if (picks.filter((x) => !x.have).length + Object.keys(heldQty).length >= Number(p.positions || 7)) break;
  }
  return { investable, bound, used: picks.reduce((a, x) => a + x.cost, 0), left, picks,
    total_profit: picks.reduce((a, x) => a + x.expected_profit, 0),
    total_per_day: picks.reduce((a, x) => a + x.profit_per_day, 0) };
}

// ── Vare ─────────────────────────────────────────────────────────────────────
async function typeDetail(q, id) {
  if (!id) throw new Error("mangler id");
  const [type] = await q`select t.*, w.status as wl_status, w.note as wl_note from jita.types t left join jita.watchlist w using (type_id) where t.type_id = ${id}`;
  const [candidate] = await q`select * from jita.candidates where type_id = ${id} order by run_at desc limit 1`;
  const hourly = await q`select snapshot_at, best_bid, best_ask, bid_top_qty, bid_orders_1pct, ask_qty_1pct from jita.type_hourly where type_id = ${id} and snapshot_at > now() - interval '7 days' order by snapshot_at`;
  const flow = await q`select hour, resolution, bfs_qty, bfs_trades, s2b_qty, s2b_trades, hours_covered from jita.type_flow_hourly where type_id = ${id} and hour > now() - interval '14 days' order by hour`;
  const history = await q`select date, average, highest, lowest, volume, order_count from jita.history_daily where type_id = ${id} order by date desc limit 30`;
  const fills = await q`select observed_at, is_buy, price, qty, kind, weight, resolution from jita.fills where type_id = ${id} order by observed_at desc limit 200`;
  const decs = await q`select * from jita.decisions where type_id = ${id} order by created_at desc limit 20`;
  const my_orders = await q`select order_id, is_buy, price, volume_remain, volume_total, issued, duration, state from jita.my_orders where type_id = ${id} order by state = 'open' desc, issued desc limit 20`;
  const my_tx = await q`select date, is_buy, unit_price, quantity from jita.my_transactions where type_id = ${id} order by date desc limit 30`;
  const [stock] = await q`select coalesce(sum(quantity), 0)::int as n from jita.my_assets where type_id = ${id} and location_flag = 'Hangar'`;
  const [memory] = await q`select * from jita.type_memory where type_id = ${id}`;
  const profile = await effectiveProfile(q);
  return { type, candidate, hourly, flow, history, fills, decisions: decs, my_orders, my_tx, stock: stock?.n || 0, memory: memory || null, profile, rules: RULES };
}

// ── Profil / hva-om ──────────────────────────────────────────────────────────
const PROFILE_FIELDS = ["cash_isk", "capital_isk", "broker_relations", "accounting", "adv_broker_relations", "trade", "retail",
  "wholesale", "tycoon", "standing_corp", "standing_faction", "broker_fee_override", "sales_tax_override",
  "positions", "target_fill_days", "reserve_share", "min_qty", "allow_t2", "allow_faction", "allow_npc_seeded", "thresholds"];

function cleanProfile(body) {
  const out = {};
  for (const k of PROFILE_FIELDS) {
    if (!(k in body)) continue;
    let v = body[k];
    if (k === "thresholds") { out[k] = typeof v === "string" ? JSON.parse(v) : v; continue; }
    if (k === "allow_t2" || k === "allow_faction" || k === "allow_npc_seeded") { out[k] = !!v; continue; }
    if (v === "" || v === null || v === undefined) { out[k] = null; continue; }
    out[k] = Number(v);
    if (Number.isNaN(out[k])) throw new Error(`ugyldig tall for ${k}`);
  }
  return out;
}

async function getProfile(q) {
  const profile = await effectiveProfile(q);
  return { profile, eve: await ssoStatus(q) };
}

async function saveProfile(q, body) {
  const fields = cleanProfile(body);
  if (Object.keys(fields).length) {
    fields.updated_at = new Date();
    await q`update jita.profile set ${q(fields)} where id = 1`;
  }
  const passed = (await q`select jita.judge() as n`)[0].n;
  return { ...(await getProfile(q)), passed };
}

async function preview(q, body) {
  const fields = cleanProfile(body);
  const rows = await q`select r.*, t.market_group_path from jita.judge_preview(${q.json(fields)}) r join jita.types t using (type_id) where r.passed order by r.score desc nulls last limit 10`;
  const [calc] = await q`select * from jita.profile_calc(jita.effective_profile() || ${q.json(fields)})`;
  return { top: rows, calc };
}

// ── Scan nå ──────────────────────────────────────────────────────────────────
async function scan(q, fallback = false, job = "hourly") {
  const [p] = await q`select last_manual_scan from jita.profile where id = 1`;
  if (fallback) {
    // Plan B (pg_cron): start jobben hvis den ikke har kjørt nylig (GitHub hopper ofte over cron).
    const maxAge = job === "watchlist" ? 15 : 50;   // history: 50 (hver time)
    const [r] = await q`select max(run_at) as last from jita.robot_runs where job = ${job}`;
    if (r.last && Date.now() - new Date(r.last).getTime() < maxAge * 60000) return { ok: true, message: `${job} er fersk – ingenting å gjøre` };
  } else if (p.last_manual_scan) {
    const wait = 10 - (Date.now() - new Date(p.last_manual_scan).getTime()) / 60000;
    if (wait > 0) return { ok: false, message: `vent ${Math.ceil(wait)} min` };
  }
  const token = process.env.GITHUB_TOKEN, repo = process.env.GITHUB_REPO;
  if (!token || !repo) return { ok: false, message: "GITHUB_TOKEN/GITHUB_REPO mangler i Vercel" };
  const r = await fetch(`https://api.github.com/repos/${repo}/dispatches`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "User-Agent": "supnet-jita" },
    body: JSON.stringify({ event_type: job === "watchlist" ? "jita-watchlist" : job === "history" ? "jita-history" : "jita-scan" }),
  });
  if (r.status !== 204) return { ok: false, message: `GitHub svarte ${r.status}: ${(await r.text()).slice(0, 200)}` };
  if (!fallback) await q`update jita.profile set last_manual_scan = now() where id = 1`;   // auto-start skal ikke sperre «Scan nå»
  return { ok: true, message: "kjører… ~3 min" };
}

// ── Watchlist ────────────────────────────────────────────────────────────────
async function watchlist(q, body) {
  const id = Number(body.type_id);
  if (!id) throw new Error("mangler type_id");
  if (!body.status) {
    await q`delete from jita.watchlist where type_id = ${id}`;
  } else {
    if (!["follow", "ignore"].includes(body.status)) throw new Error("status må være follow/ignore");
    await q`insert into jita.watchlist (type_id, status, note, updated_at) values (${id}, ${body.status}, ${body.note || null}, now())
            on conflict (type_id) do update set status = excluded.status, note = excluded.note, updated_at = now()`;
  }
  const passed = (await q`select jita.judge() as n`)[0].n;
  return { ok: true, passed };
}

// ── Lære-sløyfen ─────────────────────────────────────────────────────────────
async function decision(q, body) {
  if (body.id) {
    const f = {};
    if (body.filled_at !== undefined) f.filled_at = body.filled_at || null;
    if (body.filled_qty !== undefined) f.filled_qty = body.filled_qty === "" ? null : Number(body.filled_qty);
    if (body.sell_price !== undefined) f.sell_price = body.sell_price === "" ? null : Number(body.sell_price);
    if (body.closed_at !== undefined) f.closed_at = body.closed_at || null;
    if (body.note !== undefined) f.note = body.note || null;
    if (body.delete) { await q`delete from jita.decisions where id = ${Number(body.id)}`; return { ok: true }; }
    if (Object.keys(f).length) await q`update jita.decisions set ${q(f)} where id = ${Number(body.id)}`;
    return { ok: true };
  }
  const [row] = await q`insert into jita.decisions (type_id, side, price, qty, predicted_days, predicted_net_per_unit, note)
    values (${Number(body.type_id)}, ${body.side || "buy"}, ${Number(body.price)}, ${Number(body.qty)},
            ${body.predicted_days == null ? null : Number(body.predicted_days)},
            ${body.predicted_net_per_unit == null ? null : Number(body.predicted_net_per_unit)}, ${body.note || null})
    returning *`;
  return { ok: true, decision: row };
}

async function decisions(q) {
  const rows = await q`select d.*, t.name from jita.decisions d join jita.types t using (type_id) order by (d.closed_at is null) desc, d.created_at desc limit 200`;
  const [stats] = await q`
    select count(*) filter (where closed_at is not null) as closed,
           avg(extract(epoch from (filled_at - created_at)) / 86400 / nullif(predicted_days, 0)) filter (where filled_at is not null and predicted_days > 0) as fill_ratio,
           sum((sell_price * (1 - c.broker - c.tax) - price * (1 + c.broker)) * coalesce(filled_qty, qty)) filter (where closed_at > now() - interval '7 days') as profit_7d,
           sum((sell_price * (1 - c.broker - c.tax) - price * (1 + c.broker)) * coalesce(filled_qty, qty)) filter (where closed_at > now() - interval '30 days') as profit_30d,
           sum(price * coalesce(filled_qty, qty)) filter (where created_at > now() - interval '7 days') as turnover_7d
    from jita.decisions, (select c.* from jita.profile p, lateral jita.profile_calc(to_jsonb(p)) c where p.id = 1) c`;
  return { decisions: rows, stats };
}

// ── Søk (for å legge varer på watchlist manuelt) ─────────────────────────────
async function search(q, term) {
  if (term.length < 2) return { types: [] };
  const types = await q`select type_id, name, market_group_path from jita.types where name ilike ${"%" + term + "%"} and not is_excluded order by (name ilike ${term + "%"}) desc, length(name), name limit 20`;
  return { types };
}
