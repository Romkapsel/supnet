// Jita – resultatregnskap fra ekte transaksjoner (FIFO) og wallet-journal (ekte gebyrer).
// Per vare: salg matches mot tidligere kjøp (først inn, først ut). Skatt hentes eksakt fra journalen
// når posten kan kobles til transaksjonen, ellers profilens sats. Broker-gebyr betales ved ordrelegging
// (ikke per transaksjon) – totalen er eksakt fra journalen, fordelingen per vare er et anslag.

const SKILLBOOKS = { accounting: 16622, broker_relations: 3446, adv_broker_relations: 16597 };

export async function computeResults(q, profile) {
  const broker = Number(profile.broker), tax = Number(profile.tax);
  const txs = await q`select t.transaction_id, t.date, t.type_id, y.name, t.is_buy, t.unit_price::float8 as unit_price, t.quantity::int as quantity
                      from jita.my_transactions t join jita.types y using (type_id) order by t.date, t.transaction_id`;
  const taxRows = await q`select context_id, amount::float8 as amount from jita.my_journal where ref_type = 'transaction_tax' and context_id is not null`;
  const taxByTx = {}; for (const r of taxRows) taxByTx[r.context_id] = -r.amount;

  const lots = {};                 // type_id → [{qty, price, date}]
  const perType = {};              // type_id → aggregat
  const perDay = {};               // yyyy-mm-dd → {gross, tax, sold}
  const sales = [];                // hver salgsmatch
  const unmatched = [];
  const day = (d) => new Date(d).toISOString().slice(0, 10);
  const T = (id, name) => perType[id] ||= { type_id: id, name, sold_qty: 0, revenue: 0, cost: 0, tax: 0, broker_est: 0, hold_days: 0, bought_qty: 0, bought_value: 0 };

  for (const t of txs) {
    const pt = T(t.type_id, t.name);
    if (t.is_buy) {
      (lots[t.type_id] ||= []).push({ qty: t.quantity, price: t.unit_price, date: t.date });
      pt.bought_qty += t.quantity; pt.bought_value += t.quantity * t.unit_price;
      continue;
    }
    let left = t.quantity, costSum = 0, matched = 0, holdSum = 0;
    const L = lots[t.type_id] || [];
    while (left > 0 && L.length) {
      const lot = L[0], take = Math.min(left, lot.qty);
      costSum += take * lot.price; matched += take; left -= take; lot.qty -= take;
      holdSum += take * (new Date(t.date) - new Date(lot.date)) / 864e5;
      if (lot.qty === 0) L.shift();
    }
    const revenueMatched = matched * t.unit_price;
    const taxTotal = taxByTx[t.transaction_id] ?? t.quantity * t.unit_price * tax;
    const taxMatched = matched ? taxTotal * matched / t.quantity : 0;
    if (matched) {
      const brokerEst = (costSum + revenueMatched) * broker;
      const profit = revenueMatched - taxMatched - costSum - brokerEst;
      pt.sold_qty += matched; pt.revenue += revenueMatched; pt.cost += costSum; pt.tax += taxMatched; pt.broker_est += brokerEst; pt.hold_days += holdSum;
      const d = perDay[day(t.date)] ||= { date: day(t.date), gross: 0, tax: 0, broker_est: 0, sold: 0 };
      d.gross += revenueMatched - costSum; d.tax += taxMatched; d.broker_est += brokerEst; d.sold += matched;
      sales.push({ date: t.date, type_id: t.type_id, name: t.name, qty: matched, sell: t.unit_price, cost: costSum / matched, profit, margin: profit / (costSum || 1) });
    }
    if (left > 0) unmatched.push({ date: t.date, type_id: t.type_id, name: t.name, qty: left, revenue: left * t.unit_price });
  }

  // lager til kostpris (åpne lots) – kryssjekket mot hangar + salgsordrer: det som er utstyrt/brukt telles ikke
  const onHand = {};
  for (const r of await q`select type_id, sum(quantity)::int as n from jita.my_assets where location_flag = 'Hangar' group by type_id`) onHand[r.type_id] = (onHand[r.type_id] || 0) + r.n;
  for (const r of await q`select type_id, sum(volume_remain)::int as n from jita.my_orders where state = 'open' and not is_buy group by type_id`) onHand[r.type_id] = (onHand[r.type_id] || 0) + r.n;
  const inventory = [], used = [];
  for (const [id, L] of Object.entries(lots)) {
    if (!L.length) continue;
    let avail = onHand[id] || 0;
    let qty = 0, cost = 0, usedQty = 0, usedCost = 0;
    for (const l of [...L].reverse()) {           // nyeste kjøp regnes som det du fortsatt har
      const keep = Math.min(l.qty, avail); avail -= keep;
      qty += keep; cost += keep * l.price; usedQty += l.qty - keep; usedCost += (l.qty - keep) * l.price;
    }
    if (qty) inventory.push({ type_id: Number(id), name: perType[id].name, qty, cost, oldest: L[0].date });
    if (usedQty) used.push({ type_id: Number(id), name: perType[id].name, qty: usedQty, cost: usedCost });
  }

  // eksakte gebyrer fra journalen per periode
  const fees = await q`
    select sum(-amount) filter (where ref_type = 'brokers_fee' and date > now() - interval '7 days')::float8 as broker_7d,
           sum(-amount) filter (where ref_type = 'brokers_fee' and date > now() - interval '30 days')::float8 as broker_30d,
           sum(-amount) filter (where ref_type = 'brokers_fee')::float8 as broker_all,
           sum(-amount) filter (where ref_type = 'transaction_tax' and date > now() - interval '7 days')::float8 as tax_7d,
           sum(-amount) filter (where ref_type = 'transaction_tax' and date > now() - interval '30 days')::float8 as tax_30d,
           sum(-amount) filter (where ref_type = 'transaction_tax')::float8 as tax_all,
           min(date) as since
    from jita.my_journal where ref_type in ('brokers_fee', 'transaction_tax')`;
  const f = fees[0] || {};

  const period = (days) => {
    const cut = Date.now() - days * 864e5;
    const s = sales.filter((x) => new Date(x.date) > cut);
    const gross = s.reduce((a, x) => a + (x.sell - x.cost) * x.qty, 0);
    const taxP = s.reduce((a, x) => a + 0, 0);
    return { sales: s.length, qty: s.reduce((a, x) => a + x.qty, 0), revenue: s.reduce((a, x) => a + x.sell * x.qty, 0),
      gross, profit_est: s.reduce((a, x) => a + x.profit, 0) };
  };
  const totals = { d7: period(7), d30: period(30), all: period(36500) };
  // netto = brutto (salg − kostpris) − eksakt skatt − eksakte broker-gebyrer (journal)
  totals.d7.net = totals.d7.gross - (f.tax_7d || 0) - (f.broker_7d || 0);
  totals.d30.net = totals.d30.gross - (f.tax_30d || 0) - (f.broker_30d || 0);
  totals.all.net = totals.all.gross - (f.tax_all || 0) - (f.broker_all || 0);

  // skill-ROI fra ekte gebyrer siste 30 d (skalert til måned)
  const days = Math.max(1, Math.min(30, (Date.now() - new Date(f.since || Date.now())) / 864e5));
  const monthly = { tax: (f.tax_30d || 0) / days * 30, broker: (f.broker_30d || 0) / days * 30 };
  const acc = Number(profile.accounting || 0), br = Number(profile.broker_relations || 0), abr = Number(profile.adv_broker_relations || 0);
  const prices = await skillbookPrices(q);
  const books = await q`select type_id from jita.my_assets where type_id = any(${Object.values(SKILLBOOKS)})`;
  for (const [k, id] of Object.entries(SKILLBOOKS)) if (books.some((b) => b.type_id === id)) prices[k] = 0;   // boka ligger i hangaren
  const roi = [];
  if (acc < 5) {
    const lvl = acc < 4 ? 4 : 5;
    const save = monthly.tax * (1 - (1 - 0.11 * lvl) / (1 - 0.11 * acc));
    roi.push({ skill: `Accounting ${lvl === 4 ? 'IV' : 'V'}`, saving_month: save, book: prices.accounting, payback_days: save > 0 && prices.accounting ? prices.accounting / save * 30 : null,
      note: acc === 0 ? `boka koster ${Math.round(prices.accounting || 0).toLocaleString('nb-NO')} ISK; skatt går fra 7,5 % til ${(7.5 * (1 - 0.11 * lvl)).toFixed(1).replace('.', ',')} %` : `fra nivå ${acc}` });
  }
  if (br < 5) {
    const save = monthly.broker * (0.003 * (5 - br)) / Number(profile.broker);
    roi.push({ skill: "Broker Relations V", saving_month: save, book: prices.broker_relations, payback_days: save > 0 && prices.broker_relations ? prices.broker_relations / save * 30 : (prices.broker_relations === 0 ? 0 : null),
      note: (prices.broker_relations === 0 ? "du har boka i hangaren – bare trening gjenstår. " : "") + `broker fra ${(Number(profile.broker) * 100).toFixed(2).replace('.', ',')} % til ${((Number(profile.broker) - 0.003 * (5 - br)) * 100).toFixed(2).replace('.', ',')} % (trening tar tid – bare boka er regnet inn)` });
  }
  if (abr < 3) roi.push({ skill: "Advanced Broker Relations", saving_month: null, book: prices.adv_broker_relations, payback_days: null,
      note: "senker relist-gebyret 6 %/nivå – lønner seg hvis du endrer ordrer ofte («ENDRE» i Å gjøre)" });

  return {
    totals, fees: f, monthly, roi,
    per_type: Object.values(perType).filter((x) => x.sold_qty || x.bought_qty).map((x) => ({ ...x,
      profit: x.revenue - x.tax - x.cost - x.broker_est, margin: x.cost ? (x.revenue - x.tax - x.cost - x.broker_est) / x.cost : null,
      avg_hold_days: x.sold_qty ? x.hold_days / x.sold_qty : null })).sort((a, b) => b.profit - a.profit),
    per_day: Object.values(perDay).sort((a, b) => a.date.localeCompare(b.date)),
    sales: sales.slice(-100).reverse(), unmatched, inventory: inventory.sort((a, b) => b.cost - a.cost), used: used.sort((a, b) => b.cost - a.cost),
    since: txs[0]?.date || null,
  };
}

async function skillbookPrices(q) {
  const out = {};
  for (const [k, id] of Object.entries(SKILLBOOKS)) {
    const [h] = await q`select best_ask::float8 as ask from jita.type_hourly where type_id = ${id} order by snapshot_at desc limit 1`;
    let ask = h?.ask;
    if (!ask) {
      try {
        const r = await fetch(`https://esi.evetech.net/markets/10000002/orders/?order_type=sell&type_id=${id}`, { headers: { "X-Compatibility-Date": "2026-09-14", "User-Agent": "Supnet-Jita/0.3" } });
        const o = (await r.json()).filter((x) => x.location_id === 60003760);
        ask = o.length ? Math.min(...o.map((x) => x.price)) : null;
      } catch { ask = null; }
    }
    out[k] = ask;
  }
  return out;
}
