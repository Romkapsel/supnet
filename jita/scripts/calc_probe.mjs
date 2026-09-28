// Skriver ut kalkulatorens svar (lib/calc.js) på faste tall, så test_calc.py kan sjekke dem
// mot Python-fasiten (material_quantity i industry.py) og mot egne regler for malmplanen.
import { materialQuantity, oreToMinerals, orePlan } from "../lib/calc.js";

const rutenett = [];
for (const base of [1, 2, 3, 7, 10, 54, 219, 7488, 8071, 80000])
  for (const runs of [1, 2, 3, 10, 17, 100])
    for (const me of [0, 1, 5, 9, 10])
      rutenett.push([base, runs, me, materialQuantity(base, runs, me)]);

const ORES = [
  { id: 1230, name: "Veldspar", volume: 0.1, batch: 100, minerals: { 34: 400 } },
  { id: 1228, name: "Scordite", volume: 0.15, batch: 100, minerals: { 34: 150, 35: 110 } },
  { id: 1224, name: "Pyroxeres", volume: 0.3, batch: 100, minerals: { 35: 90, 36: 30 } },
  { id: 18, name: "Plagioclase", volume: 0.35, batch: 100, minerals: { 34: 175, 36: 70 } },
  { id: 1227, name: "Omber", volume: 0.6, batch: 100, minerals: { 35: 90, 37: 75 } },
  { id: 20, name: "Kernite", volume: 1.2, batch: 100, minerals: { 36: 60, 37: 120 } },
];
const PRISER = { 34: 3.63, 35: 16.15, 36: 49.6, 37: 150, 38: 900 };
// Thermal Shield Hardener I, ME 10, 1 run – slik den sto på skjermen 28. sept
const behov = { 34: 7264, 35: 6739, 36: 197, 37: 2 };
const plan = orePlan(behov, ORES, 0.5, PRISER);
// Nocxium (38) finnes ikke i noen av malmtypene → skal havne i uncovered
const planMedNocx = orePlan({ 34: 1000, 38: 50 }, ORES, 0.5, PRISER);
const fraMalm = oreToMinerals({ 1230: 1050, 1228: 99 }, Object.fromEntries(
  ORES.map((o) => [o.id, { batch: o.batch, minerals: o.minerals }])), 0.5);

console.log(JSON.stringify({ rutenett, plan, planMedNocx, fraMalm, ORES }));
