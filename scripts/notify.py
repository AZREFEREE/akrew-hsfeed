#!/usr/bin/env python3
"""Open a GitHub issue when azpreps365 changes a game at a watched school.
GitHub emails the repo owner about new issues, which is the 'loud' part.
Needs GITHUB_TOKEN and GITHUB_REPOSITORY (both are set inside GitHub Actions)."""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
from hsfeed import norm_name  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
LABEL = {"changed": "Changed", "cancelled": "Cancelled", "removed": "Gone from the schedule", "added": "New game"}
FIELD = {"date": "Date", "time": "Time", "location": "Site", "home": "Home", "away": "Away", "cancelled": "Cancelled"}


def watched(change, keys, all_schools):
    if all_schools:
        return True
    g = change["game"]
    names = [g.get("home"), g.get("away"), g.get("location")]
    fields = change.get("fields") or {}
    for k in ("home", "away", "location"):
        names += fields.get(k, [])
    return any(norm_name(n) in keys for n in names if n)


def fmt_time(t):
    if not t:
        return "TBA"
    h, m = map(int, t.split(":"))
    return "%d:%02d %s" % ((h % 12) or 12, m, "pm" if h >= 12 else "am")


def line(c):
    g = c["game"]
    who = "%s %s %s: %s @ %s" % (g.get("date"), fmt_time(g.get("time")),
                                 ("Boys" if g.get("gender") == "boys" else "Girls") + " " + (g.get("level") or ""),
                                 g.get("away"), g.get("home"))
    bits = []
    for k, (old, new) in (c.get("fields") or {}).items():
        if k == "time":
            old, new = fmt_time(old), fmt_time(new)
        bits.append("%s: ~~%s~~ → **%s**" % (FIELD.get(k, k), old, new))
    day = "https://azpreps365.com/scores/soccer-winter-%s/%s/%s" % (
        g.get("gender") or "boys", "jv" if g.get("level") == "JV" else "varsity", g.get("date"))
    return "| %s | %s | %s | [%s](%s) |" % (
        LABEL.get(c["kind"], c["kind"]), who, "<br>".join(bits) or g.get("location", ""), c["id"], day)


def main():
    path = os.path.join(ROOT, "data", "changes.json")
    if not os.path.exists(path):
        print("No changes file yet (first run).")
        return 0
    ch = json.load(open(path))
    w = json.load(open(os.path.join(ROOT, "watch.json")))
    keys = {norm_name(s) for s in w.get("schools", [])}
    hits = [c for c in ch.get("changes", []) if watched(c, keys, w.get("all_schools"))]
    print("%d changes, %d at watched schools" % (len(ch.get("changes", [])), len(hits)))
    if not hits:
        return 0
    hits.sort(key=lambda c: (c["game"].get("date") or "", c["game"].get("time") or ""))
    soon = sum(1 for c in hits if c["kind"] != "added")
    title = "Schedule changes: %d game%s at your schools (%s)" % (
        len(hits), "" if len(hits) == 1 else "s", ch["run_at"][:10])
    body = ["@AZREFEREE AZPreps365 changed %d game%s at your schools since the last check." % (
        len(hits), "" if len(hits) == 1 else "s"),
        "",
        "**%d** of them moved, were cancelled or dropped off the schedule, so check whether officials are already assigned "
        "and let them know. AKREW marks the assigned ones on the board." % soon if soon else
        "All of them are new games.",
        "", "| What | Game | Change | AZPreps |", "|---|---|---|---|"] + [line(c) for c in hits[:150]]
    if len(hits) > 150:
        body.append("\n…and %d more. Full list in data/changes.json." % (len(hits) - 150))
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not tok or not repo:
        print(title + "\n" + "\n".join(body))
        return 0
    req = urllib.request.Request("https://api.github.com/repos/%s/issues" % repo, method="POST",
                                 data=json.dumps({"title": title, "body": "\n".join(body),
                                                  "labels": ["schedule-change"]}).encode(),
                                 headers={"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json",
                                          "User-Agent": "akrew-hsfeed"})
    with urllib.request.urlopen(req) as r:
        print("Opened issue", json.loads(r.read())["html_url"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
