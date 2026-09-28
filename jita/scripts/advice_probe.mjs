// Skriver ut JS-sidens svar på de samme tallene som test_advice.py regner i Python.
// Brukes bare av testen: den sammenligner de to, slik at lib/advice.js og common.py ikke
// kan skli fra hverandre uten at Actions-jobben faller.
import { guardAdvice, dumpNet, undercutAdvice } from "../lib/advice.js";

const RAAD = { action: "SENK", fee: 50000, text: "senk til 12 600", new_price: 12600, gain_24h: 400000 };
const K = { side: "sell", changesTotal: 1, feesPaidEst: 0, positionProfit: 5e6,
            bestBid: 10200, remaining: 917, costPerUnit: 6229, broker: 0.018, tax: 0.05 };
const r = {
  karantene: guardAdvice(RAAD, { ...K, hoursSinceChange: 2 }),
  gebyrtak: guardAdvice(RAAD, { ...K, hoursSinceChange: 30, feesPaidEst: 800000 }),
  krig: guardAdvice(RAAD, { ...K, hoursSinceChange: 1, changesTotal: 3 }),
  fritt: guardAdvice(RAAD, { ...K, hoursSinceChange: 30 }),
  kjop: guardAdvice({ ...RAAD, action: "HEV" }, { ...K, side: "buy", hoursSinceChange: 30, changesTotal: 9 }),
};
// Rettelsene 28. sept: «senk» regnes på fortjeneste (ikke inntekt), og DUMP krever kostpris og dybde i budet
const P = { broker: 0.018, tax: 0.05, adv_broker_relations: 0 };
const u = {
  senk_netto: undercutAdvice(P, 12610, 917, 12600, 400, 50, 10000),
  senk_ukjent: undercutAdvice(P, 12610, 917, 12600, 400, 50, null),
  senk_lonner: undercutAdvice(P, 12610, 917, 12600, 2000, 300, 6229),
  dump_ukjent: guardAdvice(RAAD, { ...K, hoursSinceChange: 1, changesTotal: 3, costPerUnit: null }),
  dump_dybde: guardAdvice(RAAD, { ...K, hoursSinceChange: 1, changesTotal: 3, bidDepth: 200 }),
};
console.log(JSON.stringify({
  senk_netto: [u.senk_netto.action, u.senk_netto.gain_24h, u.senk_netto.text],
  senk_ukjent: [u.senk_ukjent.action, u.senk_ukjent.gain_24h, u.senk_ukjent.text],
  senk_lonner: [u.senk_lonner.action, u.senk_lonner.gain_24h, u.senk_lonner.text],
  dump_ukjent: [u.dump_ukjent.action, u.dump_ukjent.guard],
  dump_dybde: [u.dump_dybde.action, u.dump_dybde.dump_qty, u.dump_dybde.dump_net, u.dump_dybde.text],
  karantene: [r.karantene.action, r.karantene.guard, r.karantene.text],
  gebyrtak: [r.gebyrtak.action, r.gebyrtak.guard, r.gebyrtak.text],
  krig: [r.krig.action, r.krig.guard, r.krig.dump_net, r.krig.text],
  fritt: [r.fritt.action, r.fritt.guard ?? null],
  kjop: [r.kjop.action, r.kjop.guard ?? null],
  dump_net: dumpNet(10200, 917, 6229, 0.05, 0.018).net,
}));
