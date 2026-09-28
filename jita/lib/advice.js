// Jita – råd for egne ordrer, speil av common.py (overbid_advice / undercut_advice / modify_fee).
// Brukes av «Å gjøre» slik at lista regnes LIVE fra dine ordrer (EVE) mot siste ordrebok – ikke fra lagrede varsler.

export const tick = (p) => Math.pow(10, Math.floor(Math.log10(p)) - 3);
export const isk = (x) => Math.round(x).toLocaleString("nb-NO");

export function modifyFee(broker, abr, p1, p2, qty) {
  const relistDiscount = 0.5 + 0.06 * Math.max(0, Math.min(5, abr || 0));
  return Math.max(0, broker * (p2 - p1)) * qty + (1 - relistDiscount) * broker * p2 * qty;
}

// Kjøpsordre overbudt. Returnerer {action: HEV|TREKK|HOLD, text, new_price, fee, wall_qty, days_wall, gain_24h}
export function overbidAdvice(p, p1, remaining, bestBid, wallQty, s2bPerDay, bestAsk, minMargin = 0.10) {
  const broker = Number(p.broker), tax = Number(p.tax);
  const p2 = bestBid + tick(bestBid), sell = bestAsk - tick(bestAsk);
  const net2 = sell * (1 - broker - tax) - p2 * (1 + broker);
  const margin2 = net2 / (p2 * (1 + broker));
  const fee = modifyFee(broker, p.adv_broker_relations, p1, p2, remaining);
  const daysWall = wallQty / Math.max(s2bPerDay, 0.1);
  const gain = Math.min(remaining, s2bPerDay) * net2;
  let action, text;
  if (margin2 < minMargin) { action = "TREKK"; text = `ved ${isk(p2)} blir marginen ${(margin2 * 100).toFixed(1).replace(".", ",")} % (< ${Math.round(minMargin * 100)} %). Ikke hev – trekk ordren og frigjør ${isk(p1 * remaining)} ISK. Mur over deg: ${wallQty} stk (~${daysWall.toFixed(1)} d).`; }
  else if (daysWall > 5 && gain > 2 * fee) { action = "HEV"; text = `hev til ${isk(p2)}: gebyr ${isk(fee)} ISK, forventet ~${isk(gain)} ISK netto neste 24 t. Muren over deg (${wallQty} stk) tar ~${daysWall.toFixed(1)} d å tømme.`; }
  else if (daysWall <= 5) { action = "HOLD"; text = `muren over deg (${wallQty} stk) tømmes på ~${daysWall.toFixed(1)} d. Heving ville kostet ${isk(fee)} ISK – ikke verdt det.`; }
  else { action = "HOLD"; text = `heving til ${isk(p2)} koster ${isk(fee)} ISK og gir ~${isk(gain)} ISK neste 24 t – ikke verdt det. Mur ${wallQty} stk (~${daysWall.toFixed(1)} d).`; }
  return { action, text, new_price: p2, fee: Math.round(fee), wall_qty: wallQty, days_wall: +daysWall.toFixed(1), gain_24h: Math.round(gain), margin_at_new: margin2 };
}

// Salgsordre underbudt. Returnerer {action: SENK|HOLD, …}
export function undercutAdvice(p, p1, remaining, bestAsk, wallQty, bfsPerDay, costPerUnit, minMargin = 0.10) {
  const broker = Number(p.broker), tax = Number(p.tax);
  const p2 = bestAsk - tick(bestAsk);
  const fee = modifyFee(broker, p.adv_broker_relations, p1, p2, remaining);
  const daysWall = wallQty / Math.max(bfsPerDay, 0.1);
  const net2 = p2 * (1 - broker - tax) - (costPerUnit ? costPerUnit * (1 + broker) : 0);
  const margin2 = costPerUnit ? net2 / (costPerUnit * (1 + broker)) : null;
  // Gevinst = ekstra FORTJENESTE neste 24 t, ikke salgsinntekt (rettet 28. sept). Uten kostpris: ingen senking.
  const gain = costPerUnit ? Math.min(remaining, bfsPerDay) * net2 : 0;
  const lossVsNow = (p1 - p2) * remaining;
  let action, text;
  if (margin2 != null && margin2 < minMargin) { action = "HOLD"; text = `ved ${isk(p2)} blir marginen mot kostpris ${(margin2 * 100).toFixed(1).replace(".", ",")} % (< ${Math.round(minMargin * 100)} %). Ikke følg ned. Muren under deg er ${wallQty} stk (~${daysWall.toFixed(1)} d).`; }
  else if (daysWall > 5 && !costPerUnit) { action = "HOLD"; text = `kostprisen er ukjent (ikke kjøpt de siste 90 dagene), så det kan ikke regnes om senking til ${isk(p2)} lønner seg. Senk bare hvis du vet at prisen gir fortjeneste. Mur under deg: ${wallQty} stk (~${daysWall.toFixed(1)} d).`; }
  else if (daysWall > 5 && gain > 2 * fee + lossVsNow * 0.5) { action = "SENK"; text = `senk til ${isk(p2)}: gebyr ${isk(fee)} ISK, gir opp ${isk(lossVsNow)} ISK i pris, men muren under deg (${wallQty} stk) tar ~${daysWall.toFixed(1)} d å tømme.`; }
  else if (daysWall <= 5) { action = "HOLD"; text = `muren under deg (${wallQty} stk) liftes bort på ~${daysWall.toFixed(1)} d. Senking ville kostet ${isk(fee)} ISK i gebyr + ${isk(lossVsNow)} ISK i pris.`; }
  else { action = "HOLD"; text = `senking til ${isk(p2)} koster ${isk(fee)} ISK + ${isk(lossVsNow)} ISK i pris og gir ~${isk(gain)} neste 24 t – ikke verdt det. Mur ${wallQty} stk (~${daysWall.toFixed(1)} d).`; }
  return { action, text, new_price: p2, fee: Math.round(fee), wall_qty: wallQty, days_wall: +daysWall.toFixed(1), gain_24h: Math.round(gain), margin_at_new: margin2 };
}

// ── Vernet mot for hyppige endringer (27. sept 2026). Speil av guard_advice/dump_net i common.py ──
// Rådene over svarer på «lønner DENNE endringen seg?». De spør ikke hvor mange ganger ordren alt er
// endret. 11.–27. sept gikk ~6 mill. av 10,2 mill. i broker-gebyr til omprising, og 27. sept ble alle
// 17 ordrer endret på fire minutter: 698k i gebyr mot 429k i salg.

/** Hva får du om du selger hele resten rett i budet nå? Salg til et bud koster bare salgsskatt –
 *  ingen broker, ingen ny ordre, ingen kø. Det er utveien fra en priskrig. */
export function dumpNet(bestBid, remaining, costPerUnit, tax, broker) {
  const gross = bestBid * remaining * (1 - tax);
  const cost = costPerUnit ? costPerUnit * (1 + broker) * remaining : 0;
  return { net: gross - cost, gross, margin: cost ? (gross - cost) / cost : null };
}

/** Legger historikken oppå rådet. Gjør aldri et HOLD til en handling – bare motsatt vei.
 *  1) karantene: endret (eller lagt ut) for under cooldownH timer siden → LA STÅ. changedBefore
 *     skiller de to, slik at teksten ikke påstår en endring du ikke har gjort.
 *  2) gebyrtak: endringene har spist mer enn feeShare av posisjonens fortjeneste → LA STÅ
 *  3) utveien: endret dumpAfter ganger eller mer, og budet gir penger → DUMP (bare salgsordrer).
 *     Krever kjent kostpris, og antallet begrenses av bidDepth (enheter innenfor 1 % av toppbudet). */
export function guardAdvice(adv, ctx) {
  if (!["ENDRE", "HEV", "SENK"].includes(adv.action)) return adv;
  const { side, hoursSinceChange, changesTotal = 0, changedBefore = true, feesPaidEst = 0, positionProfit = 0,
          bestBid, remaining = 0, costPerUnit, broker, tax,
          cooldownH = 12, feeShare = 0.15, dumpAfter = 3, bidDepth = null } = ctx;
  const n = (v) => isk(v || 0);
  const en = (v, d = 1) => v.toFixed(d).replace(".", ",");

  const antall = bidDepth == null ? remaining : Math.min(remaining, Math.trunc(bidDepth));
  if (side === "sell" && changesTotal >= dumpAfter && bestBid && costPerUnit && antall > 0) {
    const d = dumpNet(bestBid, antall, costPerUnit, tax, broker);
    const hvem = antall === remaining ? `de ${remaining} resterende` : `${antall} av de ${remaining} (mer tar ikke budene innenfor 1 %)`;
    if (d.net > 0) return { ...adv, action: "DUMP", guard: "krig", new_price: bestBid, dump_qty: antall,
      dump_net: Math.round(d.net), dump_margin: d.margin,
      text: `du har endret denne ordren ${changesTotal} ganger. Selg ${hvem} rett i budet `
          + `${n(bestBid)} i stedet: ${n(d.net)} ISK netto etter skatt, ingen nytt broker-gebyr, ingen kø. `
          + `Å følge ned enda en gang koster ${n(adv.fee)} ISK og starter samme runde på nytt.` };
  }
  if (hoursSinceChange != null && hoursSinceChange < cooldownH) {
    return { ...adv, action: "LA STÅ", guard: "karantene",
      text: (changedBefore ? `du endret denne for ${en(hoursSinceChange)} t siden. `
                           : `du la den ut for ${en(hoursSinceChange)} t siden. `)
          + `Regelen er én endring per ${Math.round(cooldownH)} t `
          + `– la den stå i ${en(cooldownH - hoursSinceChange)} t til. Endring nå koster ${n(adv.fee)} ISK i gebyr, `
          + `og markedet snur fortere enn posisjonen tjener det inn. (${adv.text || ""})` };
  }
  if (positionProfit > 0 && feesPaidEst > feeShare * positionProfit) {
    return { ...adv, action: "LA STÅ", guard: "gebyrtak",
      text: `endringene på denne ordren har alt kostet ~${n(feesPaidEst)} ISK, av en fortjeneste på `
          + `~${n(positionProfit)} ISK (${Math.round(feesPaidEst / positionProfit * 100)} % spist av gebyr, `
          + `taket er ${Math.round(feeShare * 100)} %). La den stå – eller selg til budet.` };
  }
  return adv;
}
