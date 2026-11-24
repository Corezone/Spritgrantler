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
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field
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
        CREATE TABLE IF NOT EXISTS fillups(
            id INTEGER PRIMARY KEY, user_id TEXT, user_name TEXT, ts INTEGER, station_id INTEGER,
            station_name TEXT, lat REAL, lon REAL, fuel TEXT, liters REAL, price REAL,
            market_median REAL, market_min REAL, local_min REAL, local_min_name TEXT,
            day_best REAL, day_best_ts INTEGER, day_final INTEGER DEFAULT 0, created INTEGER);
        CREATE INDEX IF NOT EXISTS ix_fill_user ON fillups(user_id, ts);
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


async def ha_states() -> list:
    r = await client().get(f"{HA_API}/states", headers={"Authorization": f"Bearer {SUP_TOKEN}"})
    r.raise_for_status()
    return r.json()


@app.get("/api/ha/me")
async def api_ha_me(request: Request):
    """Standort der Person, die gerade in Home Assistant angemeldet ist (über Ingress-Header)."""
    uid = request.headers.get("X-Remote-User-Id")
    uname = request.headers.get("X-Remote-User-Display-Name") or request.headers.get("X-Remote-User-Name")
    if not uid or not SUP_TOKEN:
        return {"found": False, "user": uname, "reason": "no_ingress"}
    try:
        for s in await ha_states():
            a = s.get("attributes", {})
            if s.get("entity_id", "").startswith("person.") and a.get("user_id") == uid:
                has = a.get("latitude") is not None and a.get("longitude") is not None
                return {"found": True, "has_location": has, "entity_id": s["entity_id"],
                        "name": a.get("friendly_name") or uname, "state": s.get("state"),
                        "lat": a.get("latitude"), "lon": a.get("longitude"), "source": a.get("source"),
                        "accuracy": a.get("gps_accuracy"), "updated": s.get("last_updated")}
        return {"found": False, "user": uname, "reason": "no_person"}
    except Exception as e:  # noqa: BLE001
        log.warning("Home-Assistant-API nicht erreichbar: %s", e)
        return {"found": False, "user": uname, "reason": "api_error"}


@app.get("/api/ha/locations")
async def api_ha_locations():
    """Personen und Zuhause aus Home Assistant (nur als HA-App verfügbar)."""
    if not SUP_TOKEN:
        return {"available": False, "locations": []}
    try:
        out = []
        for s in await ha_states():
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


# ---------------------------------------------------------------- Tankbuch
class FillupIn(BaseModel):
    station_id: int | None = None
    station_name: str = Field(..., min_length=1, max_length=120)
    fuel: str = "DIE"
    liters: float = Field(..., gt=0.5, le=300)
    price: float = Field(..., gt=0.5, lt=5)
    ts: int | None = None
    lat: float | None = None
    lon: float | None = None


def who(request: Request):
    uid = request.headers.get("X-Remote-User-Id") or "lokal"
    name = request.headers.get("X-Remote-User-Display-Name") or request.headers.get("X-Remote-User-Name") or "Ich"
    return uid, name


def market_at(con, fuel: str, ts: int):
    r = con.execute("SELECT median, min FROM rounds WHERE fuel=? AND ts<=? AND ts>=? ORDER BY ts DESC LIMIT 1",
                    (fuel, ts, ts - 3 * 3600)).fetchone()
    if not r:
        r = con.execute("SELECT median, min FROM rounds WHERE fuel=? ORDER BY ABS(ts-?) LIMIT 1", (fuel, ts)).fetchone()
    return (r["median"], r["min"]) if r else (None, None)


def local_min_at(con, fuel: str, ts: int, lat, lon, km=5.0):
    if lat is None or lon is None:
        return None, None
    slack = CFG["interval_min"] * 60
    rows = con.execute(
        """SELECT p.price, s.name, s.lat, s.lon FROM prices p JOIN stations s ON s.id=p.station_id
           WHERE p.fuel=? AND p.first_seen<=? AND p.last_seen>=?""", (fuel, ts, ts - slack)).fetchall()
    best = None
    for r in rows:
        if haversine(lat, lon, r["lat"], r["lon"]) <= km and (best is None or r["price"] < best[0]):
            best = (r["price"], r["name"])
    return best if best else (None, None)


def day_best(con, station_id, fuel: str, ts: int):
    """Günstigster Preis dieser Tankstelle am selben Kalendertag."""
    if not station_id:
        return None, None
    d = local(ts)
    start = int(datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp())
    end = start + 86400
    r = con.execute("""SELECT price, first_seen FROM prices WHERE station_id=? AND fuel=? AND first_seen<? AND last_seen>=?
                       ORDER BY price ASC, first_seen ASC LIMIT 1""", (station_id, fuel, end, start)).fetchone()
    return (r["price"], max(r["first_seen"], start)) if r else (None, None)


def fill_view(con, f) -> dict:
    f = dict(f)
    if not f["day_final"]:
        db_, dts = day_best(con, f["station_id"], f["fuel"], f["ts"])
        if db_ is not None:
            f["day_best"], f["day_best_ts"] = db_, dts
        d = local(f["ts"])
        if time.time() > datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp() + 86400 + 3600:
            con.execute("UPDATE fillups SET day_best=?, day_best_ts=?, day_final=1 WHERE id=?", (f["day_best"], f["day_best_ts"], f["id"]))
    L = f["liters"]
    f["total"] = round(L * f["price"], 2)
    f["vs_median"] = round((f["market_median"] - f["price"]) * L, 2) if f["market_median"] else None
    f["vs_local"] = round((f["price"] - f["local_min"]) * L, 2) if f["local_min"] else None
    f["vs_day"] = round((f["price"] - f["day_best"]) * L, 2) if f["day_best"] else None
    return f


@app.post("/api/fillups")
def api_fill_add(body: FillupIn, request: Request):
    fuel = check_fuel(body.fuel)
    uid, uname = who(request)
    ts = body.ts or int(time.time())
    if ts > time.time() + 300:
        raise HTTPException(400, "Zeitpunkt liegt in der Zukunft")
    con = db()
    lat, lon = body.lat, body.lon
    if body.station_id:
        s = con.execute("SELECT lat, lon FROM stations WHERE id=?", (body.station_id,)).fetchone()
        if s:
            lat, lon = s["lat"], s["lon"]
    med, mn = market_at(con, fuel, ts)
    lmin, lname = local_min_at(con, fuel, ts, lat, lon)
    cur = con.execute(
        """INSERT INTO fillups(user_id,user_name,ts,station_id,station_name,lat,lon,fuel,liters,price,
           market_median,market_min,local_min,local_min_name,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uid, uname, ts, body.station_id, body.station_name.strip(), lat, lon, fuel, body.liters, body.price,
         med, mn, lmin, lname, int(time.time())))
    fid = cur.lastrowid
    f = fill_view(con, con.execute("SELECT * FROM fillups WHERE id=?", (fid,)).fetchone())
    con.commit()
    con.close()
    return f


@app.get("/api/fillups")
def api_fill_list(request: Request):
    uid, uname = who(request)
    con = db()
    items = [fill_view(con, f) for f in con.execute("SELECT * FROM fillups WHERE user_id=? ORDER BY ts DESC", (uid,))]
    con.commit()
    con.close()
    s = lambda key: round(sum(i[key] for i in items if i[key] is not None), 2)  # noqa: E731
    liters = round(sum(i["liters"] for i in items), 1)
    spent = round(sum(i["total"] for i in items), 2)
    with_med = [i for i in items if i["market_median"]]
    summary = {
        "count": len(items), "liters": liters, "spent": spent,
        "avg_price": round(spent / liters, 3) if liters else None,
        "avg_market": round(sum(i["market_median"] * i["liters"] for i in with_med) / sum(i["liters"] for i in with_med), 3) if with_med else None,
        "saved_vs_median": s("vs_median"), "missed_local": s("vs_local"), "missed_day": s("vs_day"),
    }
    monthly: dict = {}
    for i in items:
        m = local(i["ts"]).strftime("%Y-%m")
        e = monthly.setdefault(m, {"month": m, "saved": 0.0, "liters": 0.0, "spent": 0.0})
        e["saved"] += i["vs_median"] or 0
        e["liters"] += i["liters"]
        e["spent"] += i["total"]
    months = [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in e.items()} for e in sorted(monthly.values(), key=lambda x: x["month"])]
    return {"user": uname, "items": items, "summary": summary, "monthly": months}


@app.delete("/api/fillups/{fid}")
def api_fill_delete(fid: int, request: Request):
    uid, _ = who(request)
    con = db()
    n = con.execute("DELETE FROM fillups WHERE id=? AND user_id=?", (fid, uid)).rowcount
    con.commit()
    con.close()
    if not n:
        raise HTTPException(404, "Eintrag nicht gefunden")
    return {"deleted": fid}


# ---------------------------------------------------------------- Empfehlungen
WD_NAMES = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def de1(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def de2(x: float) -> str:
    return f"{x:.2f}".replace(".", ",")


def when_label(dt: datetime, now: datetime) -> str:
    days = (dt.date() - now.date()).days
    day = "heute" if days == 0 else "morgen" if days == 1 else "übermorgen" if days == 2 else WD_NAMES[dt.weekday()]
    return f"{day} {dt.hour}–{dt.hour + 1} Uhr"


@app.get("/api/recommendations")
def api_recommendations(request: Request, fuel: str = "DIE", lat: float | None = None, lon: float | None = None,
                        radius_km: float = Query(5, ge=0.5, le=60)):
    fuel = check_fuel(fuel)
    con = db()
    recs = []
    hours, _, full30 = hourly_profile(con, fuel, 30)
    _, heat, full90 = hourly_profile(con, fuel, 90)
    now = datetime.now(TZ)

    # 1) Nächstes günstiges Zeitfenster (Wochentag × Stunde ggü. Wochenschnitt)
    if full90 >= 7:
        cand = []
        for k in range(1, 7 * 24):
            dt = (now + timedelta(hours=k)).replace(minute=0, second=0, microsecond=0)
            v = heat[dt.weekday()][dt.hour]
            if v is not None:
                cand.append((v, dt))
        cur = heat[now.weekday()][now.hour]
        if cand:
            best48 = min((c for c in cand if c[1] <= now + timedelta(hours=48)), default=None, key=lambda c: c[0])
            best7 = min(cand, key=lambda c: c[0])
            if best48:
                diff = (cur - best48[0]) if cur is not None else None
                txt = f"Im Schnitt {de1(abs(best48[0]))} ct {'unter' if best48[0] < 0 else 'über'} dem Wochenschnitt"
                if diff is not None and diff > 0.3:
                    txt += f", {de1(diff)} ct/l günstiger als jetzt"
                recs.append({"kind": "time", "level": "good", "title": f"Nächstes gutes Zeitfenster: {when_label(best48[1], now)}", "text": txt + "."})
            if best7 and best7[1] > now + timedelta(hours=48) and best48 and best7[0] < best48[0] - 0.5:
                recs.append({"kind": "time", "level": "info", "title": f"Diese Woche am günstigsten: {when_label(best7[1], now)}",
                             "text": f"Noch {de1(best48[0] - best7[0])} ct/l weniger als das nächste Zeitfenster. Lohnt sich, wenn der Tank so lange reicht."})
    elif full30 >= 3:
        best = min((i for i, v in enumerate(hours) if v is not None), key=lambda i: hours[i])
        recs.append({"kind": "time", "level": "good", "title": f"Am günstigsten meist {best}–{best + 1} Uhr",
                     "text": f"Im Schnitt {de1(abs(hours[best]))} ct unter dem Tagesschnitt. Für Wochentags-Tipps brauche ich noch {max(0, 7 - full90)} Tage Daten."})
    else:
        recs.append({"kind": "time", "level": "info", "title": "Vormittags vor 12 Uhr tanken",
                     "text": "Um 12 Uhr dürfen Tankstellen erhöhen. Eigene Auswertungen gibt es nach ein paar Tagen Sammeln."})

    # 2) Preistendenz
    days = {}
    for r in con.execute("SELECT ts, median FROM rounds WHERE fuel=? AND ts>=?", (fuel, int(time.time()) - 8 * 86400)):
        days.setdefault(local(r["ts"]).date(), []).append(r["median"])
    dm = [statistics.median(v) for d, v in sorted(days.items()) if len(v) >= 12]
    if len(dm) >= 4:
        last3 = dm[-3:]
        slope = (last3[-1] - last3[0]) * 100 / 2  # ct pro Tag
        wk = sum(dm[:-1]) / len(dm[:-1])
        vs = (dm[-1] - wk) * 100
        if slope > 0.4:
            recs.append({"kind": "trend", "level": "warn", "title": "Preise steigen",
                         "text": f"Seit drei Tagen im Schnitt +{de1(slope)} ct pro Tag. Wenn du ohnehin tanken musst: lieber jetzt voll tanken."})
        elif slope < -0.4:
            recs.append({"kind": "trend", "level": "good", "title": "Preise fallen",
                         "text": f"Seit drei Tagen im Schnitt {de1(slope)} ct pro Tag. Nur so viel tanken wie nötig und in ein paar Tagen voll machen."})
        else:
            recs.append({"kind": "trend", "level": "info", "title": "Preise stabil",
                         "text": f"Heute {de1(abs(vs))} ct {'über' if vs > 0 else 'unter'} dem Schnitt der letzten Woche. Kein Grund zur Eile."})

    # 3) Stammtankstellen im Umkreis: im Schnitt am weitesten unter dem Median
    if lat is not None and lon is not None:
        since = int(time.time()) - 30 * 86400
        rounds = [(r["ts"], r["median"]) for r in con.execute("SELECT ts, median FROM rounds WHERE fuel=? AND ts>=? ORDER BY ts", (fuel, since))]
        if rounds:
            import bisect
            rts = [r[0] for r in rounds]
            agg: dict = {}
            for r in con.execute("""SELECT p.station_id, p.price, p.first_seen, p.last_seen, s.name, s.lat, s.lon
                                    FROM prices p JOIN stations s ON s.id=p.station_id WHERE p.fuel=? AND p.last_seen>=?""", (fuel, since)):
                if haversine(lat, lon, r["lat"], r["lon"]) > radius_km:
                    continue
                i = min(len(rts) - 1, bisect.bisect_left(rts, (r["first_seen"] + r["last_seen"]) // 2))
                dur = max(r["last_seen"] - r["first_seen"], CFG["interval_min"] * 60)
                e = agg.setdefault(r["station_id"], {"name": r["name"], "w": 0.0, "dev": 0.0, "dist": haversine(lat, lon, r["lat"], r["lon"])})
                e["w"] += dur
                e["dev"] += (r["price"] - rounds[i][1]) * 100 * dur
            ranked = sorted(((v["dev"] / v["w"], v) for v in agg.values() if v["w"] >= 48 * 3600), key=lambda x: x[0])[:3]
            if ranked:
                names = "; ".join(f"{v['name']} ({de1(abs(d))} ct {'unter' if d < 0 else 'über'} Ø, {de1(v['dist'])} km)" for d, v in ranked)
                recs.append({"kind": "station", "level": "good", "title": f"Verlässlich günstig im Umkreis: {ranked[0][1]['name']}",
                             "text": f"Über die letzten 30 Tage gerechnet: {names}.", "stations": [v["name"] for _, v in ranked]})

    # 4) Persönlich aus dem Tankbuch
    uid = request.headers.get("X-Remote-User-Id") or "lokal"
    fills = con.execute("SELECT ts, liters FROM fillups WHERE user_id=? AND fuel=? ORDER BY ts DESC LIMIT 30", (uid, fuel)).fetchall()
    valid_h = [i for i, v in enumerate(hours) if v is not None]
    if len(fills) >= 3 and valid_h:
        hrs = sorted(local(f["ts"]).hour for f in fills)
        typ = hrs[len(hrs) // 2]
        avg_l = sum(f["liters"] for f in fills) / len(fills)
        best = min(valid_h, key=lambda i: hours[i])
        if hours[typ] is not None and hours[typ] - hours[best] > 0.5:
            eur = (hours[typ] - hours[best]) / 100 * avg_l
            recs.append({"kind": "personal", "level": "warn", "title": f"Du tankst meist um {typ} Uhr",
                         "text": f"Um {best} Uhr wäre es im Schnitt {de1(hours[typ] - hours[best])} ct/l günstiger. Bei deinen üblichen {round(avg_l)} l sind das {de2(eur)} € pro Tankfüllung."})
        else:
            recs.append({"kind": "personal", "level": "good", "title": "Deine Tankzeit passt",
                         "text": f"Du tankst meist um {typ} Uhr, das gehört zu den günstigen Stunden."})

    # 5) Potenzial allgemein
    if valid_h and full30 >= 3:
        spread = max(hours[i] for i in valid_h) - min(hours[i] for i in valid_h)
        recs.append({"kind": "potential", "level": "info", "title": f"Bis zu {de1(spread)} ct/l Unterschied im Tagesverlauf",
                     "text": f"Bei 50 l Tankfüllung sind das bis zu {de2(spread / 100 * 50)} € zwischen der teuersten und der günstigsten Stunde."})
    con.commit()
    con.close()
    return {"fuel": fuel, "recommendations": recs, "days": full90}


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
