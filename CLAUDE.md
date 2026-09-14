# Supnet – Familie Oltedal

## Prosjektbeskrivelse
En PWA (Progressive Web App) familieside for familien Oltedal. Appen er på norsk og kjøres lokalt/hostet som statiske HTML-filer.

## Struktur
- `index.html` – Innloggingsside med brukervalg og PIN-kode
- `dashboard.html` – Hoveddashboard med vær, kalender og meldingstavle
- `manifest.json` – PWA-manifest (navn, ikoner, tema)
- `sw.js` – Service worker for offline-støtte og caching
- `icons/` – App-ikoner (icon-192.png, icon-512.png) – mangler i mappen ennå

## Brukere
| Bruker  | Emoji | PIN  |
|---------|-------|------|
| Daniel  | 🦒    | 0000 |
| Hanna   | 🦁    | 0000 |
| Frida   | 🦊    | 0000 |
| Magnus  | 🐧    | 0000 |

## Teknisk
- Rent HTML/CSS/JavaScript – ingen rammeverk, ingen build-steg
- Font: Nunito (Google Fonts)
- Vær-API: api.met.no (Yr) – koordinater satt til Fagertunveien, Jar (lat: 59.9324, lon: 10.6045)
- Data (kalender, meldinger) lagres kun i minnet – ikke persistent mellom refreshes ennå
- Service worker cacher: `/`, `index.html`, `dashboard.html`, `manifest.json`, og Nunito-fonten
- Vær-API-kall caches ikke (alltid ferske data)

## Designsystem
- Bakgrunn: `#0f1117`
- Kort: `#1a1d27`
- Aksent: `#f0c040` (gul)
- Tekst: `#e8e8f0`
- Dempet tekst: `#666880`
- Border-radius: `20px`
- Bruker-farge (standard): `#3a7bd5` (blå)
