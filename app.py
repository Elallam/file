"""
Table-Tennis & Tennis Liga-Pro Analyzer
========================================

Fetches FINISHED matches from SofaScore (via `sofascore-wrapper==1.1.1`,
which drives a headless Chromium so it survives SofaScore's 403-on-plain-REST
protection) and serves a single web page that shows, per match:

    id | game | total points/games | even/odd | winning odd | number of sets

Matches decided in 3 sets (a decider) are highlighted yellow in both tables.

Table tennis leagues (Liga Pro, Setka Cup, ...) are each a single continuous
SofaScore "unique tournament" -> selecting the tab loads matches directly.

Tennis leagues (ATP, WTA, Challenger, ITF...) are SofaScore *categories* that
contain many individual tournaments (Wimbledon, Miami Open, ...) -> selecting
the tab loads the list of tournaments in that category, and you then pick one
from the dropdown to load its matches.

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
# Table tennis "leagues" map straight to one SofaScore unique-tournament
# (resolved via search, or pin `tournament_id` to skip search).
#
# Tennis "leagues" map to a SofaScore *category* (resolved via Tennis
# categories() by name match, or pin `category_id` to skip that lookup).
# Selecting one then lists the individual tournaments inside it, and you
# pick a specific tournament from the dropdown.
# ----------------------------------------------------------------------------
SPORTS = {
    "table-tennis": {
        "label": "Table Tennis",
        "sport_key": "table-tennis",
        "metric_label": "Total points",
        "decider_sets": 3,
        "has_tournament_picker": False,
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
        "metric_label": "Total games",
        "decider_sets": 3,          # best-of-3 matches decided by a 3rd set
        "has_tournament_picker": True,
        "leagues": {
            "atp":        {"label": "ATP",        "match": ["atp"],               "category_id": None},
            "wta":        {"label": "WTA",        "match": ["wta"],               "category_id": None},
            "challenger": {"label": "Challenger", "match": ["challenger"],        "category_id": None},
            "itf-men":    {"label": "ITF Men",    "match": ["itf men", "itf m"],  "category_id": None},
            "itf-women":  {"label": "ITF Women",  "match": ["itf women", "itf w"],"category_id": None},
        },
    },
}

DEFAULT_LIMIT = 25          # finished matches analysed per tournament
CACHE_TTL = 300              # seconds, for match data
META_CACHE_TTL = 3600        # seconds, for category/tournament lists (rarely change)
ACTIVE_TTL = 120             # seconds, for the "which tournaments are ongoing" lookup

_match_cache = {}            # (sport,league,tournament_id,limit) -> (ts, payload)
_id_cache = {}                # (sport,league) -> resolved tournament_id (table tennis)
_category_cache = {}          # (sport,league) -> (ts, category_id)
_tournament_list_cache = {}   # (sport,league) -> (ts, [{"id":.., "name":..}, ...])
_active_cache = {}            # sport_key -> (ts, set(tournament_id) | None)


# ----------------------------------------------------------------------------
# Pure analysis helpers (no network) -- unit-testable.
# ----------------------------------------------------------------------------
def _period_points(score: dict):
    """Return the list of per-set tallies (points for table tennis, games for
    tennis) from a SofaScore score dict."""
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


def analyze_event(event: dict, winning_odd=None) -> dict:
    """Turn one finished SofaScore event into an analysis row. Works for both
    table tennis (periods = points) and tennis (periods = games)."""
    home = event.get("homeTeam", {}).get("name", "?")
    away = event.get("awayTeam", {}).get("name", "?")
    hs = event.get("homeScore", {}) or {}
    as_ = event.get("awayScore", {}) or {}

    home_sets_pts = _period_points(hs)
    away_sets_pts = _period_points(as_)

    num_sets = max(len(home_sets_pts), len(away_sets_pts))
    if num_sets == 0:
        num_sets = int(hs.get("current", 0)) + int(as_.get("current", 0))

    total_points = sum(home_sets_pts) + sum(away_sets_pts)

    winner_code = event.get("winnerCode")  # 1 = home, 2 = away
    winner = home if winner_code == 1 else away if winner_code == 2 else "-"

    return {
        "id": event.get("id"),
        "game": f"{home} vs {away}",
        "home": home,
        "away": away,
        "set_score": f'{hs.get("current", "?")}–{as_.get("current", "?")}',
        "winner": winner,
        "total_points": total_points,
        "even_odd": "Even" if total_points % 2 == 0 else "Odd",
        "winning_odd": winning_odd,
        "num_sets": num_sets,
    }


def fractional_to_decimal(frac: str):
    """'57/100' -> 1.57 ; '9/2' -> 5.5 ; returns None on failure."""
    try:
        num, den = frac.split("/")
        return round(1 + float(num) / float(den), 2)
    except Exception:
        return None


def pick_winning_odd(odds_payload: dict, winner_code):
    """From /odds/1/all pick the decimal odd of the outcome that actually won."""
    if not odds_payload:
        return None
    markets = odds_payload.get("markets", [])
    if not markets:
        return None
    market = next((m for m in markets if m.get("marketId") == 1), markets[0])
    choices = market.get("choices", [])

    win_choice = next((c for c in choices if c.get("winning")), None)
    if win_choice is None and winner_code in (1, 2):
        target = "1" if winner_code == 1 else "2"
        win_choice = next((c for c in choices if c.get("name") == target), None)
    if win_choice is None:
        return None
    return fractional_to_decimal(win_choice.get("fractionalValue", ""))


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
    """Defensively parse League.leagues(category_id) -> /category/{id}/unique-tournaments.
    SofaScore has returned this nested under 'groups[].uniqueTournaments' and,
    on some sports, as a flat 'uniqueTournaments' / 'tournaments' list."""
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


# ----------------------------------------------------------------------------
# Network layer (async, one Chromium session per request).
# ----------------------------------------------------------------------------
async def resolve_tournament_id(api, sport_key, cfg):
    """Table-tennis path: search -> single unique tournament."""
    search = Search(api, search_string=cfg["search"])
    data = await search.search_leagues(sport_key)
    results = data.get("results", []) if isinstance(data, dict) else data
    if not results:
        return None

    country = (cfg.get("country") or "").lower()
    for entry in results:
        ent = entry.get("entity", entry)
        cat = (ent.get("category") or {})
        cat_name = (cat.get("name") or "").lower()
        cat_country = ((cat.get("country") or {}).get("name") or "").lower()
        if country in (cat_name, cat_country):
            return ent.get("id")
    for entry in results:
        ent = entry.get("entity", entry)
        cat = (ent.get("category") or {})
        if country in (cat.get("name") or "").lower():
            return ent.get("id")
    return None


async def resolve_category_id(api, cfg):
    """Tennis path: match a category (ATP/WTA/Challenger/ITF...) by name."""
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


def extract_tournament_ids(events: list) -> set:
    """Pull unique-tournament ids out of a list of SofaScore events (live or
    scheduled-today), so we know which tournaments currently have action."""
    ids = set()
    for ev in events or []:
        t = ev.get("tournament", {}) or {}
        ut = t.get("uniqueTournament") or {}
        tid = ut.get("id", t.get("id"))
        if tid is not None:
            ids.add(tid)
    return ids


async def fetch_active_tournament_ids(api, sport_key):
    """Union of tournament ids with a live match right now, plus ids with any
    match scheduled today, for the given sport. Returns None (== unknown, so
    callers should fail open and not disable anything) if both lookups fail."""
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

    # "active" = has a live match right now or one scheduled today; None means
    # we couldn't determine it (network hiccup) -> don't disable anything.
    active_ids = await get_active_tournament_ids(SPORTS[sport_key]["sport_key"])
    tagged = []
    for t in tournaments:
        active = None if active_ids is None else (t["id"] in active_ids)
        tagged.append({**t, "active": active})
    tagged.sort(key=lambda t: (t["active"] is False, (t["name"] or "").lower()))

    return {"category_id": category_id, "tournaments": tagged, "error": None}


async def fetch_finished(api, tournament_id, limit):
    """Page through the 'last' (finished) events endpoint for the season."""
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
            return {
                "sport": sport_key, "league": cfg["label"], "tournament_id": None,
                "metric_label": sport_cfg["metric_label"], "decider_sets": sport_cfg["decider_sets"],
                "rows": [], "error": "Missing tournament id",
            }

        events = await fetch_finished(api, tid, limit)
        rows = []
        for ev in events:
            odd = None
            try:
                odds = await Match(api, ev["id"]).match_odds()
                odd = pick_winning_odd(odds, ev.get("winnerCode"))
            except Exception:
                odd = None
            rows.append(analyze_event(ev, winning_odd=odd))

        return {
            "sport": sport_key, "league": cfg["label"], "tournament_id": tid,
            "metric_label": sport_cfg["metric_label"], "decider_sets": sport_cfg["decider_sets"],
            "rows": rows, "error": None,
        }
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
    return jsonify([{"key": k, "label": v["label"], "metric_label": v["metric_label"],
                      "decider_sets": v["decider_sets"],
                      "has_tournament_picker": v["has_tournament_picker"]} for k, v in SPORTS.items()])


@app.route("/api/leagues")
def api_leagues():
    sport = request.args.get("sport")
    if sport not in SPORTS:
        return jsonify({"error": "unknown sport"}), 400
    return jsonify([{"key": k, "label": v["label"]} for k, v in SPORTS[sport]["leagues"].items()])


@app.route("/api/tournaments")
def api_tournaments():
    """List individual tournaments inside a tennis category (ATP/WTA/...)."""
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
# Front-end (single embedded page)
# ----------------------------------------------------------------------------
PAGE = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Racket-Sports Liga-Pro Analyzer</title>
<style>
  :root{
    --bg:#0f1116; --panel:#171a21; --panel2:#1e222b; --line:#2a2f3a;
    --txt:#e8eaed; --muted:#9aa2b1; --accent:#ff7a1a; --green:#37c46b; --red:#f2545b;
    --even:#2b6cff; --odd:#c56bff; --yellow:#3a3413; --yellow-txt:#ffe27a; --yellow-row:#2a260f;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
       font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
  header{padding:22px 26px;border-bottom:1px solid var(--line);
         display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  header h1{font-size:19px;margin:0;font-weight:650;letter-spacing:.2px}
  header .dot{width:10px;height:10px;border-radius:50%;background:var(--accent);
              box-shadow:0 0 0 4px rgba(255,122,26,.15)}
  .wrap{padding:20px 26px;max-width:1120px;margin:0 auto}
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
  tbody tr.decider td.id{color:var(--yellow-txt)}
  td.game{white-space:normal;min-width:230px}
  .pill{padding:3px 9px;border-radius:999px;font-size:12px;font-weight:600;display:inline-block}
  .pill.even{background:rgba(43,108,255,.16);color:#7aa2ff}
  .pill.odd{background:rgba(197,107,255,.16);color:#c99bff}
  .setpill{padding:2px 9px;border-radius:999px;font-size:12.5px;font-weight:700;display:inline-block}
  .setpill.decider{background:var(--yellow);color:var(--yellow-txt);border:1px solid #6b5c1f}
  .setpill.normal{color:var(--muted)}
  .odd-val{font-variant-numeric:tabular-nums;font-weight:650;color:var(--green)}
  .muted{color:var(--muted)}
  .id{color:var(--muted);font-variant-numeric:tabular-nums}
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
  <h1>Racket-Sports Liga-Pro Analyzer</h1>
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
  <div class="legend"><span class="swatch"></span> highlighted rows = match decided in 3 sets</div>

  <div class="tablewrap">
    <div id="status" class="status">Pick a league to begin.</div>
    <table id="table" style="display:none">
      <thead>
        <tr>
          <th>ID</th><th>Game</th><th id="metricHead">Total pts</th><th>Even / Odd</th>
          <th>Winning odd</th><th># Sets</th>
        </tr>
      </thead>
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

async function setSport(key){
  currentSport = key;
  currentMeta = sports.find(s=>s.key===key) || {};
  document.querySelectorAll('.sporttab').forEach(t=>
    t.classList.toggle('active', t.dataset.key===key));
  document.getElementById('metricHead').textContent = currentMeta.metric_label || 'Total pts';
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
  const metric = (currentMeta.metric_label || 'Total points').toLowerCase();
  document.getElementById('note').innerHTML =
    `<b>${currentMeta.metric_label || 'Total points'}</b> = sum of ${metric.includes('game')?'games':'points'} across every set (both players). ` +
    `<b>Even/Odd</b> = parity of that total. ` +
    `<b>Winning odd</b> = pre-match decimal odd of the player who actually won (Full-time market). ` +
    `<b># Sets</b> = number of sets played &mdash; rows highlighted yellow went to ${currentMeta.decider_sets || 3} sets (a decider). ` +
    (currentMeta.has_tournament_picker ? 'Pick a specific tournament above &mdash; ATP/WTA/etc. are categories containing many events. ' : '') +
    `Data is cached for 5 minutes.`;
}

function fmtOdd(o){ return (o===null||o===undefined) ? '<span class="muted">—</span>'
                                                     : '<span class="odd-val">'+o.toFixed(2)+'</span>'; }

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
  status.innerHTML = '<div class="spinner"></div>Fetching &amp; analysing finished matches… (first load spins up Chromium, can take ~20s)';

  try{
    let url = `/api/matches?sport=${currentSport}&league=${currentLeague}&limit=${limit}`;
    if(currentTournamentId) url += `&tournament_id=${currentTournamentId}`;
    const res = await fetch(url);
    const data = await res.json();
    if(data.error && (!data.rows || !data.rows.length)){
      status.innerHTML = '<span class="err">'+data.error+'</span>'; return;
    }
    const rows = data.rows || [];
    const deciderSets = data.decider_sets || 3;
    if(!rows.length){ status.innerHTML='No finished matches found.'; return; }

    const evenN = rows.filter(r=>r.even_odd==='Even').length;
    const oddN  = rows.length - evenN;
    const avgPts = (rows.reduce((s,r)=>s+r.total_points,0)/rows.length).toFixed(1);
    const odds = rows.map(r=>r.winning_odd).filter(v=>v!=null);
    const avgOdd = odds.length ? (odds.reduce((s,v)=>s+v,0)/odds.length).toFixed(2) : '—';
    const deciderN = rows.filter(r=>r.num_sets===deciderSets).length;
    cards.innerHTML = `
      <div class="card"><div class="k">Matches</div><div class="v">${rows.length}</div></div>
      <div class="card"><div class="k">Even / Odd</div><div class="v">${evenN} / ${oddN}</div></div>
      <div class="card"><div class="k">Avg ${data.metric_label.toLowerCase()}</div><div class="v">${avgPts}</div></div>
      <div class="card"><div class="k">Avg winning odd</div><div class="v">${avgOdd}</div></div>
      <div class="card"><div class="k">${deciderSets}-set deciders</div><div class="v">${deciderN}</div></div>`;

    const tb = document.getElementById('tbody'); tb.innerHTML='';
    rows.forEach(r=>{
      const isDecider = r.num_sets === deciderSets;
      const tr = document.createElement('tr');
      if(isDecider) tr.className = 'decider';
      tr.innerHTML = `
        <td class="id">${r.id}</td>
        <td class="game">${r.game}
            <div class="winner">${r.set_score} · won: ${r.winner}</div></td>
        <td><b>${r.total_points}</b></td>
        <td><span class="pill ${r.even_odd.toLowerCase()}">${r.even_odd}</span></td>
        <td>${fmtOdd(r.winning_odd)}</td>
        <td><span class="setpill ${isDecider?'decider':'normal'}">${r.num_sets}</span></td>`;
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
    app.run(debug=True, port=5000)