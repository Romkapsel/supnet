// Jita – beste tidspunkt å endre eller legge ut en ordre (29. sept 2026). Brukes av «Å gjøre».
//
// Idé: en ordre tjener bare penger når den ligger øverst MENS handelen skjer. Kjøpsordrer fylles når
// folk dumper (selger til bud), salgsordrer når folk lifter (kjøper fra ask). Beste tid å heve/senke er
// derfor timen rett FØR de tre timene på rad med mest slik handel i denne varen – da rekker du å komme på
// toppen før toppen, og konkurrentene har kortere tid på å legge seg over deg igjen.
//
// Grunnlaget er hvor mange av de siste 7 dagene varen ble handlet i hver norsk klokketime (0–7 per time),
// ikke antall enheter eller handler: én dumping av 38 000 Cap Boostere, eller 24 handler én formiddag, er
// ikke et mønster – men de ville ha bestemt «beste tid». Et mønster må gå igjen flere dager.
// Har varen under MIN_ACTIVE handelstimer til sammen, brukes alle varene i lista samlet, og det sies i
// teksten. Handles varen ikke minst FLAT_RATIO ganger så ofte i toppen som resten av døgnet, sier teksten
// at tidspunktet betyr lite – det er det ærlige svaret for varer som handles hele døgnet.

export const WIDTH = 3;            // timer i toppvinduet
export const MIN_ACTIVE = 20;      // handelstimer (time × dag) siste 7 d før varens eget mønster brukes
export const FLAT_RATIO = 1.5;     // toppen må ha minst 1,5 × så mye handel per time som resten av døgnet
export const MIN_DAYS = 3;         // …og gå igjen minst 3 av 7 dager – «2 av 7, ellers 1» er tilfeldigheter

/** Største sum over WIDTH timer på rad (døgnet går rundt). → { start, share, ratio, inn, ut }
 *  inn/ut = snitt per time i vinduet / resten av døgnet; ratio = inn / ut. */
export function peakWindow(flow24, width = WIDTH) {
  const total = flow24.reduce((a, v) => a + v, 0);
  if (!(total > 0)) return null;
  let best = -1, start = 0;
  for (let h = 0; h < 24; h++) {
    let s = 0;
    for (let k = 0; k < width; k++) s += flow24[(h + k) % 24];
    if (s > best) { best = s; start = h; }
  }
  const inn = best / width, ut = (total - best) / (24 - width);
  return { start, share: best / total, inn, ut, ratio: ut > 0 ? inn / ut : Infinity };
}

const kl = (h) => `${String(((h % 24) + 24) % 24).padStart(2, "0")}`;

/** Når neste gang er klokka `hour` (norsk tid), tidligst `fromHour` timer fra nå (desimaltimer, 0 = nå)?
 *  → "nå" | "i dag kl. HH" | "i morgen kl. HH" */
export function nextAt(hour, nowHour, fromHours = 0) {
  const earliest = nowHour + fromHours;                     // kan være > 24 (i morgen)
  let t = Math.floor(earliest / 24) * 24 + hour;            // samme døgn som «tidligst»
  if (t + 1 <= earliest) t += 24;                           // timen er passert (hele timen teller som «nå»)
  if (fromHours <= 0 && t <= nowHour && nowHour < t + 1) return "nå";
  const dag = t < 24 ? "i dag" : t < 48 ? "i morgen" : "i overmorgen";
  if (t < earliest) {                                       // karantenen slutter midt i den gode timen
    const min = Math.ceil((earliest - t) * 60 - 1e-9);          // flyttall: 0,3 t = 18 min, ikke 19
    return `${dag} kl. ${kl(hour)}:${String(Math.min(min, 59)).padStart(2, "0")}`;
  }
  return `${dag} kl. ${kl(hour)}`;
}

/**
 * @param side   "buy" (flyten er dumping) eller "sell" (flyten er lifting)
 * @param own    24 tall: varens flyt per norsk klokketime (siste 7 d), eller null
 * @param pool   24 tall: alle varene i lista samlet (reserve når varen har for lite data)
 * @param nowHour  norsk klokketime nå (0–23, med desimaler)
 * @param waitHours  karantene igjen (timer) – neste tid regnes fra når den er over
 * @param downtimeHour  norsk klokketime for EVEs nedetid (11:00 UTC)
 * → { hour, window, share, source, when, text } eller null
 */
export function bestTime(side, own, pool, nowHour, waitHours = 0, downtimeHour = 13) {
  const active = own ? own.reduce((a, v) => a + v, 0) : 0;
  const source = active >= MIN_ACTIVE ? "vare" : "samlet";
  const flow = source === "vare" ? own : pool;
  const w = flow && peakWindow(flow);
  if (!w) return null;
  const kilde = source === "vare" ? "" : "; for lite data på varen – mønsteret for alle varene dine";
  if (w.ratio < FLAT_RATIO) {
    return { hour: null, window: null, share: w.share, source, when: null,
      text: `Tidspunktet betyr lite: ${side === "buy" ? "dumpingen" : "kjøpet"} er jevnt fordelt over døgnet${kilde ? ` (${kilde.slice(2)})` : ""}.` };
  }
  if (source === "vare" && w.inn < MIN_DAYS) {
    return { hour: null, window: null, share: w.share, source, when: null,
      text: `Ingen fast tid: varen handles for sjelden til at noen timer skiller seg ut (toppen er ${Math.round(w.inn)} av 7 dager).` };
  }
  let hour = (w.start + 23) % 24;                           // timen før toppen
  let tid = `kl. ${kl(hour)}`;
  if (hour === downtimeHour) { tid = `kl. ${kl(hour)}:30 (etter nedetiden)`; }
  const slutt = (w.start + WIDTH) % 24;
  let when = nextAt(hour, nowHour, waitHours);
  if (hour === downtimeHour && /kl\. \d\d$/.test(when)) when += ":30";      // samme klokkeslett som i «Beste tid»
  const en = (v) => v.toFixed(1).replace(".", ",");
  const hvor = source === "vare"
    ? `handel i disse timene ${Math.round(w.inn)} av 7 dager, ellers ${Math.round(w.ut)}`
    : `${en(Math.min(w.ratio, 9.9))} ganger så mye handel som resten av døgnet`;
  const topp = side === "buy" ? `dumpingen topper kl. ${kl(w.start)}–${kl(slutt)}` : `kjøperne kommer kl. ${kl(w.start)}–${kl(slutt)}`;
  const naar = when === "nå" ? "Det er nå." : `Neste gang: ${when}${waitHours > 0 ? " (etter karantenen)" : ""}.`;
  return { hour, window: [w.start, slutt], share: w.share, source, when,
    text: `Beste tid: ${tid}, rett før ${topp} (${hvor}${kilde}). ${naar}` };
}
