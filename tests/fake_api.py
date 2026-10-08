"""Faux API-Football v3 pour les tests (aucun réseau) : remplace requests.get pour api-sports.io.

- /fixtures?date=  : les matchs du jour fournis (format des vraies réponses) ;
- /fixtures?team=&last= : 12 matchs terminés synthétiques de l'équipe (ids et statistiques déterministes) ;
- /fixtures?ids=a-b : méthode groupée (matchs avec « statistics »), désactivable (batch=False : réponse sans statistiques) ;
- /fixtures/statistics?fixture= : statistiques d'un match (Shots on Goal, Total Shots, Corner Kicks, Fouls, Yellow/Red Cards).
`plan_error` imite l'erreur du forfait gratuit (HTTP 200 + « errors »). `null_stats` : ids d'équipes dont les statistiques sont null.
"""
import random
from datetime import datetime, timezone

BASE = "https://v3.football.api-sports.io"


class Resp:
    def __init__(self, data, status=200, headers=None):
        self._d, self.status_code, self.headers = data, status, headers or {}
        self.ok, self.text = status < 400, str(data)[:200]

    def json(self):
        return self._d


def raw_fixture(e, status=None):
    """Entrée allégée de stats._day_fixtures -> réponse brute de /fixtures (avec ligue et pays pour l'Afrique)."""
    lg = {"name": "Brasileirão", "country": "Brazil"}
    if e.get("lg"):
        name, _, country = e["lg"].rpartition(" (")
        lg = {"name": name or e["lg"], "country": country.rstrip(")") if name else "World"}
    st = status or e.get("status") or "NS"
    return {"fixture": {"id": e["id"], "timestamp": e["ts"], "status": {"short": st}}, "league": lg,
            "teams": {"home": {"id": e["home_id"], "name": e["home"]}, "away": {"id": e["away_id"], "name": e["away"]}},
            "goals": {"home": e.get("gh") if st in ("FT", "AET", "PEN") else None, "away": e.get("ga") if st in ("FT", "AET", "PEN") else None}}


def team_params(tid):
    r = random.Random(f"team{tid}")
    return {"goals": r.uniform(0.8, 1.9), "shots": r.uniform(9, 16), "sot_ratio": r.uniform(0.28, 0.42), "corners": r.uniform(3.5, 6.8),
            "fouls": r.uniform(11, 17), "yellow": r.uniform(1.4, 3.0)}


class FakeFootballAPI:
    def __init__(self, day_entries=(), batch=True, plan_error=False, null_teams=(), per_day_limit=100, first_ts=None):
        self.day = list(day_entries)
        self.by_id = {e["id"]: e for e in self.day}
        self.names = {e[k + "_id"]: e[k] for e in self.day for k in ("home", "away")}
        self.results = {}                                       # id de match du jour -> (buts dom, buts ext) : le match est terminé
        self.batch, self.plan_error, self.null_teams = batch, plan_error, set(null_teams)
        self.calls = []                                         # (chemin, paramètres)
        self.first_ts = first_ts or int(datetime(2026, 10, 7, 20, tzinfo=timezone.utc).timestamp())
        self.limit = per_day_limit
        self.status_rem = per_day_limit

    def new_day(self):
        """Le compteur de requêtes annoncé par l'API (en-têtes x-ratelimit-*) repart à zéro."""
        self.status_rem = self.limit

    def count(self, path=None, **match):
        return sum(1 for p, kw in self.calls if (path is None or p == path) and all(kw.get(k) == v for k, v in match.items()))

    # --- données synthétiques ---
    def history(self, tid, last):
        out = []
        for k in range(1, last + 1):
            fid = tid * 1000 + k
            home = k % 2 == 1
            opp = 500000 + tid * 20 + k
            r = random.Random(f"fx{fid}")
            gh, ga = r.randint(0, 3), r.randint(0, 2)
            out.append({"fixture": {"id": fid, "timestamp": self.first_ts - k * 5 * 86400, "status": {"short": "FT"}},
                        "league": {"name": "Série", "country": "Brazil"},
                        "teams": {"home": {"id": tid if home else opp, "name": self.names.get(tid, "T%d" % tid) if home else "Opp%d" % opp},
                                  "away": {"id": opp if home else tid, "name": "Opp%d" % opp if home else self.names.get(tid, "T%d" % tid)}},
                        "goals": {"home": gh, "away": ga}, "score": {"fulltime": {"home": gh, "away": ga}}})
        return out

    def stats_for(self, fid):
        """[{team, statistics}] du match `fid` (déterministe) ; None si l'équipe est dans null_teams."""
        if fid in self.by_id:                                   # match du jour (terminé via .results) : ses deux équipes
            e = self.by_id[fid]
            tid, teams = e["home_id"], [e["home_id"], e["away_id"]]
        else:                                                   # match de l'historique synthétique d'une équipe
            tid = fid // 1000
            home = fid % 1000 % 2 == 1
            opp = 500000 + tid * 20 + fid % 1000
            teams = [tid, opp] if home else [opp, tid]
        out = []
        for t in teams:
            r = random.Random(f"st{fid}-{t}")
            tp = team_params(t if t < 500000 else tid)
            shots = max(2, round(r.gauss(tp["shots"], 3)))
            vals = {"Total Shots": shots, "Shots on Goal": max(0, round(shots * tp["sot_ratio"] + r.gauss(0, 1))),
                    "Corner Kicks": max(0, round(r.gauss(tp["corners"], 2))), "Fouls": max(3, round(r.gauss(tp["fouls"], 3))),
                    "Yellow Cards": max(0, round(r.gauss(tp["yellow"], 1))), "Red Cards": None}
            if tid in self.null_teams:
                vals = {k: None for k in vals}
                vals["Corner Kicks"] = None
            out.append({"team": {"id": t, "name": "T%d" % t},
                        "statistics": [{"type": k, "value": v} for k, v in vals.items()]})
        return None if tid in self.null_teams else out

    # --- requests.get ---
    def __call__(self, url, params=None, headers=None, timeout=None, **kw):
        path, p = url.replace(BASE, ""), dict(params or {})
        self.calls.append((path, p))
        self.status_rem = max(0, self.status_rem - 1)
        hdr = {"x-ratelimit-requests-remaining": str(self.status_rem), "x-ratelimit-requests-limit": str(self.limit)}
        if self.plan_error:
            return Resp({"errors": {"plan": "Free plans do not have access to this season, try from 2021 to 2023."},
                         "response": []}, headers=hdr)
        ok = lambda resp: Resp({"errors": [], "response": resp}, headers=hdr)               # noqa: E731
        if path == "/fixtures" and "date" in p:
            return ok([raw_fixture(e | {"gh": self.results[e["id"]][0], "ga": self.results[e["id"]][1]}, "FT") if e["id"] in self.results
                       else raw_fixture(e, "NS") for e in self.day])
        if path == "/fixtures" and "team" in p:
            return ok(self.history(int(p["team"]), int(p.get("last", 12))))
        if path == "/fixtures" and "ids" in p:
            ids = [int(i) for i in p["ids"].split("-")]
            if not self.batch:
                return ok([{"fixture": {"id": i, "status": {"short": "FT"}}} for i in ids])
            return ok([{"fixture": {"id": i, "status": {"short": "FT"}}, "statistics": self.stats_for(i) or []} for i in ids])
        if path == "/fixtures/statistics":
            return ok(self.stats_for(int(p["fixture"])) or [])
        return ok([])
