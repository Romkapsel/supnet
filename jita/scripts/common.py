"""
Jita – felles kode for roboten (spec v3, del 4 og 6).

Innhold:
  - formler (tick/over/under, gebyrer, break-even, score, gone_weight …) – FASITEN som SQL-dommeren testes mot
  - ESI-klient med compat-date, User-Agent, ETag-cache, rate-limit- og feilgrense-håndtering
  - Postgres-tilkobling (psycopg) via SUPABASE_DB_URL
  - Discord-varsel (maks 5 per kjøring)
  - robot_runs-logging
"""
from __future__ import annotations

import gzip
import json
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

# ── Konstanter ────────────────────────────────────────────────────────────────
REGION_FORGE = 10000002
JITA_44 = 60003760          # Jita IV - Moon 4 - Caldari Navy Assembly Plant
JITA_SYSTEM = 30000142
ESI = "https://esi.evetech.net"
COMPAT_DATE = "2026-09-14"
USER_AGENT = os.environ.get(
    "ESI_USER_AGENT",
    "Supnet-Jita/0.3 (+https://supnet-oltedal.vercel.app; github.com/kvalem/supnet)",
)
SNAPSHOT_DIR = Path(os.environ.get("SNAPSHOT_DIR", "./jita/snapshots"))
UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def log(*a):
    print(f"[{now_utc().strftime('%H:%M:%S')}]", *a, flush=True)


# ── Formler (del 6) ───────────────────────────────────────────────────────────
def tick(p: float) -> float:
    """Minste prissteg ved 4 signifikante siffer."""
    return 10 ** (math.floor(math.log10(p)) - 3)


def over(p: float) -> float:
    return p + tick(p)


def under(p: float) -> float:
    return p - tick(p)


@dataclass
class Profile:
    capital_isk: float = 4_500_000
    broker_relations: int = 4
    accounting: int = 0
    adv_broker_relations: int = 0
    trade: int = 4
    retail: int = 3
    wholesale: int = 0
    tycoon: int = 0
    standing_corp: float = 0.0
    standing_faction: float = 0.0
    broker_fee_override: float | None = None
    sales_tax_override: float | None = None
    positions: int = 7
    target_fill_days: float = 4
    reserve_share: float = 0.25
    min_qty: int = 20
    allow_t2: bool = False
    allow_faction: bool = False
    thresholds: dict | None = None

    @classmethod
    def from_row(cls, row: dict) -> "Profile":
        fields = {k: row[k] for k in cls.__dataclass_fields__ if k in row and row[k] is not None}
        for k, v in list(fields.items()):
            if k != "thresholds" and not isinstance(v, (bool, dict)):
                fields[k] = float(v) if k in ("capital_isk", "standing_corp", "standing_faction",
                                              "broker_fee_override", "sales_tax_override",
                                              "target_fill_days", "reserve_share") else int(v)
        return cls(**fields)


def fees(profile: Profile) -> tuple[float, float]:
    """Broker: 3 % − 0,3 pp/nivå BR − 0,03 pp/faction-standing − 0,02 pp/corp-standing, gulv 1 %.
    Sales tax: 7,5 % × (1 − 0,11 × Accounting)."""
    broker = profile.broker_fee_override
    if broker is None:
        broker = max(0.01,
                     0.03 - 0.003 * profile.broker_relations
                     - 0.0003 * max(0.0, profile.standing_faction or 0)
                     - 0.0002 * max(0.0, profile.standing_corp or 0))
    tax = profile.sales_tax_override
    if tax is None:
        tax = 0.075 * (1 - 0.11 * profile.accounting)
    return broker, tax


def break_even(broker: float, tax: float) -> float:
    return (1 + broker) / (1 - broker - tax) - 1


def order_slots(profile: Profile) -> int:
    return 5 + 4 * profile.trade + 8 * profile.retail + 16 * profile.wholesale + 32 * profile.tycoon


def position_budget(profile: Profile) -> float:
    return profile.capital_isk * (1 - profile.reserve_share) / profile.positions


def max_buy_price(profile: Profile) -> float:
    return position_budget(profile) / profile.min_qty


def min_net_per_unit(profile: Profile) -> float:
    return profile.capital_isk / 1000


def economics(best_bid: float, best_ask: float, broker: float, tax: float):
    """→ (kjøpspris, salgspris, netto per enhet, margin)"""
    buy = over(best_bid)
    sell = under(best_ask)
    inn = buy * (1 + broker)
    ut = sell * (1 - broker - tax)
    return buy, sell, ut - inn, (ut - inn) / inn


def qty_recommendation(profile: Profile, buy_price: float, s2b_per_day: float) -> int:
    budget = position_budget(profile)
    by_capital = budget // buy_price
    by_flow = s2b_per_day * profile.target_fill_days
    return int(max(1, min(by_capital, by_flow)))


def days_to_fill(units_ahead: float, my_qty: float, flow_per_day: float) -> float:
    return (units_ahead + my_qty) / max(flow_per_day, 0.1)


def flow_per_day(sum_qty: float, hours_covered: float) -> float:
    return sum_qty / max(hours_covered, 1) * 24


def score(net_per_unit: float, s2b: float, bfs: float, days: float, hist_pos: float) -> float:
    base = net_per_unit * min(s2b, bfs) / (1 + days)
    ratio = bfs / max(s2b, 0.1)
    return base * min(1.0, max(hist_pos, 0) / 0.7) * min(1.0, ratio)


def isk(x: float) -> str:
    """1234567 → '1 234 567' (norsk tusenskille)."""
    return f"{x:,.0f}".replace(",", " ")


def modify_fee(broker: float, adv_broker_relations: int, p1: float, p2: float, qty: int) -> float:
    """Gebyr for å endre en ordre fra P1 til P2 (CCP, «Broker Relations» 2020):
    broker × (P2 − P1) × antall ved prisøkning + (50 % − 6 % × ABR) × broker × P2 × antall (relist)."""
    relist_discount = 0.5 + 0.06 * max(0, min(5, adv_broker_relations))
    return max(0.0, broker * (p2 - p1)) * qty + (1 - relist_discount) * broker * p2 * qty


def overbid_advice(profile: "Profile", p1: float, remaining: int, best_bid: float, wall_qty: int,
                   s2b_per_day: float, best_ask: float, min_margin: float = 0.10) -> dict:
    """Råd når noen ligger over kjøpsordren din (spec 2.4):
    ENDRE bare hvis forventet ekstra fylling neste 24 t × netto > 2 × gebyr OG muren over deg er > 5 dagers flyt.
    Ellers HOLD. Blir marginen ved ny pris under terskelen → ikke øk (vurder å trekke)."""
    broker, tax = fees(profile)
    p2 = over(best_bid)
    sell = under(best_ask)
    net2 = sell * (1 - broker - tax) - p2 * (1 + broker)
    margin2 = net2 / (p2 * (1 + broker))
    fee = modify_fee(broker, profile.adv_broker_relations, p1, p2, remaining)
    days_wall = wall_qty / max(s2b_per_day, 0.1)
    gain_24h = min(remaining, s2b_per_day) * net2
    if margin2 < min_margin:
        action = "TREKK"
        why = (f"ved {isk(p2)} blir marginen {margin2 * 100:.1f} % (< {min_margin * 100:.0f} %). Ikke øk. "
               f"Muren over deg er {wall_qty} stk (~{days_wall:.1f} d) – trekk ordren hvis du vil frigjøre kapitalen.")
    elif days_wall > 5 and gain_24h > 2 * fee:
        action = "ENDRE"
        why = (f"endre til {isk(p2)}: gebyr {isk(fee)} ISK, forventet ~{isk(gain_24h)} ISK netto neste 24 t. "
               f"Muren over deg ({wall_qty} stk) tar ~{days_wall:.1f} d å tømme.")
    elif days_wall <= 5:
        action = "HOLD"
        why = (f"muren over deg ({wall_qty} stk) tømmes på ~{days_wall:.1f} d. "
               f"Endring ville kostet {isk(fee)} ISK – ikke verdt det.")
    else:
        action = "HOLD"
        why = (f"endring til {isk(p2)} koster {isk(fee)} ISK og ville gitt ~{isk(gain_24h)} ISK neste 24 t – ikke verdt det. "
               f"Mur {wall_qty} stk (~{days_wall:.1f} d).")
    return dict(action=action, text=why, new_price=p2, fee=round(fee), margin_at_new=margin2,
                wall_qty=wall_qty, days_wall=round(days_wall, 1), gain_24h=round(gain_24h))


def undercut_advice(profile: "Profile", p1: float, remaining: int, best_ask: float, wall_qty: int,
                    bfs_per_day: float, cost_per_unit: float | None, min_margin: float = 0.10) -> dict:
    """Råd når noen ligger under salgsordren din. Speilbildet av overbid_advice:
    ENDRE (senk til ett tick under laveste ask) bare hvis muren under deg er > 5 dagers lifting OG
    forventet ekstra netto neste 24 t > 2 × gebyr. HOLD hvis muren tømmes raskt. Blir marginen mot
    kostpris under terskelen ved ny pris → HOLD (ikke selg med tap for å komme først)."""
    broker, tax = fees(profile)
    p2 = under(best_ask)
    fee = modify_fee(broker, profile.adv_broker_relations, p1, p2, remaining)   # senking: bare relist-delen
    days_wall = wall_qty / max(bfs_per_day, 0.1)
    net2 = p2 * (1 - broker - tax) - (cost_per_unit * (1 + broker) if cost_per_unit else 0)
    margin2 = (net2 / (cost_per_unit * (1 + broker))) if cost_per_unit else None
    gain_24h = min(remaining, bfs_per_day) * p2 * (1 - broker - tax)
    loss_vs_now = (p1 - p2) * remaining
    if margin2 is not None and margin2 < min_margin:
        action = "HOLD"
        why = (f"ved {isk(p2)} blir marginen mot kostpris {margin2 * 100:.1f} % (< {min_margin * 100:.0f} %). "
               f"Ikke følg ned. Muren under deg er {wall_qty} stk (~{days_wall:.1f} d).")
    elif days_wall > 5 and gain_24h > 2 * fee + loss_vs_now * 0.5:
        action = "ENDRE"
        why = (f"senk til {isk(p2)}: gebyr {isk(fee)} ISK, gir opp {isk(loss_vs_now)} ISK i pris, men muren under deg "
               f"({wall_qty} stk) tar ~{days_wall:.1f} d å tømme.")
    elif days_wall <= 5:
        action = "HOLD"
        why = (f"muren under deg ({wall_qty} stk) liftes bort på ~{days_wall:.1f} d. "
               f"Senking ville kostet {isk(fee)} ISK i gebyr + {isk(loss_vs_now)} ISK i pris.")
    else:
        action = "HOLD"
        why = (f"senking til {isk(p2)} koster {isk(fee)} ISK + {isk(loss_vs_now)} ISK i pris og gir ~{isk(gain_24h)} neste 24 t – ikke verdt det. "
               f"Mur {wall_qty} stk (~{days_wall:.1f} d).")
    return dict(action=action, text=why, new_price=p2, fee=round(fee), margin_at_new=margin2,
                wall_qty=wall_qty, days_wall=round(days_wall, 1), gain_24h=round(gain_24h))


def gone_weight(order_price: float, best_price: float, is_buy: bool) -> float:
    """Hvor nær toppen lå ordren da den forsvant? Nær = sannsynligvis fylt."""
    if best_price <= 0:
        return 0.5
    d = (best_price - order_price) / best_price if is_buy else (order_price - best_price) / best_price
    if d <= 0.01:
        return 0.8
    if d <= 0.05:
        return 0.5
    return 0.2


# ── ESI-klient ────────────────────────────────────────────────────────────────
class EsiError(Exception):
    pass


class EsiDown(Exception):
    """Tranquility er nede (downtime)."""


class Esi:
    """Tynn klient rundt requests med alt spec-en krever:
    X-Compatibility-Date, User-Agent, ETag/If-None-Match (.etag.json),
    X-Ratelimit-Remaining (brems < 2000), 429 + Retry-After (maks 3),
    X-ESI-Error-Limit-Remain (< 20 → vent til reset)."""

    def __init__(self, etag_path: Path | None = None):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": USER_AGENT,
            "X-Compatibility-Date": COMPAT_DATE,
            "Accept": "application/json",
        })
        self.etag_path = etag_path or (SNAPSHOT_DIR / ".etag.json")
        self.etags: dict[str, dict] = {}
        if self.etag_path.exists():
            try:
                self.etags = json.loads(self.etag_path.read_text())
            except Exception:
                self.etags = {}
        self.lock = threading.Lock()
        self.ratelimit_remaining: int | None = None
        self.error_limit_remain: int | None = None
        self.calls = 0

    def save_etags(self):
        self.etag_path.parent.mkdir(parents=True, exist_ok=True)
        # behold bare de siste ~5000 for å ikke vokse evig
        if len(self.etags) > 5000:
            keys = sorted(self.etags, key=lambda k: self.etags[k].get("t", 0))[-5000:]
            self.etags = {k: self.etags[k] for k in keys}
        self.etag_path.write_text(json.dumps(self.etags))

    def get(self, path: str, params: dict | None = None, use_etag: bool = True, timeout: int = 30):
        """→ (status, json-eller-None, headers). 304 gir cachet body fra .etag.json."""
        url = ESI + path
        key = url + ("?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items())) if params else "")
        headers = {}
        cached = self.etags.get(key) if use_etag else None
        if cached and cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]

        for attempt in range(4):
            self._throttle()
            try:
                r = self.s.get(url, params=params, headers=headers, timeout=timeout)
            except requests.RequestException as e:
                if attempt == 3:
                    raise EsiError(f"nettverksfeil {path}: {e}")
                time.sleep(2 * (attempt + 1))
                continue
            self.calls += 1
            self._read_limits(r.headers)

            if r.status_code == 304 and cached:
                return 304, cached.get("body"), r.headers
            if r.status_code == 200:
                body = r.json()
                if use_etag and r.headers.get("ETag") and len(r.content) < 2_000_000:
                    with self.lock:
                        self.etags[key] = {"etag": r.headers["ETag"], "body": body, "t": time.time()}
                return 200, body, r.headers
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "10"))
                log(f"429 på {path} – venter {wait}s (forsøk {attempt + 1}/3)")
                time.sleep(min(wait, 120))
                continue
            if r.status_code == 420:                       # feilgrense nådd – vent til reset
                reset = int(r.headers.get("X-ESI-Error-Limit-Reset", "60"))
                log(f"420 på {path} – venter {reset}s")
                time.sleep(reset + 1)
                continue
            if r.status_code in (502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            if r.status_code == 404:
                return 404, None, r.headers
            raise EsiError(f"{r.status_code} på {path}: {r.text[:200]}")
        raise EsiError(f"ga opp på {path} etter 4 forsøk")

    def _read_limits(self, h):
        rl = h.get("X-Ratelimit-Remaining") or h.get("x-ratelimit-remaining")
        if rl is not None:
            try:
                self.ratelimit_remaining = int(rl)
            except ValueError:
                pass
        el = h.get("X-ESI-Error-Limit-Remain")
        if el is not None:
            try:
                self.error_limit_remain = int(el)
                if self.error_limit_remain < 20:
                    reset = int(h.get("X-ESI-Error-Limit-Reset", "60"))
                    log(f"feilgrense lav ({self.error_limit_remain}) – venter {reset}s")
                    time.sleep(reset + 1)
            except ValueError:
                pass

    def _throttle(self):
        if self.ratelimit_remaining is not None and self.ratelimit_remaining < 2000:
            time.sleep(0.5)

    # Hjelpere
    def status_ok(self) -> bool:
        try:
            st, body, _ = self.get("/status/", use_etag=False, timeout=15)
            return st == 200 and body is not None and body.get("players", 0) > 0
        except Exception as e:
            log("status-kall feilet:", e)
            return False


def parse_http_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).astimezone(UTC)
    except Exception:
        return None


# ── Snapshot-filer ────────────────────────────────────────────────────────────
def read_snapshot(name: str) -> dict | None:
    p = SNAPSHOT_DIR / name
    if not p.exists():
        return None
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


def write_snapshot(name: str, data: dict):
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT_DIR / (name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
        json.dump(data, f, separators=(",", ":"))
    tmp.replace(SNAPSHOT_DIR / name)


# ── Postgres ──────────────────────────────────────────────────────────────────
def db():
    """psycopg-tilkobling via SUPABASE_DB_URL (Supavisor session-pooler, IPv4)."""
    import psycopg
    url = os.environ.get("SUPABASE_DB_URL")
    if not url:
        raise SystemExit("SUPABASE_DB_URL mangler i miljøet")
    return psycopg.connect(url, autocommit=False, connect_timeout=20)


def load_profile(conn) -> Profile:
    with conn.cursor() as cur:
        cur.execute("select * from jita.profile where id = 1")
        cols = [d.name for d in cur.description]
        row = cur.fetchone()
    return Profile.from_row(dict(zip(cols, row))) if row else Profile()


def db_size_bytes(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("select pg_database_size(current_database())")
        return int(cur.fetchone()[0])


# ── Discord ───────────────────────────────────────────────────────────────────
_discord_sent = 0


def notify(text: str):
    """Poster til DISCORD_WEBHOOK. Maks 5 per kjøring. Feiler stille."""
    global _discord_sent
    hook = os.environ.get("DISCORD_WEBHOOK")
    log("DISCORD:", text)
    if not hook or _discord_sent >= 5:
        return
    try:
        requests.post(hook, json={"content": text[:1900]}, timeout=10)
        _discord_sent += 1
    except Exception as e:
        log("discord feilet:", e)


# ── robot_runs ────────────────────────────────────────────────────────────────
class RunLog:
    """Samler tall gjennom en jobb og skriver én rad til jita.robot_runs på slutten."""

    def __init__(self, job: str):
        self.job = job
        self.t0 = time.time()
        self.run_at = now_utc()
        self.snapshot_at = None
        self.pages_total = 0
        self.pages_ok = 0
        self.orders_count = 0
        self.ratelimit_remaining = None
        self.ok = True
        self.message = ""

    def finish(self, conn=None, ok: bool | None = None, message: str | None = None):
        if ok is not None:
            self.ok = ok
        if message:
            self.message = (self.message + " | " + message) if self.message else message
        dur = round(time.time() - self.t0, 1)
        size = None
        try:
            own = conn is None
            conn = conn or db()
            size = db_size_bytes(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """insert into jita.robot_runs
                       (run_at, job, snapshot_at, pages_total, pages_ok, orders_count,
                        ratelimit_remaining, duration_s, db_bytes, ok, message)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       on conflict (run_at) do update set ok = excluded.ok, message = excluded.message""",
                    (self.run_at, self.job, self.snapshot_at, self.pages_total, self.pages_ok,
                     self.orders_count, self.ratelimit_remaining, dur, size, self.ok, self.message[:2000]),
                )
            conn.commit()
            if own:
                conn.close()
        except Exception as e:
            log("kunne ikke skrive robot_runs:", e)
        log(f"{self.job}: ok={self.ok} {dur}s sider {self.pages_ok}/{self.pages_total} "
            f"ordrer {self.orders_count} db={round((size or 0) / 1e6, 1)}MB {self.message}")
        if not self.ok:
            notify(f"⚠️ Jita-robot `{self.job}` feilet: {self.message[:300]}")
        if size and size > 350_000_000:
            notify(f"⚠️ Jita: databasen er {round(size / 1e6)} MB (> 350 MB) – rydding trengs")


def fail(runlog: RunLog, msg: str, conn=None, code: int = 1):
    runlog.finish(conn, ok=False, message=msg)
    sys.exit(code)
