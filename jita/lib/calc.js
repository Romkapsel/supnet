// Blueprint-kalkulator: «hvor mye mangler jeg, og hvor mye malm må jeg mine for det?»
// Ren logikk uten database – API-et (action=calc) henter tallene og kaller disse.
//
// materialQuantity() er speilet av material_quantity() i scripts/industry.py; test_calc.py
// kjører begge over et rutenett av ME/runs/mengder og krever likhet. Endrer du én, endre begge.

/** Materialbehov for hele jobben (EVE-regelen): aldri under 1 per run, og rundes opp per JOBB,
 *  ikke per run. Mengde 1 reduseres ikke av ME – derfor er 10 runs à 1 stk alltid 10 stk. */
export function materialQuantity(baseQty, runs, me, facilityMult = 1) {
  if (baseQty <= 1) return runs;
  const exact = runs * baseQty * (1 - me / 100) * facilityMult;
  // round(..., 2) før ceil: 4,000000001 skal bli 4, ikke 5 (samme som Python-siden)
  return Math.max(runs, Math.ceil(Math.round(exact * 100) / 100));
}

/** Hva gir malmen du allerede har, når den refines? `holdings` = {oreTypeId: antall},
 *  `yields` = {oreTypeId: {batch, minerals: {mineralId: mengde per batch}}}.
 *  Bare hele batcher refines – rest under batch-størrelsen blir liggende. */
export function oreToMinerals(holdings, yields, yieldFactor) {
  const ut = {};
  for (const [ore, qty] of Object.entries(holdings)) {
    const y = yields[ore];
    if (!y) continue;
    const batcher = Math.floor(qty / y.batch);
    for (const [min, per] of Object.entries(y.minerals)) {
      ut[min] = (ut[min] || 0) + Math.floor(batcher * per * yieldFactor);
    }
  }
  return ut;
}

/** Hvor mye malm må du mine for å dekke `missing` = {mineralId: antall}?
 *  `ores` = [{id, name, volume, batch, minerals: {mineralId: per batch}}] – bare malmen der du miner.
 *
 *  Sjeldneste mineral først (rekkefølge = høyest Jita-pris per enhet): for hvert mineral som
 *  fortsatt mangler, velg malmen som gir mest av det per m³, og ta hele batcher nok til å dekke
 *  det. Alt annet den malmen gir, trekkes fra de andre behovene – så Tritanium-en fra Kernite
 *  teller. Det er ikke et optimalt LP-svar, men det er forklarbart og nær nok med seks malmtyper.
 *  Mineraler ingen av malmtypene gir, havner i `uncovered` (må kjøpes). */
export function orePlan(missing, ores, yieldFactor, prices = {}) {
  const rest = { ...missing };
  const plan = {};
  const uncovered = {};
  const rekkefolge = Object.keys(rest).sort((a, b) => (prices[b] || 0) - (prices[a] || 0));

  for (const min of rekkefolge) {
    if (!(rest[min] > 0)) continue;
    let beste = null, bestePerM3 = 0;
    for (const o of ores) {
      const perBatch = (o.minerals[min] || 0) * yieldFactor;
      if (perBatch <= 0) continue;
      const perM3 = perBatch / (o.batch * o.volume);
      if (perM3 > bestePerM3) { beste = o; bestePerM3 = perM3; }
    }
    if (!beste) { uncovered[min] = rest[min]; rest[min] = 0; continue; }
    const perBatch = beste.minerals[min] * yieldFactor;
    const batcher = Math.ceil(rest[min] / perBatch);
    plan[beste.id] = (plan[beste.id] || 0) + batcher;
    for (const [m, per] of Object.entries(beste.minerals)) {
      if (rest[m] != null) rest[m] -= Math.floor(batcher * per * yieldFactor);
    }
  }

  const linjer = Object.entries(plan).map(([id, batcher]) => {
    const o = ores.find((x) => String(x.id) === String(id));
    const units = batcher * o.batch;
    return { ore_type_id: Number(id), name: o.name, units, batches: batcher,
             m3: Math.round(units * o.volume * 100) / 100 };
  }).sort((a, b) => b.m3 - a.m3);
  // Overskudd: det planen gir utover behovet (negativ rest), så det kan brukes neste gang
  const surplus = {};
  for (const [m, v] of Object.entries(rest)) if (v < 0) surplus[m] = -v;
  return { ores: linjer, m3: Math.round(linjer.reduce((s, x) => s + x.m3, 0) * 100) / 100,
           uncovered, surplus };
}
