# akrew-hsfeed

Arizona high school winter soccer (boys and girls, 6A–2A, varsity / JV / freshman-sophomore), pulled from [azpreps365.com](https://azpreps365.com) twice a day for AKREW.

It runs by itself on GitHub at about **5:23 AM and 1:23 PM Arizona time**, plus a results-history pull early every Sunday. Each run takes about 25 minutes, because azpreps365 asks for 10 seconds between page requests and we honor that. To run it now: **Actions → Daily AZPreps pull → Run workflow**.

## What's in `data/`

| File | What it is |
|---|---|
| `schedule.csv` | Every game, with the same columns as the AIA schedule export (ID, Date, Time, Sport, Level, …, Visitor, Home, Venue). AKREW can load it like an AIA file. |
| `games.json` | The same games with more detail: azpreps contest ID, ISO date, 24-hour time, team and program IDs, Region/Freedom/Invitational tag, and the class listing(s) it appeared in. It also lists the tournaments (multi-team invitationals). |
| `schools.csv` / `schools.json` | Every school: class (6A–2A) and region for boys and girls, mascot, home venue, map location, and a suggested area (East / Central / North / West / Tucson / Outlying). |
| `standings.json` | This season's region standings: region, conference and overall records, plus conference rank. |
| `rankings.json` | This season's AIA rankings (rating, record, Open seed) plus **last season's final rankings**, so schools can be weighted before this season's rankings come out. |
| `history_<year>.json` | Every varsity game of a season with the final score, from each team's page (weekly, Sunday). AKREW uses it for matchups: when two teams last met and whether it was close. |
| `changes.json` | What changed since the previous run: moved, cancelled, removed or new games, future dates only. |
| `changes_log.jsonl` | Every change ever seen, one per line. |
| `status.json` | When the last run happened, the counts, and any errors. |

## The loud part

When a game at one of the schools in `watch.json` changes, the run opens an **issue** in this repo titled "Schedule changes: N games at your schools". GitHub emails you about it. AKREW also reads `changes.json` and flags games that already have officials assigned.

To watch different schools, edit `watch.json` (names match loosely: "Chandler" matches "Chandler High School"). Set `"all_schools": true` to hear about every change statewide.

## Safety nets

- If a page fails or the site returns far fewer games than last time, games that went missing are kept from the previous run and marked `stale`, rather than reported as removed.
- If no games come back at all, the run stops and leaves yesterday's files untouched.

## Running it on a computer

```
python3 scripts/hsfeed.py --out data            # everything (about 25 minutes)
python3 scripts/hsfeed.py --out /tmp/q --quick  # 6A boys varsity only
python3 -m unittest discover -s tests           # parser tests
```

Standard-library Python 3.9+, no installs needed.
