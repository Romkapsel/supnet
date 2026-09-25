// Industri – felles server-side logikk for /api/industry.
// Porteføljevelgeren er speilet av pick_portfolio() i scripts/industry.py – endrer du én, endre begge.

export const INDUSTRY_RULES = {
  i1: "Margin under terskel",
  i1x: "Urealistisk margin (prisen er nok ikke ekte)",
  i2: "For lite dagsvolum",
  i3: "For få selgere (tynt marked)",
  i3b: "For stor spread (tynt marked)",
  i4: "BPO-en er for dyr",
  i5: "Binder for mye kapital per jobb",
  i6: "Prisen faller",
  i7: "Mangler data",
  i8: "For lang tilbakebetaling på BPO-en",
  i9: "Pristopp (prisen er langt over 30-dagers snitt)",
  i10: "BPO-en kan ikke kjøpes (blueprinten finnes ikke på markedet)",
  i11: "For få handler per dag (ingen moment i markedet)",
  i12: "Én batch kan ikke selges unna (markedet er for tregt)",
};

// Kategoriene vi rangerer (ESI category_id) – brukes til filteret i fanen.
export const CATEGORY_NAMES = { 6: "Skip", 7: "Moduler", 8: "Ammo og charges", 18: "Droner", 22: "Deployables" };

/** «Kom i gang»: 5–10 blueprints for en nybegynner. Speilet av starter_list() i scripts/industry.py.
 *  Regner med UFORSKET blueprint (ME 0), per RUN, maks 3 runs per døgn og aldri mer enn markedet tar.
 *  Rangeres på AVKASTNING per døgn på bundet kapital – sorterer man på ISK/dag, fylles lista av
 *  Large-rigger med 4 mill. i materialer per run i markeder med 12 handler om dagen.
 *  Krever 20 handler/dag, og at én run ikke koster mer enn 25 % av kapitalen (mykes opp om lista
 *  blir for kort). Filtrerer ikke bort noe på kapital; det du ikke har råd til merkes i stedet. */
export function starterList(rows, p, capital, antall = 10) {
  const th = p.thresholds || {};
  const minMargin = Number(th.min_margin ?? 0.10);
  const minProfit = Number(th.min_profit_per_run ?? 50000);
  const minTrades = Number(th.starter_min_trades ?? 20);
  const maksRuns = Number(th.newbro_runs_per_day ?? 3);
  const andel = Number(th.starter_max_cost_share ?? 0.25);
  const fees = Number(p.sell_fees);

  const bygg = (kostnadstak) => {
    const ut = [];
    for (const r of rows) {
      if (!r.passed || r.bpo_price == null) continue;
      const me0 = r.margin_me0 == null ? null : Number(r.margin_me0);
      const kost0 = r.cost_per_unit_me0 == null ? null : Number(r.cost_per_unit_me0);
      if (me0 == null || kost0 == null || me0 < minMargin) continue;
      const handler = r.factors?.trades_per_day ?? null;
      if (handler != null && Number(handler) < minTrades) continue;
      const perRun = Number(r.units_per_run || 1);
      const kostRun = kost0 * perRun;
      if (kostnadstak != null && kostRun > kostnadstak) continue;
      const nettoStk = Number(r.sell_price) * (1 - fees) - kost0;
      const nettoRun = nettoStk * perRun;
      if (nettoRun < minProfit) continue;
      const runsMarked = r.runs_market_per_day == null ? null : Number(r.runs_market_per_day);
      const runsDag = runsMarked != null ? Math.min(maksRuns, runsMarked) : maksRuns;
      const perDag = nettoRun * runsDag;
      const start = Number(r.bpo_price) + kostRun;
      ut.push({
        product_type_id: r.product_type_id, blueprint_type_id: r.blueprint_type_id,
        name: r.name, blueprint_name: `${r.name} Blueprint`,
        bpo_price: Number(r.bpo_price), bpo_price_source: r.bpo_price_source,
        units_per_run: perRun, cost_per_unit_me0: kost0, cost_per_run: kostRun,
        sell_price: Number(r.sell_price), profit_per_unit: nettoStk, profit_per_run: nettoRun,
        margin_me0: me0, margin_me10: r.margin == null ? null : Number(r.margin),
        hours_per_run: Math.round((Number(r.time_per_run_s) || 0) / 36) / 100,
        runs_market_per_day: runsMarked, runs_per_day: runsDag, profit_per_day: perDag,
        daily_return: kostRun > 0 ? perDag / kostRun : 0,
        startup_cost: start, affordable: start <= capital,
        daily_volume: r.daily_volume == null ? null : Number(r.daily_volume),
        trades_per_day: handler, sell_orders: r.sell_orders, group_name: r.group_name,
      });
    }
    ut.sort((a, b) => (a.affordable === b.affordable ? b.daily_return - a.daily_return
                                                    : (a.affordable ? -1 : 1)));
    return ut;
  };

  const tak = capital > 0 ? capital * andel : null;
  let liste = bygg(tak);
  if (liste.length < 5 && tak) liste = bygg(tak * 2);
  if (liste.length < 5) liste = bygg(null);
  return liste.slice(0, antall);
}

/** Hvorfor kom ikke resten med? Teller avslagsgrunnene, så siden kan forklare seg. */
export function whyNot(rows, rules) {
  const teller = new Map();
  for (const r of rows) for (const regel of (r.failed_rules || [])) teller.set(regel, (teller.get(regel) || 0) + 1);
  return [...teller.entries()].sort((a, b) => b[1] - a[1])
    .map(([rule, count]) => ({ rule, text: rules[rule] || rule, count }));
}

/** Hva bør du kjøpe FØRST? Speilet av start_recommendation() i scripts/industry.py.
 *  Rangeringen ellers antar ferdig forsket blueprint (ME 10). Kjøper du en ny BPO, er den ME 0,
 *  og da må varen (1) ha en startkostnad du har råd til, og (2) være lønnsom alt ved ME 0 –
 *  ellers taper du penger mens forskningen går. */
export function startRecommendation(rows, minMargin, capital, antall = 3) {
  const ut = [];
  for (const r of [...rows].filter((x) => x.passed)
    .sort((a, b) => Number(b.isk_per_day_slot || 0) - Number(a.isk_per_day_slot || 0))) {
    if (ut.length >= antall) break;
    const bpo = r.bpo_price == null ? null : Number(r.bpo_price);
    const start = (bpo || 0) + Number(r.capital_per_job || 0);
    if (bpo == null || start > capital) continue;
    if (r.margin_me0 == null || Number(r.margin_me0) < minMargin) continue;
    ut.push({ ...r, startup_cost: start });
  }
  return ut;
}

/** Velg hvilke produkter slottene skal brukes på: høyest score først, én vare per slot,
 *  innenfor kapitalen. 10 %-taket på dagsvolum ligger allerede inne i isk_per_day_slot. */
export function pickPortfolio(rows, slots, capital) {
  const picks = [];
  let left = Number(capital) || 0;
  for (const r of [...rows].filter((x) => x.passed).sort((a, b) => Number(b.score || 0) - Number(a.score || 0))) {
    if (picks.length >= slots) break;
    const cost = Number(r.capital_per_job || 0);
    if (cost <= 0 || cost > left) continue;
    picks.push({
      product_type_id: r.product_type_id, name: r.name, runs: r.runs, units: r.units,
      capital_per_job: cost, isk_per_day_slot: Number(r.isk_per_day_slot || 0),
      hours: Math.round((Number(r.time_per_batch_s) || 0) / 360) / 10,
      margin: Number(r.margin), m3_in: Number(r.m3_in), m3_out: Number(r.m3_out),
    });
    left -= cost;
  }
  return {
    slots, capital: Number(capital) || 0, left,
    picks,
    isk_per_day: picks.reduce((s, p) => s + p.isk_per_day_slot, 0),
    capital_used: picks.reduce((s, p) => s + p.capital_per_job, 0),
  };
}

/** «Hvis jeg skulle velge for deg». Speilet av my_pick() i scripts/industry.py.
 *  Blant dem som er nesten like gode på avkastning, velg den som er lettest å få solgt –
 *  det er likviditeten som gjør vondt når man er ny. Begrunnelsen skrives ut, slik at siden
 *  kan si HVORFOR, ikke bare hva. */
export function myPick(liste, p) {
  const kandidater = liste.filter((x) => x.affordable).length ? liste.filter((x) => x.affordable) : liste;
  if (!kandidater.length) return null;
  const avk = (x) => Number(x.daily_return || 0);
  const handler = (x) => Number(x.trades_per_day || 0);
  const besteAvk = Math.max(...kandidater.map(avk));
  const andel = Number(p.thresholds?.pick_return_share ?? 0.7);
  const likeverdige = kandidater.filter((x) => avk(x) >= besteAvk * andel);
  const valg = likeverdige.reduce((a, b) => (handler(b) > handler(a) ? b : a));
  const topp = kandidater.reduce((a, b) => (avk(b) > avk(a) ? b : a));
  const p0 = (v) => `${Math.round(v * 100)} %`;
  const grunn = valg.product_type_id === topp.product_type_id
    ? `Best avkastning (${p0(avk(valg))} av pengene per døgn) og ${Math.round(handler(valg))} handler per dag – den selges lett.`
    : `Nesten samme avkastning som ${topp.name} (${p0(avk(valg))} mot ${p0(avk(topp))}), men `
      + `${Math.round(handler(valg))} handler per dag mot ${Math.round(handler(topp))} – du får varen ut igjen `
      + `lettere, og det er det som gjør vondt når man er ny.`;
  return { ...valg, reason: grunn };
}

/** Hvor forsvinner forslagene? Speilet av starter_funnel() i scripts/industry.py.
 *  Teller hvor mange som faller for hvert krav i tur og orden, slik at en tom liste kan
 *  forklare seg selv i stedet for at vi må gjette. */
export function starterFunnel(rows, p, capital) {
  const th = p.thresholds || {};
  const minMargin = Number(th.min_margin ?? 0.10);
  const minProfit = Number(th.min_profit_per_run ?? 50000);
  const minTrades = Number(th.starter_min_trades ?? 20);
  const fees = Number(p.sell_fees);
  const tak = capital > 0 ? capital * Number(th.starter_max_cost_share ?? 0.25) : null;

  const steg = [["passerer reglene", 0], ["har BPO-pris", 0],
    [`margin ved ME 0 over ${Math.round(minMargin * 100)} %`, 0],
    [`minst ${Math.round(minTrades)} handler per dag`, 0],
    ["én run innenfor kostnadstaket", 0],
    [`minst ${Math.round(minProfit / 1000)}k fortjeneste per run`, 0]];
  for (const r of rows) {
    if (!r.passed) continue;
    steg[0][1]++;
    if (r.bpo_price == null) continue;
    steg[1][1]++;
    const me0 = r.margin_me0 == null ? null : Number(r.margin_me0);
    const kost0 = r.cost_per_unit_me0 == null ? null : Number(r.cost_per_unit_me0);
    if (me0 == null || kost0 == null || me0 < minMargin) continue;
    steg[2][1]++;
    const handler = r.factors?.trades_per_day ?? null;
    if (handler != null && Number(handler) < minTrades) continue;
    steg[3][1]++;
    const perRun = Number(r.units_per_run || 1);
    const kostRun = kost0 * perRun;
    if (tak != null && kostRun > tak) continue;
    steg[4][1]++;
    if ((Number(r.sell_price) * (1 - fees) - kost0) * perRun < minProfit) continue;
    steg[5][1]++;
  }
  return steg.map(([step, count]) => ({ step, count }));
}
