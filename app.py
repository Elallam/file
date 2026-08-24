"""
Racket-Sports & Football Liga-Pro Analyzer
============================================

Fetches FINISHED matches from SofaScore (via `sofascore-wrapper==1.1.1`,
which drives a headless Chromium so it survives SofaScore's 403-on-plain-REST
protection) and serves a single web page with a per-sport analysis table.

    Table tennis / Tennis: id, game, total points/games, even/odd,
                            winning odd, # sets (3-set deciders highlighted)
    Football:               id, game, winner (1/X/2), winner odd,
                            goals (O/U 2.5), corners (O/U 9.5), cards (O/U 5.5)

Each sport declares its own `columns` and an `analyzer` tag; rows are built
server-side as {key: {"text":..., "variant":..., "sub":...}} cells so the
front end can render any sport generically.

Run:
    pip install -r requirements.txt
    python -m playwright install chromium      # one-time, downloads Chromium
    python app.py
    # open http://127.0.0.1:5000
"""

import asyncio
import time
from datetime import date
from flask import Flask, jsonify, request, render_template_string

from sofascore_wrapper.api import SofascoreAPI
from sofascore_wrapper.league import League
from sofascore_wrapper.match import Match
from sofascore_wrapper.search import Search
from sofascore_wrapper.tennis import Tennis

app = Flask(__name__)

# ----------------------------------------------------------------------------
# Sports & competitions.
#
# `analyzer`:
#   "sets"      -> table tennis / tennis: periods are per-set points/games.
#   "football"  -> winner (1/X/2) + goals/corners/cards vs a fixed line.
#
# Table tennis & football "leagues" map straight to one SofaScore
# unique-tournament (resolved via search + country match, or pin
# `tournament_id` to skip search).
#
# Tennis "leagues" map to a SofaScore *category* (ATP/WTA/...) containing
# many individual tournaments -> pick one from the dropdown (has_tournament_picker).
# ----------------------------------------------------------------------------
SPORTS = {
    "table-tennis": {
        "label": "Table Tennis",
        "sport_key": "table-tennis",
        "analyzer": "sets",
        "metric_label": "Total points",
        "decider_sets": 3,
        "has_tournament_picker": False,
        "columns": [
            {"key": "id", "label": "ID"},
            {"key": "game", "label": "Game"},
            {"key": "total_points", "label": "Total points"},
            {"key": "even_odd", "label": "Even / Odd"},
            {"key": "winning_odd", "label": "Winning odd"},
            {"key": "num_sets", "label": "# Sets"},
        ],
        "note": ("<b>Total points</b> = sum of points across every set (both players). "
                 "<b>Even/Odd</b> = parity of that total. "
                 "<b>Winning odd</b> = pre-match decimal odd of the player who actually won. "
                 "<b># Sets</b> = sets played &mdash; rows highlighted yellow went to 3 sets (a decider)."),
        "leagues": {
            "belarus-liga-pro":  {"label": "Belarus · Liga Pro",  "country": "Belarus",        "search": "Liga Pro",  "tournament_id": None},
            "czech-liga-pro":    {"label": "Czech · Liga Pro",    "country": "Czech Republic", "search": "Liga Pro",  "tournament_id": None},
            "czech-tt-cup":      {"label": "Czech · TT Cup",      "country": "Czech Republic", "search": "TT Cup",    "tournament_id": None},
            "russia-liga-pro":   {"label": "Russia · Liga Pro",   "country": "Russia",         "search": "Liga Pro",  "tournament_id": None},
            "ukraine-setka-cup": {"label": "Ukraine · Setka Cup", "country": "Ukraine",        "search": "Setka Cup", "tournament_id": None},
        },
    },
    "tennis": {
        "label": "Tennis",
        "sport_key": "tennis",
        "analyzer": "sets",
        "metric_label": "Total games",
        "decider_sets": 3,
        "has_tournament_picker": True,
        "columns": [
            {"key": "id", "label": "ID"},
            {"key": "game", "label": "Game"},
            {"key": "total_points", "label": "Total games"},
            {"key": "even_odd", "label": "Even / Odd"},
            {"key": "winning_odd", "label": "Winning odd"},
            {"key": "num_sets", "label": "# Sets"},
        ],
        "note": ("<b>Total games</b> = sum of games across every set (both players). "
                 "<b>Even/Odd</b> = parity of that total. "
                 "<b>Winning odd</b> = pre-match decimal odd of the player who actually won. "
                 "<b># Sets</b> = sets played &mdash; rows highlighted yellow went to 3 sets (a decider). "
                 "Pick a specific tournament above &mdash; ATP/WTA/etc. are categories containing many events."),
        "leagues": {
            "atp":        {"label": "ATP",        "match": ["atp"],                "category_id": None},
            "wta":        {"label": "WTA",        "match": ["wta"],                "category_id": None},
            "challenger": {"label": "Challenger",  "match": ["challenger"],        "category_id": None},
            "itf-men":    {"label": "ITF Men",    "match": ["itf men", "itf m"],   "category_id": None},
            "itf-women":  {"label": "ITF Women",  "match": ["itf women", "itf w"], "category_id": None},
        },
    },
    "football": {
        "label": "Football",
        "sport_key": "football",
        "analyzer": "football",
        "metric_label": None,
        "decider_sets": None,
        "has_tournament_picker": False,
        "columns": [
            {"key": "id", "label": "ID"},
            {"key": "game", "label": "Game"},
            {"key": "winner", "label": "Winner (1X2)"},
            {"key": "winner_odd", "label": "Winner odd"},
            {"key": "total_goals", "label": "Goals (O/U 2.5)"},
            {"key": "total_corners", "label": "Corners (O/U 9.5)"},
            {"key": "total_cards", "label": "Cards (O/U 5.5)"},
        ],
        "note": ("<b>Winner</b> = 1 (home), X (draw), 2 (away). "
                 "<b>Winner odd</b> = pre-match decimal odd of the outcome that actually happened. "
                 "<b>Goals/Corners/Cards</b> = the match's actual total vs. the fixed line, "
                 "shown as Over/Under. Corners/cards show N/A if SofaScore has no match statistics for that game."),
        "leagues": {
            "premier-league": {"label": "Premier League", "country": "England", "search": "Premier League", "tournament_id": None},
            "la-liga":        {"label": "La Liga",        "country": "Spain",   "search": "LaLiga",         "tournament_id": None},
            "serie-a":        {"label": "Serie A",        "country": "Italy",   "search": "Serie A",        "tournament_id": None},
            "bundesliga":     {"label": "Bundesliga",     "country": "Germany", "search": "Bundesliga",     "tournament_id": None},
            "ligue-1":        {"label": "Ligue 1",        "country": "France",  "search": "Ligue 1",        "tournament_id": None},
        },
    },
}

DEFAULT_LIMIT = 25
CACHE_TTL = 300
META_CACHE_TTL = 3600
ACTIVE_TTL = 120

_match_cache = {}             # (sport,league,tournament_id,limit) -> (ts, payload)
_id_cache = {}                 # (sport,league) -> resolved tournament_id
_category_cache = {}           # (sport,league) -> (ts, category_id)
_tournament_list_cache = {}    # (sport,league) -> (ts, (tournaments, category_id))
_active_cache = {}             # sport_key -> (ts, set(tournament_id) | None)


# ----------------------------------------------------------------------------
# Pure helpers -- no network, unit-testable.
# ----------------------------------------------------------------------------
def cell(text, variant=None, sub=None):
    c = {"text": text, "variant": variant}
    if sub:
        c["sub"] = sub
    return c


def _to_number(x):
    if x is None:
        return None
    try:
        return float(str(x).replace("%", "").strip())
    except Exception:
        return None


def _period_points(score: dict):
    """Per-set tallies (points for table tennis, games for tennis)."""
    pts = []
    i = 1
    while True:
        key = f"period{i}"
        if key not in score:
            break
        val = score.get(key)
        pts.append(int(val) if isinstance(val, (int, float)) else 0)
        i += 1
    return pts


def fractional_to_decimal(frac: str):
    """'57/100' -> 1.57 ; '9/2' -> 5.5 ; returns None on failure."""
    try:
        num, den = frac.split("/")
        return round(1 + float(num) / float(den), 2)
    except Exception:
        return None


def pick_choice_odd(odds_payload, choice_name, market_id=1):
    """From /odds/1/all, get the decimal odd for a named outcome ('1'/'X'/'2')
    in the given market (defaults to the Full-time / match-winner market)."""
    if not odds_payload:
        return None
    markets = odds_payload.get("markets", [])
    if not markets:
        return None
    market = next((m for m in markets if m.get("marketId") == market_id), markets[0])
    choice = next((c for c in market.get("choices", []) if c.get("name") == choice_name), None)
    if not choice:
        return None
    return fractional_to_decimal(choice.get("fractionalValue", ""))


def parse_football_stats(stats_payload):
    """Parse /event/{id}/statistics -> {'corners': (home,away)|None, 'cards': (home,away)|None}."""
    result = {"corners": None, "cards": None}
    if not stats_payload:
        return result
    blocks = stats_payload.get("statistics", [])
    if not blocks:
        return result
    period = next((b for b in blocks if b.get("period") == "ALL"), blocks[0])
    for group in period.get("groups", []):
        for item in group.get("statisticsItems", []):
            key = (item.get("key") or "").lower()
            name = (item.get("name") or "").lower()
            hv = item.get("homeValue")
            av = item.get("awayValue")
            if hv is None or av is None:
                hv = _to_number(item.get("home"))
                av = _to_number(item.get("away"))
            if hv is None or av is None:
                continue
            if "corner" in key or "corner" in name:
                result["corners"] = (hv, av)
            elif "yellowcard" in key.replace(" ", "") or "yellow card" in name:
                result["cards"] = (hv, av)
    return result


def parse_categories(data) -> list:
    """Defensively parse Tennis.categories() -> /sport/tennis/categories."""
    if isinstance(data, dict):
        cats = data.get("categories", data.get("results", []))
    elif isinstance(data, list):
        cats = data
    else:
        cats = []
    out = []
    for c in cats:
        ent = c.get("entity", c) if isinstance(c, dict) else {}
        if "id" in ent and ("name" in ent or "slug" in ent):
            out.append({"id": ent["id"], "name": ent.get("name") or ent.get("slug")})
    return out


def parse_tournaments(data) -> list:
    """Defensively parse League.leagues(category_id) -> /category/{id}/unique-tournaments."""
    out, seen = [], set()

    def add(items):
        for t in items or []:
            if not isinstance(t, dict):
                continue
            tid, name = t.get("id"), t.get("name") or t.get("slug")
            if tid is not None and tid not in seen:
                seen.add(tid)
                out.append({"id": tid, "name": name or f"Tournament {tid}"})

    if isinstance(data, dict):
        if "groups" in data:
            for g in data["groups"]:
                add(g.get("uniqueTournaments"))
        add(data.get("uniqueTournaments"))
        add(data.get("tournaments"))
        add(data.get("results"))
    elif isinstance(data, list):
        add(data)

    out.sort(key=lambda t: (t["name"] or "").lower())
    return out


def extract_tournament_ids(events: list) -> set:
    ids = set()
    for ev in events or []:
        t = ev.get("tournament", {}) or {}
        ut = t.get("uniqueTournament") or {}
        tid = ut.get("id", t.get("id"))
        if tid is not None:
            ids.add(tid)
    return ids


# ----------------------------------------------------------------------------
# Row analyzers -- pure, unit-testable.
# ----------------------------------------------------------------------------
def analyze_row_sets(event: dict, winning_odd, decider_sets: int) -> dict:
    home = event.get("homeTeam", {}).get("name", "?")
    away = event.get("awayTeam", {}).get("name", "?")
    hs = event.get("homeScore", {}) or {}
    as_ = event.get("awayScore", {}) or {}

    home_pts = _period_points(hs)
    away_pts = _period_points(as_)
    num_sets = max(len(home_pts), len(away_pts))
    if num_sets == 0:
        num_sets = int(hs.get("current", 0)) + int(as_.get("current", 0))
    total = sum(home_pts) + sum(away_pts)

    winner_code = event.get("winnerCode")
    winner = home if winner_code == 1 else away if winner_code == 2 else "-"
    is_decider = num_sets == decider_sets

    return {
        "id": event.get("id"),
        "game": cell(f"{home} vs {away}", sub=f'{hs.get("current","?")}–{as_.get("current","?")} · won: {winner}'),
        "total_points": cell(str(total)),
        "even_odd": cell("Even" if total % 2 == 0 else "Odd", variant="even" if total % 2 == 0 else "odd"),
        "winning_odd": cell(f"{winning_odd:.2f}" if winning_odd is not None else "—",
                             variant="odd-value" if winning_odd is not None else None),
        "num_sets": cell(str(num_sets), variant="decider" if is_decider else "normal"),
        "highlight": "decider" if is_decider else None,
        "_raw": {"total_points": total, "even_odd": "Even" if total % 2 == 0 else "Odd",
                  "winning_odd": winning_odd, "num_sets": num_sets},
    }


def analyze_row_football(event: dict, winner_odd, corners, cards) -> dict:
    home = event.get("homeTeam", {}).get("name", "?")
    away = event.get("awayTeam", {}).get("name", "?")
    hs = event.get("homeScore", {}) or {}
    as_ = event.get("awayScore", {}) or {}
    home_goals = int(hs.get("current", 0) or 0)
    away_goals = int(as_.get("current", 0) or 0)
    total_goals = home_goals + away_goals

    winner_code = event.get("winnerCode")
    if winner_code == 1:
        outcome, variant = "1", "home"
    elif winner_code == 2:
        outcome, variant = "2", "away"
    elif winner_code == 3 or home_goals == away_goals:
        outcome, variant = "X", "draw"
    elif home_goals > away_goals:
        outcome, variant = "1", "home"
    else:
        outcome, variant = "2", "away"

    goals_variant = "over" if total_goals > 2.5 else "under"
    goals_text = f'{total_goals} ({"Over" if goals_variant == "over" else "Under"} 2.5)'

    if corners is not None:
        total_corners = corners[0] + corners[1]
        corners_variant = "over" if total_corners > 9.5 else "under"
        corners_text = f'{total_corners:g} ({"Over" if corners_variant == "over" else "Under"} 9.5)'
    else:
        total_corners, corners_variant, corners_text = None, None, "N/A"

    if cards is not None:
        total_cards = cards[0] + cards[1]
        cards_variant = "over" if total_cards > 5.5 else "under"
        cards_text = f'{total_cards:g} ({"Over" if cards_variant == "over" else "Under"} 5.5)'
    else:
        total_cards, cards_variant, cards_text = None, None, "N/A"

    return {
        "id": event.get("id"),
        "game": cell(f"{home} vs {away}", sub=f"{home_goals}-{away_goals} FT"),
        "winner": cell(outcome, variant=variant),
        "winner_odd": cell(f"{winner_odd:.2f}" if winner_odd is not None else "—",
                            variant="odd-value" if winner_odd is not None else None),
        "total_goals": cell(goals_text, variant=goals_variant),
        "total_corners": cell(corners_text, variant=corners_variant),
        "total_cards": cell(cards_text, variant=cards_variant),
        "highlight": None,
        "_raw": {"outcome": outcome, "winner_odd": winner_odd,
                  "total_goals": total_goals, "goals_variant": goals_variant,
                  "total_corners": total_corners, "corners_variant": corners_variant,
                  "total_cards": total_cards, "cards_variant": cards_variant},
    }


def build_summary(sport_cfg, rows):
    raws = [r["_raw"] for r in rows]
    n = len(raws)
    if n == 0:
        return []
    if sport_cfg["analyzer"] == "sets":
        even_n = sum(1 for r in raws if r["even_odd"] == "Even")
        avg_total = round(sum(r["total_points"] for r in raws) / n, 1)
        odds = [r["winning_odd"] for r in raws if r["winning_odd"] is not None]
        avg_odd = round(sum(odds) / len(odds), 2) if odds else None
        decider_n = sum(1 for r in raws if r["num_sets"] == sport_cfg["decider_sets"])
        return [
            {"label": "Matches", "value": str(n)},
            {"label": "Even / Odd", "value": f"{even_n} / {n - even_n}"},
            {"label": f"Avg {sport_cfg['metric_label'].lower()}", "value": str(avg_total)},
            {"label": "Avg winning odd", "value": f"{avg_odd:.2f}" if avg_odd is not None else "—"},
            {"label": f"{sport_cfg['decider_sets']}-set deciders", "value": str(decider_n)},
        ]
    if sport_cfg["analyzer"] == "football":
        home_n = sum(1 for r in raws if r["outcome"] == "1")
        draw_n = sum(1 for r in raws if r["outcome"] == "X")
        away_n = sum(1 for r in raws if r["outcome"] == "2")
        over_goals = sum(1 for r in raws if r["goals_variant"] == "over")
        corners_known = [r for r in raws if r["corners_variant"] is not None]
        cards_known = [r for r in raws if r["cards_variant"] is not None]
        over_corners = sum(1 for r in corners_known if r["corners_variant"] == "over")
        over_cards = sum(1 for r in cards_known if r["cards_variant"] == "over")
        return [
            {"label": "Matches", "value": str(n)},
            {"label": "Home / Draw / Away", "value": f"{home_n} / {draw_n} / {away_n}"},
            {"label": "Over 2.5 goals", "value": f"{over_goals}/{n}"},
            {"label": "Over 9.5 corners", "value": f"{over_corners}/{len(corners_known)}" if corners_known else "—"},
            {"label": "Over 5.5 cards", "value": f"{over_cards}/{len(cards_known)}" if cards_known else "—"},
        ]
    return []


# ----------------------------------------------------------------------------
# Network layer (async, one Chromium session per request).
# ----------------------------------------------------------------------------
async def resolve_tournament_id(api, sport_key, cfg):
    """Search -> single unique tournament, matched by country."""
    search = Search(api, search_string=cfg["search"])
    data = await search.search_leagues(sport_key)
    results = data.get("results", []) if isinstance(data, dict) else data
    if not results:
        return None
    country = (cfg.get("country") or "").lower()
    for entry in results:
        ent = entry.get("entity", entry)
        cat = ent.get("category") or {}
        cat_name = (cat.get("name") or "").lower()
        cat_country = ((cat.get("country") or {}).get("name") or "").lower()
        if country in (cat_name, cat_country):
            return ent.get("id")
    for entry in results:
        ent = entry.get("entity", entry)
        cat = ent.get("category") or {}
        if country in (cat.get("name") or "").lower():
            return ent.get("id")
    return None


async def resolve_category_id(api, cfg):
    data = await Tennis(api).categories()
    categories = parse_categories(data)
    patterns = cfg["match"]
    for c in categories:
        name = (c["name"] or "").lower()
        if any(name == p for p in patterns):
            return c["id"]
    for c in categories:
        name = (c["name"] or "").lower()
        if any(p in name for p in patterns):
            return c["id"]
    return None


async def get_category_id(api, sport_key, league_key):
    cfg = SPORTS[sport_key]["leagues"][league_key]
    if cfg.get("category_id"):
        return cfg["category_id"]
    cache_key = (sport_key, league_key)
    hit = _category_cache.get(cache_key)
    if hit and time.time() - hit[0] < META_CACHE_TTL:
        return hit[1]
    cid = await resolve_category_id(api, cfg)
    if cid:
        _category_cache[cache_key] = (time.time(), cid)
    return cid


def extract_tournament_ids_wrapper(events):  # kept for symmetry / testability
    return extract_tournament_ids(events)


async def fetch_active_tournament_ids(api, sport_key):
    ids, got_any = set(), False
    try:
        live = await api._get(f"/sport/{sport_key}/events/live")
        ids |= extract_tournament_ids(live.get("events", []))
        got_any = True
    except Exception:
        pass
    try:
        today = date.today().isoformat()
        scheduled = await api._get(f"/sport/{sport_key}/scheduled-events/{today}")
        ids |= extract_tournament_ids(scheduled.get("events", []))
        got_any = True
    except Exception:
        pass
    return ids if got_any else None


async def get_active_tournament_ids(sport_key):
    hit = _active_cache.get(sport_key)
    if hit and time.time() - hit[0] < ACTIVE_TTL:
        return hit[1]
    api = SofascoreAPI()
    try:
        ids = await fetch_active_tournament_ids(api, sport_key)
    finally:
        await api.close()
    _active_cache[sport_key] = (time.time(), ids)
    return ids


async def get_tournament_list(sport_key, league_key):
    cache_key = (sport_key, league_key)
    hit = _tournament_list_cache.get(cache_key)
    if hit and time.time() - hit[0] < META_CACHE_TTL:
        tournaments, category_id = hit[1]
    else:
        api = SofascoreAPI()
        try:
            cid = await get_category_id(api, sport_key, league_key)
            if not cid:
                return {"category_id": None, "tournaments": [], "error": "Could not resolve category"}
            raw = await League(api, 0).leagues(cid)
            tournaments = parse_tournaments(raw)
            category_id = cid
            _tournament_list_cache[cache_key] = (time.time(), (tournaments, category_id))
        finally:
            await api.close()

    active_ids = await get_active_tournament_ids(SPORTS[sport_key]["sport_key"])
    tagged = []
    for t in tournaments:
        active = None if active_ids is None else (t["id"] in active_ids)
        tagged.append({**t, "active": active})
    tagged.sort(key=lambda t: (t["active"] is False, (t["name"] or "").lower()))
    return {"category_id": category_id, "tournaments": tagged, "error": None}


async def fetch_finished(api, tournament_id, limit):
    league = League(api, tournament_id)
    season = await league.current_season()
    if not season:
        return []
    season_id = season["id"]
    finished, page = [], 0
    while len(finished) < limit and page < 8:
        try:
            data = await api._get(
                f"/unique-tournament/{tournament_id}/season/{season_id}/events/last/{page}"
            )
        except Exception:
            break
        events = data.get("events", [])
        for ev in reversed(events):
            if (ev.get("status", {}) or {}).get("type") == "finished":
                finished.append(ev)
        if not data.get("hasNextPage"):
            break
        page += 1
    return finished[:limit]


async def build_row(api, sport_cfg, event):
    try:
        odds = await Match(api, event["id"]).match_odds()
    except Exception:
        odds = None

    if sport_cfg["analyzer"] == "football":
        winner_code = event.get("winnerCode")
        hs = event.get("homeScore", {}) or {}
        as_ = event.get("awayScore", {}) or {}
        hg, ag = int(hs.get("current", 0) or 0), int(as_.get("current", 0) or 0)
        if winner_code == 1:
            outcome = "1"
        elif winner_code == 2:
            outcome = "2"
        elif winner_code == 3 or hg == ag:
            outcome = "X"
        else:
            outcome = "1" if hg > ag else "2"
        winner_odd = pick_choice_odd(odds, outcome)
        try:
            stats_raw = await Match(api, event["id"]).stats()
            parsed = parse_football_stats(stats_raw)
        except Exception:
            parsed = {"corners": None, "cards": None}
        return analyze_row_football(event, winner_odd, parsed["corners"], parsed["cards"])
    else:
        winner_code = event.get("winnerCode")
        choice_name = "1" if winner_code == 1 else "2" if winner_code == 2 else None
        winning_odd = pick_choice_odd(odds, choice_name) if choice_name else None
        return analyze_row_sets(event, winning_odd, sport_cfg["decider_sets"])


async def build_match_payload(sport_key, league_key, tournament_id, limit):
    sport_cfg = SPORTS[sport_key]
    cfg = sport_cfg["leagues"][league_key]
    api = SofascoreAPI()
    try:
        tid = tournament_id
        if not tid and not sport_cfg["has_tournament_picker"]:
            cache_id_key = (sport_key, league_key)
            tid = cfg.get("tournament_id") or _id_cache.get(cache_id_key)
            if not tid:
                tid = await resolve_tournament_id(api, sport_cfg["sport_key"], cfg)
                if tid:
                    _id_cache[cache_id_key] = tid

        if not tid:
            return {"sport": sport_key, "league": cfg["label"], "tournament_id": None,
                    "rows": [], "summary": [], "error": "Missing tournament id"}

        events = await fetch_finished(api, tid, limit)
        rows = [await build_row(api, sport_cfg, ev) for ev in events]
        summary = build_summary(sport_cfg, rows)
        display_rows = [{k: v for k, v in r.items() if k != "_raw"} for r in rows]

        return {"sport": sport_key, "league": cfg["label"], "tournament_id": tid,
                "rows": display_rows, "summary": summary, "error": None}
    finally:
        await api.close()


def get_match_data(sport_key, league_key, tournament_id, limit):
    cache_key = (sport_key, league_key, tournament_id, limit)
    hit = _match_cache.get(cache_key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    payload = asyncio.run(build_match_payload(sport_key, league_key, tournament_id, limit))
    _match_cache[cache_key] = (time.time(), payload)
    return payload


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.route("/api/sports")
def api_sports():
    return jsonify([
        {"key": k, "label": v["label"], "columns": v["columns"], "note": v["note"],
         "has_tournament_picker": v["has_tournament_picker"]}
        for k, v in SPORTS.items()
    ])


@app.route("/api/leagues")
def api_leagues():
    sport = request.args.get("sport")
    if sport not in SPORTS:
        return jsonify({"error": "unknown sport"}), 400
    return jsonify([{"key": k, "label": v["label"]} for k, v in SPORTS[sport]["leagues"].items()])


@app.route("/api/tournaments")
def api_tournaments():
    sport = request.args.get("sport")
    league = request.args.get("league")
    if sport not in SPORTS or league not in SPORTS[sport]["leagues"]:
        return jsonify({"error": "unknown sport/league"}), 400
    if not SPORTS[sport]["has_tournament_picker"]:
        return jsonify({"error": "this sport has no tournament picker"}), 400
    return jsonify(asyncio.run(get_tournament_list(sport, league)))


@app.route("/api/matches")
def api_matches():
    sport = request.args.get("sport")
    league = request.args.get("league")
    tournament_id = request.args.get("tournament_id", type=int)
    limit = min(int(request.args.get("limit", DEFAULT_LIMIT)), 60)
    if sport not in SPORTS:
        return jsonify({"error": "unknown sport"}), 400
    if league not in SPORTS[sport]["leagues"]:
        return jsonify({"error": "unknown league"}), 400
    if SPORTS[sport]["has_tournament_picker"] and not tournament_id:
        return jsonify({"error": "tournament_id required for this sport"}), 400
    return jsonify(get_match_data(sport, league, tournament_id, limit))


@app.route("/")
def index():
    return render_template_string(PAGE)


# ----------------------------------------------------------------------------
# Front-end (single embedded page, generic over sport columns)
# ----------------------------------------------------------------------------
PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sports Liga-Pro Analyzer</title>
<style>
  :root{
    --bg:#0f1116; --panel:#171a21; --panel2:#1e222b; --line:#2a2f3a;
    --txt:#e8eaed; --muted:#9aa2b1; --accent:#ff7a1a; --green:#37c46b;
    --yellow:#3a3413; --yellow-txt:#ffe27a; --yellow-row:#2a260f;
    --over:#c56bff; --under:#2b6cff; --home:#37c46b; --draw:#9aa2b1; --away:#f2545b;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
       font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
  header{padding:22px 26px;border-bottom:1px solid var(--line);
         display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  header h1{font-size:19px;margin:0;font-weight:650;letter-spacing:.2px}
  header .dot{width:10px;height:10px;border-radius:50%;background:var(--accent);
              box-shadow:0 0 0 4px rgba(255,122,26,.15)}
  .wrap{padding:20px 26px;max-width:1160px;margin:0 auto}
  .sporttabs{display:flex;gap:8px;margin-bottom:14px}
  .sporttab{padding:9px 18px;border:1px solid var(--line);background:var(--panel);
       color:var(--muted);border-radius:10px;cursor:pointer;font-size:14px;font-weight:600;
       transition:.15s;user-select:none}
  .sporttab:hover{color:var(--txt);border-color:#3a4150}
  .sporttab.active{background:#2a2f3a;border-color:var(--accent);color:var(--accent)}
  .tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
  .tab{padding:8px 14px;border:1px solid var(--line);background:var(--panel);
       color:var(--muted);border-radius:999px;cursor:pointer;font-size:13.5px;
       transition:.15s;user-select:none}
  .tab:hover{color:var(--txt);border-color:#3a4150}
  .tab.active{background:var(--accent);border-color:var(--accent);color:#111;font-weight:600}
  .picker{display:flex;align-items:center;gap:10px;margin-bottom:16px}
  .picker label{font-size:13px;color:var(--muted)}
  .picker select{flex:1;max-width:420px;background:var(--panel2);border:1px solid var(--line);
                 color:var(--txt);border-radius:8px;padding:9px 10px;font-size:14px}
  .picker select:disabled{opacity:.5}
  .picker select option:disabled{color:#5a6272}
  .bar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
  .bar label{font-size:13px;color:var(--muted)}
  .bar input{width:64px;background:var(--panel2);border:1px solid var(--line);
             color:var(--txt);border-radius:8px;padding:6px 8px}
  button.reload{margin-left:auto;background:var(--panel2);border:1px solid var(--line);
                color:var(--txt);border-radius:8px;padding:8px 14px;cursor:pointer}
  button.reload:hover{border-color:var(--accent)}
  .cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;
        padding:12px 16px;min-width:120px}
  .card .k{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
  .card .v{font-size:22px;font-weight:680;margin-top:4px}
  .tablewrap{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}
  table{width:100%;border-collapse:collapse;font-size:14px}
  th,td{padding:11px 14px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
  th{background:var(--panel2);color:var(--muted);font-weight:600;font-size:12px;
     text-transform:uppercase;letter-spacing:.5px;position:sticky;top:0}
  tbody tr:hover{background:#1b1f28}
  tbody tr.decider{background:var(--yellow-row)}
  tbody tr.decider:hover{background:#332d10}
  tbody tr.decider td.idcol{color:var(--yellow-txt)}
  td.gamecol{white-space:normal;min-width:230px}
  .pill{padding:3px 9px;border-radius:999px;font-size:12px;font-weight:600;display:inline-block}
  .pill.even{background:rgba(43,108,255,.16);color:#7aa2ff}
  .pill.odd{background:rgba(197,107,255,.16);color:#c99bff}
  .pill.over{background:rgba(197,107,255,.16);color:#c99bff}
  .pill.under{background:rgba(43,108,255,.16);color:#7aa2ff}
  .pill.home{background:rgba(55,196,107,.16);color:#6fe0a0}
  .pill.draw{background:rgba(154,162,177,.16);color:#c2c8d3}
  .pill.away{background:rgba(242,84,91,.16);color:#ff9298}
  .setpill{padding:2px 9px;border-radius:999px;font-size:12.5px;font-weight:700;display:inline-block}
  .setpill.decider{background:var(--yellow);color:var(--yellow-txt);border:1px solid #6b5c1f}
  .setpill.normal{color:var(--muted)}
  .odd-val{font-variant-numeric:tabular-nums;font-weight:650;color:var(--green)}
  .muted{color:var(--muted)}
  .idcol{color:var(--muted);font-variant-numeric:tabular-nums}
  .status{padding:40px;text-align:center;color:var(--muted)}
  .spinner{width:26px;height:26px;border:3px solid var(--line);border-top-color:var(--accent);
           border-radius:50%;animation:spin 1s linear infinite;margin:0 auto 12px}
  @keyframes spin{to{transform:rotate(360deg)}}
  .err{color:#ff8a8a}
  .note{font-size:12px;color:var(--muted);margin-top:14px;line-height:1.6}
  .winner{font-size:12px;color:var(--muted)}
  .legend{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--muted);margin:2px 0 16px}
  .swatch{width:12px;height:12px;border-radius:3px;background:var(--yellow);border:1px solid #6b5c1f}
</style>
</head>
<body>
<header>
  <span class="dot"></span>
  <h1>Sports Liga-Pro Analyzer</h1>
  <span class="muted" style="font-size:13px">finished matches · via sofascore-wrapper 1.1.1</span>
</header>

<div class="wrap">
  <div class="sporttabs" id="sporttabs"></div>
  <div class="tabs" id="tabs"></div>

  <div class="picker" id="pickerRow" style="display:none">
    <label>Tournament</label>
    <select id="tournamentSelect" disabled>
      <option value="">Loading tournaments…</option>
    </select>
  </div>

  <div class="bar">
    <label>Matches to load</label>
    <input id="limit" type="number" min="5" max="60" value="25">
    <button class="reload" id="reload">↻ Reload</button>
  </div>

  <div class="cards" id="cards"></div>
  <div class="legend" id="legend" style="display:none"><span class="swatch"></span> highlighted rows = match decided in 3 sets</div>

  <div class="tablewrap">
    <div id="status" class="status">Pick a league to begin.</div>
    <table id="table" style="display:none">
      <thead><tr id="theadRow"></tr></thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>

  <div class="note" id="note"></div>
</div>

<script>
let sports = [], leagues = [], currentSport = null, currentLeague = null,
    currentMeta = {}, currentTournamentId = null;

async function loadSports(){
  sports = await (await fetch('/api/sports')).json();
  const st = document.getElementById('sporttabs');
  st.innerHTML = '';
  sports.forEach((s,i)=>{
    const el = document.createElement('div');
    el.className = 'sporttab' + (i===0?' active':'');
    el.textContent = s.label;
    el.dataset.key = s.key;
    el.onclick = ()=> setSport(s.key);
    st.appendChild(el);
  });
  await setSport(sports[0].key);
}

function renderHeader(){
  const tr = document.getElementById('theadRow');
  tr.innerHTML = currentMeta.columns.map(c=>`<th>${c.label}</th>`).join('');
}

async function setSport(key){
  currentSport = key;
  currentMeta = sports.find(s=>s.key===key) || {};
  document.querySelectorAll('.sporttab').forEach(t=>
    t.classList.toggle('active', t.dataset.key===key));
  renderHeader();
  document.getElementById('legend').style.display = currentMeta.columns.some(c=>c.key==='num_sets') ? 'flex' : 'none';

  leagues = await (await fetch(`/api/leagues?sport=${key}`)).json();
  const tabs = document.getElementById('tabs');
  tabs.innerHTML = '';
  leagues.forEach((l,i)=>{
    const el = document.createElement('div');
    el.className = 'tab' + (i===0?' active':'');
    el.textContent = l.label;
    el.dataset.key = l.key;
    el.onclick = ()=> setLeague(l.key);
    tabs.appendChild(el);
  });
  await setLeague(leagues[0].key);
}

async function setLeague(key){
  currentLeague = key;
  currentTournamentId = null;
  document.querySelectorAll('.tab').forEach(t=>
    t.classList.toggle('active', t.dataset.key===key));

  const pickerRow = document.getElementById('pickerRow');
  const sel = document.getElementById('tournamentSelect');

  if(currentMeta.has_tournament_picker){
    pickerRow.style.display = 'flex';
    sel.disabled = true;
    sel.innerHTML = '<option value="">Loading tournaments…</option>';
    document.getElementById('status').style.display='block';
    document.getElementById('table').style.display='none';
    document.getElementById('status').innerHTML = 'Loading tournament list…';
    document.getElementById('cards').innerHTML = '';

    const data = await (await fetch(`/api/tournaments?sport=${currentSport}&league=${key}`)).json();
    if(data.error || !data.tournaments || !data.tournaments.length){
      sel.innerHTML = '<option value="">No tournaments found</option>';
      document.getElementById('status').innerHTML =
        '<span class="err">'+(data.error || 'No tournaments found for this category')+'</span>';
      updateNote();
      return;
    }
    sel.innerHTML = '<option value="">Select a tournament…</option>' +
      data.tournaments.map(t=>{
        const disabled = t.active === false;
        const label = disabled ? `${t.name} (not ongoing)` : t.name;
        return `<option value="${t.id}" ${disabled?'disabled':''}>${label}</option>`;
      }).join('');
    sel.disabled = false;
    document.getElementById('status').innerHTML = 'Pick a tournament above to load its matches.';
    updateNote();
  } else {
    pickerRow.style.display = 'none';
    updateNote();
    load();
  }
}

document.getElementById('tournamentSelect').addEventListener('change', (e)=>{
  currentTournamentId = e.target.value || null;
  if(currentTournamentId) load();
});

function updateNote(){
  document.getElementById('note').innerHTML = (currentMeta.note || '') + ' Data is cached for 5 minutes.';
}

async function load(){
  if(currentMeta.has_tournament_picker && !currentTournamentId){
    document.getElementById('status').style.display='block';
    document.getElementById('status').innerHTML = 'Pick a tournament above to load its matches.';
    document.getElementById('table').style.display='none';
    return;
  }
  const limit = document.getElementById('limit').value || 25;
  const table = document.getElementById('table');
  const status = document.getElementById('status');
  const cards = document.getElementById('cards');
  table.style.display='none'; cards.innerHTML='';
  status.style.display='block';
  status.innerHTML = '<div class="spinner"></div>Fetching &amp; analysing finished matches… (first load spins up Chromium, can take ~20-40s)';

  try{
    let url = `/api/matches?sport=${currentSport}&league=${currentLeague}&limit=${limit}`;
    if(currentTournamentId) url += `&tournament_id=${currentTournamentId}`;
    const res = await fetch(url);
    const data = await res.json();
    if(data.error && (!data.rows || !data.rows.length)){
      status.innerHTML = '<span class="err">'+data.error+'</span>'; return;
    }
    const rows = data.rows || [];
    if(!rows.length){ status.innerHTML='No finished matches found.'; return; }

    cards.innerHTML = (data.summary||[]).map(c=>
      `<div class="card"><div class="k">${c.label}</div><div class="v">${c.value}</div></div>`).join('');

    const tb = document.getElementById('tbody'); tb.innerHTML='';
    rows.forEach(r=>{
      const tr = document.createElement('tr');
      if(r.highlight === 'decider') tr.className = 'decider';
      tr.innerHTML = currentMeta.columns.map(col=>{
        const c = r[col.key];
        if(c === undefined || c === null) return '<td>—</td>';
        if(col.key === 'id') return `<td class="idcol">${c.text}</td>`;
        if(col.key === 'game'){
          return `<td class="gamecol">${c.text}${c.sub?`<div class="winner">${c.sub}</div>`:''}</td>`;
        }
        if(col.key === 'num_sets'){
          return `<td><span class="setpill ${c.variant}">${c.text}</span></td>`;
        }
        if(c.variant === 'odd-value'){
          return `<td><span class="odd-val">${c.text}</span></td>`;
        }
        if(c.variant){
          return `<td><span class="pill ${c.variant}">${c.text}</span></td>`;
        }
        return `<td>${c.text}</td>`;
      }).join('');
      tb.appendChild(tr);
    });
    status.style.display='none'; table.style.display='table';
  }catch(e){
    status.innerHTML = '<span class="err">Request failed: '+e.message+'</span>';
  }
}

document.getElementById('reload').onclick = ()=>{ load(); };
loadSports();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    import os
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", 5000)),
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
    )