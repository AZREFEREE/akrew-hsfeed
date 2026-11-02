"""Parser tests against page structure copied from azpreps365.com (Nov 2026).
Run: python3 -m unittest discover -s tests"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import hsfeed as H  # noqa: E402

FX = os.path.join(HERE, "fixtures")


def fx(name):
    with open(os.path.join(FX, name), encoding="utf-8") as f:
        return f.read()


class Parsers(unittest.TestCase):
    def test_schedule(self):
        games, events, last = H.parse_schedule_page(fx("schedules_soccer_winter_boys_6a_varsity.html"), 2026)
        self.assertEqual(last, 2)
        self.assertEqual(len(games), 2)
        g = games[0]
        self.assertEqual(g["id"], "2463820")
        self.assertEqual(g["date"], "2026-11-30")
        self.assertEqual(g["time"], "18:00")
        self.assertEqual(g["away"]["name"], "Horizon")
        self.assertEqual(g["away"]["program"], "391")
        self.assertEqual(g["home"]["name"], "AZ College Prep")
        self.assertEqual(g["home"]["team"], "226176")
        self.assertEqual(g["location"], "Arizona College Prep")
        self.assertEqual(g["tags"], ["Freedom"])
        self.assertEqual(games[1]["date"], "2027-01-28")  # January rolls into next year
        self.assertEqual(games[1]["time"], "19:00")
        self.assertEqual(events[0]["name"], "PXU Copa")
        self.assertEqual(events[0]["location"], "Maryvale High School")
        self.assertEqual(events[0]["date"], "2026-12-03")

    def test_tba_time(self):
        games, _, _ = H.parse_schedule_page(fx("schedules_soccer_winter_boys_6a_varsity_page_2.html"), 2026)
        self.assertIsNone(games[0]["time"])
        self.assertEqual(games[0]["date"], "2027-02-03")

    def test_teams(self):
        rows = H.parse_teams_page(fx("teams_soccer_winter_boys_6a.html"))
        self.assertEqual([r["name"] for r in rows], ["Desert Vista", "Chandler", "Brophy Prep"])
        self.assertEqual(rows[0]["region"], "Premier")
        self.assertEqual(rows[0]["program"], "2035")
        self.assertEqual(rows[0]["teams"], {"varsity": "221660", "jv": "221662"})
        self.assertEqual(rows[2]["region"], "Central")

    def test_rankings(self):
        rows = H.parse_rankings_page(fx("2025_rankings_soccer_winter_boys_6a.html"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["rank"], 1)
        self.assertEqual(rows[0]["name"], "Sunnyside")
        self.assertAlmostEqual(rows[0]["rating"], 20.1909)
        self.assertEqual(rows[0]["record"], "11-0-1")
        self.assertEqual(rows[0]["open_seed"], 1)
        self.assertEqual(rows[0]["program"], "387")
        self.assertAlmostEqual(rows[1]["rating"], -0.7385)
        self.assertFalse(rows[1]["open_qualifier"])

    def test_standings(self):
        rows = H.parse_standings_page(fx("standings_soccer_winter_boys_6a.html"))
        self.assertEqual(len(rows), 2)
        r = rows[0]
        self.assertEqual(r["region"], "6A Central")
        self.assertEqual(r["name"], "Brophy Prep")
        self.assertEqual(r["region_rec"], "4-0")
        self.assertEqual(r["conf"], "11-0")
        self.assertEqual(r["overall"], "11-1")
        self.assertAlmostEqual(r["overall_pct"], 0.917)
        self.assertEqual(rows[1]["conf_rank"], 16)

    def test_times(self):
        self.assertEqual(H.parse_time("12:30 pm"), "12:30")
        self.assertEqual(H.parse_time("12:05am"), "00:05")
        self.assertEqual(H.parse_time("3:15\tPM"), "15:15")
        self.assertIsNone(H.parse_time("TBA"))

    def test_areas(self):
        self.assertEqual(H.suggest_area(33.30, -111.84), "East")      # Chandler
        self.assertEqual(H.suggest_area(33.31, -111.98), "East")      # Ahwatukee
        self.assertEqual(H.suggest_area(33.49, -112.07), "Central")   # central Phoenix
        self.assertEqual(H.suggest_area(33.53, -112.27), "West")      # Glendale west
        self.assertEqual(H.suggest_area(33.70, -112.10), "North")     # north Phoenix
        self.assertEqual(H.suggest_area(32.22, -110.97), "Tucson")
        self.assertEqual(H.suggest_area(35.19, -111.65), "Outlying")  # Flagstaff


class Diff(unittest.TestCase):
    def g(self, **kw):
        base = {"id": "1", "date": "2026-12-01", "time": "18:00", "location": "A HS",
                "home": {"name": "A"}, "away": {"name": "B"}, "tags": ["Region"]}
        base.update(kw)
        return base

    def test_changes(self):
        old = {"1": self.g(), "2": self.g(id="2"), "3": self.g(id="3", date="2026-10-01")}
        new = {"1": self.g(time="16:00"), "4": self.g(id="4"), "3": self.g(id="3", date="2026-10-01", time="1")}
        ch = {c["id"]: c for c in H.diff_games(old, new, "2026-11-02")}
        self.assertEqual(ch["1"]["kind"], "changed")
        self.assertEqual(ch["1"]["fields"], {"time": ["18:00", "16:00"]})
        self.assertEqual(ch["2"]["kind"], "removed")
        self.assertEqual(ch["4"]["kind"], "added")
        self.assertNotIn("3", ch)  # past games are ignored

    def test_cancel_tag(self):
        old = {"1": self.g()}
        new = {"1": self.g(tags=["Region", "Cancelled"])}
        self.assertEqual(H.diff_games(old, new, "2026-11-02")[0]["kind"], "cancelled")


class EndToEnd(unittest.TestCase):
    def test_replay_run(self):
        out = tempfile.mkdtemp()
        try:
            H.main(["--out", out, "--quick", "--replay", FX, "--today", "2026-11-02"])
            games = json.load(open(os.path.join(out, "games.json")))
            ids = sorted(g["id"] for g in games["games"])
            self.assertEqual(ids, ["2463820", "2465734", "2470001"])  # duplicate across pages kept once
            schools = json.load(open(os.path.join(out, "schools.json")))["schools"]
            dv = next(s for s in schools if s["school"] == "Desert Vista")
            self.assertEqual(dv["venue"], "Desert Vista High School")
            self.assertEqual(dv["class"], "6A")
            self.assertEqual(dv["region"], "Premier")
            rk = json.load(open(os.path.join(out, "rankings.json")))
            self.assertEqual(rk["last_season"][0]["name"], "Sunnyside")
            st = json.load(open(os.path.join(out, "standings.json")))
            self.assertEqual(st["rows"][0]["name"], "Brophy Prep")
            csvt = open(os.path.join(out, "schedule.csv")).read().splitlines()
            self.assertTrue(csvt[0].startswith("ID,Date,Time,Sport,Level"))
            self.assertIn("2463820,Nov 30,6:00pm,Boys Winter Soccer,Varsity", csvt[1])
            # second run with a moved game -> change recorded
            p = os.path.join(out, "games.json")
            js = json.load(open(p))
            js["games"][0]["time"] = "17:00"
            json.dump(js, open(p, "w"))
            H.main(["--out", out, "--quick", "--replay", FX, "--today", "2026-11-02", "--only", "schedules"])
            ch = json.load(open(os.path.join(out, "changes.json")))["changes"]
            self.assertEqual(len(ch), 1)
            self.assertEqual(ch[0]["fields"]["time"], ["17:00", "18:00"])
        finally:
            shutil.rmtree(out)


if __name__ == "__main__":
    unittest.main()
