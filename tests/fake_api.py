"""Faux API-Football v3 pour les tests (aucun réseau) : remplace requests.get pour api-sports.io. Il imite le FORFAIT GRATUIT tel que
les vraies sondes du 8 octobre 2026 l'ont mesuré :

- /fixtures?date=  : seulement hier, aujourd'hui et demain (self.today), sinon l'erreur de plan exacte
  {"plan": "Free plans do not have access to this date, try from 2026-10-07 to 2026-10-09."} ;
- /fixtures?...&last= : {"plan": "Free plans do not have access to the Last parameter."} ;
- /fixtures?ids=      : {"plan": "Free plans do not have access to the Ids parameter."} ;
- /fixtures/statistics?fixture= : statistiques d'un match (Shots on Goal, Total Shots, Corner Kicks, Fouls, Yellow/Red Cards) ;
- /fixtures/players : vide (et compté : les profils ne doivent jamais le demander).

Toute demande refusée est comptée dans self.refused : un test qui vérifie `api.refused == []` prouve que Kelly n'a jamais envoyé de
paramètre ou de date interdits. `plan_error` : tout est refusé (HTTP 200 + « errors »). `stats_plan_error` : seules les statistiques
sont refusées. `null_teams` : ids d'équipes dont les statistiques sont null.
"""
import random
from datetime import date, datetime, timedelta, timezone

BASE = "https://v3.football.api-sports.io"
FINISHED = ("FT", "AET", "PEN")


class Resp:
    def __init__(self, data, status=200, headers=None):
        self._d, self.status_code, self.headers = data, status, headers or {}
        self.ok, self.text = status < 400, str(data)[:200]

    def json(self):
        return self._d


def raw_fixture(e, status=None, now=None):
    """Entrée allégée de stats._day_fixtures -> réponse brute de /fixtures. La ligue vient de e["league"] = (nom, pays) ; sinon de
    e["lg"] ("Ligue (Pays)", Afrique) ; sinon une ligue sans intérêt pour les profils."""
    if e.get("league"):
        lg = {"name": e["league"][0], "country": e["league"][1]}
    elif e.get("lg"):
        name, _, country = e["lg"].rpartition(" (")
        lg = {"name": name or e["lg"], "country": country.rstrip(")") if name else "World"}
    else:
        lg = {"name": "Brasileirão", "country": "Brazil"}
    st = status or e.get("status") or "NS"
    if e.get("sim") and now is not None and e["ts"] > now:       # match synthétique pas encore joué à l'heure du faux serveur (08:00 UTC)
        st = "NS"
    done = st in FINISHED
    return {"fixture": {"id": e["id"], "timestamp": e["ts"], "status": {"short": st}}, "league": lg,
            "teams": {"home": {"id": e["home_id"], "name": e["home"]}, "away": {"id": e["away_id"], "name": e["away"]}},
            "goals": {"home": e.get("gh") if done else None, "away": e.get("ga") if done else None}}


def team_params(tid):
    r = random.Random(f"team{tid}")
    return {"goals": r.uniform(0.8, 1.9), "shots": r.uniform(9, 16), "sot_ratio": r.uniform(0.28, 0.42), "corners": r.uniform(3.5, 6.8),
            "fouls": r.uniform(11, 17), "yellow": r.uniform(1.4, 3.0)}


def match(fid, home, away, ts, league=("Serie A", "Brazil"), status="FT", goals=None):
    """Un match synthétique {id, ts, status, home, home_id, away, away_id, gh, ga, league} ; home / away : (id, nom)."""
    r = random.Random(f"fx{fid}")
    gh, ga = goals if goals else (r.randint(0, 3), r.randint(0, 2))
    return {"id": fid, "ts": ts, "status": status, "home": home[1], "home_id": home[0], "away": away[1], "away_id": away[0],
            "gh": gh, "ga": ga, "league": league, "sim": True}


def ts_of(day, hour=21):
    return int(datetime.fromisoformat(f"{day}T{hour:02d}:00:00+00:00").timestamp())


class FakeFootballAPI:
    def __init__(self, day_entries=(), plan_error=False, stats_plan_error=False, null_teams=(), per_day_limit=100, today="2026-10-08"):
        self.today = str(today)
        self.days = {self.today: list(day_entries)}             # jour AAAA-MM-JJ -> matchs (format allégé de stats._entry + league)
        self.by_id = {e["id"]: e for e in day_entries}
        self.results = {}                                       # id de match du jour -> (buts dom, buts ext) : le match est terminé
        self.plan_error, self.stats_plan_error, self.null_teams = plan_error, stats_plan_error, set(null_teams)
        self.calls = []                                         # (chemin, paramètres)
        self.refused = []                                       # demandes que le forfait gratuit aurait refusées (ne doit rester vide)
        self.limit = per_day_limit
        self.status_rem = per_day_limit

    # --- calendrier ---
    def add_day(self, day, entries):
        """Matchs du jour `day` (les listes d'hier / de demain, jamais plus loin pour être servies)."""
        self.days.setdefault(str(day), [])
        self.days[str(day)] += list(entries)
        self.by_id.update({e["id"]: e for e in entries})

    def new_day(self, today=None):
        """Le jour suivant (ou `today`) : le compteur de requêtes annoncé par l'API (en-têtes x-ratelimit-*) repart à zéro."""
        self.today = str(today or (date.fromisoformat(self.today) + timedelta(days=1)))
        self.status_rem = self.limit

    def window(self):
        t = date.fromisoformat(self.today)
        return str(t - timedelta(days=1)), str(t + timedelta(days=1))

    def count(self, path=None, **match):
        return sum(1 for p, kw in self.calls if (path is None or p == path) and all(kw.get(k) == v for k, v in match.items()))

    # --- données synthétiques ---
    def past_matches(self, tid, n, league=("Serie A", "Brazil"), name=None, days_ago=5, first=1):
        """`n` matchs terminés (ids tid*1000+k, k à partir de `first`) de l'équipe `tid` contre des adversaires synthétiques : de quoi
        garnir un historique."""
        out = []
        for k in range(first, first + n):
            home = k % 2 == 1
            opp = (500000 + tid * 20 + k, "Opp%d" % (500000 + tid * 20 + k))
            me = (tid, name or "T%d" % tid)
            e = match(tid * 1000 + k, me if home else opp, opp if home else me, ts_of(self.today, 20) - k * days_ago * 86400, league)
            out.append(e)
            self.by_id[e["id"]] = e
        return out

    def stats_for(self, fid):
        """[{team, statistics}] du match `fid` (déterministe) ; None si l'API n'a rien (match inconnu, équipe dans null_teams)."""
        e = self.by_id.get(fid)
        if not e or {e["home_id"], e["away_id"]} & self.null_teams:
            return None
        tid, out = e["home_id"], []
        for t in (e["home_id"], e["away_id"]):
            r = random.Random(f"st{fid}-{t}")
            tp = team_params(t if t < 500000 else tid)
            shots = max(2, round(r.gauss(tp["shots"], 3)))
            vals = {"Total Shots": shots, "Shots on Goal": max(0, round(shots * tp["sot_ratio"] + r.gauss(0, 1))),
                    "Corner Kicks": max(0, round(r.gauss(tp["corners"], 2))), "Fouls": max(3, round(r.gauss(tp["fouls"], 3))),
                    "Yellow Cards": max(0, round(r.gauss(tp["yellow"], 1))), "Red Cards": None}
            out.append({"team": {"id": t, "name": "T%d" % t}, "statistics": [{"type": k, "value": v} for k, v in vals.items()]})
        return out

    # --- requests.get ---
    def __call__(self, url, params=None, headers=None, timeout=None, **kw):
        path, p = url.replace(BASE, ""), dict(params or {})
        self.calls.append((path, p))
        self.status_rem = max(0, self.status_rem - 1)
        hdr = {"x-ratelimit-requests-remaining": str(self.status_rem), "x-ratelimit-requests-limit": str(self.limit)}
        ok = lambda resp: Resp({"errors": [], "response": resp}, headers=hdr)               # noqa: E731
        no = lambda errors: (self.refused.append((path, p)), Resp({"errors": errors, "response": []}, headers=hdr))[1]    # noqa: E731
        if self.plan_error:
            return no({"plan": "Free plans do not have access to this season, try from 2021 to 2023."})
        if path == "/fixtures" and "last" in p:
            return no({"plan": "Free plans do not have access to the Last parameter."})
        if path == "/fixtures" and "ids" in p:
            return no({"plan": "Free plans do not have access to the Ids parameter."})
        if path == "/fixtures" and "date" in p:
            lo, hi = self.window()
            if not lo <= p["date"] <= hi:
                return no({"plan": f"Free plans do not have access to this date, try from {lo} to {hi}."})
            now = datetime.fromisoformat(self.today + "T08:00:00+00:00").timestamp()
            return ok([raw_fixture(e | {"gh": self.results[e["id"]][0], "ga": self.results[e["id"]][1]}, "FT") if e["id"] in self.results
                       else raw_fixture(e, now=now) for e in self.days.get(p["date"], [])])
        if path == "/fixtures/statistics":
            if self.stats_plan_error:
                return no({"plan": "Free plans do not have access to the Statistics endpoint."})
            return ok(self.stats_for(int(p["fixture"])) or [])
        return ok([])


def seed_history(api, team, n, league=("Serie A", "Brazil"), lk="soccer_brazil_campeonato", ln="Brasileirão", first=1):
    """Donne `n` matchs d'historique à `team` (id, nom) en passant par apistats.record, comme le fera le passage du matin (les adversaires
    synthétiques de ces matchs ne sont pas suivis). Les profils déjà chargés par sources.features doivent être rechargés ensuite."""
    from kelly import apistats, stats
    d = apistats.load()
    known = set(d["teams"]) | {str(team[0])}
    for e in api.past_matches(team[0], n, league, name=team[1], days_ago=3, first=first):
        apistats.record(d, dict(e, lk=lk, ln=ln), stats._entry_from(api.stats_for(e["id"])))
    d["teams"] = {k: v for k, v in d["teams"].items() if k in known}
    apistats.save(d)
