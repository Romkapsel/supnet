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
  const gain = Math.min(remaining, bfsPerDay) * p2 * (1 - broker - tax);
  const lossVsNow = (p1 - p2) * remaining;
  let action, text;
  if (margin2 != null && margin2 < minMargin) { action = "HOLD"; text = `ved ${isk(p2)} blir marginen mot kostpris ${(margin2 * 100).toFixed(1).replace(".", ",")} % (< ${Math.round(minMargin * 100)} %). Ikke følg ned. Mur under deg: ${wallQty} stk (~${daysWall.toFixed(1)} d).`; }
  else if (daysWall > 5 && gain > 2 * fee + lossVsNow * 0.5) { action = "SENK"; text = `senk til ${isk(p2)}: gebyr ${isk(fee)} ISK, gir opp ${isk(lossVsNow)} ISK i pris, men muren under deg (${wallQty} stk) tar ~${daysWall.toFixed(1)} d å tømme.`; }
  else if (daysWall <= 5) { action = "HOLD"; text = `muren under deg (${wallQty} stk) liftes bort på ~${daysWall.toFixed(1)} d. Senking ville kostet ${isk(fee)} + ${isk(lossVsNow)} ISK.`; }
  else { action = "HOLD"; text = `senking til ${isk(p2)} koster ${isk(fee)} + ${isk(lossVsNow)} ISK og gir ~${isk(gain)} neste 24 t – ikke verdt det. Mur ${wallQty} stk (~${daysWall.toFixed(1)} d).`; }
  return { action, text, new_price: p2, fee: Math.round(fee), wall_qty: wallQty, days_wall: +daysWall.toFixed(1), gain_24h: Math.round(gain), margin_at_new: margin2 };
}
