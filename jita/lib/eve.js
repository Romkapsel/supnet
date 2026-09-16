// Jita – fase 2: EVE SSO (OAuth2) og synk av karakterdata fra ESI.
// Brukes av api/[action].js. Refresh-token ligger bare i jita.sso_tokens (server-side).

import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";

const SSO = "https://login.eveonline.com/v2/oauth";
const ESI = "https://esi.evetech.net";
const COMPAT = "2026-09-14";
const UA = "Supnet-Jita/0.3 (+https://jita-eve.vercel.app)";
export const SCOPES = [
  "esi-wallet.read_character_wallet.v1", "esi-markets.read_character_orders.v1", "esi-assets.read_assets.v1",
  "esi-skills.read_skills.v1", "esi-characters.read_standings.v1", "esi-markets.structure_markets.v1",
];
export const CALLBACK = "https://jita-eve.vercel.app/api/sso";
const JITA_44 = 60003760;
// skill-ID-er
const SKILLS = { 3446: "broker_relations", 16622: "accounting", 16597: "adv_broker_relations",
  3443: "trade", 3444: "retail", 16596: "wholesale", 18580: "tycoon" };
const CALDARI_STATE = 500001, CALDARI_NAVY = 1000035;

function creds() {
  const id = process.env.EVE_CLIENT_ID, secret = process.env.EVE_CLIENT_SECRET;
  if (!id || !secret) throw new Error("EVE_CLIENT_ID/EVE_CLIENT_SECRET mangler i Vercel");
  return { id, secret, basic: Buffer.from(`${id}:${secret}`).toString("base64") };
}

// ── state = nonce.ts.hmac – beskytter callback mot forfalskning ──────────────
export function makeState() {
  const { secret } = creds();
  const nonce = randomBytes(8).toString("hex"), ts = Date.now().toString(36);
  const mac = createHmac("sha256", secret).update(nonce + ts).digest("hex").slice(0, 24);
  return `${nonce}.${ts}.${mac}`;
}
export function checkState(state) {
  const { secret } = creds();
  const [nonce, ts, mac] = String(state || "").split(".");
  if (!nonce || !ts || !mac) return false;
  const want = createHmac("sha256", secret).update(nonce + ts).digest("hex").slice(0, 24);
  const ok = mac.length === want.length && timingSafeEqual(Buffer.from(mac), Buffer.from(want));
  return ok && Date.now() - parseInt(ts, 36) < 15 * 60000;
}

export function authorizeUrl() {
  const { id } = creds();
  const u = new URL(SSO + "/authorize");
  u.searchParams.set("response_type", "code");
  u.searchParams.set("redirect_uri", CALLBACK);
  u.searchParams.set("client_id", id);
  u.searchParams.set("scope", SCOPES.join(" "));
  u.searchParams.set("state", makeState());
  return u.toString();
}

async function tokenRequest(body) {
  const { basic } = creds();
  const r = await fetch(SSO + "/token", {
    method: "POST",
    headers: { Authorization: `Basic ${basic}`, "Content-Type": "application/x-www-form-urlencoded", Host: "login.eveonline.com", "User-Agent": UA },
    body: new URLSearchParams(body),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`SSO ${r.status}: ${j.error_description || j.error || "ukjent feil"}`);
  return j;   // access_token, refresh_token, expires_in, token_type
}

function decodeJwt(token) {
  const payload = token.split(".")[1];
  return JSON.parse(Buffer.from(payload.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8"));
}

// Bytter code → tokens og lagrer karakteren
export async function completeLogin(q, code) {
  const t = await tokenRequest({ grant_type: "authorization_code", code });
  const jwt = decodeJwt(t.access_token);
  const characterId = Number(String(jwt.sub).split(":").pop());
  const name = jwt.name;
  const expires = new Date(Date.now() + (t.expires_in - 60) * 1000);
  await q`insert into jita.sso_tokens (character_id, character_name, refresh_token, access_token, expires_at, scopes, updated_at)
          values (${characterId}, ${name}, ${t.refresh_token}, ${t.access_token}, ${expires}, ${(jwt.scp || []).join(" ")}, now())
          on conflict (character_id) do update set character_name = excluded.character_name, refresh_token = excluded.refresh_token,
            access_token = excluded.access_token, expires_at = excluded.expires_at, scopes = excluded.scopes, updated_at = now(), last_error = null`;
  return { characterId, name };
}

async function accessToken(q, row) {
  if (row.access_token && row.expires_at && new Date(row.expires_at) > new Date()) return row.access_token;
  const t = await tokenRequest({ grant_type: "refresh_token", refresh_token: row.refresh_token });
  const expires = new Date(Date.now() + (t.expires_in - 60) * 1000);
  await q`update jita.sso_tokens set access_token = ${t.access_token}, refresh_token = ${t.refresh_token || row.refresh_token},
          expires_at = ${expires}, updated_at = now() where character_id = ${row.character_id}`;
  return t.access_token;
}

async function esi(path, token, params = {}) {
  const u = new URL(ESI + path);
  for (const [k, v] of Object.entries(params)) u.searchParams.set(k, v);
  const r = await fetch(u, { headers: { Authorization: `Bearer ${token}`, "X-Compatibility-Date": COMPAT, "User-Agent": UA, Accept: "application/json" } });
  if (r.status === 304) return { data: null, pages: 1 };
  if (!r.ok) throw new Error(`ESI ${r.status} ${path}: ${(await r.text()).slice(0, 120)}`);
  return { data: await r.json(), pages: Number(r.headers.get("X-Pages") || 1) };
}

async function esiAll(path, token) {
  const first = await esi(path, token, { page: 1 });
  let out = first.data || [];
  for (let p = 2; p <= first.pages; p++) out = out.concat((await esi(path, token, { page: p })).data || []);
  return out;
}

async function bulk(q, table, rows, cols, conflict) {
  for (let i = 0; i < rows.length; i += 300) {
    const chunk = rows.slice(i, i + 300);
    await q`insert into ${q(table)} ${q(chunk, ...cols)} on conflict (${q(conflict)}) do nothing`;
  }
}

// ── Synk: wallet, ordrer, transaksjoner, hangar, skills, standings → DB ──────
export async function syncCharacter(q, notify) {
  const [row] = await q`select * from jita.sso_tokens order by updated_at desc limit 1`;
  if (!row) return { ok: false, message: "Ingen EVE-karakter er logget inn" };
  const cid = row.character_id;
  const out = { character: row.character_name, alerts: [] };
  try {
    const token = await accessToken(q, row);
    const [wallet, orders, txs, assets, skills, standings, journal] = await Promise.all([
      esi(`/characters/${cid}/wallet/`, token).then((r) => r.data),
      esi(`/characters/${cid}/orders/`, token).then((r) => r.data || []),
      esi(`/characters/${cid}/wallet/transactions/`, token).then((r) => r.data || []),
      esiAll(`/characters/${cid}/assets/`, token),
      esi(`/characters/${cid}/skills/`, token).then((r) => r.data),
      esi(`/characters/${cid}/standings/`, token).then((r) => r.data || []),
      esiAll(`/characters/${cid}/wallet/journal/`, token),
    ]);

    // profil: cash + skills + standings
    const skillMap = {};
    for (const s of skills?.skills || []) if (SKILLS[s.skill_id]) skillMap[SKILLS[s.skill_id]] = s.active_skill_level;
    const stF = standings.find((s) => s.from_id === CALDARI_STATE)?.standing ?? 0;
    const stC = standings.find((s) => s.from_id === CALDARI_NAVY)?.standing ?? 0;
    const prof = { cash_isk: Number(wallet), standing_faction: stF, standing_corp: stC, updated_at: new Date(), ...skillMap };
    await q`update jita.profile set ${q(prof)} where id = 1`;
    out.wallet = Number(wallet); out.skills = skillMap; out.standings = { faction: stF, corp: stC };

    // ordrer: upsert åpne, lukk de som er borte
    const now = new Date();
    const openIds = orders.map((o) => o.order_id);
    for (const o of orders) {
      await q`insert into jita.my_orders (order_id, type_id, is_buy, price, volume_remain, volume_total, issued, duration, state,
                first_seen, last_seen, escrow, location_id, range, region_id, character_id)
              values (${o.order_id}, ${o.type_id}, ${!!o.is_buy_order}, ${o.price}, ${o.volume_remain}, ${o.volume_total}, ${o.issued},
                ${o.duration}, 'open', ${now}, ${now}, ${o.escrow ?? null}, ${o.location_id}, ${String(o.range)}, ${o.region_id}, ${cid})
              on conflict (order_id) do update set price = excluded.price, volume_remain = excluded.volume_remain, state = 'open',
                last_seen = excluded.last_seen, escrow = excluded.escrow, issued = excluded.issued, duration = excluded.duration`;
    }
    const closed = await q`update jita.my_orders set state = 'closed', last_seen = ${now}
                           where state = 'open' and character_id = ${cid} and not (order_id = any(${openIds}::bigint[])) returning order_id, type_id, is_buy, volume_remain, volume_total`;
    out.orders = orders.length; out.closed = closed.length;

    // transaksjoner + journal (bulk)
    await bulk(q, "jita.my_transactions", txs.map((t) => ({ transaction_id: t.transaction_id, date: t.date, type_id: t.type_id, is_buy: !!t.is_buy,
      unit_price: t.unit_price, quantity: t.quantity, location_id: t.location_id, journal_ref_id: t.journal_ref_id, character_id: cid, client_id: t.client_id ?? null })),
      ["transaction_id", "date", "type_id", "is_buy", "unit_price", "quantity", "location_id", "journal_ref_id", "character_id", "client_id"], "transaction_id");
    out.transactions = txs.length;
    await bulk(q, "jita.my_journal", journal.map((j) => ({ id: j.id, date: j.date, ref_type: j.ref_type, amount: j.amount ?? 0, balance: j.balance ?? null,
      description: j.description ?? null, context_id: j.context_id ?? null, context_id_type: j.context_id_type ?? null, character_id: cid })),
      ["id", "date", "ref_type", "amount", "balance", "description", "context_id", "context_id_type", "character_id"], "id");
    out.journal = journal.length;

    // hangar (bare vanlige stasjoner, Hangar-flagget)
    await q`delete from jita.my_assets`;
    const hangar = assets.filter((a) => a.location_flag === "Hangar" && a.location_type === "station");
    await bulk(q, "jita.my_assets", hangar.map((a) => ({ item_id: a.item_id, type_id: a.type_id, quantity: a.quantity, location_id: a.location_id, location_flag: a.location_flag, seen_at: now })),
      ["item_id", "type_id", "quantity", "location_id", "location_flag", "seen_at"], "item_id");
    out.assets = hangar.length;

    // beslutninger speiler kjøpsordrene (lære-sløyfen blir automatisk)
    await syncDecisions(q, orders, closed, txs, now);

    // varsler (spec 2.4): ulistet lager, utløp < 24 t
    const unlisted = await q`
      select t.name, a.type_id, sum(a.quantity)::int as qty from jita.my_assets a join jita.types t using (type_id)
      where a.location_id = ${JITA_44} and not t.is_excluded and not (t.is_ship and a.quantity = 1) and t.category_id <> 16
        and a.type_id not in (select type_id from jita.my_orders where state = 'open' and not is_buy)
        and (a.quantity >= 2 or exists (select 1 from jita.my_transactions x where x.type_id = a.type_id and x.is_buy and x.date > now() - interval '60 days'))
      group by t.name, a.type_id order by qty desc`;
    for (const u of unlisted) out.alerts.push({ kind: "unlisted", type_id: u.type_id, text: `${u.qty} × ${u.name} i hangaren uten salgsordre` });
    for (const o of orders) {
      const exp = new Date(o.issued).getTime() + o.duration * 86400e3;
      if (exp - Date.now() < 24 * 3600e3 && o.volume_remain > 0) {
        const [t] = await q`select name from jita.types where type_id = ${o.type_id}`;
        out.alerts.push({ kind: "expiry", type_id: o.type_id, text: `${o.is_buy_order ? "Kjøpsordre" : "Salgsordre"} ${t?.name} (${o.volume_remain} igjen) utløper om ${Math.max(0, Math.round((exp - Date.now()) / 3600e3))} t` });
      }
    }
    await recordAlerts(q, out.alerts, notify);
    await q`select jita.refresh_type_memory()`;          // rullebladet per vare (lag 1)
    // Målt broker-sats: gebyrpost i journalen matchet mot ordre lagt/endret i samme sekund. Plassering gir full sats,
    // relist gir lavere – derfor brukes den høyeste plausible verdien (median av topp 3) siste 30 d.
    const meas = await q`
      select percentile_cont(0.5) within group (order by r desc) as rate, count(*) as n from (
        select (-j.amount) / (o.price * o.volume_total) as r
        from jita.my_journal j join jita.my_orders o on abs(extract(epoch from (j.date - o.issued))) <= 2
        where j.ref_type = 'brokers_fee' and j.date > now() - interval '30 days' and o.price * o.volume_total > 0
        order by r desc limit 3) x`;
    if (meas[0]?.n >= 2 && meas[0].rate > 0.005 && meas[0].rate < 0.035)
      await q`update jita.profile set broker_fee_measured = ${Number(meas[0].rate)}, broker_measured_at = now() where id = 1`;
    out.broker_measured = meas[0]?.rate ? Number(meas[0].rate) : null;

    await q`update jita.sso_tokens set last_sync = now(), last_error = null where character_id = ${cid}`;
    out.ok = true;
    return out;
  } catch (e) {
    await q`update jita.sso_tokens set last_error = ${String(e.message || e)} where character_id = ${cid}`;
    throw e;
  }
}

// Kjøpsordre → beslutning (én per order_id). Borte → fylt (om noe ble kjøpt) eller kansellert. Solgt → lukket.
async function syncDecisions(q, orders, closed, txs, now) {
  for (const o of orders.filter((o) => o.is_buy_order)) {
    const filled = o.volume_total - o.volume_remain;
    const [d] = await q`select id from jita.decisions where order_id = ${o.order_id}`;
    if (d) {
      await q`update jita.decisions set price = ${o.price}, qty = ${o.volume_total}, filled_qty = ${filled || null} where id = ${d.id}`;
    } else {
      // knytt til en manuell beslutning på samme vare uten order_id, ellers opprett
      const [m] = await q`select id from jita.decisions where type_id = ${o.type_id} and side = 'buy' and closed_at is null and order_id is null order by created_at desc limit 1`;
      if (m) await q`update jita.decisions set order_id = ${o.order_id}, price = ${o.price}, qty = ${o.volume_total}, filled_qty = ${filled || null}, source = 'eve' where id = ${m.id}`;
      else await q`insert into jita.decisions (type_id, side, price, qty, filled_qty, order_id, source, created_at, note)
                   values (${o.type_id}, 'buy', ${o.price}, ${o.volume_total}, ${filled || null}, ${o.order_id}, 'eve', ${o.issued}, 'fra EVE')`;
    }
  }
  for (const c of closed.filter((c) => c.is_buy)) {
    const filled = c.volume_total - c.volume_remain;
    if (filled > 0) await q`update jita.decisions set filled_at = coalesce(filled_at, ${now}), filled_qty = ${filled},
                            note = concat_ws(' · ', note, ${c.volume_remain > 0 ? `${c.volume_remain} ikke fylt (utløpt/kansellert)` : "fylt"}) where order_id = ${c.order_id}`;
    else await q`update jita.decisions set closed_at = ${now}, note = concat_ws(' · ', note, 'kansellert/utløpt uten fylling') where order_id = ${c.order_id} and closed_at is null`;
  }
  // solgt: fylte beslutninger der salgstransaksjoner etter fylling dekker antallet
  const open = await q`select id, type_id, filled_at, coalesce(filled_qty, qty) as n from jita.decisions where filled_at is not null and closed_at is null`;
  for (const d of open) {
    const [s] = await q`select coalesce(sum(quantity),0)::int as sold, sum(unit_price * quantity) / nullif(sum(quantity),0) as avg
                        from jita.my_transactions where type_id = ${d.type_id} and not is_buy and date >= ${d.filled_at}`;
    if (s.sold >= d.n) await q`update jita.decisions set closed_at = ${now}, sell_price = ${s.avg} where id = ${d.id}`;
  }
}

async function recordAlerts(q, alerts, notify) {
  for (const a of alerts) {
    const [prev] = await q`select 1 from jita.alerts where kind = ${a.kind} and type_id = ${a.type_id} and created_at > now() - interval '24 hours' limit 1`;
    if (prev) continue;
    await q`insert into jita.alerts (kind, type_id, payload) values (${a.kind}, ${a.type_id}, ${q.json({ text: a.text })})`;
    if (notify) await notify((a.kind === "unlisted" ? "📦 " : "⏳ ") + a.text);
  }
}

export async function ssoStatus(q) {
  const [row] = await q`select character_id, character_name, last_sync, last_error, scopes from jita.sso_tokens order by updated_at desc limit 1`;
  return row || null;
}
