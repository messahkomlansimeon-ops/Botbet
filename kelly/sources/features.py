"""Chiffres d'avant-match issus des profils (football-data, Understat, FBref).

match_features(home, away, sport_key, referee=None) -> dict (clés absentes si donnée inconnue) :
  exp_corners[_home|_away], exp_cards (+ exp_cards_team, ref_cards), exp_fouls, exp_shots_home/away,
  exp_sot_home/away, exp_goals_home/away, xg_home/xga_home/xg_away/xga_away, exp_xg_home/away/total,
  n_home / n_away (matchs utilisés), sources (liste), home_team / away_team (noms retenus).
  Quand le modèle de force de la ligue (kelly/strength.py : Dixon-Coles pondéré, contexte, arbitre) existe et connaît les
  deux équipes, il REMPLACE les moyennes glissantes pour exp_goals*, exp_corners*, exp_cards*, exp_shots*, exp_sot*,
  exp_fouls* et ajoute : deep (la prévision complète : lh, la, rho, q, qs, stats, notes, why...), "modèle Kelly" dans sources.
player_features(name, team=None, sport_key=None) -> dict ou None : shots90, sot90, fouls90, fouled90,
  cards90, xg90, xa90, minutes...
Convention : l'espérance d'une équipe mêle sa production (vue domicile ou extérieur) et ce que
l'adversaire concède, à parts égales ; repli sur la vue « all » sous MIN_N matchs.
"""
from . import fbref, footballdata, names, understat

MIN_N = 3                    # matchs minimum dans une vue domicile / extérieur
REF_MIN_N = 3                # matchs minimum pour croire un arbitre
REF_WEIGHT = 0.3             # poids de l'arbitre dans les cartons / fautes attendus
_cache = {}


def reload():
    _cache.clear()
    try:
        from .. import strength
        strength.reload()
    except Exception:                       # noqa: BLE001
        pass


def _p(name, mod):
    if name not in _cache:
        try:
            _cache[name] = mod.profiles() or {}
        except Exception:
            _cache[name] = {}
    return _cache[name]


def _r(x):
    return round(x, 2)


def _view(team, venue):
    v = team.get(venue) or {}
    return v if v.get("n", 0) >= MIN_N else (team.get("all") or {})


def _mix(*vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _expect(hteam, ateam, stat):
    """(attendu domicile, attendu extérieur) pour une stat : production + concédé adverse."""
    h, a = _view(hteam, "home"), _view(ateam, "away")
    return (_mix(h.get(stat + "_for"), a.get(stat + "_against")),
            _mix(a.get(stat + "_for"), h.get(stat + "_against")))


def _put_pair(out, stat, key, hv, av, total=True):
    if hv is not None:
        out[f"exp_{key}_home"] = _r(hv)
    if av is not None:
        out[f"exp_{key}_away"] = _r(av)
    if total and hv is not None and av is not None:
        out[f"exp_{key}"] = _r(hv + av)


def _football_data(out, home, away, sport_key, referee):
    prof = _p("fd", footballdata)
    div = footballdata.SPORT_TO_DIV.get(sport_key)
    teams = {t: v for t, v in (prof.get("teams") or {}).items() if not div or v.get("div") == div}
    h, a = names.best_team(home, teams), names.best_team(away, teams)
    if not h or not a or h == a:
        return
    out["sources"].append("football-data")
    out["home_team"], out["away_team"] = h, a
    ht, at = teams[h], teams[a]
    out["n_home"], out["n_away"] = (ht.get("all") or {}).get("n", 0), (at.get("all") or {}).get("n", 0)
    for stat, key in (("corners", "corners"), ("goals", "goals"), ("shots", "shots"), ("sot", "sot"),
                      ("fouls", "fouls"), ("cards", "cards")):
        hv, av = _expect(ht, at, stat)
        _put_pair(out, stat, key, hv, av, total=key in ("corners", "cards", "fouls", "goals", "shots", "sot"))
    league = (prof.get("leagues") or {}).get(div or ht.get("div")) or {}
    refs = prof.get("referees") or {}
    rname = names.player_match(referee, refs) if referee and refs else None
    ref = refs.get(rname) if rname else None
    if "exp_cards" in out:
        out["exp_cards_team"] = out["exp_cards"]
    if ref and ref.get("n", 0) >= REF_MIN_N:
        out["referee"] = rname
        if "cards" in ref:
            out["ref_cards"] = ref["cards"]
            if "exp_cards" in out:
                out["exp_cards"] = _r((1 - REF_WEIGHT) * out["exp_cards_team"] + REF_WEIGHT * ref["cards"])
            if league.get("cards"):
                out["ref_cards_factor"] = _r(ref["cards"] / league["cards"])
        if "fouls" in ref:
            out["ref_fouls"] = ref["fouls"]
            if "exp_fouls" in out:
                out["exp_fouls"] = _r((1 - REF_WEIGHT) * out["exp_fouls"] + REF_WEIGHT * ref["fouls"])
    elif referee:
        out["referee_unknown"] = referee


def _understat(out, home, away, sport_key):
    lg = understat.SPORT_TO_LEAGUE.get(sport_key)
    data = ((_p("us", understat).get("leagues") or {}).get(lg) or {}).get("teams") or {}
    h, a = names.best_team(home, data), names.best_team(away, data)
    if not h or not a or h == a:
        return
    out["sources"].append("understat")
    hv, av = _view(data[h], "home"), _view(data[a], "away")
    for k, v in (("xg_home", hv.get("xg")), ("xga_home", hv.get("xga")), ("xg_away", av.get("xg")),
                 ("xga_away", av.get("xga")), ("ppda_home", hv.get("ppda")), ("ppda_away", av.get("ppda"))):
        if v is not None:
            out[k] = _r(v)
    eh, ea = _mix(hv.get("xg"), av.get("xga")), _mix(av.get("xg"), hv.get("xga"))
    if eh is not None and ea is not None:
        out["exp_xg_home"], out["exp_xg_away"], out["exp_xg_total"] = _r(eh), _r(ea), _r(eh + ea)


def _fbref(out, home, away, sport_key):
    """FBref : sert de repli pour tirs / fautes / cartons / corners absents de football-data."""
    squads = ((_p("fb", fbref).get("leagues") or {}).get(sport_key) or {}).get("squads") or {}
    h, a = names.best_team(home, squads), names.best_team(away, squads)
    if not h or not a or h == a:
        return
    out["sources"].append("fbref")
    for stat, key in (("shots", "shots"), ("sot", "sot"), ("fouls", "fouls"), ("cards", "cards"), ("corners", "corners")):
        if f"exp_{key}_home" in out or f"exp_{key}" in out:
            continue
        col = stat + "_pm"
        hv = _mix(squads[h].get("for", {}).get(col), squads[a].get("against", {}).get(col))
        av = _mix(squads[a].get("for", {}).get(col), squads[h].get("against", {}).get(col))
        _put_pair(out, stat, key, hv, av, total=key in ("cards", "fouls", "corners"))


def _deep(out, home, away, sport_key, referee):
    """Modèle de force (kelly/strength.py) : si la ligue a assez de matchs en cache, ses chiffres remplacent les moyennes
    glissantes (même clés exp_*) et la prévision complète est jointe sous « deep »."""
    from .. import strength
    div = footballdata.SPORT_TO_DIV.get(sport_key)
    model = strength.get(div) if div else None
    pred = strength.predict(model, out.get("home_team") or home, out.get("away_team") or away, referee=referee) if model else None
    if not pred:
        return
    st = pred["stats"]
    out["sources"].append("modèle Kelly")
    out["home_team"], out["away_team"] = pred["home"], pred["away"]
    out["n_home"], out["n_away"] = pred["n"]
    out["deep"] = pred
    for stat, key in (("goals", "goals"), ("corners", "corners"), ("cards", "cards"), ("shots", "shots"), ("sot", "sot"),
                      ("fouls", "fouls")):
        if stat in st:
            _put_pair(out, stat, key, st[stat]["h"], st[stat]["a"])
    if "cards" in st:
        out["exp_cards_team"] = _r((st["cards"]["h"] + st["cards"]["a"]) / st["cards"].get("ref", 1.0))
    r = pred["info"].get("referee")
    if r:
        out["referee"], out["ref_cards"], out["ref_cards_factor"] = r["name"], r["cards"], _r(r["factor"])
        out.pop("referee_unknown", None)
    rf = pred["info"].get("referee_fouls")
    if rf and "fouls" in st:
        out["ref_fouls"] = rf["avg"]


def match_features(home, away, sport_key, referee=None):
    """Chiffres d'avant-match fusionnés (voir l'en-tête du module). Dict avec au moins « sources »."""
    out = {"sources": []}
    for fn, args in ((_football_data, (home, away, sport_key, referee)), (_understat, (home, away, sport_key)),
                     (_fbref, (home, away, sport_key)), (_deep, (home, away, sport_key, referee))):
        try:
            fn(out, *args)
        except Exception:                       # une source cassée ne doit jamais bloquer les autres
            pass
    return out


def _player_pool(name, team, sport_key):
    """[(source, ligue, équipe, joueur, stats)] des joueurs plausibles."""
    pools = []
    us = _p("us", understat).get("leagues") or {}
    ulg = understat.SPORT_TO_LEAGUE.get(sport_key)
    for lg, d in us.items():
        if not sport_key or lg == ulg:
            pools.append(("understat", d.get("players") or {}))
    fb = _p("fb", fbref).get("leagues") or {}
    for lg, d in fb.items():
        if not sport_key or lg == sport_key:
            pools.append(("fbref", d.get("players") or {}))
    found = []
    for src, by_team in pools:
        tkeys = [names.best_team(team, by_team)] if team else list(by_team)
        for t in tkeys:
            if not t:
                continue
            pn = names.player_match(name, by_team[t])
            if pn:
                found.append((src, t, pn, by_team[t][pn]))
    return found


def player_features(name, team=None, sport_key=None):
    """Stats par 90 min d'un joueur (Understat : tirs, xG, xA ; FBref : tirs cadrés, fautes, cartons).
    None si inconnu ou ambigu (même nom dans deux équipes sans `team`)."""
    try:
        found = _player_pool(name, team, sport_key)
    except Exception:
        return None
    if not found:
        return None
    if len({(t, p) for _, t, p, _ in found}) > 1:
        return None                             # deux joueurs distincts : ambigu
    out = {"name": found[0][2], "team": found[0][1], "sources": []}
    for src, _, _, st in found:
        out["sources"].append(src)
        for k, v in st.items():
            if k == "min":
                out["minutes"] = max(out.get("minutes", 0), v)
            elif k == "min90":
                out["minutes"] = max(out.get("minutes", 0), round(v * 90))
            elif k == "shots90" and src == "fbref" and "shots90" in out:
                continue                         # Understat prime pour les tirs
            elif k != "pos":
                out.setdefault(k, v)
    return out


def _find(query, candidates):
    """('ok', nom) | ('many', [noms]) | None : nom proche de `query` (plusieurs noms aussi proches = ambigu),
    sinon nom qui contient tous ses mots."""
    candidates = list(candidates)
    good = sorted(((names.team_sim(query, c), c) for c in candidates), reverse=True)
    good = [(s, c) for s, c in good if s >= 0.8]
    if good:
        tied = [c for s, c in good if s >= good[0][0] - 0.05]
        return ("ok", tied[0]) if len(tied) == 1 else ("many", sorted(tied)[:8])
    q = set(names.norm(query).split())
    hits = sorted(c for c in candidates if q and q <= set(names.norm(c).split()))
    return ("ok", hits[0]) if len(hits) == 1 else (("many", hits[:8]) if hits else None)


def team_profile(query):
    """Profil d'une équipe pour /stats : {team, div, fd (vues all/home/away sur ~10 matchs), us (forme xG),
    updated}, {"many": [noms]} si plusieurs équipes correspondent, None si inconnue."""
    fdp = _p("fd", footballdata)
    teams = fdp.get("teams") or {}
    us = (_p("us", understat).get("leagues") or {})
    found = _find(query, teams)
    if found and found[0] == "many":
        return {"many": found[1]}
    if found:
        name = found[1]
        div = teams[name].get("div")
        ulg = understat.SPORT_TO_LEAGUE.get(footballdata.DIV_TO_SPORT.get(div))
        uteams = (us.get(ulg) or {}).get("teams") or {}
        un = names.best_team(name, uteams)
        return {"team": name, "div": div, "fd": teams[name], "us": uteams.get(un), "updated": fdp.get("updated")}
    for lg, d in us.items():                             # équipe absente de football-data (ligue non couverte)
        f2 = _find(query, d.get("teams") or {})
        if f2 and f2[0] == "ok":
            return {"team": f2[1], "div": lg, "fd": None, "us": d["teams"][f2[1]], "updated": None}
    return None
