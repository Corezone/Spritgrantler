"""Spritgrantler – sammelt Spritpreise der E-Control und liefert Live-Daten + Statistik."""
import asyncio
import json
import logging
import math
import os
import random
import sqlite3
import statistics
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("spritgrantler")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

API = "https://api.e-control.at/sprit/1.0"
FUELS = ("DIE", "SUP")
TZ = ZoneInfo("Europe/Vienna")
BASE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "spritgrantler.db"


# ---------------------------------------------------------------- Konfiguration
def load_config() -> dict:
    cfg = {
        "region": "Österreich",      # "Österreich", "Wien", Bundesland-Name oder Bezirkscodes "901,902"
        "interval_min": 30,          # Abfrageintervall
        "retention_days": 365,       # Löschfrist
        "request_delay_s": 2.5,      # Pause zwischen API-Anfragen (Schutz vor Sperre)
        "center_lat": 48.2082,       # Standardmitte der Karte (Stephansplatz)
        "center_lon": 16.3738,
        "demo": False,               # Demo-Daten statt echter API
    }
    opt = Path("/data/options.json")  # Home-Assistant-App-Optionen
    if opt.exists():
        try:
            cfg.update(json.loads(opt.read_text()))
        except Exception as e:  # noqa: BLE001
            log.warning("options.json nicht lesbar: %s", e)
    for k in list(cfg):
        env = os.environ.get(k.upper())
        if env is not None:
            t = type(cfg[k])
            cfg[k] = env.lower() in ("1", "true", "yes") if t is bool else t(env)
    return cfg


CFG = load_config()
STATE = {"last_round": None, "last_round_obs": 0, "last_error": None, "targets": [], "running": False}


# ---------------------------------------------------------------- Datenbank
def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.execute("PRAGMA auto_vacuum=INCREMENTAL")
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS stations(
            id INTEGER PRIMARY KEY, name TEXT, address TEXT, postal TEXT, city TEXT,
            lat REAL, lon REAL, opening TEXT, updated INTEGER);
        CREATE TABLE IF NOT EXISTS prices(
            id INTEGER PRIMARY KEY, station_id INTEGER, fuel TEXT, price REAL,
            first_seen INTEGER, last_seen INTEGER);
        CREATE INDEX IF NOT EXISTS ix_prices_sf ON prices(station_id, fuel, last_seen);
        CREATE INDEX IF NOT EXISTS ix_prices_ls ON prices(last_seen);
        CREATE TABLE IF NOT EXISTS rounds(
            ts INTEGER, fuel TEXT, n INTEGER, min REAL, p25 REAL, median REAL,
            PRIMARY KEY(ts, fuel));
        """
    )
    con.commit()
    con.close()


def upsert_station(con, s: dict, now: int):
    loc = s.get("location") or {}
    if loc.get("latitude") is None:
        return
    con.execute(
        """INSERT INTO stations(id,name,address,postal,city,lat,lon,opening,updated)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET name=excluded.name, address=excluded.address,
             postal=excluded.postal, city=excluded.city, lat=excluded.lat, lon=excluded.lon,
             opening=excluded.opening, updated=excluded.updated""",
        (s["id"], s.get("name"), loc.get("address"), loc.get("postalCode"), loc.get("city"),
         loc["latitude"], loc["longitude"], json.dumps(s.get("openingHours") or []), now),
    )


def record_price(con, station_id: int, fuel: str, price: float, now: int):
    """Speichert nur Änderungen: gleicher Preis verlängert den bestehenden Eintrag."""
    gap = CFG["interval_min"] * 60 * 3
    row = con.execute(
        "SELECT id, price, last_seen FROM prices WHERE station_id=? AND fuel=? ORDER BY last_seen DESC LIMIT 1",
        (station_id, fuel),
    ).fetchone()
    if row and abs(row["price"] - price) < 1e-6 and now - row["last_seen"] <= gap:
        con.execute("UPDATE prices SET last_seen=? WHERE id=?", (now, row["id"]))
    else:
        con.execute(
            "INSERT INTO prices(station_id,fuel,price,first_seen,last_seen) VALUES(?,?,?,?,?)",
            (station_id, fuel, price, now, now),
        )


def ingest(con, stations: list, fuel: str, now: int) -> dict:
    """Station-Liste der API speichern, gibt {station_id: preis} zurück."""
    seen = {}
    for s in stations or []:
        if not isinstance(s, dict) or "id" not in s:
            continue
        upsert_station(con, s, now)
        for p in s.get("prices") or []:
            if p.get("fuelType") == fuel and p.get("amount"):
                record_price(con, s["id"], fuel, float(p["amount"]), now)
                seen[s["id"]] = float(p["amount"])
    return seen


def pct(vals, q):
    v = sorted(vals)
    if not v:
        return None
    k = (len(v) - 1) * q
    f, c = math.floor(k), math.ceil(k)
    return v[f] if f == c else v[f] + (v[c] - v[f]) * (k - f)


def store_round(con, ts: int, fuel: str, prices: list):
    if not prices:
        return
    con.execute(
        "INSERT OR REPLACE INTO rounds(ts,fuel,n,min,p25,median) VALUES(?,?,?,?,?,?)",
        (ts, fuel, len(prices), min(prices), pct(prices, 0.25), statistics.median(prices)),
    )


def cleanup():
    cut = int(time.time()) - CFG["retention_days"] * 86400
    con = db()
    con.execute("DELETE FROM prices WHERE last_seen < ?", (cut,))
    con.execute("DELETE FROM rounds WHERE ts < ?", (cut,))
    con.execute("DELETE FROM stations WHERE updated < ? AND id NOT IN (SELECT DISTINCT station_id FROM prices)", (cut,))
    con.commit()
    con.execute("PRAGMA incremental_vacuum")
    con.close()


# ---------------------------------------------------------------- E-Control-API
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=20, headers={"User-Agent": "Spritgrantler/1.0 (privat, Home Assistant)"})
    return _client


async def api_get(path: str, **params):
    r = await client().get(f"{API}{path}", params=params)
    r.raise_for_status()
    return r.json()


async def resolve_targets() -> list:
    """Liefert die Abfrageziele: Bezirke (PB) oder als Fallback ein Punkteraster."""
    region = str(CFG["region"]).strip()
    if region and all(part.strip().isdigit() for part in region.split(",")):
        return [("PB", int(c)) for c in region.split(",")]
    try:
        regions = await api_get("/regions", includeCities="false")
        out = []
        for bl in regions:
            if region.lower() in ("österreich", "oesterreich", "austria", "at") or bl.get("name", "").lower() == region.lower():
                out += [("PB", sub["code"]) for sub in bl.get("subRegions", []) if sub.get("type") == "PB"]
        if out:
            return out
        log.warning("Region '%s' nicht gefunden – nutze Punkteraster", region)
    except Exception as e:  # noqa: BLE001
        log.warning("Regionen nicht abrufbar (%s) – nutze Punkteraster", e)
    # Fallback: Raster mit 2,5 km Abstand, 9 km um die Mitte
    lat0, lon0, pts = CFG["center_lat"], CFG["center_lon"], []
    step_lat, step_lon = 2.5 / 111.0, 2.5 / (111.0 * math.cos(math.radians(lat0)))
    for i in range(-4, 5):
        for j in range(-4, 5):
            la, lo = lat0 + i * step_lat, lon0 + j * step_lon
            if haversine(lat0, lon0, la, lo) <= 9.5:
                pts.append(("PT", (round(la, 5), round(lo, 5))))
    return pts


async def fetch_target(t, fuel):
    kind, val = t
    if kind == "PB":
        return await api_get("/search/gas-stations/by-region", code=val, type="PB", fuelType=fuel, includeClosed="false")
    return await api_get("/search/gas-stations/by-address", latitude=val[0], longitude=val[1], fuelType=fuel, includeClosed="false")


async def collect_round():
    if not STATE["targets"]:
        STATE["targets"] = await resolve_targets()
        log.info("%d Abfrageziele", len(STATE["targets"]))
    ts = int(time.time())
    total = 0
    for fuel in FUELS:
        seen: dict = {}
        for t in STATE["targets"]:
            try:
                data = await fetch_target(t, fuel)
                con = db()
                seen.update(ingest(con, data, fuel, ts))
                con.commit()
                con.close()
                STATE["last_error"] = None
            except Exception as e:  # noqa: BLE001
                STATE["last_error"] = f"{t}: {e}"
                log.warning("Abfrage fehlgeschlagen %s %s: %s", t, fuel, e)
            await asyncio.sleep(CFG["request_delay_s"])
        con = db()
        store_round(con, ts, fuel, list(seen.values()))
        con.commit()
        con.close()
        total += len(seen)
    STATE["last_round"], STATE["last_round_obs"] = ts, total
    log.info("Runde fertig: %d Preise", total)


async def collector_loop():
    STATE["running"] = True
    last_clean = 0
    while True:
        start = time.time()
        try:
            if CFG["demo"]:
                demo_tick()
            else:
                await collect_round()
            if start - last_clean > 6 * 3600:
                cleanup()
                last_clean = start
        except Exception as e:  # noqa: BLE001
            STATE["last_error"] = str(e)
            log.exception("Sammelrunde fehlgeschlagen")
        await asyncio.sleep(max(30, CFG["interval_min"] * 60 - (time.time() - start)))


# ---------------------------------------------------------------- Demo-Daten
BRANDS = ["OMV", "BP", "Shell", "ENI", "Avanti", "Turmöl", "JET", "Diskont", "SB-Tankstelle", "IQ", "Genol", "Lagerhaus"]
STREETS = ["Triester Straße", "Wagramer Straße", "Brünner Straße", "Linzer Straße", "Altmannsdorfer Straße", "Handelskai",
           "Gürtel", "Erdberger Lände", "Laxenburger Straße", "Hernalser Hauptstraße", "Simmeringer Hauptstraße",
           "Prager Straße", "Hadikgasse", "Donaufelder Straße", "Favoritenstraße", "Leopoldauer Straße"]


def demo_price(base: float, offset: float, t: datetime, seed: int) -> float:
    """Preis mit realistischem Tagesmuster: Sprung um 12 Uhr, Abschmelzen bis abends, Wochenende günstiger."""
    h = t.hour + t.minute / 60
    rnd = random.Random(seed * 1000 + t.toordinal())
    jump = 0.07 + rnd.random() * 0.05
    if h < 12:
        day = 0.0 - (0.012 if h >= 9 else 0.0)
    elif h < 12.25:
        day = jump * 0.6
    else:
        day = jump - min(jump * 0.85, (h - 12) * jump / 7.5)
        if h >= 21:
            day += 0.01
    weekend = -0.015 if t.weekday() >= 5 else 0.0
    trend = math.sin(t.toordinal() / 6.0) * 0.03
    return round(base + offset + day + weekend + trend + rnd.random() * 0.004, 3)


def demo_stations():
    rnd = random.Random(42)
    out = []
    for i in range(110):
        r = math.sqrt(rnd.random()) * 9.5
        a = rnd.random() * 2 * math.pi
        la = CFG["center_lat"] + r * math.cos(a) / 111
        lo = CFG["center_lon"] + r * math.sin(a) / (111 * math.cos(math.radians(CFG["center_lat"])))
        out.append({
            "id": 900000 + i, "name": f"{rnd.choice(BRANDS)} {rnd.choice(STREETS).split()[0]}",
            "location": {"address": f"{rnd.choice(STREETS)} {rnd.randint(1, 240)}", "postalCode": f"1{rnd.randint(1, 23):02d}0",
                         "city": "Wien", "latitude": la, "longitude": lo},
            "openingHours": [{"day": d, "label": d, "from": "06:00", "to": "22:00"} for d in ("MO", "DI", "MI", "DO", "FR", "SA", "SO")],
            "off": rnd.uniform(-0.06, 0.09),
        })
    return out


DEMO = demo_stations()


def demo_snapshot(t: datetime):
    snap = {"DIE": [], "SUP": []}
    for s in DEMO:
        for fuel, base in (("DIE", 1.62), ("SUP", 1.58)):
            snap[fuel].append((s, demo_price(base, s["off"], t, s["id"] + (7 if fuel == "SUP" else 0))))
    return snap


def demo_ingest(con, t: datetime):
    ts = int(t.timestamp())
    for fuel, rows in demo_snapshot(t).items():
        # Wie die echte API: nur die günstigsten Stationen haben Preise
        rows.sort(key=lambda x: x[1])
        keep = rows[: int(len(rows) * 0.55)]
        for s, p in keep:
            upsert_station(con, s, ts)
            record_price(con, s["id"], fuel, p, ts)
        store_round(con, ts, fuel, [p for _, p in keep])


def demo_seed():
    con = db()
    if con.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]:
        con.close()
        return
    days = min(CFG["retention_days"], 120)
    log.info("Erzeuge Demo-Daten (%d Tage)…", days)
    now = datetime.now(TZ).replace(second=0, microsecond=0)
    t = now - timedelta(days=days)
    while t <= now:
        demo_ingest(con, t)
        t += timedelta(minutes=CFG["interval_min"])
    con.commit()
    con.close()


def demo_tick():
    con = db()
    demo_ingest(con, datetime.now(TZ))
    con.commit()
    con.close()
    STATE["last_round"] = int(time.time())


# ---------------------------------------------------------------- Hilfen
def haversine(a, b, c, d):
    r = 6371.0
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = p2 - p1, math.radians(d - b)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


_live_cache: dict = {}


async def live_refresh(lat: float, lon: float, fuel: str):
    """Holt die nächstgelegenen Tankstellen direkt bei der E-Control (max. alle 5 Min. je Ort)."""
    if CFG["demo"]:
        return
    key = (round(lat, 2), round(lon, 2), fuel)
    if time.time() - _live_cache.get(key, 0) < 300:
        return
    _live_cache[key] = time.time()
    try:
        data = await api_get("/search/gas-stations/by-address", latitude=lat, longitude=lon, fuelType=fuel, includeClosed="false")
        con = db()
        ingest(con, data, fuel, int(time.time()))
        con.commit()
        con.close()
    except Exception as e:  # noqa: BLE001
        log.warning("Live-Abfrage fehlgeschlagen: %s", e)


def local(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, TZ)


def check_fuel(fuel: str) -> str:
    fuel = fuel.upper()
    if fuel not in FUELS:
        raise HTTPException(400, "fuel muss DIE oder SUP sein")
    return fuel


def hourly_profile(con, fuel: str, days: int):
    """Abweichung des Medians je Stunde vom jeweiligen Tagesschnitt, in Cent."""
    since = int(time.time()) - days * 86400
    rows = con.execute("SELECT ts, median, min FROM rounds WHERE fuel=? AND ts>=? ORDER BY ts", (fuel, since)).fetchall()
    by_day: dict = {}
    for r in rows:
        t = local(r["ts"])
        by_day.setdefault(t.date(), []).append((t, r["median"], r["min"]))
    hours = [[] for _ in range(24)]
    heat = [[[] for _ in range(24)] for _ in range(7)]
    full = {d: v for d, v in by_day.items() if len(v) >= 24}  # unvollständige Tage auslassen
    day_mean = {d: sum(v[1] for v in vals) / len(vals) for d, vals in full.items()}
    for d, vals in full.items():
        mean = day_mean[d]
        # Wochen-Basis: Mittel der Tage d-3 … d+3, damit Wochentags-Effekte sichtbar bleiben
        near = [day_mean[d + timedelta(days=k)] for k in range(-3, 4) if d + timedelta(days=k) in day_mean]
        base = sum(near) / len(near) if len(near) >= 4 else None
        for t, med, _ in vals:
            hours[t.hour].append((med - mean) * 100)
            if base is not None:
                heat[t.weekday()][t.hour].append((med - base) * 100)
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None  # noqa: E731
    full_days = len(full)
    return [avg(h) for h in hours], [[avg(c) for c in row] for row in heat], full_days


# ---------------------------------------------------------------- Web-App
@asynccontextmanager
async def lifespan(_app):
    init_db()
    if CFG["demo"]:
        demo_seed()
    task = asyncio.create_task(collector_loop())
    yield
    task.cancel()


app = FastAPI(title="Spritgrantler", lifespan=lifespan)


@app.get("/api/config")
def api_config():
    return {"center": [CFG["center_lat"], CFG["center_lon"]], "region": CFG["region"], "demo": CFG["demo"],
            "interval_min": CFG["interval_min"], "retention_days": CFG["retention_days"]}


@app.get("/api/stations")
async def api_stations(lat: float, lon: float, radius_km: float = Query(5, ge=0.5, le=60), fuel: str = "DIE"):
    fuel = check_fuel(fuel)
    await live_refresh(lat, lon, fuel)
    now = int(time.time())
    fresh = CFG["interval_min"] * 60 * 4
    con = db()
    rows = con.execute(
        """SELECT s.*, p.price, p.first_seen, p.last_seen FROM stations s
           JOIN prices p ON p.id = (SELECT id FROM prices WHERE station_id=s.id AND fuel=? ORDER BY last_seen DESC LIMIT 1)""",
        (fuel,),
    ).fetchall()
    out = []
    for r in rows:
        dist = haversine(lat, lon, r["lat"], r["lon"])
        if dist > radius_km:
            continue
        out.append({"id": r["id"], "name": r["name"], "address": r["address"], "postal": r["postal"], "city": r["city"],
                    "lat": r["lat"], "lon": r["lon"], "dist_km": round(dist, 2), "price": r["price"],
                    "since": r["first_seen"], "seen": r["last_seen"], "fresh": now - r["last_seen"] <= fresh,
                    "opening": json.loads(r["opening"] or "[]")})
    con.close()
    out.sort(key=lambda x: (not x["fresh"], x["price"]))
    return {"fuel": fuel, "now": now, "stations": out}


@app.get("/api/station/{sid}")
def api_station(sid: int, fuel: str = "DIE"):
    fuel = check_fuel(fuel)
    con = db()
    s = con.execute("SELECT * FROM stations WHERE id=?", (sid,)).fetchone()
    if not s:
        raise HTTPException(404, "Tankstelle unbekannt")
    hist = [dict(r) for r in con.execute(
        "SELECT price, first_seen, last_seen FROM prices WHERE station_id=? AND fuel=? ORDER BY first_seen", (sid, fuel))]
    con.close()
    return {"id": sid, "name": s["name"], "history": hist}


@app.get("/api/summary")
def api_summary(fuel: str = "DIE"):
    fuel = check_fuel(fuel)
    con = db()
    last = con.execute("SELECT * FROM rounds WHERE fuel=? ORDER BY ts DESC LIMIT 1", (fuel,)).fetchone()
    week = con.execute("SELECT AVG(median) a, AVG(min) m FROM rounds WHERE fuel=? AND ts>=?",
                       (fuel, int(time.time()) - 7 * 86400)).fetchone()
    day_ago = con.execute("SELECT median FROM rounds WHERE fuel=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                          (fuel, int(time.time()) - 86400)).fetchone()
    hours, _, full_days = hourly_profile(con, fuel, 30)
    con.close()
    now = datetime.now(TZ)
    h = now.hour
    rec = recommend(hours, full_days, now)
    return {
        "fuel": fuel, "round_ts": last["ts"] if last else None,
        "min": last["min"] if last else None, "median": last["median"] if last else None,
        "n": last["n"] if last else 0,
        "week_median": round(week["a"], 3) if week and week["a"] else None,
        "week_min": round(week["m"], 3) if week and week["m"] else None,
        "median_24h_ago": day_ago["median"] if day_ago else None,
        "full_days": full_days, "hour": h, "recommendation": rec,
    }


def de(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def recommend(hours, full_days, now: datetime):
    h = now.hour
    best = [i for i, v in sorted(enumerate(hours), key=lambda x: (x[1] is None, x[1] or 0)) if hours[i] is not None][:3]
    if full_days < 3 or hours[h] is None:
        # Regel-basiert, solange zu wenig eigene Daten vorliegen
        if h < 12:
            return {"level": "good", "title": "Jetzt tanken", "text": "Vor 12 Uhr ist Sprit meist am günstigsten. Um 12 Uhr dürfen Tankstellen erhöhen.",
                    "best_hours": [9, 10, 11], "learning": True}
        if h < 17:
            return {"level": "wait", "title": "Lieber warten", "text": "Nach der 12-Uhr-Erhöhung sinken die Preise bis zum Abend wieder.",
                    "best_hours": [9, 10, 11], "learning": True}
        return {"level": "ok", "title": "Okay zum Tanken", "text": "Abends ist der Mittagsaufschlag meist abgebaut. Am günstigsten bleibt der Vormittag.",
                "best_hours": [9, 10, 11], "learning": True}
    cur = hours[h]
    later = [(i, hours[i]) for i in range(h + 1, 24) if hours[i] is not None]
    cheaper_later = min(later, key=lambda x: x[1]) if later else None
    if 11 <= h < 12:
        return {"level": "good", "title": "Jetzt tanken", "text": "Letzte Chance vor der 12-Uhr-Erhöhung.", "best_hours": best, "learning": False}
    if cheaper_later and cur - cheaper_later[1] >= 1.0:
        return {"level": "wait", "title": f"Warte bis {cheaper_later[0]} Uhr",
                "text": f"Dann im Schnitt {de(cur - cheaper_later[1])} Cent pro Liter günstiger als jetzt.",
                "best_hours": best, "learning": False}
    if cur <= 0:
        return {"level": "good", "title": "Jetzt tanken", "text": f"Gerade {de(abs(cur))} Cent unter dem Tagesschnitt.", "best_hours": best, "learning": False}
    return {"level": "ok", "title": "Okay zum Tanken", "text": f"{de(cur)} Cent über dem Tagesschnitt. Heute wird es kaum noch billiger.",
            "best_hours": best, "learning": False}


@app.get("/api/stats")
def api_stats(fuel: str = "DIE", days: int = Query(30, ge=1, le=365)):
    fuel = check_fuel(fuel)
    con = db()
    hours, heat, full_days = hourly_profile(con, fuel, days)
    since = int(time.time()) - days * 86400
    rows = con.execute("SELECT ts, min, median FROM rounds WHERE fuel=? AND ts>=? ORDER BY ts", (fuel, since)).fetchall()
    weekly = days > 62
    daily: dict = {}
    for r in rows:
        dt = local(r["ts"]).date()
        if weekly:
            dt = dt - timedelta(days=dt.weekday())
        d = dt.isoformat()
        e = daily.setdefault(d, {"min": 9, "med": []})
        e["min"] = min(e["min"], r["min"])
        e["med"].append(r["median"])
    trend = [{"day": d, "min": round(v["min"], 3), "median": round(statistics.median(v["med"]), 3)} for d, v in daily.items()]
    since48 = int(time.time()) - 48 * 3600
    intraday = [{"ts": r["ts"], "min": r["min"], "median": r["median"]} for r in rows if r["ts"] >= since48]
    con.close()
    weekday = []
    for row in heat:
        vals = [v for v in row if v is not None]
        weekday.append(round(sum(vals) / len(vals), 2) if vals else None)
    period = None
    if rows:
        meds = [r["median"] for r in rows]
        period = {"min": min(r["min"] for r in rows), "median_avg": round(sum(meds) / len(meds), 3),
                  "median_max": max(meds), "median_min": min(meds)}
    return {"fuel": fuel, "days": days, "weekly": weekly, "hours": hours, "heat": heat, "weekday": weekday,
            "full_days": full_days, "trend": trend, "intraday": intraday, "period": period}


@app.get("/api/info")
def api_info():
    con = db()
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("stations", "prices", "rounds")}
    first = con.execute("SELECT MIN(ts) FROM rounds").fetchone()[0]
    con.close()
    size = sum(p.stat().st_size for p in DATA_DIR.glob("spritgrantler.db*"))
    return {"db_bytes": size, "counts": counts, "first_round": first, "last_round": STATE["last_round"],
            "last_error": STATE["last_error"], "targets": len(STATE["targets"]), **api_config()}


# ---------------------------------------------------------------- Home Assistant + Adresssuche
SUP_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
HA_API = os.environ.get("HA_API", "http://supervisor/core/api")
_geo_cache: dict = {}
_geo_last = [0.0]


@app.get("/api/ha/locations")
async def api_ha_locations():
    """Personen und Zuhause aus Home Assistant (nur als HA-App verfügbar)."""
    if not SUP_TOKEN:
        return {"available": False, "locations": []}
    try:
        r = await client().get(f"{HA_API}/states", headers={"Authorization": f"Bearer {SUP_TOKEN}"})
        r.raise_for_status()
        out = []
        for s in r.json():
            eid, a = s.get("entity_id", ""), s.get("attributes", {})
            if a.get("latitude") is None or a.get("longitude") is None:
                continue
            if eid.startswith("person.") or eid == "zone.home":
                name = "Zuhause" if eid == "zone.home" else a.get("friendly_name", eid)
                out.append({"id": eid, "name": name, "lat": a["latitude"], "lon": a["longitude"],
                            "state": s.get("state"), "home": eid == "zone.home"})
        out.sort(key=lambda x: (not x["home"], x["name"]))
        return {"available": True, "locations": out}
    except Exception as e:  # noqa: BLE001
        log.warning("Home-Assistant-API nicht erreichbar: %s", e)
        return {"available": False, "locations": [], "error": str(e)}


@app.get("/api/geocode")
async def api_geocode(q: str = Query(..., min_length=3, max_length=200)):
    """Adresssuche über OpenStreetMap Nominatim, auf Österreich beschränkt."""
    key = q.strip().lower()
    if key in _geo_cache:
        return _geo_cache[key]
    wait = 1.1 - (time.time() - _geo_last[0])  # Nominatim: max. 1 Anfrage pro Sekunde
    if wait > 0:
        await asyncio.sleep(wait)
    _geo_last[0] = time.time()
    try:
        r = await client().get("https://nominatim.openstreetmap.org/search",
                               params={"q": q, "format": "jsonv2", "countrycodes": "at", "limit": 6,
                                       "accept-language": "de", "addressdetails": 0})
        r.raise_for_status()
        res = {"results": [{"name": x.get("display_name", ""), "lat": float(x["lat"]), "lon": float(x["lon"])} for x in r.json()]}
    except Exception as e:  # noqa: BLE001
        log.warning("Adresssuche fehlgeschlagen: %s", e)
        raise HTTPException(502, "Adresssuche nicht erreichbar")
    _geo_cache[key] = res
    return res


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
