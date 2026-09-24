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
