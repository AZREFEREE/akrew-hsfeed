#!/usr/bin/env python3
"""AKREW high school feed: pulls Arizona high school soccer schools, schedules,
standings and rankings from azpreps365.com and writes them as JSON and CSV.

Standard library only, so it runs anywhere Python 3.9+ is installed
(GitHub Actions, a Mac, a Linux box).

  python3 scripts/hsfeed.py --out data            full run (about 30 minutes, polite crawl)
  python3 scripts/hsfeed.py --out data --quick    6A boys varsity only, for a smoke test
  python3 scripts/hsfeed.py --out data --only schools

azpreps365's robots.txt asks for 10 seconds between requests. We honour it.
"""
import argparse
import csv
import datetime as dt
import html
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

BASE = "https://azpreps365.com"
UA = "AKREW-hsfeed/1.0 (Arizona referee assigning; contact admin@azref.com)"
SPORTS = {"boys": "soccer-winter-boys", "girls": "soccer-winter-girls"}
CLASSES = ["6a", "5a", "4a", "3a", "2a"]
LEVELS = ["varsity", "jv", "fr-so"]
LEVEL_NAME = {"varsity": "Varsity", "jv": "JV", "fr-so": "Freshman/Sophomore"}
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

# --------------------------------------------------------------------------- tiny DOM

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = dict(attrs or {})
        self.children = []
        self.parent = parent

    # helpers ---------------------------------------------------------------
    @property
    def classes(self):
        return (self.attrs.get("class") or "").split()

    def has_class(self, *names):
        c = self.classes
        return all(n in c for n in names)

    def text(self):
        out = []
        self._text(out)
        return re.sub(r"\s+", " ", "".join(out)).strip()

    def _text(self, out):
        for ch in self.children:
            if isinstance(ch, str):
                out.append(ch)
            elif ch.tag not in ("script", "style"):
                ch._text(out)

    def own_text(self):
        return re.sub(r"\s+", " ", "".join(c for c in self.children if isinstance(c, str))).strip()

    def iter(self):
        for ch in self.children:
            if isinstance(ch, Node):
                yield ch
                yield from ch.iter()

    def find_all(self, tag=None, cls=None, attr=None):
        for n in self.iter():
            if tag and n.tag != tag:
                continue
            if cls and not n.has_class(*cls.split(".")):
                continue
            if attr and attr not in n.attrs:
                continue
            yield n

    def find(self, tag=None, cls=None, attr=None):
        return next(self.find_all(tag, cls, attr), None)

    def kids(self):
        return [c for c in self.children if isinstance(c, Node)]


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        n = Node(tag, [(k, v if v is not None else "") for k, v in attrs], self.cur)
        self.cur.children.append(n)
        if tag not in VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        n = Node(tag, [(k, v if v is not None else "") for k, v in attrs], self.cur)
        self.cur.children.append(n)

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def parse_html(text):
    b = _Builder()
    b.feed(text)
    b.close()
    return b.root


# --------------------------------------------------------------------------- helpers

def path_of(href):
    """'/teams/soccer-winter-boys/391-horizon/227118-varsity?x' -> path without host/query"""
    href = href or ""
    href = re.sub(r"^https?://[^/]+", "", href)
    return href.split("?")[0].split("#")[0]


def team_from_href(href):
    """Return (program_id, program_slug, team_id, level) from a team link."""
    p = path_of(href)
    m = re.search(r"/teams/[^/]+/(\d+)-([^/]+)/(\d+)-([^/]+)", p)
    if not m:
        return None, None, None, None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def season_start_year(today=None):
    today = today or dt.date.today()
    return today.year if today.month >= 7 else today.year - 1


def parse_day(day_text, mon_text, start_year):
    try:
        d = int(re.sub(r"\D", "", day_text))
        m = MONTHS[mon_text.strip().lower()[:3]]
    except (ValueError, KeyError):
        return None
    y = start_year if m >= 7 else start_year + 1
    try:
        return dt.date(y, m, d).isoformat()
    except ValueError:
        return None


def parse_time(t):
    t = (t or "").strip().lower().replace("\t", " ")
    m = re.match(r"(\d{1,2}):(\d{2})\s*([ap])\.?m", t)
    if not m:
        m2 = re.match(r"(\d{1,2})\s*([ap])\.?m", t)
        if not m2:
            return None
        h, mi, ap = int(m2.group(1)), 0, m2.group(2)
    else:
        h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3)
    if ap == "p" and h != 12:
        h += 12
    if ap == "a" and h == 12:
        h = 0
    return "%02d:%02d" % (h, mi)


def norm_name(s):
    """Loose school name key: 'Mountain View (Mesa) High School' -> 'mountain view mesa'."""
    s = html.unescape(s or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"\b(high school|high|school|hs|academy|acad|preparatory|prep|the)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- page parsers

def parse_schedule_page(text, start_year):
    """Contests on one schedule page, plus the highest page number linked."""
    root = parse_html(text)
    box = root.find(cls="contests")
    games, events = [], []
    if box is not None:
        for el in box.kids():
            cid = el.attrs.get("data-contest-id")
            if el.has_class("event-contest"):
                games.append(_contest(el, start_year))
            elif el.has_class("event-multi-team"):
                events.append(_multi(el, start_year))
            elif cid:
                # unknown wrapper: try both
                inner = el.find(cls="event-contest")
                if inner is not None:
                    games.append(_contest(inner, start_year))
    last = 1
    for a in root.find_all("a"):
        m = re.search(r"[?&]page=(\d+)", a.attrs.get("href", ""))
        if m:
            last = max(last, int(m.group(1)))
    return [g for g in games if g], [e for e in events if e], last


def _date_of(el, start_year):
    dnode = el.find(cls="date")
    if dnode is None:
        return None
    ems = [e.text() for e in dnode.find_all("em")]
    strong = dnode.find("strong")
    day = strong.text() if strong is not None else (ems[0] if ems else "")
    mon = next((e for e in ems if re.match(r"^[A-Za-z]{3}", e)), "")
    return parse_day(day, mon, start_year)


def _team(span):
    if span is None:
        return {"name": "", "mascot": "", "program": None, "team": None}
    a = span.find("a")
    mascot = ""
    name = ""
    prog = team = None
    if a is not None:
        m = a.find(cls="mascot")
        mascot = m.text() if m is not None else ""
        name = a.own_text() or a.text().replace(mascot, "").strip()
        prog, _slug, team, _lv = team_from_href(a.attrs.get("href"))
    else:
        name = span.text()
    return {"name": html.unescape(name).strip(), "mascot": mascot, "program": prog, "team": team}


def _contest(el, start_year):
    cid = el.attrs.get("data-contest-id")
    if not cid:
        return None
    tnode = el.find(cls="time")
    raw_time = tnode.text() if tnode is not None else ""
    loc = el.find(cls="location")
    tags = [t.text() for t in el.find_all(cls="tag")]
    res = el.find(cls="results")
    score = None
    if res is not None:
        sc = res.find(cls="scores")
        txt = sc.text() if sc is not None else res.text()
        if re.search(r"\d+\s*[-–]\s*\d+", txt):
            score = txt
    return {
        "id": cid,
        "date": _date_of(el, start_year),
        "time": parse_time(raw_time),
        "time_raw": re.sub(r"\s+", " ", raw_time).strip(),
        "away": _team(el.find(cls="away")),
        "home": _team(el.find(cls="home")),
        "location": loc.text() if loc is not None else "",
        "tags": [t for t in tags if t],
        "result": score,
    }


def _multi(el, start_year):
    cid = el.attrs.get("data-contest-id")
    name = el.find(cls="event-name")
    venue = el.find(cls="venue") or el.find(cls="location")
    return {
        "id": cid,
        "date": _date_of(el, start_year),
        "name": name.text() if name is not None else "",
        "location": venue.text() if venue is not None else "",
        "tags": [t.text() for t in el.find_all(cls="tag")],
    }


def parse_teams_page(text):
    """Programs grouped by region on /teams/<sport>/<class>."""
    root = parse_html(text)
    out = []
    for box in root.find_all(cls="box"):
        hdr = box.find(cls="box-header")
        if hdr is None:
            continue
        title = hdr.find(cls="title")
        progs = [p for p in box.find_all(cls="program") if p.has_class("media")]
        if title is None or not progs:
            continue
        region = title.text()
        for p in progs:
            nm = p.find(cls="program-name")
            ms = p.find(cls="mascot")
            teams = {}
            prog_id = slug = None
            for a in p.find_all("a"):
                pid, sl, tid, lv = team_from_href(a.attrs.get("href"))
                if pid:
                    prog_id, slug = pid, sl
                    teams[lv] = tid
            if not nm:
                continue
            out.append({"region": region, "name": nm.text(), "mascot": ms.text() if ms else "",
                        "program": prog_id, "slug": slug, "teams": teams})
    return out


def parse_rankings_page(text):
    root = parse_html(text)
    rows = []
    for sec in root.find_all(cls="box-section"):
        cols = [c for c in sec.kids() if c.has_class("column")]
        if len(cols) < 3:
            continue
        rank = re.sub(r"\D", "", cols[0].text())
        teamcol = next((c for c in cols if "data-team-id" in c.attrs), None)
        if not rank or teamcol is None:
            continue
        nm = teamcol.find(cls="program-name")
        a = teamcol.find("a", attr="href")
        prog, _s, team, _l = team_from_href(a.attrs.get("href") if a is not None else "")
        rest = [c for c in cols if c is not cols[0] and c is not teamcol]
        rating = None
        record = ""
        open_seed = None
        for c in rest:
            own = c.own_text()
            t = c.text()
            if rating is None and re.match(r"^-?\d+\.\d+", own or ""):
                rating = float(re.match(r"^-?\d+\.\d+", own).group(0))
                m = re.search(r"#\s*(\d+)", t)
                if m and "open" in t.lower():
                    open_seed = int(m.group(1))
            elif re.match(r"^\d+-\d+(-\d+)?$", t):
                record = t
        rows.append({"rank": int(rank), "name": nm.text() if nm else teamcol.text(),
                     "program": teamcol.attrs.get("data-program-id") or prog,
                     "team": teamcol.attrs.get("data-team-id") or team,
                     "rating": rating, "record": record, "open_seed": open_seed,
                     "open_qualifier": sec.has_class("is-open-qualifier")})
    return rows


def parse_standings_page(text):
    root = parse_html(text)
    out = []
    for box in root.find_all(cls="box"):
        hdr = box.find(cls="box-header")
        if hdr is None:
            continue
        heads = [h.text().lower() for h in hdr.find_all(cls="record-header")]
        if not heads:
            continue
        title = hdr.find(cls="title")
        region = title.text() if title is not None else ""
        for sec in box.find_all(cls="box-section"):
            cols = [c for c in sec.kids() if c.has_class("column")]
            if not cols:
                continue
            a = cols[0].find(cls="program-name") or cols[0].find("a")
            if a is None:
                continue
            prog, _s, team, _l = team_from_href(a.attrs.get("href"))
            recs = [c.text() for c in cols[1:1 + len(heads)]]
            row = {"region": region, "name": a.text(), "program": prog, "team": team}
            for h, r in zip(heads, recs):
                hk = h.replace(" ", "")
                key = ("region_rec" if hk.startswith("region") else "conf" if hk.startswith("conf")
                       else "overall" if hk.startswith(("ovr", "overall")) else "tb" if hk.startswith("tb")
                       or hk.startswith("tie") else hk[:12])
                m = re.match(r"(\d+-\d+(?:-\d+)?)\s*([\d.]+)?", r)
                row[key] = m.group(1) if m else r
                if m and m.group(2):
                    row[key + "_pct"] = float(m.group(2))
            extra = " ".join(c.text() for c in cols[1:])
            m = re.search(r"#\s*(\d+)", extra)
            if row.get("tb", "").startswith("#"):
                row["tb"] = ""
            if m:
                row["conf_rank"] = int(m.group(1))
            out.append(row)
    return out


def parse_team_page(text, start_year, me_program, me_name):
    """One team's season (a /teams/.../<id>-varsity page): every game with the result if played."""
    root = parse_html(text)
    out = []
    for el in root.find_all(cls="event-contest"):
        cid = el.attrs.get("data-contest-id")
        if not cid:
            continue
        hd = el.find(cls="home-designation")
        opp = _team(el.find(cls="opponent"))
        if not opp["name"]:
            continue
        is_home = hd is not None and (hd.has_class("is-home") or hd.text().lower().startswith("vs"))
        res = el.find(cls="results")
        sc = res.find(cls="scores") if res is not None else None
        mine = theirs = None
        if sc is not None:
            m = re.match(r"\s*(\d+)\s*-\s*(\d+)", sc.text())
            if m:
                mine, theirs = int(m.group(1)), int(m.group(2))
        loc = el.find(cls="location")
        tn = el.find(cls="time")
        me = {"name": me_name, "program": me_program}
        out.append({"id": cid, "date": _date_of(el, start_year), "time": parse_time(tn.text() if tn else ""),
                    "home": me if is_home else {"name": opp["name"], "program": opp["program"]},
                    "away": {"name": opp["name"], "program": opp["program"]} if is_home else me,
                    "home_score": mine if is_home else theirs, "away_score": theirs if is_home else mine,
                    "location": loc.text() if loc is not None else "",
                    "tags": [t.text() for t in el.find_all(cls="tag")]})
    return out


def crawl_history(cr, year, start_year, sports, classes, log=print):
    """Every varsity game (with scores) of one season, from each team's page."""
    games = {}
    for g, sport in sports.items():
        for c in classes:
            text = cr.get(("/%d" % year if year else "") + "/teams/%s/%s" % (sport, c))
            if text is None:
                continue
            for p in parse_teams_page(text):
                tid = p["teams"].get("varsity")
                if not tid:
                    continue
                path = ("/%d" % year if year else "") + "/teams/%s/%s-%s/%s-varsity" % (sport, p["program"], p["slug"], tid)
                page = cr.get(path)
                if page is None:
                    continue
                for x in parse_team_page(page, start_year, p["program"], p["name"]):
                    x.update(gender=g, level="Varsity")
                    old = games.get(x["id"])
                    if old is None or (old.get("home_score") is None and x.get("home_score") is not None):
                        games[x["id"]] = x
                log("  history %s %s %s %s: %d games so far" % (year or "now", g, c, p["name"], len(games)))
    return games


# --------------------------------------------------------------------------- crawler

class Crawler:
    def __init__(self, delay=10.0, log=print, cache_dir=None):
        self.delay = delay
        self.last = 0.0
        self.log = log
        self.count = 0
        self.errors = []
        self.cache_dir = cache_dir  # for tests / offline replays

    def get(self, path):
        url = path if path.startswith("http") else BASE + path
        if self.cache_dir:
            fn = os.path.join(self.cache_dir, re.sub(r"[^a-z0-9]+", "_", path.lower()).strip("_") + ".html")
            if os.path.exists(fn):
                with open(fn, encoding="utf-8") as f:
                    return f.read()
            return None
        wait = self.delay - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(3):
            self.last = time.time()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
                with urllib.request.urlopen(req, timeout=45) as r:
                    self.count += 1
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                msg = "HTTP %s on %s" % (e.code, path)
            except Exception as e:  # network blips
                msg = "%s on %s" % (e, path)
            self.log("  retry %d: %s" % (attempt + 1, msg))
            time.sleep(self.delay * (attempt + 2))
        self.errors.append(msg)
        return None


def crawl_schedules(cr, start_year, sports, classes, levels):
    games, events = {}, {}
    for g, sport in sports.items():
        for c in classes:
            for lv in levels:
                page, last = 1, 1
                while page <= last:
                    path = "/schedules/%s/%s/%s" % (sport, c, lv) + ("" if page == 1 else "?page=%d" % page)
                    text = cr.get(path)
                    if text is None:
                        break
                    gs, evs, last = parse_schedule_page(text, start_year)
                    for x in gs:
                        x["gender"] = g
                        x["level"] = LEVEL_NAME[lv]
                        prev = games.get(x["id"])
                        if prev:
                            prev["listed_in"] = sorted(set(prev["listed_in"] + [c.upper()]))
                        else:
                            x["listed_in"] = [c.upper()]
                            games[x["id"]] = x
                    for e in evs:
                        e["gender"] = g
                        e["level"] = LEVEL_NAME[lv]
                        events.setdefault(e["id"], e)
                    cr.log("  %s %s %s page %d/%d: %d games" % (g, c, lv, page, last, len(gs)))
                    page += 1
    return games, events


def crawl_teams(cr, sports, classes):
    progs = []
    for g, sport in sports.items():
        for c in classes:
            text = cr.get("/teams/%s/%s" % (sport, c))
            if text is None:
                continue
            for p in parse_teams_page(text):
                p["gender"] = g
                p["class"] = c.upper()
                progs.append(p)
            cr.log("  teams %s %s: %d programs" % (g, c, len([p for p in progs if p["class"] == c.upper() and p["gender"] == g])))
    return progs


def crawl_table(cr, kind, sports, classes, year=None):
    out = []
    for g, sport in sports.items():
        for c in classes:
            path = ("/%d" % year if year else "") + "/%s/%s/%s" % (kind, sport, c)
            text = cr.get(path)
            if text is None:
                continue
            rows = parse_rankings_page(text) if kind == "rankings" else parse_standings_page(text)
            for r in rows:
                r["gender"] = g
                r["class"] = c.upper()
            out.extend(rows)
            cr.log("  %s %s %s %s: %d rows" % (kind, year or "now", g, c, len(rows)))
    return out


# --------------------------------------------------------------------------- schools

def build_schools(programs, games):
    """One row per school (boys and girls programs merged by name)."""
    # most common home location per program
    home_loc = {}
    for gm in games.values():
        p = gm["home"].get("program")
        if p and gm.get("location"):
            home_loc.setdefault(p, {}).setdefault(gm["location"], 0)
            home_loc[p][gm["location"]] += 1
    schools = {}
    for p in programs:
        key = norm_name(p["name"])
        s = schools.setdefault(key, {"school": p["name"], "mascot": p["mascot"], "class": {},
                                     "region": {}, "programs": {}, "teams": {}, "venue": ""})
        s["class"][p["gender"]] = p["class"]
        s["region"][p["gender"]] = p["region"]
        s["programs"][p["gender"]] = p["program"]
        s["teams"][p["gender"]] = p["teams"]
        locs = home_loc.get(p["program"]) or {}
        if locs and not s["venue"]:
            s["venue"] = max(locs.items(), key=lambda kv: kv[1])[0]
    rows = []
    for key, s in sorted(schools.items(), key=lambda kv: kv[1]["school"].lower()):
        cls = s["class"].get("boys") or s["class"].get("girls") or ""
        reg = s["region"].get("boys") or s["region"].get("girls") or ""
        rows.append({
            "school": s["school"], "key": key, "venue": s["venue"], "mascot": s["mascot"],
            "class": cls, "region": reg,
            "class_girls": s["class"].get("girls", ""), "region_girls": s["region"].get("girls", ""),
            "class_boys": s["class"].get("boys", ""), "region_boys": s["region"].get("boys", ""),
            "program_boys": s["programs"].get("boys"), "program_girls": s["programs"].get("girls"),
            "teams": s["teams"],
        })
    return rows


# Rough metro lines, kept in step with AKREW's own "Sort by location" so the two agree.
def suggest_area(lat, lon):
    if lat is None or lon is None:
        return ""
    # well outside the Phoenix metro
    if lat < 32.75 or lat > 33.95 or lon < -112.75 or lon > -111.35:
        if 31.9 < lat < 32.55 and -111.2 < lon < -110.6:
            return "Tucson"
        return "Outlying"
    if lon <= -112.155:  # about 43rd Ave
        return "West"
    if lat >= 33.645:  # about the 101 loop on the north side
        return "North"
    if lon >= -111.926:  # about Scottsdale Rd
        return "East"
    if lat <= 33.37 and lon >= -112.06:  # Ahwatukee
        return "East"
    return "Central"


def geocode_schools(rows, cache_path, log=print, delay=1.1):
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
    done = 0
    for r in rows:
        q = r["venue"] or (r["school"] + " High School")
        if cache.get(q):
            hit = cache[q]
        else:
            hit = None
            base = re.sub(r"\s*\(.*?\)", "", r["school"]).replace("ALA - ", "American Leadership Academy ").strip()
            city = (re.search(r"\(([^)]+)\)", r["school"]) or [None, None])[1]
            tries = [q + ", Arizona", r["school"] + " High School, Arizona", base + " High School" + (", " + city if city else "") + ", Arizona",
                     base + (", " + city if city else "") + ", Arizona"]
            for query in dict.fromkeys(tries):
                url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
                    {"q": query, "format": "json", "limit": 1, "countrycodes": "us",
                     "viewbox": "-114.9,37.1,-109.0,31.3", "bounded": 1})
                try:
                    time.sleep(delay)
                    req = urllib.request.Request(url, headers={"User-Agent": UA})
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        js = json.loads(resp.read().decode("utf-8"))
                    if js:
                        hit = {"lat": float(js[0]["lat"]), "lon": float(js[0]["lon"])}
                        break
                except Exception as e:
                    log("  geocode failed for %s: %s" % (query, e))
            if hit:
                cache[q] = hit  # misses are tried again next run
            done += 1
        r["lat"] = hit["lat"] if hit else None
        r["lon"] = hit["lon"] if hit else None
        r["area_suggested"] = suggest_area(r["lat"], r["lon"])
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, sort_keys=True)
    log("  geocoded %d new venues (%d cached)" % (done, len(cache)))


# --------------------------------------------------------------------------- changes

WATCH_FIELDS = ("date", "time", "location", "home", "away", "cancelled")


def game_sig(g):
    return {"date": g.get("date"), "time": g.get("time"), "location": g.get("location", ""),
            "home": g["home"]["name"], "away": g["away"]["name"],
            "cancelled": any(re.search(r"cancel|postpon|forfeit", t, re.I) for t in g.get("tags", []))}


def diff_games(old, new, today_iso):
    """old/new: id -> game. Returns list of change records (future games only)."""
    changes = []
    for gid, g in new.items():
        o = old.get(gid)
        if o is None:
            if (g.get("date") or "") >= today_iso:
                changes.append({"id": gid, "kind": "added", "game": brief(g)})
            continue
        a, b = game_sig(o), game_sig(g)
        diffs = {k: [a[k], b[k]] for k in WATCH_FIELDS if a[k] != b[k]}
        if diffs and max(a["date"] or "", b["date"] or "") >= today_iso:
            changes.append({"id": gid, "kind": "cancelled" if diffs.get("cancelled") == [False, True] else "changed",
                            "fields": diffs, "game": brief(g)})
    for gid, o in old.items():
        if gid not in new and (o.get("date") or "") >= today_iso:
            changes.append({"id": gid, "kind": "removed", "game": brief(o)})
    return changes


def brief(g):
    return {"date": g.get("date"), "time": g.get("time"), "gender": g.get("gender"), "level": g.get("level"),
            "home": g["home"]["name"], "away": g["away"]["name"], "location": g.get("location", ""),
            "tags": g.get("tags", [])}


# --------------------------------------------------------------------------- writers

def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def ampm(t):
    if not t:
        return "TBA"
    h, m = map(int, t.split(":"))
    return "%d:%02d%s" % ((h % 12) or 12, m, "pm" if h >= 12 else "am")


def schedule_csv_rows(games):
    """Same columns as the AIA schedule export, so AKREW's loader takes it as is."""
    rows = []
    for g in sorted(games.values(), key=lambda x: (x.get("date") or "", x.get("time") or "", x["home"]["name"])):
        if not g.get("date"):
            continue
        d = dt.date.fromisoformat(g["date"])
        sport = ("Boys" if g["gender"] == "boys" else "Girls") + " Winter Soccer"
        typ = "Region" if any(t.lower() == "region" for t in g.get("tags", [])) else (g.get("tags") or ["Regular"])[0]
        rows.append([g["id"], d.strftime("%b ") + str(d.day), ampm(g.get("time")), sport, g["level"], "",
                     "Regular Season", typ, g["away"]["name"], g["home"]["name"], g.get("location", ""),
                     "/".join(g.get("listed_in", [])), g["date"]])
    return rows


# --------------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data")
    ap.add_argument("--quick", action="store_true", help="6A boys varsity only (smoke test)")
    ap.add_argument("--only", choices=["schools", "schedules", "tables"], help="run one part")
    ap.add_argument("--history", choices=["last", "now", "both"],
                    help="pull past results from every varsity team page (weekly job, about an hour)")
    ap.add_argument("--delay", type=float, default=10.0, help="seconds between requests (robots.txt: 10)")
    ap.add_argument("--no-geo", action="store_true", help="skip map lookups for schools")
    ap.add_argument("--replay", help="read pages from this folder instead of the web (tests)")
    ap.add_argument("--today", help="pretend today is YYYY-MM-DD (tests)")
    a = ap.parse_args(argv)

    os.makedirs(a.out, exist_ok=True)
    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    start_year = season_start_year(today)
    t0 = time.time()
    cr = Crawler(delay=0 if a.replay else a.delay, cache_dir=a.replay)
    sports = {"boys": SPORTS["boys"]} if a.quick else SPORTS
    classes = ["6a"] if a.quick else CLASSES
    levels = ["varsity"] if a.quick else LEVELS
    status = {"run_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "season": "%d-%d" % (start_year, start_year + 1), "quick": a.quick, "source": BASE}

    games_path = os.path.join(a.out, "games.json")
    old_games = {}
    if os.path.exists(games_path):
        with open(games_path, encoding="utf-8") as f:
            old_games = {g["id"]: g for g in json.load(f).get("games", [])}

    if a.history:
        seasons = {"last": [start_year - 1], "now": [start_year], "both": [start_year - 1, start_year]}[a.history]
        for yr in seasons:
            print("History %d-%d ..." % (yr, yr + 1))
            hist = crawl_history(cr, None if yr == start_year else yr, yr, sports, classes)
            write_json(os.path.join(a.out, "history_%d.json" % yr),
                       {"season": "%d-%d" % (yr, yr + 1), "updated": status["run_at"],
                        "games": sorted(hist.values(), key=lambda x: (x.get("date") or "", x["id"]))})
            status["history_%d" % yr] = len(hist)
        status["requests"] = cr.count
        status["errors"] = cr.errors
        write_json(os.path.join(a.out, "status_history.json"), status)
        print(json.dumps(status, indent=1))
        return 0

    games = events = None
    if a.only in (None, "schedules", "schools"):
        print("Schedules ...")
        games, events = crawl_schedules(cr, start_year, sports, classes, levels)
        status["games"] = len(games)
        status["tournament_days"] = len(events)

    if a.only in (None, "schedules") and games is not None:
        if not games and old_games:
            raise SystemExit("No games came back; keeping yesterday's files untouched.")
        if old_games and (cr.errors or len(games) < 0.8 * len(old_games)):
            # a page failed or the site came back short: keep yesterday's copy of anything missing,
            # rather than reporting a pile of games as "removed"
            kept = 0
            for gid, og in old_games.items():
                if gid not in games:
                    games[gid] = dict(og, stale=True)
                    kept += 1
            status["kept_from_last_run"] = kept
        changes = diff_games(old_games, games, today.isoformat()) if old_games else []
        status["changes"] = len(changes)
        write_json(games_path, {"season": status["season"], "updated": status["run_at"],
                                "games": sorted(games.values(), key=lambda g: (g.get("date") or "", g["id"])),
                                "tournaments": sorted(events.values(), key=lambda e: (e.get("date") or "", e["id"]))})
        write_csv(os.path.join(a.out, "schedule.csv"),
                  ["ID", "Date", "Time", "Sport", "Level", "Endorsement", "Season", "Type", "Visitor", "Home",
                   "Venue", "Class", "ISO Date"], schedule_csv_rows(games))
        write_json(os.path.join(a.out, "changes.json"), {"run_at": status["run_at"], "changes": changes})
        if changes:
            with open(os.path.join(a.out, "changes_log.jsonl"), "a", encoding="utf-8") as f:
                for c in changes:
                    f.write(json.dumps(dict(c, seen=status["run_at"]), ensure_ascii=False) + "\n")

    if a.only in (None, "schools"):
        print("Teams ...")
        programs = crawl_teams(cr, sports, classes)
        rows = build_schools(programs, games or old_games)
        if not a.no_geo and not a.replay:
            print("Map lookups ...")
            geocode_schools(rows, os.path.join(a.out, "geo_cache.json"))
        else:
            for r in rows:
                r.setdefault("lat", None)
                r.setdefault("lon", None)
                r.setdefault("area_suggested", suggest_area(r["lat"], r["lon"]))
        status["schools"] = len(rows)
        write_json(os.path.join(a.out, "schools.json"), {"updated": status["run_at"], "schools": rows})
        write_csv(os.path.join(a.out, "schools.csv"),
                  ["School", "Venue", "Class", "Region", "Girls class", "Girls region", "Suggested area",
                   "Mascot", "Latitude", "Longitude"],
                  [[r["school"], r["venue"], r["class"], r["region"], r["class_girls"], r["region_girls"],
                    r["area_suggested"], r["mascot"], r["lat"] or "", r["lon"] or ""] for r in rows])

    if a.only in (None, "tables"):
        print("Standings and rankings ...")
        standings = crawl_table(cr, "standings", sports, classes)
        rk_classes = classes + ([] if a.quick else ["open"])  # Open = the statewide top bracket
        rankings = crawl_table(cr, "rankings", sports, rk_classes)
        prior = crawl_table(cr, "rankings", sports, rk_classes, year=start_year - 1)
        status["standings"] = len(standings)
        status["rankings"] = len(rankings)
        status["rankings_last_season"] = len(prior)
        write_json(os.path.join(a.out, "standings.json"), {"updated": status["run_at"], "rows": standings})
        write_json(os.path.join(a.out, "rankings.json"),
                   {"updated": status["run_at"], "season": status["season"], "rows": rankings,
                    "last_season": prior})

    status["requests"] = cr.count
    status["errors"] = cr.errors
    status["minutes"] = round((time.time() - t0) / 60, 1)
    write_json(os.path.join(a.out, "status.json"), status)
    print(json.dumps(status, indent=1))
    return 0 if not cr.errors else 1


if __name__ == "__main__":
    sys.exit(main())
