from . import betexplorer, fbref, fmmatch, footballdata, fotmob, names, understat
from .. import apistats, catalogue, config as C

MIN_N = 3
REF_MIN_N = 3
REF_WEIGHT = 0.3
_cache = {}


def reload():
    _cache.clear()
    try:
        from .. import strength
        strength.reload()
    except Exception:
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
    if not div and not sport_key.startswith("soccer_uefa_"):       # ligue sans division football-data : jamais les 600 autres équipes
        return                                                      # (« Atlético-GO » n'est pas « Ath Madrid », « Juventude » pas « Juventus »)
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


def _fotmob(out, home, away, sport_key):
    """xG attendu d'après les classements FotMob (repli d'Understat : couverture mondiale des ligues mappées)."""
    lgid = fotmob.SPORT_TO_LEAGUE.get(sport_key)
    if lgid is None:
        return
    data = ((_p("fm", fotmob).get("leagues") or {}).get(sport_key) or {}).get("teams") or {}
    h, a = names.best_team(home, data), names.best_team(away, data)
    if not h or not a or h == a:
        return
    hv, av = data[h], data[a]
    # Le classement xG de FotMob donne des TOTAUX de saison : par match = total / matchs joués.
    per = lambda t, k: t[k] / t["n"] if t.get(k) is not None and (t.get("n") or 0) >= MIN_N else None      # noqa: E731
    eh, ea = _mix(per(hv, "xg"), per(av, "xga")), _mix(per(av, "xg"), per(hv, "xga"))
    if eh is None or ea is None:
        return
    new = {k: _r(v) for k, v in (("exp_xg_home", eh), ("exp_xg_away", ea), ("exp_xg_total", eh + ea))
           if k not in out}
    if not new:
        return
    out.update(new)
    out["sources"].append("FotMob")


def _fbref(out, home, away, sport_key):
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


def _aps_candidates(prof, sport_key):
    same = {n: p for n, p in prof.items() if sport_key in p.get("sk", [])}
    if same or sport_key.startswith("africa"):
        return same
    region = catalogue.region(sport_key)
    if region == catalogue.OTHER:
        return {}
    return {n: p for n, p in prof.items() if any(catalogue.region(k) == region for k in p.get("sk", []))}


def aps_quality(n):
    """Qualité (0 à 1) d'un profil API-Football de `n` matchs : 0 sous APISTATS_MIN_N (non servi) ; « utilisable » de MIN_N à GOOD_N - 1
    (0,20 à 0,30 : poids du modèle atténué, pronostics seuls) ; « bon » dès GOOD_N (0,40 à 6 matchs, 0,60 à 10) : 0,40 est aussi le seuil
    des combinés à cote juste (C.FAIR_COMBO_MIN_Q)."""
    if n < C.APISTATS_MIN_N:
        return 0.0
    if n < C.APISTATS_GOOD_N:
        return round(0.20 + 0.05 * (n - C.APISTATS_MIN_N), 2)
    return round(min(0.6, 0.40 + 0.05 * (n - C.APISTATS_GOOD_N)), 2)


def _apistats(out, home, away, sport_key):
    prof = _aps_candidates(_p("aps", apistats).get("teams") or {}, sport_key)
    h, a = names.best_team(home, prof), names.best_team(away, prof)
    if not h or not a or prof[h] is prof[a]:
        return
    ht, at = prof[h], prof[a]
    tmp = {}
    for stat, key in (("corners", "corners"), ("goals", "goals"), ("shots", "shots"), ("sot", "sot"), ("fouls", "fouls"),
                      ("cards", "cards")):
        hv, av = _expect(ht, at, stat)
        _put_pair(tmp, stat, key, hv, av, total=True)
    new = {k: v for k, v in tmp.items() if k not in out}
    if not new:
        return
    out.update(new)
    if "exp_cards" in new:
        out.setdefault("exp_cards_team", out["exp_cards"])
    n_h, n_a = ht["all"]["n"], at["all"]["n"]
    out["sources"].append("API-Football")
    q = aps_quality(min(n_h, n_a))
    if out.get("aps_keys"):                           # déjà des stats FotMob : on garde la qualité la plus prudente
        q, (n_h, n_a) = min(q, out.get("aps_q", q)), min((n_h, n_a), tuple(out.get("aps_n") or (n_h, n_a)))
    out["aps_keys"] = sorted(set(out.get("aps_keys") or ()) | set(new))
    out["aps_n"] = (n_h, n_a)
    out["aps_q"] = q
    out.setdefault("home_team", h)
    out.setdefault("away_team", a)
    out.setdefault("n_home", n_h)
    out.setdefault("n_away", n_a)


FM_STATS = ("corners", "goals", "shots", "sot", "fouls", "cards", "throws", "offsides")


def _fm_candidates(prof, sport_key):
    """Équipes FotMob de la ligue (toutes les ligues africaines pour un match « africa », nom de ligue inconnu)."""
    if sport_key == "africa":
        return {n: p for n, p in prof.items() if any(k.startswith("africa") for k in p.get("sk", []))}
    return {n: p for n, p in prof.items() if sport_key in p.get("sk", [])}


def _fmmatch(out, home, away, sport_key):
    """Stats de match FotMob accumulées (fmmatch) : complètent ce que les sources précédentes n'ont pas, et sont seules à donner
    touches et hors-jeu. Comme les profils API-Football, leurs clés vont dans aps_keys (qualité selon le nombre de matchs)."""
    prof = _fm_candidates(_p("fmm", fmmatch).get("teams") or {}, sport_key)
    h, a = names.best_team(home, prof), names.best_team(away, prof)
    if not h or not a or prof[h] is prof[a]:
        return
    ht, at = prof[h], prof[a]
    tmp = {}
    for stat in FM_STATS:
        hv, av = _expect(ht, at, stat)
        _put_pair(tmp, stat, stat, hv, av, total=True)
    new = {k: v for k, v in tmp.items() if k not in out}
    if not new:
        return
    out.update(new)
    if "exp_cards" in new:
        out.setdefault("exp_cards_team", out["exp_cards"])
    n_h, n_a = ht["all"]["n"], at["all"]["n"]
    out["sources"].append("FotMob (matchs)")
    out["aps_keys"] = sorted(set(out.get("aps_keys") or ()) | set(new))
    out["aps_n"] = (n_h, n_a)
    out["aps_q"] = aps_quality(min(n_h, n_a))
    out.setdefault("home_team", h)
    out.setdefault("away_team", a)
    out.setdefault("n_home", n_h)
    out.setdefault("n_away", n_a)


def match_features(home, away, sport_key, referee=None):
    out = {"sources": []}
    if sport_key.startswith("africa"):
        steps = [(_fmmatch, (home, away, sport_key)), (_apistats, (home, away, sport_key))]
    else:
        steps = [(_football_data, (home, away, sport_key, referee)), (_understat, (home, away, sport_key)),
                 (_fotmob, (home, away, sport_key)), (_fbref, (home, away, sport_key)),
                 (_deep, (home, away, sport_key, referee)), (_fmmatch, (home, away, sport_key)), (_apistats, (home, away, sport_key))]
    for fn, args in steps:
        try:
            fn(out, *args)
        except Exception:
            pass
    return out


def _player_pool(name, team, sport_key):
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
    try:
        found = _player_pool(name, team, sport_key)
    except Exception:
        return None
    if not found:
        return None
    if len({(t, p) for _, t, p, _ in found}) > 1:
        return None
    out = {"name": found[0][2], "team": found[0][1], "sources": []}
    for src, _, _, st in found:
        out["sources"].append(src)
        for k, v in st.items():
            if k == "min":
                out["minutes"] = max(out.get("minutes", 0), v)
            elif k == "min90":
                out["minutes"] = max(out.get("minutes", 0), round(v * 90))
            elif k == "shots90" and src == "fbref" and "shots90" in out:
                continue
            elif k != "pos":
                out.setdefault(k, v)
    return out


def _find(query, candidates):
    candidates = list(candidates)
    good = sorted(((names.team_sim(query, c), c) for c in candidates), reverse=True)
    good = [(s, c) for s, c in good if s >= 0.8]
    if good:
        tied = [c for s, c in good if s >= good[0][0] - 0.05]
        return ("ok", tied[0]) if len(tied) == 1 else ("many", sorted(tied)[:8])
    q = set(names.norm(query).split())
    hits = sorted(c for c in candidates if q and q <= set(names.norm(c).split()))
    return ("ok", hits[0]) if len(hits) == 1 else (("many", hits[:8]) if hits else None)


def _aps_block(name, query=None):
    teams = _p("aps", apistats).get("teams") or {}
    n = names.best_team(name, teams)
    return teams.get(n) if n else None


def _fm_block(name):
    teams = _p("fmm", fmmatch).get("teams") or {}
    n = names.best_team(name, teams)
    return teams.get(n) if n else None


def team_profile(query):
    fdp = _p("fd", footballdata)
    teams = fdp.get("teams") or {}
    us = (_p("us", understat).get("leagues") or {})
    aps = _p("aps", apistats).get("teams") or {}
    found = _find(query, teams)
    if found and found[0] == "many":
        return {"many": found[1]}
    if found:
        name = found[1]
        div = teams[name].get("div")
        ulg = understat.SPORT_TO_LEAGUE.get(footballdata.DIV_TO_SPORT.get(div))
        uteams = (us.get(ulg) or {}).get("teams") or {}
        un = names.best_team(name, uteams)
        return {"team": name, "div": div, "fd": teams[name], "us": uteams.get(un), "aps": _aps_block(name),
                "fm": _fm_block(name), "updated": fdp.get("updated")}
    for lg, d in us.items():
        f2 = _find(query, d.get("teams") or {})
        if f2 and f2[0] == "ok":
            return {"team": f2[1], "div": lg, "fd": None, "us": d["teams"][f2[1]], "aps": _aps_block(f2[1]), "fm": _fm_block(f2[1]),
                    "updated": None}
    uniq = {p["nm"]: p for p in aps.values() if p.get("nm")}
    f3 = _find(query, uniq)
    if f3 and f3[0] == "many":
        return {"many": f3[1]}
    if f3:
        p = uniq[f3[1]]
        return {"team": f3[1], "div": p.get("lg"), "fd": None, "us": None, "aps": p, "fm": _fm_block(f3[1]), "updated": p.get("d")}
    fm = _p("fmm", fmmatch).get("teams") or {}
    f4 = _find(query, fm)
    if f4 and f4[0] == "many":
        return {"many": f4[1]}
    if f4:
        p = fm[f4[1]]
        return {"team": f4[1], "div": None, "fd": None, "us": None, "aps": None, "fm": p, "updated": p.get("d")}
    return None
