// Jita – Vercel-funksjon for alle /api/* (spec del 4, blokk 1.2 og 1.3). Eget Vercel-prosjekt med rot = jita/.
// Alle kall krever header x-jita-pin = JITA_PIN (server-side PIN-sjekk). Ellers 401.
// Miljøvariabler: SUPABASE_DB_URL (pooler, port 6543), JITA_PIN, GITHUB_TOKEN, GITHUB_REPO.

import postgres from "postgres";

// Én tilkobling per kall (serverless): en gjenbrukt tilkobling mot transaction-pooleren hang på kall nr. 2.
function db() {
  return postgres(process.env.SUPABASE_DB_URL, {
    ssl: "require", prepare: false, max: 1, connect_timeout: 10, idle_timeout: 5,
  });
}

const RULES = {
  "1": "Margin under terskel", "1b": "Netto/enhet for lav for kapitalen", "1x": "Urealistisk spread (ingen ekte bud)", "2": "Toppbud for stort (mur)",
  "3": "For mange budgivere", "4": "Selgere klumpet", "5": "For lite innflyt", "5t": "Liftes for sjelden",
  "7": "Salgspris faller", "7b": "Kjøpspris stiger", "8": "For dyr for profilen", "9": "Feil varetype (meta/T2/faction)",
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

export default async function handler(req, res) {
  const action = req.query.action;
  const q = db();
  try {
    const auth = await checkPin(q, req.headers["x-jita-pin"]);
    if (auth.status !== 200) return json(res, auth.status, { error: auth.error });
    switch (action) {
      case "summary": return json(res, 200, await summary(q));
      case "type": return json(res, 200, await typeDetail(q, Number(req.query.id)));
      case "profile":
        if (req.method === "POST") return json(res, 200, await saveProfile(q, await readBody(req)));
        return json(res, 200, await getProfile(q));
      case "preview": return json(res, 200, await preview(q, await readBody(req)));
      case "scan": return json(res, 200, await scan(q, req.query.fallback === "1"));
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
  const nearly = runAt ? await q`
    select c.*, t.name from jita.candidates c join jita.types t using (type_id)
    where c.run_at = (select max(run_at) from jita.candidates) and not c.passed and not (c.failed_rules && array['9','1x'])
    order by cardinality(c.failed_rules), c.score desc nulls last limit 10` : [];
  const robot = await q`
    select distinct on (job) job, run_at, snapshot_at, pages_total, pages_ok, orders_count,
           ratelimit_remaining, duration_s, db_bytes, ok, message
    from jita.robot_runs order by job, run_at desc`;
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
  const portfolio = buildPortfolio(profile, passedAll, open);
  return { profile, run_at: runAt, snapshot_at: counts.snapshot_at, top, open, portfolio, nearly: nearly.map((r) => ({ ...r, failed_text: (r.failed_rules || []).map((c) => RULES[c] || c) })), robot, counts, rules: RULES };
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
  const profile = await effectiveProfile(q);
  return { type, candidate, hourly, flow, history, fills, decisions: decs, profile, rules: RULES };
}

// ── Profil / hva-om ──────────────────────────────────────────────────────────
const PROFILE_FIELDS = ["cash_isk", "capital_isk", "broker_relations", "accounting", "adv_broker_relations", "trade", "retail",
  "wholesale", "tycoon", "standing_corp", "standing_faction", "broker_fee_override", "sales_tax_override",
  "positions", "target_fill_days", "reserve_share", "min_qty", "allow_t2", "allow_faction", "thresholds"];

function cleanProfile(body) {
  const out = {};
  for (const k of PROFILE_FIELDS) {
    if (!(k in body)) continue;
    let v = body[k];
    if (k === "thresholds") { out[k] = typeof v === "string" ? JSON.parse(v) : v; continue; }
    if (k === "allow_t2" || k === "allow_faction") { out[k] = !!v; continue; }
    if (v === "" || v === null || v === undefined) { out[k] = null; continue; }
    out[k] = Number(v);
    if (Number.isNaN(out[k])) throw new Error(`ugyldig tall for ${k}`);
  }
  return out;
}

async function getProfile(q) {
  const profile = await effectiveProfile(q);
  return { profile };
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
async function scan(q, fallback = false) {
  const [p] = await q`select last_manual_scan from jita.profile where id = 1`;
  if (fallback) {
    // Plan B (pg_cron :40 hver time): start hvis timesjobben ikke har kjørt på 50 min (GitHub hopper ofte over :23).
    const [r] = await q`select max(run_at) as last from jita.robot_runs where job = 'hourly'`;
    if (r.last && Date.now() - new Date(r.last).getTime() < 50 * 60000) return { ok: true, message: "timesjobben er fersk – ingenting å gjøre" };
  } else if (p.last_manual_scan) {
    const wait = 10 - (Date.now() - new Date(p.last_manual_scan).getTime()) / 60000;
    if (wait > 0) return { ok: false, message: `vent ${Math.ceil(wait)} min` };
  }
  const token = process.env.GITHUB_TOKEN, repo = process.env.GITHUB_REPO;
  if (!token || !repo) return { ok: false, message: "GITHUB_TOKEN/GITHUB_REPO mangler i Vercel" };
  const r = await fetch(`https://api.github.com/repos/${repo}/dispatches`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "User-Agent": "supnet-jita" },
    body: JSON.stringify({ event_type: "jita-scan" }),
  });
  if (r.status !== 204) return { ok: false, message: `GitHub svarte ${r.status}: ${(await r.text()).slice(0, 200)}` };
  await q`update jita.profile set last_manual_scan = now() where id = 1`;
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
