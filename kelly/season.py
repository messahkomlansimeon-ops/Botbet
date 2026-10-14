"""Voie `saison` : modèle « saison en cours » des statistiques d'équipe.

Pour chaque équipe, UNIQUEMENT les matchs de la saison (ou de la compétition) en cours, sans la saison précédente, sans demi-vie, sans ancrage marché
et sans facteur de contexte. Par statistique (buts, corners, cartons, jaunes, tirs, tirs cadrés, fautes) :

  - moyennes « pour » et « contre » à domicile et à l'extérieur, rétrécies vers la moyenne de la ligue avec le poids n / (n + k), k = C.SEASON_PRIOR_K (12 : le meilleur réglage mesuré au rejeu du 14/10) ;
  - attente d'un match : domicile = attaque(A, dom.) x défense(B, ext.) / moyenne des « domicile » de la ligue ; extérieur = attaque(B, ext.) x défense(A, dom.) /
    moyenne des « extérieur » de la ligue (le facteur domicile de la ligue est ce rapport) ;
  - binomiale négative avec la dispersion de la ligue en cours (moments, plancher 0) ; sous C.SEASON_ALPHA_MIN_MATCHES matchs de ligue : repli donné par l'appelant
    (celui du rejeu : strength.fit_stat de la ligue, ou les constantes de fair.py) ;
  - buts : Poisson bivarié / binomiale négative bivariée (strength.score_matrix) avec le kappa de la ligue en cours, 1N2, double chance, les 2 marquent et buts
    d'équipe compris. Pas de correction de Dixon-Coles (rho = 0).
Au moins C.SEASON_MIN_MATCHES (4) matchs par équipe, sinon pas de prévision. Rien n'est gardé au-delà du jour : le modèle est réajusté à chaque passage du matin.

`fit` et `predict` sont purs (observations en entrée, aucun accès disque) ; `collect` lit les données déjà stockées (football-data, FotMob, 365scores,
API-Football), jamais une requête. `predict` rend un dict de la forme de strength.predict : strength.grid / strength.lines s'en servent tels quels.

Limites connues : une ligne stockée (FotMob, 365scores, API-Football) n'a pas d'identifiant d'adversaire (la défense d'une équipe est moyennée sur ses adversaires
réels, pas corrigée de leur force) ; la dispersion est mesurée avec l'adversaire moyen (elle est donc plutôt un peu trop large) ; FotMob et 365scores ne portent pas
la compétition par ligne (la ligue est celle de l'équipe) ; pour API-Football, la compétition d'une équipe est la plus jouée cette saison (à égalité la plus
récente) : en tout début de saison une coupe peut l'emporter sur le championnat."""
import math
from datetime import date

from . import config as C

STATS = ("goals", "corners", "cards", "yellow", "shots", "sot", "fouls")
MIN_ALPHA_ROWS = 8                       # sous ce nombre d'observations, la dispersion mesurée n'a aucun sens (même avec un repli absent)
CALENDAR_TOKENS = frozenset(("brazil", "argentina", "usa", "usl", "canada", "japan", "china", "norway", "sweden", "finland", "chile", "colombia", "peru", "ecuador",
                             "uruguay", "paraguay", "bolivia", "venezuela", "korea", "conmebol"))      # mots entiers de la clé : saison sur l'année civile (reste : 1er juillet)
CALENDAR_KEYS = frozenset(("soccer_league_of_ireland",))                                              # clés exactes (l'Irlande du Nord joue août-mai)


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def season_start(sport_key, on):
    """Début (date) de la saison en cours de la ligue `sport_key` à la date `on` : 1er janvier pour une ligue en année civile, sinon 1er juillet."""
    key = str(sport_key or "")
    if key in CALENDAR_KEYS or CALENDAR_TOKENS & set(key.split("_")):
        return date(on.year, 1, 1)
    return date(on.year if on.month >= 7 else on.year - 1, 7, 1)


# ---------- ajustement ----------

def _shrink(total, n, prior, k):
    """Moyenne de n valeurs (somme `total`) rétrécie vers `prior` avec le poids n / (n + k)."""
    if n <= 0:
        return prior
    w = n / (n + k)
    return w * (total / n) + (1 - w) * prior


def _fit_stat(rows, k, fallback_alpha, min_alpha_matches):
    """rows : {équipe: [(domicile 1/0, pour, contre), ...]} d'une statistique. -> dict du modèle de cette statistique, ou None sans donnée."""
    sums = {"hf": 0.0, "ha": 0.0, "nh": 0, "af": 0.0, "aa": 0.0, "na": 0}
    team = {}
    for t, lst in rows.items():
        s = team.setdefault(t, {"nh": 0, "hf": 0.0, "ha": 0.0, "na": 0, "af": 0.0, "aa": 0.0})
        for home, f, a in lst:
            key = "h" if home else "a"
            s["n" + key] += 1
            s[key + "f"] += f
            s[key + "a"] += a
            sums["n" + key] += 1
            sums[key + "f"] += f
            sums[key + "a"] += a
    if not sums["nh"] + sums["na"]:
        return None
    # moyennes de ligue : le côté « domicile » d'un match vu par l'équipe qui reçoit (pour) et par celle qui visite (contre), idem « extérieur »
    n_home_side = sums["nh"] + sums["na"]
    lg_h = (sums["hf"] + sums["aa"]) / n_home_side
    lg_a = (sums["ha"] + sums["af"]) / n_home_side
    if lg_h <= 0 or lg_a <= 0:
        return None
    out = {}
    for t, s in team.items():
        out[t] = {"n": s["nh"] + s["na"], "nh": s["nh"], "na": s["na"],
                  "ah": _shrink(s["hf"], s["nh"], lg_h, k), "dh": _shrink(s["ha"], s["nh"], lg_a, k),       # attaque / défense à domicile
                  "aa": _shrink(s["af"], s["na"], lg_a, k), "da": _shrink(s["aa"], s["na"], lg_h, k)}       # ... à l'extérieur
    # dispersion de la ligue : moments, avec l'attente de CHAQUE observation recalculée sans elle (sinon la mesure est trop basse) et l'adversaire moyen
    num = den = num_t = den_t = 0.0
    n_obs = 0
    for t, lst in rows.items():
        s = team[t]
        for home, f, a in lst:
            key = "h" if home else "a"
            n = s["n" + key]
            sf, sa = s[key + "f"] - f, s[key + "a"] - a
            pf, pa = (lg_h, lg_a) if home else (lg_a, lg_h)
            mf, ma = _shrink(sf, n - 1, pf, k), _shrink(sa, n - 1, pa, k)
            num += (f - mf) ** 2 - mf + (a - ma) ** 2 - ma
            den += mf * mf + ma * ma
            mt = mf + ma
            num_t += (f + a - mt) ** 2 - mt
            den_t += mt * mt
            n_obs += 2
    matches = max(sums["nh"], sums["na"])
    measured = den > 0 and n_obs >= MIN_ALPHA_ROWS
    alpha = max(0.0, num / den) if measured else None
    atot = max(0.0, num_t / den_t) if measured and den_t > 0 else None
    measured_ok = matches >= min_alpha_matches
    fb = fallback_alpha or {}
    return {"lg_h": lg_h, "lg_a": lg_a, "matches": matches,
            "alpha": alpha if measured_ok and alpha is not None else fb.get("alpha", 0.0),
            "atot": atot if measured_ok and atot is not None else fb.get("atot", fb.get("alpha", 0.0)),
            "alpha_src": "ligue" if measured_ok and alpha is not None else "repli",
            "t": out}


def fit(obs, on, sport_key=None, stats=STATS, k=None, fallback=None, min_alpha_matches=None):
    """Modèle `saison` d'une ligue. `obs` : [{"team", "d" (AAAA-MM-JJ), "home" (1/0), "s": {stat: (pour, contre)}}], une ligne par équipe et par match ; seules les lignes de
    la saison en cours (season_start) et antérieures à `on` (date) comptent. `fallback` : {stat: {"alpha", "atot"}, "kappa": x} pour les ligues de moins de
    `min_alpha_matches` matchs (C.SEASON_ALPHA_MIN_MATCHES). -> dict JSON ou None sans aucun but observé."""
    k = C.SEASON_PRIOR_K if k is None else k
    min_alpha_matches = C.SEASON_ALPHA_MIN_MATCHES if min_alpha_matches is None else min_alpha_matches
    start, end = season_start(sport_key, on).isoformat(), on.isoformat()
    mine = [o for o in obs if start <= str(o.get("d") or "") < end]
    model = {"v": 1, "lane": "saison", "sport_key": sport_key, "on": end, "start": start, "k": k, "rows": len(mine), "stats": {}}
    for stat in stats:
        rows = {}
        for o in mine:
            pair = (o.get("s") or {}).get(stat)
            if pair and pair[0] is not None and pair[1] is not None:
                rows.setdefault(o["team"], []).append((1 if o.get("home") else 0, float(pair[0]), float(pair[1])))
        fs = _fit_stat(rows, k, (fallback or {}).get(stat), min_alpha_matches) if rows else None
        if fs:
            model["stats"][stat] = fs
    g = model["stats"].get("goals")
    if not g:
        return None
    from . import strength
    model["kappa"] = (strength.kappa_of({"atot": g["atot"]}) if g["alpha_src"] == "ligue"
                      else (fallback or {}).get("kappa", C.GOALS_KAPPA_DEFAULT))
    return model


def teams(model, stat="goals"):
    return ((model or {}).get("stats", {}).get(stat) or {}).get("t") or {}


def find_team(model, name):
    from .sources import names
    ts = teams(model)
    return names.best_team(name, ts) if ts else None


def eligible(model, home, away):
    """(équipe domicile du modèle, équipe extérieur du modèle) si les deux ont au moins C.SEASON_MIN_MATCHES matchs de buts, sinon None."""
    h, a = find_team(model, home), find_team(model, away)
    ts = teams(model)
    if not h or not a or h == a or min(ts[h]["n"], ts[a]["n"]) < C.SEASON_MIN_MATCHES:
        return None
    return h, a


def _expect(s, h, a):
    """(attente domicile, attente extérieur) d'une statistique pour l'équipe h (à domicile) contre a (à l'extérieur), ou None si l'une manque de matchs."""
    th, ta = s["t"].get(h), s["t"].get(a)
    if not th or not ta or min(th["n"], ta["n"]) < C.SEASON_MIN_MATCHES:
        return None
    return th["ah"] * ta["da"] / s["lg_h"], ta["aa"] * th["dh"] / s["lg_a"]


def _quality(s, h, a, k):
    n = min(s["t"][h]["n"], s["t"][a]["n"])
    return n / (n + k)


def predict(model, home, away):
    """Prévision `saison` d'un match, de la forme de strength.predict (strength.grid / strength.lines la lisent telle quelle) : {home, away, lh, la, rho, kappa, q, qs,
    stats:{stat:{h, a, al, at}}, base, n, lane}. None si une équipe est inconnue ou a moins de C.SEASON_MIN_MATCHES matchs."""
    pair = eligible(model, home, away) if model else None
    if not pair:
        return None
    h, a = pair
    k = model["k"]
    g = model["stats"]["goals"]
    lh, la = _expect(g, h, a) or (None, None)
    if lh is None:
        return None
    stats, base, qs = {"goals": {"h": lh, "a": la, "al": 0.0, "at": 0.0}}, {}, {"goals": _quality(g, h, a, k)}
    base["goals"] = (g["lg_h"], g["lg_a"])
    for stat, s in model["stats"].items():
        if stat == "goals":
            continue
        e = _expect(s, h, a)
        if not e:
            continue
        stats[stat] = {"h": e[0], "a": e[1], "al": s["alpha"], "at": s["atot"]}
        base[stat] = (s["lg_h"], s["lg_a"])
        qs[stat] = _quality(s, h, a, k)
    ts = g["t"]
    return {"lane": "saison", "div": model.get("sport_key"), "home": h, "away": a, "lh": lh, "la": la, "lh0": lh, "la0": la, "rho": 0.0, "kappa": model["kappa"],
            "fh": 1.0, "fa": 1.0, "q": qs["goals"], "qs": qs, "stats": stats, "notes": [], "info": {}, "tm": {},
            "base": {"lh": base["goals"][0], "la": base["goals"][1], "stats": {s: {"h": v[0], "a": v[1]} for s, v in base.items() if s != "goals"}},
            "n": [ts[h]["n"], ts[a]["n"]], "neff": [float(ts[h]["n"]), float(ts[a]["n"])], "model_n": model["rows"], "last": model["on"], "stale": 0,
            "promo": [False, False], "unavailable_referee": False, "newcomer": None}


# ---------- coupes : deux équipes de championnats différents ----------

def locate(models, name):
    """(clé de ligue, nom de l'équipe dans le modèle) de l'équipe `name` parmi les modèles de championnat `models` ({clé: modèle `saison`}), ou None : le meilleur rapprochement de nom
    (>= 0,8) toutes ligues confondues ; deux ligues différentes à 0,05 près (ou plus) l'une de l'autre = ambigu, sauf nom exact."""
    from .sources import names
    hits = []
    for sk, model in models.items():
        ts = teams(model)
        t = names.best_team(name, ts) if ts else None
        if t:
            hits.append((names.team_sim(name, t), sk, t))
    if not hits:
        return None
    hits.sort(key=lambda h: -h[0])
    best = hits[0]
    if len(hits) > 1 and hits[1][0] >= best[0] - 0.05 and not (names.norm(best[2]) == names.norm(name) and names.norm(hits[1][2]) != names.norm(name)):
        return None
    return best[1], best[2]


def _rel(entry, s):
    """(attaque, défense) d'une équipe RAPPORTÉES à la moyenne de sa ligue : 1 = une équipe moyenne de son championnat. Moyenne des lignes domicile et extérieur (déjà rétrécies)."""
    return (0.5 * (entry["ah"] / s["lg_h"] + entry["aa"] / s["lg_a"]), 0.5 * (entry["dh"] / s["lg_a"] + entry["da"] / s["lg_h"]))


def cup_predict(models, home, away, home_factor=None):
    """Prévision `saison` d'un match de COUPE (ou entre championnats) : l'attente d'une équipe = profil de championnat de A (attaque) x profil de championnat de B (défense), chacun rapporté
    à la moyenne de sa propre ligue, x le niveau moyen des deux ligues (moyenne des deux moyennes de côté), avec un facteur domicile NEUTRE (C.SEASON_CUP_HOME = 1,10, réparti
    à parts égales entre les deux équipes : domicile / extérieur = 1,10). Au moins C.SEASON_MIN_MATCHES matchs de championnat par équipe. Dispersion : moyenne de celles des deux ligues.
    Même forme que predict (strength.grid / strength.lines), `cup` vrai. None si une équipe est introuvable, ambiguë ou trop peu jouée ; aucun niveau de ligue n'est corrigé au-delà
    de ce rapport à la moyenne de la ligue (limite connue)."""
    f = C.SEASON_CUP_HOME if home_factor is None else home_factor
    la, lb = locate(models, home), locate(models, away)
    if not la or not lb or la == lb:
        return None
    (ska, ta), (skb, tb) = la, lb
    ma, mb = models[ska], models[skb]
    ga, gb = ma["stats"]["goals"], mb["stats"]["goals"]
    if min(ga["t"][ta]["n"], gb["t"][tb]["n"]) < C.SEASON_MIN_MATCHES:
        return None
    k = ma["k"]
    h = math.sqrt(f)
    stats, base, qs = {}, {}, {}
    for stat in ma["stats"]:
        sa, sb = ma["stats"].get(stat), mb["stats"].get(stat)
        if not sa or not sb or ta not in sa["t"] or tb not in sb["t"] or min(sa["t"][ta]["n"], sb["t"][tb]["n"]) < C.SEASON_MIN_MATCHES:
            continue
        att_a, def_a = _rel(sa["t"][ta], sa)
        att_b, def_b = _rel(sb["t"][tb], sb)
        level = 0.5 * (0.5 * (sa["lg_h"] + sa["lg_a"]) + 0.5 * (sb["lg_h"] + sb["lg_a"]))
        lam_a, lam_b = level * att_a * def_b * h, level * att_b * def_a / h
        al, at = 0.5 * (sa["alpha"] + sb["alpha"]), 0.5 * (sa["atot"] + sb["atot"])
        stats[stat] = {"h": lam_a, "a": lam_b, "al": 0.0 if stat == "goals" else al, "at": 0.0 if stat == "goals" else at}
        base[stat] = (level * h, level / h)
        n = min(sa["t"][ta]["n"], sb["t"][tb]["n"])
        qs[stat] = n / (n + k)
    if "goals" not in stats:
        return None
    kappa = 0.5 * (ma["kappa"] + mb["kappa"])
    g = stats["goals"]
    return {"lane": "saison", "cup": True, "div": f"{ska}+{skb}", "home": home, "away": away, "lh": g["h"], "la": g["a"], "lh0": g["h"], "la0": g["a"], "rho": 0.0, "kappa": kappa,
            "fh": 1.0, "fa": 1.0, "q": qs["goals"], "qs": qs, "stats": stats, "notes": [], "info": {"cup_leagues": [ska, skb]}, "tm": {},
            "base": {"lh": base["goals"][0], "la": base["goals"][1], "stats": {st: {"h": v[0], "a": v[1]} for st, v in base.items() if st != "goals"}},
            "n": [ga["t"][ta]["n"], gb["t"][tb]["n"]], "neff": [float(ga["t"][ta]["n"]), float(gb["t"][tb]["n"])], "model_n": ma["rows"] + mb["rows"], "last": ma["on"], "stale": 0,
            "promo": [False, False], "unavailable_referee": False, "newcomer": None}


# ---------- lecture des données déjà stockées (aucune requête) ----------

def _day(i):
    return f"20{i // 10000:02d}-{i // 100 % 100:02d}-{i % 100:02d}"


def _from_matches(matches):
    """Observations d'une liste de matchs football-data normalisés (footballdata.parse_csv) : deux lignes par match."""
    from .sources import footballdata as fd
    out = []
    for m in matches:
        for side, team in (("home", m["home"]), ("away", m["away"])):
            pairs = fd._side(m, side)
            out.append({"team": team, "d": m["date"], "home": 1 if side == "home" else 0, "s": {st: v for st, v in pairs.items() if st in STATS},
                        "opp": m["away"] if side == "home" else m["home"]})        # football-data nomme toujours l'adversaire
    return out


def _from_team_rows(teams_doc, sport_key, keys, r_stats, r_home=1, r_day=0):
    """Observations des équipes d'une ligue rangées par FotMob / 365scores / API-Football : {id: {nm, sk:[...], m:[[jour, domicile, (pour, contre)...]]}}."""
    from .sources import rows as srows
    out = []
    for tid, t in teams_doc.items():
        if not isinstance(t, dict) or sport_key not in (t.get("sk") or []) or not t.get("nm"):
            continue
        for r in t.get("m") or []:
            s = {}
            for i, key in enumerate(keys):
                if key in STATS and len(r) > r_stats + 2 * i + 1 and r[r_stats + 2 * i] is not None and r[r_stats + 2 * i + 1] is not None:
                    s[key] = (r[r_stats + 2 * i], r[r_stats + 2 * i + 1])
            if "goals" in s:
                opp = srows.opponent(r, r_stats)                    # identifiant de l'adversaire (de la MÊME source) quand la ligne le porte, sinon None
                out.append({"team": t["nm"], "d": _day(r[r_day]), "home": 1 if r[r_home] else 0, "s": s, "tid": tid, "opp": opp})
    return out


def collect(sport_key, on, loaders=None, docs=None):
    """(observations, source) de la ligue : UNE source par ligue, dans l'ordre football-data > FotMob > 365scores > API-Football, la première qui donne au moins
    C.SEASON_MIN_TEAMS équipes ayant C.SEASON_MIN_MATCHES matchs cette saison. ([], None) sinon. `loaders` : {source: fonction() -> observations} (tests). `docs` : documents des sources
    déjà chargés ({"fotmob": équipes, "365scores": équipes, "api-football": document}) pour compter beaucoup de ligues sans relire trois gros documents à chaque ligue."""
    from . import apistats
    from .sources import fmmatch, s365, footballdata as fd

    def football_data():
        div = fd.SPORT_TO_DIV.get(sport_key)
        if not div or div in fd.NEW_DIVS:
            return []
        from . import strength
        return _from_matches(strength.load_matches(div))

    def fotmob():
        return _from_team_rows(docs["fotmob"] if docs else fmmatch.load()["teams"], sport_key, fmmatch.KEYS, fmmatch.R_STATS)

    def three():
        return _from_team_rows(docs["365scores"] if docs else s365.load()["teams"], sport_key, s365.KEYS, s365.R_STATS)

    start = season_start(sport_key, on).isoformat()

    def api():
        d = docs["api-football"] if docs else apistats.load()
        teams_doc = {}
        for tid, t in d["teams"].items():
            if not isinstance(t, dict) or sport_key not in (t.get("sk") or []):
                continue
            labels = [(r[apistats.R_LG], r[apistats.R_DAY]) for r in t.get("m") or []
                      if len(r) > apistats.R_LG and isinstance(r[apistats.R_LG], int) and _day(r[apistats.R_DAY]) >= start]      # cette saison seulement (l'équipe promue/reléguée garde 15 matchs d'avant)
            main = max({lb for lb, _ in labels}, key=lambda lb: (sum(1 for x, _ in labels if x == lb), max(d for x, d in labels if x == lb))) if labels else None      # la compétition la plus jouée de l'équipe cette saison = son championnat (t["lg"] est celle du DERNIER match, une coupe peut-être) ; à égalité la plus récente
            teams_doc[tid] = dict(t, m=[r for r in t.get("m") or [] if main is None or (len(r) > apistats.R_LG and r[apistats.R_LG] == main)])
        return _from_team_rows(teams_doc, sport_key, apistats.KEYS, apistats.R_STATS, apistats.R_HOME, apistats.R_DAY)

    for name, fn in (("football-data", football_data), ("fotmob", fotmob), ("365scores", three), ("api-football", api)):
        try:
            obs = ((loaders or {}).get(name) or fn)()
        except Exception:                      # noqa: BLE001 - une source illisible passe à la suivante
            continue
        per = {}
        for o in obs:
            if o["d"] >= start and o["d"] < on.isoformat() and (o.get("s") or {}).get("goals"):
                per[o["team"]] = per.get(o["team"], 0) + 1
        if sum(1 for n in per.values() if n >= C.SEASON_MIN_MATCHES) >= C.SEASON_MIN_TEAMS:
            return obs, name
    return [], None


def coverage(obs, on, sport_key=None):
    """(équipes suivies, équipes AVEC adversaire) d'une liste d'observations : une équipe est suivie si elle a au moins C.SEASON_MIN_MATCHES matchs de buts cette saison, « avec adversaire »
    si au moins C.SEASON_MIN_MATCHES de ces matchs portent l'identifiant de l'adversaire (l'ajustement à l'adversaire n'est possible que pour elles)."""
    start, end = season_start(sport_key, on).isoformat(), on.isoformat()
    n, k = {}, {}
    for o in obs:
        if start <= str(o.get("d") or "") < end and (o.get("s") or {}).get("goals"):
            n[o["team"]] = n.get(o["team"], 0) + 1
            if o.get("opp") is not None:
                k[o["team"]] = k.get(o["team"], 0) + 1
    tracked = [t for t, c in n.items() if c >= C.SEASON_MIN_MATCHES]
    return len(tracked), sum(1 for t in tracked if k.get(t, 0) >= C.SEASON_MIN_MATCHES)


def snapshot(on, leagues, loaders=None):
    """{clé de ligue: {"source", "teams", "opp"}} des ligues COUVERTES (au moins C.SEASON_MIN_TEAMS équipes suivies) parmi `leagues` : ce que la voie `saison` suit exactement, et
    pour combien d'équipes l'identifiant de l'adversaire est déjà enregistré. Lecture seule des données stockées (jamais une requête)."""
    from . import apistats
    from .sources import fmmatch, s365
    docs = None if loaders else {"fotmob": fmmatch.load()["teams"], "365scores": s365.load()["teams"], "api-football": apistats.load()}      # chargés UNE fois pour toutes les ligues
    out = {}
    for sk in leagues:
        try:
            obs, src = collect(sk, on, loaders, docs)
            if src:
                teams, opp = coverage(obs, on, sk)
                out[sk] = {"source": src, "teams": teams, "opp": opp}
        except Exception:                      # noqa: BLE001 - une ligue illisible n'arrête pas le compte
            continue
    return out


_memo = {}


def get(sport_key, on=None, loaders=None, fallback=None):
    """Modèle `saison` du jour d'une ligue (mémoire du processus, jamais écrit sur le disque), ou None. Ne lève jamais."""
    try:
        from . import strength
        on = on or strength.today()
        key = (sport_key, on.isoformat(), C.SEASON_PRIOR_K, bool(fallback))
        if loaders is None and key in _memo:
            return _memo[key]
        obs, src = collect(sport_key, on, loaders)
        model = fit(obs, on, sport_key, fallback=fallback) if obs else None
        if model:
            model["source"] = src
        if loaders is None:
            if len(_memo) > 200:
                _memo.clear()
            _memo[key] = model
        return model
    except Exception as e:                      # noqa: BLE001
        import sys
        print(f"[saison] {sport_key} ignorée : {type(e).__name__}: {e}", file=sys.stderr)
        return None


def reset():
    _memo.clear()
