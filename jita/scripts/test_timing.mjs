// test_timing.mjs – «beste tid» i «Å gjøre» (lib/timing.js). Krever verken nett eller database.
// Kjør:  node jita/scripts/test_timing.mjs
import { peakWindow, nextAt, bestTime } from "../lib/timing.js";

let feil = 0;
const sjekk = (navn, fikk, vil) => {
  const ok = JSON.stringify(fikk) === JSON.stringify(vil);
  console.log(`${ok ? "ok  " : "FEIL"} ${navn}: ${JSON.stringify(fikk)}${ok ? "" : ` (ventet ${JSON.stringify(vil)})`}`);
  if (!ok) feil++;
};
const flat = Array(24).fill(2);
// Tallene er antall dager (av 7) varen ble handlet i hver klokketime
const kveld = Array(24).fill(1); kveld[19] = 6; kveld[20] = 7; kveld[21] = 6;        // topp kl. 19–22
const midnatt = Array(24).fill(1); midnatt[23] = 6; midnatt[0] = 6; midnatt[1] = 6;   // går over midnatt

// ── toppvinduet ──
sjekk("topp: kveld starter kl. 19", peakWindow(kveld).start, 19);
sjekk("topp: 19 handelsdager i vinduet mot 21 på resten", [peakWindow(kveld).inn, peakWindow(kveld).ut].map((v) => Math.round(v * 100) / 100), [6.33, 1]);
sjekk("topp: over midnatt starter kl. 23", peakWindow(midnatt).start, 23);
sjekk("topp: ingen flyt → null", peakWindow(Array(24).fill(0)), null);

// ── neste gang ──
sjekk("neste: inne i timen = nå", nextAt(18, 18.4), "nå");
sjekk("neste: senere i dag", nextAt(18, 9.0), "i dag kl. 18");
sjekk("neste: passert → i morgen", nextAt(18, 19.2), "i morgen kl. 18");
sjekk("neste: karantene 5 t fra kl. 16 → i morgen kl. 18", nextAt(18, 16.0, 5), "i morgen kl. 18");
sjekk("neste: karantene 1 t fra kl. 16 → i dag kl. 18", nextAt(18, 16.0, 1), "i dag kl. 18");
sjekk("neste: karantene slutter midt i timen → klokkeslett", nextAt(18, 18.2, 0.1), "i dag kl. 18:18");
sjekk("neste: karantene sier aldri «nå»", nextAt(18, 18.0, 0.5) !== "nå", true);

// ── rådet ──
const b = bestTime("buy", kveld, flat, 10.0);
sjekk("råd: timen før toppen", b.hour, 18);
sjekk("råd: bruker varens egne tall", b.source, "vare");
sjekk("råd: tekst", b.text, "Beste tid: kl. 18, rett før dumpingen topper kl. 19–22 (handel i disse timene 6 av 7 dager, ellers 1). Neste gang: i dag kl. 18.");
const s = bestTime("sell", kveld, flat, 18.5);
sjekk("råd: salg, nå", s.when, "nå");
sjekk("råd: salg-tekst sier kjøperne", s.text.includes("kjøperne kommer kl. 19–22"), true);
const lite = Array(24).fill(0); lite[20] = 7; lite[3] = 7;                             // 14 handelstimer: for lite
const r = bestTime("buy", lite, kveld, 10.0);
sjekk("råd: for lite data → samlet mønster", r.source, "samlet");
sjekk("råd: samlet sier fra", r.text.includes("mønsteret for alle varene dine"), true);
sjekk("råd: samlet oppgir forholdstall", r.text.includes("(6,3 ganger så mye handel som resten av døgnet; for lite data på varen – mønsteret for alle varene dine)."), true);
// Én travel dag er ikke et mønster: 1 dag med handel i kl. 10 (selv med mange handler) teller som 1
const burst = Array(24).fill(3); burst[10] = 4;
sjekk("råd: én travel formiddag gir ikke «beste tid»", bestTime("sell", burst, flat, 9.0).hour, null);
sjekk("råd: jevn flyt → ingen time", bestTime("buy", flat, flat, 10.0).hour, null);
sjekk("råd: jevn flyt sier det", bestTime("sell", flat, flat, 10.0).text.startsWith("Tidspunktet betyr lite: kjøpet"), true);
const ned = Array(24).fill(1); ned[14] = 6; ned[15] = 6; ned[16] = 6;                   // time før = 13 = nedetid
sjekk("råd: nedetid → :30", bestTime("buy", ned, flat, 9.0, 0, 13).text.startsWith("Beste tid: kl. 13:30 (etter nedetiden)"), true);
sjekk("råd: nedetid → neste gang også :30", bestTime("buy", ned, flat, 14.5, 0, 13).when, "i morgen kl. 13:30");
const sjelden = Array(24).fill(1); sjelden[19] = 3; sjelden[21] = 3;                   // 2,3 av 7 i toppen
sjekk("råd: sjelden handel gir ingen fast tid", bestTime("buy", sjelden, kveld, 10.0).hour, null);
sjekk("råd: sjelden sier det", bestTime("buy", sjelden, kveld, 10.0).text.startsWith("Ingen fast tid"), true);
sjekk("råd: karantene nevnes", bestTime("buy", kveld, flat, 10.0, 12).text.endsWith("(etter karantenen)."), true);

console.log(feil ? `\n${feil} feil` : "\nTidspunktene regnes riktig.");
process.exit(feil ? 1 : 0);
