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

// ── «Kom i gang»-lista: faste regler, ikke koblet til tersklene ───────────────
// Speilet av NYBEGYNNER/starter_list()/my_pick()/starter_funnel() i scripts/industry.py –
// endrer du én, endre begge. Tallene står HER, i koden, og ikke i thresholds: tersklene
// styrer dommeren (den store tabellen), mens denne lista er for en nybegynner som bare vil
// vite hva som er lurt å kjøpe blueprint av. Den bruker heller ikke dommens «passed».
export const NYBEGYNNER = {
  min_handler: 10,          // «selger helt ok» – handler per dag i Jita
  min_handler_myk: 3,       // brukes bare hvis lista ellers blir kortere enn ti
  min_volum: 20,            // stk per dag, hvis handelstallet mangler
  min_fortjeneste: 5000,    // ISK per run – under dette er det ikke verdt turen
  maks_margin: 3.0,         // over 300 % er nesten alltid en feilpris
  runs_per_dag: 3,
  antall: 10,
  pick_margin_share: 0.7,
};

// Avslag som betyr «tallene er ikke til å stole på» eller «prisen faller».
const STARTER_SKIP = ["i1x", "i6", "i7", "i9", "i10"];

/** Én kandidatrad → én «kom i gang»-rad: ME 0, én run, alle kostnader med.
 *  cost_per_unit_me0 inneholder materialer (med kjøpsordregebyr) og jobbavgiften;
 *  salgssiden trekker broker + skatt. Frakten Ylandoki→Jita er ikke med (noen få m3). */
function starterRad(r, p, capital) {
  const me0 = r.margin_me0 == null ? null : Number(r.margin_me0);
  const kost0 = r.cost_per_unit_me0 == null ? null : Number(r.cost_per_unit_me0);
  if (r.bpo_price == null || me0 == null || kost0 == null || !r.sell_price) return null;
  if (me0 <= 0 || me0 > NYBEGYNNER.maks_margin) return null;
  if ((r.failed_rules || []).some((x) => STARTER_SKIP.includes(x))) return null;
  const perRun = Number(r.units_per_run || 1);
  const kostRun = kost0 * perRun;
  const nettoStk = Number(r.sell_price) * (1 - Number(p.sell_fees)) - kost0;
  const nettoRun = nettoStk * perRun;
  if (nettoRun < NYBEGYNNER.min_fortjeneste) return null;
  const handler = r.factors?.trades_per_day ?? null;
  const runsMarked = r.runs_market_per_day == null ? null : Number(r.runs_market_per_day);
  const runsDag = runsMarked ? Math.min(NYBEGYNNER.runs_per_dag, runsMarked) : NYBEGYNNER.runs_per_dag;
  const perDag = nettoRun * runsDag;
  const start = Number(r.bpo_price) + kostRun;
  return {
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
    trades_per_day: handler == null ? null : Number(handler),
    sell_orders: r.sell_orders, group_name: r.group_name,
  };
}

/** «Selger helt ok»: nok handler per dag, eller nok dagsvolum hvis handelstallet mangler. */
function selgerOk(rad, minHandler) {
  if (rad.trades_per_day != null) return rad.trades_per_day >= minHandler;
  if (rad.daily_volume != null) return rad.daily_volume >= NYBEGYNNER.min_volum;
  return false;
}

/** «Kom i gang»: varene med best margin som selger helt ok. Rangert på margin ved ME 0,
 *  de du har råd til øverst. Blir lista kortere enn ti, fylles den opp med tynnere markeder
 *  (merket thin_market) framfor å vise en kort eller tom liste. */
export function starterList(rows, p, capital, antall = NYBEGYNNER.antall) {
  const alle = rows.map((r) => starterRad(r, p, capital)).filter(Boolean);
  const sorter = (liste) => liste.sort((a, b) => (a.affordable === b.affordable
    ? (b.margin_me0 - a.margin_me0) || ((b.trades_per_day || 0) - (a.trades_per_day || 0))
    : (a.affordable ? -1 : 1)));
  const gode = sorter(alle.filter((x) => selgerOk(x, NYBEGYNNER.min_handler)));
  for (const x of gode) x.thin_market = false;
  let ut = gode;
  if (gode.length < antall) {
    const ekstra = sorter(alle.filter((x) => !selgerOk(x, NYBEGYNNER.min_handler)
                                          && selgerOk(x, NYBEGYNNER.min_handler_myk)));
    for (const x of ekstra) x.thin_market = true;
    ut = gode.concat(ekstra);
  }
  return ut.slice(0, antall);
}

/** «Hvis jeg skulle velge for deg»: blant dem med nesten like god margin (minst 70 % av
 *  den beste), den som selges oftest. Begrunnelsen skrives ut. Kan ikke skrus på i Avansert. */
export function myPick(liste) {
  const kandidater = liste.filter((x) => x.affordable).length ? liste.filter((x) => x.affordable) : liste;
  if (!kandidater.length) return null;
  const marg = (x) => Number(x.margin_me0 || 0);
  const handler = (x) => Number(x.trades_per_day || 0);
  const beste = Math.max(...kandidater.map(marg));
  const likeverdige = kandidater.filter((x) => marg(x) >= beste * NYBEGYNNER.pick_margin_share);
  const valg = likeverdige.reduce((a, b) => (handler(b) > handler(a) ? b : a));
  const topp = kandidater.reduce((a, b) => (marg(b) > marg(a) ? b : a));
  const p0 = (v) => `${Math.round(v * 100)} %`;
  let grunn = valg.product_type_id === topp.product_type_id
    ? `Best margin (${p0(marg(valg))} med uforsket blueprint) og ${Math.round(handler(valg))} handler per dag – den selges lett.`
    : `Nesten like god margin som ${topp.name} (${p0(marg(valg))} mot ${p0(marg(topp))}), men `
      + `${Math.round(handler(valg))} handler per dag mot ${Math.round(handler(topp))} – du får varen `
      + `ut igjen lettere, og det er det som gjør vondt når man er ny.`;
  if (valg.thin_market) grunn += ' Markedet er tynt, så legg varen ut og vent framfor å dumpe den.';
  return { ...valg, reason: grunn };
}

/** Hvor forsvinner forslagene? Teller de FASTE kravene i tur og orden. */
export function starterFunnel(rows, p, capital) {
  const steg = [["vurdert av roboten", 0], ["har blueprint-pris og priser å regne på", 0],
    ["positiv margin med uforsket blueprint", 0],
    [`minst ${Math.round(NYBEGYNNER.min_fortjeneste / 1000)}k fortjeneste per run`, 0],
    [`selges minst ${NYBEGYNNER.min_handler} ganger per dag`, 0]];
  for (const r of rows) {
    steg[0][1]++;
    const me0 = r.margin_me0 == null ? null : Number(r.margin_me0);
    const kost0 = r.cost_per_unit_me0 == null ? null : Number(r.cost_per_unit_me0);
    if (r.bpo_price == null || me0 == null || kost0 == null || !r.sell_price
        || (r.failed_rules || []).some((x) => STARTER_SKIP.includes(x))) continue;
    steg[1][1]++;
    if (me0 <= 0 || me0 > NYBEGYNNER.maks_margin) continue;
    steg[2][1]++;
    const rad = starterRad(r, p, capital);
    if (!rad) continue;
    steg[3][1]++;
    if (!selgerOk(rad, NYBEGYNNER.min_handler)) continue;
    steg[4][1]++;
  }
  return steg.map(([step, count]) => ({ step, count }));
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
