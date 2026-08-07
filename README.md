# Racket-Sports Liga-Pro Analyzer

Fetches **finished** matches from SofaScore (via `sofascore-wrapper==1.1.1`)
for two sports and shows, per match:

`id · game · total points/games · even/odd · winning odd · number of sets`

Rows for matches decided in **3 sets** (a decider) are highlighted **yellow**
in both tables.

**Table Tennis:** Belarus Liga Pro, Czech Liga Pro, Czech TT Cup, Russia Liga
Pro, Ukraine Setka Cup — "total points" = sum of points across all sets.

**Tennis:** ATP, WTA, Challenger, ITF Men, ITF Women (added defaults, since
none were specified — edit `SPORTS["tennis"]["leagues"]` in `app.py` to swap
in specific tournaments) — "total games" = sum of games across all sets, same
columns otherwise. Note: the 3-set highlight assumes best-of-3 matches (true
for all the tours listed); it wouldn't mean "decider" for best-of-5 Grand
Slam matches.

## Why a Python backend (and not pure front-end)?

`sofascore-wrapper` is a **Python** library, and SofaScore now returns `403` on
plain REST calls — the wrapper works around this by driving a **headless
Chromium**. That can't run inside a browser page, so this app is a tiny Flask
server that does the fetching/analysis and serves the web page.

## Run

```bash
pip install -r requirements.txt
python -m playwright install chromium   # one-time (downloads Chromium)
python app.py
```

Open http://127.0.0.1:5000 — pick a sport (top tabs), then a league.

## Tournament picker (tennis)

After picking ATP/WTA/Challenger/ITF, the tournament dropdown greys out and
disables any tournament that has **no live match and nothing scheduled
today** (checked against SofaScore's live + today's-schedule feeds for
tennis, refreshed every 2 minutes). Disabled entries stay visible, labeled
"(not ongoing)", rather than being removed — a tournament between rounds
today but resuming tomorrow will re-enable on the next refresh. If both
lookups fail (network hiccup), the app fails open and leaves everything
enabled rather than guessing.

## Notes

- The URL slugs you gave for table tennis are resolved to SofaScore's numeric
  tournament IDs automatically by searching. Tennis leagues use the same
  mechanism but without a country filter (global tours). If a search ever
  fails, open `app.py` and set `tournament_id` for that league (the number in
  the SofaScore URL).
- **Winning odd** = decimal odd of the player who actually won, read from the
  Full-time market (`/event/{id}/odds/1/all`). Shows `—` if odds aren't
  available for that match.
- **Total points/games** = sum of every set's points (table tennis) or games
  (tennis) for both players; **Even/Odd** is that total's parity; **# Sets**
  = sets played; 3-set rows are highlighted yellow.
- Results are cached 5 min per (sport, league). First load is slow (Chromium
  starts up).
- Unofficial API — respect SofaScore's ToS and keep request rates reasonable.

