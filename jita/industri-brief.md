# Industri-marginfinner – opprinnelig brief

(Kopiert inn 24. sept 2026. Avvik og valg som er tatt i implementasjonen står i README.md under «Industri».)

## Mål
Bygg et verktøy som rangerer hvilke T1-produkter karakteren **Starpete** bør produsere i **Ylandoki (system_id 30001395, Lonetrek, 0.8)**, med salg i **Jita 4-4 (station_id 60003760)**. Verktøyet skal utvides etter hvert som en modul i det eksisterende Jita-trading-verktøyet mitt.

## Forutsetninger
- Produksjon i NPC-stasjon: facility tax 0,25 %, SCC-avgift 4 % av EIV, pluss systemets manufacturing cost index (hentes live).
- Blueprints: NPC-selgde BPO-er, research til ME 10 / TE 20.
- Materialer kjøpes i Jita via buy orders (bruk høyeste buy-pris), alternativt instant fra laveste sell.
- Salg i Jita via sell orders: trekk fra sales tax + broker fee (parameter, standard 5 % totalt).
- Karakterens skills er parametre (Industry, Advanced Industry osv.), standard Industry 5 / Advanced Industry 3.

## Datakilder
1. **EVE Ref Industry Cost API** – `https://api.everef.net/v1/industry/cost` med `product_id`, `runs`, `me`, `te`, `system_id`, `facility_tax`, skill-parametre. Gir materialmengder, jobbavgift og tid. Vær høflig: cache svar, maks noen få kall i sekundet.
2. **Fuzzwork market aggregates** – `https://market.fuzzwork.co.uk/aggregates/?station=60003760&types=...` for buy max / sell min i Jita 4-4.
3. **ESI** – `/markets/10000002/history/?type_id=` (dagsvolum, 30- og 90-dagers snitt, volatilitet), `/markets/prices/` (adjusted/average price, også for BPO-priser), `/industry/systems/` (kostnadsindekser), `/markets/10000002/orders/` (antall konkurrerende sell orders).
4. **SDE** (EVE Ref eller Fuzzwork-dump) for å liste alle blueprints med manufacturing-aktivitet og filtrere til T1-produkter med NPC-selgde BPO-er.

## Beregninger per produkt
- Kost per enhet = (materialer + jobbavgift) / enheter
- Netto salgspris = Jita sell min × (1 − salgsavgifter)
- Fortjeneste per enhet, margin %, ISK per time per produksjonsslot
- **Realistisk ISK per døgn per slot** = fortjeneste × min(enheter slotten rekker per døgn, 10 % av dagsvolum)
- BPO-pris og tilbakebetalingstid (antall enheter / døgn)
- Kapitalbinding per jobb (materialkost for en full batch)
- Konkurransemål: antall sell orders i Jita, hvor ofte laveste pris endres, prisfall siste 30 dager
- Volum (m3) inn og ut, for hauling-planlegging (Jita ↔ Ylandoki, 3 hopp)

## Output
1. Tabell sortert på realistisk ISK per døgn per slot, med filtre for kategori, min. dagsvolum, maks BPO-pris og maks kapitalbinding.
2. En «portefølje-anbefaling»: gitt N produksjonsslots og X ISK kapital, velg kombinasjonen som maksimerer daglig fortjeneste uten å ta mer enn 10 % av noe markeds dagsvolum.
3. Historikk: lagre hver kjøring (dato, pris, margin) slik at vi ser trender over tid. Bruk samme database som Jita-verktøyet om mulig.
4. Varsel når margin på et produkt i porteføljen faller under en terskel.

## Viktige fallgruver å håndtere
- ESI gir kostnadsindekser med 4 desimaler, så små avvik fra spillet er normalt.
- Produkter med `units_per_run` > 1 (ammo, probes) må regnes per enhet.
- Ekskluder produkter der Jita-markedet er tynt (få ordre, stor spread) eller manipulert (plutselige pristopper).
- Region Exordium har egne straffeavgifter (+5 % industri, +5 % markedsavgifter på ordre). Den skal aldri brukes som produksjons- eller salgssted.
- Mineraler fra egen mining er ikke gratis: verdsett dem til markedspris (alternativkost).

## Første leveranse
Et Python-skript (eller en modul i det eksisterende verktøyet) som kjører analysen for alle T1-moduler, droner, ammo, rigs, deployables og T1-skip, og skriver en CSV/tabell med topp 30. Deretter bygger vi porteføljevelgeren og historikken.
