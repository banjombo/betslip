import os
from datetime import datetime, timedelta, timezone
from typing import List, Tuple, Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx

# ---------- Config ----------
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
if not ODDS_API_KEY:
    # Don’t crash in local dev where you might stub; raise in hosted envs
    print("WARNING: ODDS_API_KEY not set. Live odds routes will fail.")
BASE_URL = "https://api.the-odds-api.com/v4"
SPORTS = {
    "nfl": "americanfootball_nfl",
    "cfb": "americanfootball_ncaaf",
}
BOOK_PRIORITY = ["DraftKings", "FanDuel", "BetMGM", "Caesars", "PointsBet (US)", "bet365"]
CACHE_TTL_SECS = 45

app = FastAPI(title="betslip1 NFL/CFB Live Odds")

# ---------- Cache ----------
_cache: Dict[str, Dict[str, Any]] = {}  # key -> {"data":..., "exp": datetime}
def now_utc() -> datetime: return datetime.now(timezone.utc)
def cache_get(key: str) -> Optional[Any]:
    e = _cache.get(key)
    if e and now_utc() < e["exp"]: return e["data"]
    return None
def cache_set(key: str, data: Any, ttl=CACHE_TTL_SECS):
    _cache[key] = {"data": data, "exp": now_utc() + timedelta(seconds=ttl)}

# ---------- Models (shape your iOS app expects) ----------
class Moneyline(BaseModel):
    home: int
    away: int

class Spread(BaseModel):
    favorite: str
    line: float
    price: int

class Total(BaseModel):
    line: float
    over_price: int
    under_price: int

class Game(BaseModel):
    week: int
    kickoff_iso: str
    home: str
    away: str
    moneyline: Moneyline
    spread: Spread
    total: Total

class GamesResponse(BaseModel):
    as_of: str
    games: List[Game]

# ---------- Helpers ----------
def football_week_window(now: datetime) -> Tuple[datetime, datetime]:
    """Thu 00:00 → Tue 00:00 window containing 'now' (captures TNF, Sunday, MNF)."""
    wkday = now.weekday()  # Mon=0..Sun=6
    monday = (now - timedelta(days=wkday)).replace(hour=0, minute=0, second=0, microsecond=0)
    thursday = monday + timedelta(days=3)
    tuesday_next = monday + timedelta(days=8)
    return thursday.astimezone(timezone.utc), tuesday_next.astimezone(timezone.utc)

async def fetch_odds(sport_key: str) -> List[dict]:
    """Call The Odds API for a sport with needed markets."""
    if not ODDS_API_KEY:
        raise HTTPException(status_code=500, detail="Server missing ODDS_API_KEY")
    url = f"{BASE_URL}/sports/{sport_key}/odds"
    params = {"regions": "us", "markets": "h2h,spreads,totals", "oddsFormat": "american", "apiKey": ODDS_API_KEY}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params=params)
        if r.status_code == 429:
            raise HTTPException(status_code=503, detail="Upstream rate limited (429). Try again shortly.")
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Provider error {r.status_code}: {r.text}")
        return r.json()

def choose_book(bookmakers: List[dict]) -> Optional[dict]:
    if not bookmakers: return None
    by_name = {b.get("title"): b for b in bookmakers}
    for name in BOOK_PRIORITY:
        if name in by_name: return by_name[name]
    return bookmakers[0]

def american_int(x) -> int:
    try:
        return int(float(x))
    except Exception:
        return int(x)

def map_event_to_game(ev: dict, book: dict) -> Optional[Game]:
    home = ev.get("home_team")
    away = ev.get("away_team")
    ko_iso = ev.get("commence_time")  # ISO UTC
    week_guess = 1  # TODO: replace with true week mapping if desired

    markets = {m.get("key"): m for m in book.get("markets", [])}

    # Moneyline
    ml = markets.get("h2h")
    if not ml or not ml.get("outcomes"): return None
    ml_out = {o.get("name"): o for o in ml["outcomes"]}
    ml_home = ml_out.get(home) or ml_out.get("Home")
    ml_away = ml_out.get(away) or ml_out.get("Away")
    if not ml_home or not ml_away: return None
    moneyline = Moneyline(home=american_int(ml_home.get("price")), away=american_int(ml_away.get("price")))

    # Spreads
    sp = markets.get("spreads")
    if not sp or not sp.get("outcomes"): return None
    spread_home = next((o for o in sp["outcomes"] if o.get("name") in (home, "Home")), None)
    spread_away = next((o for o in sp["outcomes"] if o.get("name") in (away, "Away")), None)
    if not spread_home or not spread_away: return None
    home_point = float(spread_home.get("point"))
    away_point = float(spread_away.get("point"))
    if home_point < away_point:
        favorite, line, price = home, abs(home_point), american_int(spread_home.get("price"))
    else:
        favorite, line, price = away, abs(away_point), american_int(spread_away.get("price"))
    spread = Spread(favorite=favorite, line=line, price=price)

    # Totals
    tot = markets.get("totals")
    if not tot or not tot.get("outcomes"): return None
    over = next((o for o in tot["outcomes"] if str(o.get("name","")).lower().startswith("over")), None)
    under = next((o for o in tot["outcomes"] if str(o.get("name","")).lower().startswith("under")), None)
    if not over or not under: return None
    total = Total(line=float(over.get("point")), over_price=american_int(over.get("price")),
                  under_price=american_int(under.get("price")))

    return Game(week=week_guess, kickoff_iso=ko_iso, home=home, away=away,
                moneyline=moneyline, spread=spread, total=total)

def within_window(commence_iso: str, start: datetime, end: datetime) -> bool:
    iso = commence_iso.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return False
    return start <= dt <= end

# ---------- Routes ----------
@app.get("/health")
def health(): return {"ok": True, "as_of": now_utc().isoformat()}

@app.get("/nfl/weekend", response_model=GamesResponse)
async def nfl_weekend():
    key = "nfl_weekend"
    if (cached := cache_get(key)): return cached
    raw = await fetch_odds(SPORTS["nfl"])
    start, end = football_week_window(now_utc())
    games: List[Game] = []
    for ev in raw:
        if not within_window(ev.get("commence_time",""), start, end): continue
        book = choose_book(ev.get("bookmakers", []))
        if not book: continue
        mapped = map_event_to_game(ev, book)
        if mapped: games.append(mapped)
    data = {"as_of": now_utc().isoformat(), "games": games}
    cache_set(key, data); return data

@app.get("/nfl/today", response_model=GamesResponse)
async def nfl_today():
    key = "nfl_today"
    if (cached := cache_get(key)): return cached
    raw = await fetch_odds(SPORTS["nfl"])
    today = now_utc().date()
    games: List[Game] = []
    for ev in raw:
        iso = ev.get("commence_time","").replace("Z","+00:00")
        try: dt = datetime.fromisoformat(iso)
        except ValueError: continue
        if dt.date() != today: continue
        book = choose_book(ev.get("bookmakers", []))
        if not book: continue
        mapped = map_event_to_game(ev, book)
        if mapped: games.append(mapped)
    if not games:  # fallback so UI isn’t empty in dev
        for ev in sorted(raw, key=lambda e: e.get("commence_time",""))[:2]:
            book = choose_book(ev.get("bookmakers", []))
            if not book: continue
            mapped = map_event_to_game(ev, book)
            if mapped: games.append(mapped)
    data = {"as_of": now_utc().isoformat(), "games": games}
    cache_set(key, data); return data

@app.get("/cfb/weekend", response_model=GamesResponse)
async def cfb_weekend():
    key = "cfb_weekend"
    if (cached := cache_get(key)): return cached
    raw = await fetch_odds(SPORTS["cfb"])
    start, end = football_week_window(now_utc())
    games: List[Game] = []
    for ev in raw:
        if not within_window(ev.get("commence_time",""), start, end): continue
        book = choose_book(ev.get("bookmakers", []))
        if not book: continue
        mapped = map_event_to_game(ev, book)
        if mapped: games.append(mapped)
    data = {"as_of": now_utc().isoformat(), "games": games}
    cache_set(key, data); return data
