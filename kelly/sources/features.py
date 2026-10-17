import time

from . import betexplorer, fbref, fmmatch, footballdata, fotmob, names, s365, understat
from .. import apistats, catalogue, config as C, stats, store

MIN_N = 3
REF_MIN_N = 3
REF_WEIGHT = 0.3
_cache = {}                          # nom court de source -> profils (dict) ; les tests y posent directement des profils fictifs
_sig = {}                            # nom court -> (id des profils chargés, signature du document dans la base au chargement)
_checked = {}                        # nom court -> instant de la dernière interrogation du document
RECHECK_SECONDS = 1.0                # pas plus d'une interrogation par seconde et par source (match_features appelle _p des dizaines de fois par match)
_clock = time.monotonic


def reload():
    _cache.clear()
    _sig.clear()
    _checked.clear()
    try:
        from .. import strength
        strength.reload()
    except Exception:
        pass


def _doc(mod):
    """Nom du document de profils (table `docs` de la base) qu'une source lit, ou None."""
    return getattr(mod, "PROFILE_FILE", None) or getattr(mod, "FILE", None)


def _p(name, mod):
    """Profils d'une source, gardés en mémoire MAIS relus dès que leur document change dans la base (store.version, comme strength.tuning) :
    le bot Telegram qui tourne en continu (--serve) voit ainsi les profils que le passage du matin ou le relevé horaire viennent d'écrire,
    sans attendre un redémarrage. Des profils posés à la main dans _cache (tests) ne sont jamais écrasés."""
    have, now = _cache.get(name), _clock()
    if have is not None and now - _checked.get(name, float("-inf")) < RECHECK_SECONDS:
        return have
    doc = _doc(mod)
    sig = store.version(doc) if doc else None
    _checked[name] = now
    if have is not None:
        loaded = _sig.get(name)
        if loaded is None or loaded[0] != id(have) or loaded[1] == sig:
            return have
    try:
        data = mod.profiles() or {}
    except Exception:                                   # noqa: BLE001 - une source en panne ne bloque pas les autres
        data = None
    if data is None or (doc and doc in store._unreadable):
        # lecture ratée (exception, base illisible) : JAMAIS gardée en mémoire — la source reviendrait vide jusqu'à la prochaine écriture du document ;
        # on rend les derniers bons profils s'il y en a, et on retente au prochain contrôle (RECHECK_SECONDS)
        return have if have is not None else {}
    _cache[name] = data
    _sig[name] = (id(data), sig)
    return data


def _r(x):
    return round(x, 2)


def _ref_tokens(name):
    return [t for t in names.norm(stats.clean_referee(name) or "").split() if t not in ("jr", "sr")]


def _covers(small, big):
    """Les mots de `small` se retrouvent tous dans `big` (un mot d'une lettre = l'initiale d'un mot de `big`), dont au moins un mot entier."""
    used, whole = set(), 0
    for t in small:
        hit = next((i for i, b in enumerate(big) if i not in used and ((len(t) == 1 and len(b) > 1 and b[0] == t) or b == t)), None)
        if hit is None:
            return False
        used.add(hit)
        whole += len(t) > 1
    return whole >= 1


def match_referee(name, candidates):
    """Nom d'arbitre de `candidates` (football-data : « M Oliver », « Gil Manzano », « Felix Zwayer ») qui désigne l'arbitre `name` (API-Football : « Michael
    Oliver, England », « Jesús Gil Manzano ») : mots égaux sans accents ni pays, ou initiale + nom de famille. JAMAIS une devinette : deux candidats possibles,
    une initiale qui ne correspond pas, ou un nom d'un seul mot qui n'est pas exactement le nom d'un candidat -> None."""
    q = _ref_tokens(name)
    if not q:
        return None
    cands = {c: _ref_tokens(c) for c in candidates}
    exact = sorted({c for c, t in cands.items() if t == q})
    if exact:
        return exact[0] if len(exact) == 1 else None
    if len(q) < 2:
        return None
    hits = sorted({c for c, t in cands.items() if len(t) >= 2 and (_covers(t, q) or _covers(q, t))})
    return hits[0] if len(hits) == 1 else None


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
                      ("fouls", "fouls"), ("cards", "cards"), ("yellow", "yellow")):
        hv, av = _expect(ht, at, stat)
        _put_pair(out, stat, key, hv, av, total=key in ("corners", "cards", "fouls", "goals", "shots", "sot", "yellow"))
    league = (prof.get("leagues") or {}).get(div or ht.get("div")) or {}
    refs = prof.get("referees") or {}
    rname = match_referee(referee, refs) if referee and refs else None
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
                if "exp_yellow" in out:          # les jaunes suivent l'arbitre comme les cartons (les rouges sont rares) : même mélange, sur le rapport à la ligue
                    out["exp_yellow"] = _r(out["exp_yellow"] * ((1 - REF_WEIGHT) + REF_WEIGHT * ref["cards"] / league["cards"]))
        if "fouls" in ref:
            out["ref_fouls"] = ref["fouls"]
            if league.get("fouls"):
                out["ref_fouls_factor"] = _r(ref["fouls"] / league["fouls"])
            if "exp_fouls" in out:
                before = out["exp_fouls"]
                out["exp_fouls"] = _r((1 - REF_WEIGHT) * before + REF_WEIGHT * ref["fouls"])
                if before:                       # le même effet sur les fautes de chaque équipe (marchés « fautes d'une équipe »)
                    for side in ("home", "away"):
                        if f"exp_fouls_{side}" in out:
                            out[f"exp_fouls_{side}"] = _r(out[f"exp_fouls_{side}"] * out["exp_fouls"] / before)
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
        out["xg_src"] = "understat"


def _fotmob(out, home, away, sport_key):
    """xG attendu d'après les classements FotMob (repli d'Understat : couverture mondiale des ligues mappées). Championnats vus par
    1xBet seulement (fotmob.EXTRA_LEAGUES : Roumanie, Colombie...) : aussi les buts attendus du classement (_standings_goals)."""
    lgid = fotmob.SPORT_TO_LEAGUE.get(sport_key) or fotmob.EXTRA_LEAGUES.get(sport_key)
    if lgid is None:
        return
    data = ((_p("fm", fotmob).get("leagues") or {}).get(sport_key) or {}).get("teams") or {}
    h, a = names.best_team(home, data), names.best_team(away, data)
    if not h or not a or h == a:
        return
    hv, av = data[h], data[a]
    extra = sport_key not in fotmob.SPORT_TO_LEAGUE
    matches = ((_p("fm", fotmob).get("leagues") or {}).get(sport_key) or {}).get("matches") or []
    if extra:
        _standings_goals(out, h, a, hv, av, matches, league=data)
    # Le classement xG de FotMob donne des TOTAUX de saison : par match = total / matchs joués.
    per = lambda t, k: t[k] / t["n"] if t.get(k) is not None and (t.get("n") or 0) >= MIN_N else None      # noqa: E731
    eh, ea = _mix(per(hv, "xg"), per(av, "xga")), _mix(per(av, "xg"), per(hv, "xga"))
    if eh is None or ea is None:
        return
    # classement saison (domicile + extérieur mêlés, donc symétrique) : l'avantage du terrain du championnat y est remis pour TOUTES les ligues
    # (Incheon - Pohang donnait le même xG aux deux équipes), seulement ici où aucune source meilleure (Understat) n'a fourni d'xG
    hs = home_share(matches)
    eh, ea = eh * 2 * hs, ea * 2 * (1 - hs)
    new = {k: _r(v) for k, v in (("exp_xg_home", eh), ("exp_xg_away", ea), ("exp_xg_total", eh + ea))
           if k not in out}
    if not new:
        return
    out.update(new)
    out["sources"].append("FotMob")
    if "exp_xg_home" in new:
        out["xg_src"] = "fotmob-classement"           # totaux de saison : _fmmatch les remplace par les xG des matchs récents s'il en a


STANDINGS_PRIOR_N = 6                 # poids (en matchs fictifs) de la moyenne de la ligue dans les buts par match d'une équipe du classement
STANDINGS_Q = (0.30, 0.01, 0.50)      # qualité des buts tirés du classement : 0,30 + 0,01 par match joué (équipe la moins fournie), 0,50 au plus
HOME_SHARE = (0.555, 60)              # part des buts marqués à domicile (moyenne mondiale) et son poids en matchs fictifs


def home_share(matches):
    """Part des buts marqués par l'équipe à domicile dans le championnat (derniers résultats FotMob), rapprochée de la moyenne mondiale
    HOME_SHARE (30 matchs ne suffisent pas à la mesurer seule) : 0,555 = l'équipe qui reçoit marque 1,25 fois plus."""
    prior, k = HOME_SHARE
    done = [m for m in matches if m.get("hg") is not None and m.get("ag") is not None]
    hg, tot = sum(m["hg"] for m in done), sum(m["hg"] + m["ag"] for m in done)
    n = len(done)
    share = hg / tot if tot else prior
    return (n * share + k * prior) / (n + k)


def _league_rate(league, field):
    """Buts marqués (« gf ») ou encaissés (« ga ») par équipe et par match dans tout le classement `league` ; None s'il est vide."""
    tot, n = 0.0, 0.0
    for t in (league or {}).values():
        if (t.get("n") or 0) > 0 and t.get(field) is not None:
            tot, n = tot + t[field], n + t["n"]
    return tot / n if n else None


def _shrunk(t, field, mean):
    """Buts par match d'une équipe du classement, rapprochés de la moyenne de la ligue (poids STANDINGS_PRIOR_N matchs) : en début de
    saison, 3 matchs ne font pas une attaque. Sans moyenne (classement inconnu), le taux brut."""
    raw = t[field] / t["n"]
    if mean is None:
        return raw
    return (t["n"] * raw + STANDINGS_PRIOR_N * mean) / (t["n"] + STANDINGS_PRIOR_N)


def _standings_goals(out, h, a, hv, av, matches=(), league=None):
    """Buts attendus d'un match d'après le classement FotMob (buts marqués et encaissés par match, mêlés comme _expect) : seule
    source de buts des championnats vus par 1xBet tant que les stats de match FotMob (fmmatch) ne sont pas accumulées. Qualité
    modeste (STANDINGS_Q, dans aps_q : le modèle simple la reprend) ; rien sous MIN_N matchs joués. `league` : tout le classement,
    dont la moyenne de buts rapproche les taux de chaque équipe (_shrunk). out["std_n"] : matchs joués des deux équipes (la source des
    buts n'est pas celle des autres statistiques de aps_keys)."""
    n = min(hv.get("n") or 0, av.get("n") or 0)
    if n < MIN_N or "exp_goals_home" in out:
        return
    hs = home_share(matches)                  # le classement mêle domicile et extérieur : avantage du terrain du championnat
    mf, ma = _league_rate(league, "gf"), _league_rate(league, "ga")
    eh = _mix(_shrunk(hv, "gf", mf), _shrunk(av, "ga", ma)) * 2 * hs
    ea = _mix(_shrunk(av, "gf", mf), _shrunk(hv, "ga", ma)) * 2 * (1 - hs)
    lo, step, hi = STANDINGS_Q
    out.update(exp_goals_home=_r(eh), exp_goals_away=_r(ea), exp_goals_total=_r(eh + ea))
    out["sources"].append("FotMob (classement)")
    out["aps_keys"] = sorted(set(out.get("aps_keys") or ()) | {"exp_goals_home", "exp_goals_away", "exp_goals_total"})
    out["aps_q"] = round(min(hi, lo + step * n), 2)
    out["std_n"] = (int(hv["n"]), int(av["n"]))
    out.setdefault("home_team", h)
    out.setdefault("away_team", a)
    out.setdefault("n_home", int(hv["n"]))
    out.setdefault("n_away", int(av["n"]))


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


def _deep(out, home, away, sport_key, referee, on=None):
    from .. import strength
    div = footballdata.SPORT_TO_DIV.get(sport_key)
    model = strength.get(div) if div else None
    pred = strength.predict(model, out.get("home_team") or home, out.get("away_team") or away, strength.match_day(on), referee) if model else None
    if pred:
        fill_deep(out, pred)


def fill_deep(out, pred):
    """Chiffres d'avant-match tirés de la prévision `pred` du modèle approfondi (strength.predict) : aussi utilisé par le rejeu de la saison passée
    (kelly/replay.py), qui prédit chaque match avec le modèle ajusté sur les seuls matchs d'avant."""
    out.setdefault("sources", [])
    st = pred["stats"]
    out["sources"].append("modèle Kelly")
    out["home_team"], out["away_team"] = pred["home"], pred["away"]
    out["n_home"], out["n_away"] = pred["n"]
    out["deep"] = pred
    for stat, key in (("goals", "goals"), ("corners", "corners"), ("cards", "cards"), ("shots", "shots"), ("sot", "sot"),
                      ("fouls", "fouls"), ("yellow", "yellow")):
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
        if rf.get("factor") is not None:
            out["ref_fouls_factor"] = _r(rf["factor"])


AFR_XB = frozenset(row[0] for row in C.XB_LEAGUES.get("afrique", ()))      # Égypte, Maroc, Algérie : vus par 1xBet ET listés « africa » par API-Football


def _aps_candidates(prof, sport_key):
    if sport_key in AFR_XB:              # le profil API-Football de ces équipes porte « africa » (fair.make écarte la copie « africa » du match)
        return {n: p for n, p in prof.items() if sport_key in p.get("sk", []) or "africa" in p.get("sk", [])}
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
                      ("cards", "cards"), ("yellow", "yellow")):
        hv, av = _expect(ht, at, stat)
        _put_pair(tmp, stat, key, hv, av, total=True)
    new = {k: v for k, v in tmp.items() if k not in out}
    # xG par match des profils API-Football (même calcul que FotMob : _fm_xg). Priorité d'exp_xg_* : Understat > FotMob matchs > API-Football
    # matchs > classement FotMob de saison. Les xG d'API-Football ne remplacent donc que ceux du classement ou comblent l'absence d'xG.
    xg = _fm_xg(ht, at)
    if xg and (not any(k in out for k in XG_KEYS) or out.get("xg_src") == "fotmob-classement"):
        new |= xg
        if out.get("xg_src") == "fotmob-classement" and "FotMob" in out["sources"]:
            out["sources"].remove("FotMob")              # le classement n'a plus rien fourni
        out["xg_src"] = "api-football-matchs"
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


GOAL_KEYS = ("exp_goals_home", "exp_goals_away", "exp_goals_total")
FM_STATS = ("corners", "goals", "shots", "sot", "fouls", "cards", "throws", "offsides", "yellow")


def _fm_candidates(prof, sport_key):
    """Équipes FotMob de la ligue (toutes les ligues africaines pour un match « africa », nom de ligue inconnu)."""
    if sport_key == "africa":
        return {n: p for n, p in prof.items() if any(k.startswith("africa") for k in p.get("sk", []))}
    if sport_key in AFR_XB:
        return {n: p for n, p in prof.items() if sport_key in p.get("sk", []) or "africa" in p.get("sk", [])}
    return {n: p for n, p in prof.items() if sport_key in p.get("sk", [])}


XG_KEYS = ("exp_xg_home", "exp_xg_away", "exp_xg_total")


def _xg_view(team, venue):
    """Vue (domicile / extérieur, sinon toutes) d'un profil fmmatch ou API-Football qui compte au moins XG_MIN_MATCHES matchs avec xG (et dont les
    moyennes xg_for / xg_against sont servies : min_count des profils) ; None sinon."""
    for v in (team.get(venue) or {}, team.get("all") or {}):
        if v.get("xg_n", 0) >= C.XG_MIN_MATCHES and v.get("xg_for") is not None and v.get("xg_against") is not None:
            return v
    return None


def _xg_part(v, side, goals_side):
    """xG moyen d'une vue (`side` : « for » / « against »), ramené vers les buts quand l'échantillon est court : poids du xG
    = min(1, matchs avec xG / XG_FULL_N), le reste en buts (4 matchs sur XG_FULL_N=6 : 2/3 xG + 1/3 buts)."""
    xg = v.get("xg_" + side)
    if xg is None:
        return None
    g = v.get(f"{goals_side}_{side}")
    w = min(1.0, v["xg_n"] / C.XG_FULL_N) if C.XG_FULL_N > 0 else 1.0
    return xg if g is None else w * xg + (1 - w) * g


def _fm_xg(ht, at):
    """{exp_xg_home, exp_xg_away, exp_xg_total} d'après les xG par match des deux équipes (profils FotMob ou API-Football : mêmes clés) (mêmes mélanges que _expect : l'attaque de l'une
    et la défense de l'autre, à domicile / à l'extérieur) ; {} si l'une des deux a moins de XG_MIN_MATCHES matchs avec xG."""
    h, a = _xg_view(ht, "home"), _xg_view(at, "away")
    if not h or not a:
        return {}
    eh = _mix(_xg_part(h, "for", "goals"), _xg_part(a, "against", "goals"))
    ea = _mix(_xg_part(a, "for", "goals"), _xg_part(h, "against", "goals"))
    if eh is None or ea is None:
        return {}
    return dict(zip(XG_KEYS, (_r(eh), _r(ea), _r(eh + ea))))


def _fmmatch(out, home, away, sport_key):
    """Stats de match FotMob accumulées (fmmatch) : complètent ce que les sources précédentes n'ont pas, et sont seules à donner
    touches et hors-jeu. Comme les profils API-Football, leurs clés vont dans aps_keys (qualité selon le nombre de matchs).
    Exception : les xG par match (exp_xg_*) remplacent ceux du classement de saison de _fotmob (xg_src « fotmob-classement », plus
    grossier) ; ils ne touchent pas à Understat. out["xg_src"] dit d'où vient exp_xg_* : « fotmob-matchs » ici."""
    prof = _fm_candidates(_p("fmm", fmmatch).get("teams") or {}, sport_key)
    h, a = names.best_team(home, prof), names.best_team(away, prof)
    if not h or not a or prof[h] is prof[a]:
        return
    ht, at = prof[h], prof[a]
    tmp = {}
    for stat in FM_STATS:
        hv, av = _expect(ht, at, stat)
        _put_pair(tmp, stat, stat, hv, av, total=True)
    n_h, n_a = ht["all"]["n"], at["all"]["n"]
    std = out.get("std_n") and min(n_h, n_a) >= C.APISTATS_GOOD_N and all(k in tmp for k in GOAL_KEYS[:2] + ("exp_goals",))
    new = {k: v for k, v in tmp.items() if k not in out}
    if std:                                               # assez de matchs FotMob : leurs buts valent mieux que ceux du classement de saison
        new |= {"exp_goals_home": tmp["exp_goals_home"], "exp_goals_away": tmp["exp_goals_away"], "exp_goals_total": tmp["exp_goals"]}
    xg = _fm_xg(ht, at)
    if xg and (not any(k in out for k in XG_KEYS) or out.get("xg_src") == "fotmob-classement"):
        new |= xg
        if out.get("xg_src") == "fotmob-classement" and "FotMob" in out["sources"]:
            out["sources"].remove("FotMob")              # le classement n'a plus rien fourni
        out["xg_src"] = "fotmob-matchs"
    if not new:
        return
    out.update(new)
    if "exp_cards" in new:
        out.setdefault("exp_cards_team", out["exp_cards"])
    if std:
        out.pop("std_n")                                  # les buts ne viennent plus du classement : leur échantillon est celui de FotMob
        out["n_home"], out["n_away"] = n_h, n_a
        if "FotMob (classement)" in out["sources"]:
            out["sources"].remove("FotMob (classement)")
    out["sources"].append("FotMob (matchs)")
    q = aps_quality(min(n_h, n_a))
    if set(out.get("aps_keys") or ()) - set(new):         # d'autres chiffres déjà là (classement) : on garde la qualité la plus prudente
        q = min(q, out.get("aps_q", q))
    out["aps_keys"] = sorted(set(out.get("aps_keys") or ()) | set(new))
    out["aps_n"] = (n_h, n_a)
    out["aps_q"] = q
    out.setdefault("home_team", h)
    out.setdefault("away_team", a)
    out.setdefault("n_home", n_h)
    out.setdefault("n_away", n_a)


S365_STATS = ("corners", "shots", "sot", "fouls", "cards", "throws", "offsides", "yellow")      # pas les buts : le modèle de buts garde ses sources
S365_LABEL = "365scores"


def _s365(out, home, away, sport_key):
    """Stats de match 365scores accumulées (s365) : DERNIÈRE source, elle ne complète que les chiffres qu'aucune autre n'a donnés (football-data,
    classements et matchs FotMob, API-Football passent avant elle). Mêmes seuils que les profils FotMob : APISTATS_MIN_N matchs par équipe
    (s365.profiles), aps_quality, moyennes d'une stat sur au moins APISTATS_MIN_N matchs qui la portent. Les clés vont dans aps_keys (la
    qualité et l'effectif suivent, comme pour FotMob et API-Football : le plus prudent des profils courts) ET dans out["s365_keys"] (provenance :
    les textes peuvent dire « 365scores » pour ces chiffres-là seulement)."""
    data = ((_p("s3", s365).get("leagues") or {}).get(sport_key) or {}).get("teams") or {}
    h, a = names.best_team(home, data), names.best_team(away, data)
    if not h or not a or data[h] is data[a]:
        return
    ht, at = data[h], data[a]
    tmp = {}
    for stat in S365_STATS:
        hv, av = _expect(ht, at, stat)
        _put_pair(tmp, stat, stat, hv, av, total=True)
    new = {k: v for k, v in tmp.items() if k not in out}
    if not new:
        return
    out.update(new)
    if "exp_cards" in new:
        out.setdefault("exp_cards_team", out["exp_cards"])
    n_h, n_a = ht["all"]["n"], at["all"]["n"]
    out["sources"].append(S365_LABEL)
    q = aps_quality(min(n_h, n_a))
    if out.get("aps_keys"):                              # d'autres chiffres d'un profil court déjà là : on garde l'effectif et la qualité les plus prudents
        o = tuple(out.get("aps_n") or (n_h, n_a))
        q, n_h, n_a = min(q, out.get("aps_q", q)), min(n_h, o[0]), min(n_a, o[1])
    out["aps_keys"] = sorted(set(out.get("aps_keys") or ()) | set(new))
    out["s365_keys"] = sorted(set(out.get("s365_keys") or ()) | set(new))
    out["aps_n"] = (n_h, n_a)
    out["aps_q"] = q
    out.setdefault("home_team", h)
    out.setdefault("away_team", a)
    out.setdefault("n_home", n_h)
    out.setdefault("n_away", n_a)


def match_features(home, away, sport_key, referee=None, on=None):
    """Chiffres d'avant-match d'un match. `on` : coup d'envoi (ISO) ou jour du match, pour les jours de repos du modèle approfondi (A15) ; absent : aujourd'hui."""
    out = {"sources": []}
    if sport_key.startswith("africa"):
        steps = [(_fmmatch, (home, away, sport_key)), (_apistats, (home, away, sport_key)), (_s365, (home, away, sport_key))]
        if sport_key in AFR_XB:                         # championnat vu par 1xBet : aussi les buts du classement FotMob
            steps.insert(0, (_fotmob, (home, away, sport_key)))
    else:
        steps = [(_football_data, (home, away, sport_key, referee)), (_understat, (home, away, sport_key)),
                 (_fotmob, (home, away, sport_key)), (_fbref, (home, away, sport_key)),
                 (_deep, (home, away, sport_key, referee, on)), (_fmmatch, (home, away, sport_key)), (_apistats, (home, away, sport_key)),
                 (_s365, (home, away, sport_key))]
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
