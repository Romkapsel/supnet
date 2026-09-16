// Jita – felles klientkode for alle sidene under /jita/
// PIN lagres i localStorage av login.html og sendes som header til /api/*.

const PIN = localStorage.getItem('jita_pin');
if (!PIN) location.replace('login.html');

export async function api(action, { method = 'GET', body, query = {} } = {}) {
  const qs = new URLSearchParams(query).toString();
  const r = await fetch(`/api/${action}${qs ? '?' + qs : ''}`, {
    method,
    headers: { 'x-jita-pin': PIN, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401 || r.status === 423) { localStorage.removeItem('jita_pin'); location.replace('login.html'); return; }
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

// ── Formatering ──────────────────────────────────────────────────────────────
const nb = new Intl.NumberFormat('nb-NO', { maximumFractionDigits: 0 });
export function isk(v, digits) {
  if (v == null) return '–';
  v = Number(v);
  if (digits != null) return new Intl.NumberFormat('nb-NO', { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v);
  if (Math.abs(v) >= 1e9) return (v / 1e9).toFixed(2).replace('.', ',') + ' mrd';
  if (Math.abs(v) >= 1e6) return (v / 1e6).toFixed(2).replace('.', ',') + ' mill';
  return nb.format(v);
}
export function price(v) {
  if (v == null) return '–';
  v = Number(v);
  return new Intl.NumberFormat('nb-NO', { minimumFractionDigits: v < 100 ? 2 : 0, maximumFractionDigits: v < 100 ? 2 : 0 }).format(v);
}
export const pct = (v, d = 1) => v == null ? '–' : (Number(v) * 100).toFixed(d).replace('.', ',') + ' %';
export const num = (v, d = 0) => v == null ? '–' : Number(v).toFixed(d).replace('.', ',');
export const days = (v) => v == null ? '–' : (v < 0.5 ? '< ½ dag' : Number(v).toFixed(1).replace('.', ',') + ' d');

export function ago(ts) {
  if (!ts) return 'aldri';
  const m = Math.round((Date.now() - new Date(ts).getTime()) / 60000);
  if (m < 1) return 'nå';
  if (m < 60) return m + ' min siden';
  const h = Math.floor(m / 60);
  if (h < 48) return h + ' t ' + (m % 60) + ' min siden';
  return Math.floor(h / 24) + ' dager siden';
}
export const dt = (ts) => ts ? new Date(ts).toLocaleString('nb-NO', { timeZone: 'Europe/Oslo', dateStyle: 'short', timeStyle: 'short' }) : '–';
export const el = (id) => document.getElementById(id);
export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// ── Downtime-advarsel: EVE er nede 11:00 UTC daglig (13:00 norsk sommertid), normalt 10–15 min ──
export function downtimeWarning() {
  const now = new Date();
  const t = now.getUTCHours() * 60 + now.getUTCMinutes();
  return t >= 10 * 60 + 55 && t <= 11 * 60 + 20;
}

// ── Navigasjon ───────────────────────────────────────────────────────────────
export function nav(active) {
  const items = [['index.html', 'Topp 10'], ['decisions.html', 'Beholdning'], ['results.html', 'Resultat'], ['settings.html', 'Profil']];
  return `<nav class="jnav">${items.map(([href, label]) =>
    `<a href="${href}" class="${href === active ? 'on' : ''}">${label}</a>`).join('')}<a href="#" class="back" onclick="localStorage.removeItem('jita_pin');location.href='login.html';return false">Logg ut</a></nav>`;
}

// ── Enkel sparkline (SVG) ────────────────────────────────────────────────────
export function sparkline(series, { width = 300, height = 60, colors = ['#f0c040', '#3a7bd5'] } = {}) {
  const all = series.flat().filter((v) => v != null);
  if (!all.length) return '';
  const min = Math.min(...all), max = Math.max(...all), span = max - min || 1;
  const n = Math.max(...series.map((s) => s.length));
  const paths = series.map((s, i) => {
    const d = s.map((v, j) => v == null ? null : `${(j / Math.max(n - 1, 1) * width).toFixed(1)},${(height - 4 - (v - min) / span * (height - 8)).toFixed(1)}`)
      .filter(Boolean).map((p, j) => (j ? 'L' : 'M') + p).join(' ');
    return `<path d="${d}" fill="none" stroke="${colors[i % colors.length]}" stroke-width="2"/>`;
  });
  return `<svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" preserveAspectRatio="none">${paths.join('')}</svg>`;
}

// ── Felles CSS ───────────────────────────────────────────────────────────────
export const CSS = `
:root { --bg:#0f1117; --card:#1a1d27; --card2:#22263a; --accent:#f0c040; --text:#e8e8f0; --muted:#666880; --radius:20px; --green:#4cd58a; --red:#e85d5d; --blue:#3a7bd5; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font-family:'Nunito',system-ui,sans-serif; padding:16px; padding-bottom:40px; max-width:900px; margin-inline:auto; }
h1 { font-size:1.6rem; margin:6px 0 2px; } h2 { font-size:1.05rem; margin:22px 0 8px; color:var(--accent); } h3 { font-size:0.95rem; margin:0 0 6px; }
a { color:var(--accent); text-decoration:none; }
.jnav { display:flex; gap:6px; flex-wrap:wrap; margin:10px 0 14px; }
.jnav a { padding:7px 14px; border-radius:999px; background:var(--card); color:var(--muted); font-weight:700; font-size:0.9rem; }
.jnav a.on { background:var(--accent); color:#1a1d27; } .jnav a.back { margin-left:auto; }
.card { background:var(--card); border-radius:var(--radius); padding:14px 16px; margin-bottom:10px; }
.card.link { cursor:pointer; } .card.link:hover { background:var(--card2); }
.row { display:flex; gap:10px; flex-wrap:wrap; align-items:baseline; justify-content:space-between; }
.muted { color:var(--muted); font-size:0.85rem; } .small { font-size:0.85rem; }
.big { font-size:1.25rem; font-weight:800; } .accent { color:var(--accent); } .green { color:var(--green); } .red { color:var(--red); }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(120px,1fr)); gap:8px; }
.stat { background:var(--card2); border-radius:14px; padding:10px 12px; } .stat .l { color:var(--muted); font-size:0.75rem; } .stat .v { font-weight:800; font-size:1.05rem; }
.pill { display:inline-block; padding:2px 9px; border-radius:999px; font-size:0.75rem; font-weight:700; background:var(--card2); color:var(--muted); margin:2px 3px 0 0; }
.pill.ok { background:#1f3a2a; color:var(--green); } .pill.bad { background:#3a1f1f; color:var(--red); } .pill.warn { background:#3a331f; color:var(--accent); }
button, .btn { font:inherit; font-weight:800; border:0; border-radius:999px; padding:9px 16px; background:var(--accent); color:#1a1d27; cursor:pointer; }
button.ghost { background:var(--card2); color:var(--text); } button:disabled { opacity:0.5; cursor:default; }
input, select, textarea { font:inherit; background:var(--card2); color:var(--text); border:1px solid #2e3350; border-radius:12px; padding:8px 10px; width:100%; }
label { display:block; font-size:0.8rem; color:var(--muted); margin:8px 0 3px; }
.grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:8px 12px; }
.banner { background:#3a1f1f; color:var(--red); border-radius:14px; padding:10px 14px; font-weight:800; margin-bottom:10px; }
table { width:100%; border-collapse:collapse; font-size:0.85rem; } th, td { text-align:right; padding:5px 6px; border-bottom:1px solid #2a2e40; } th:first-child, td:first-child { text-align:left; }
th { color:var(--muted); font-weight:700; }
.reason { font-size:0.85rem; color:#b8bacb; margin-top:6px; }
.err { color:var(--red); font-weight:700; }
@media (max-width:480px) { body { padding:12px; } h1 { font-size:1.35rem; } }
`;
export function injectCss() {
  const s = document.createElement('style'); s.textContent = CSS; document.head.appendChild(s);
}
