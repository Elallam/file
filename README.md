# Table-Tennis Liga-Pro Analyzer

Fetches **finished** table-tennis matches from SofaScore (via
`sofascore-wrapper==1.1.1`) for 5 competitions and shows, per match:

`id · game · total points · even/odd · winning odd · number of sets`

Competitions: Belarus Liga Pro, Czech Liga Pro, Czech TT Cup,
Russia Liga Pro, Ukraine Setka Cup.

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

Open http://127.0.0.1:5000 — pick a league from the tabs.

## Notes

- The URL slugs you gave (e.g. `/table-tennis/tournament/russia/liga-pro/`) are
  resolved to SofaScore's numeric tournament IDs automatically by searching.
  If a search ever fails, open `app.py` and set `tournament_id` for that league
  (the number in the SofaScore URL).
- **Winning odd** = decimal odd of the player who actually won, read from the
  Full-time market (`/event/{id}/odds/1/all`). Shows `—` if odds aren't
  available for that match.
- **Total points** = sum of every set's points for both players;
  **Even/Odd** is that total's parity; **# Sets** = sets played.
- Results are cached 5 min per league. First load is slow (Chromium starts up).
- Unofficial API — respect SofaScore's ToS and keep request rates reasonable.
