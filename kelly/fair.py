"""Pronostics Kelly SANS cote de bookmaker, pour les matchs que le budget de crédits n'a pas payés.

Pour chaque match du relevé gratuit (/events), Kelly calcule avec les stats gratuites sa propre probabilité (modèle de
Poisson, mêmes chiffres que prematch.py) et la « cote juste » = 1 / proba. Marchés : buts (plus/moins), les 2 équipes
marquent, double chance, corners, cartons. Sources : profils football-data (kelly/sources) d'abord ; pour quelques
matchs sans profil, les moyennes de corners/cartons d'API-Football dans la limite de son quota.

Modèle approfondi (kelly/strength.py : Dixon-Coles pondéré dans le temps, binomiale négative pour corners et cartons,
contexte, arbitre) dès que la ligue a assez de matchs en cache : lignes de buts de 0,5 à 4,5, double chance, corners,
cartons. Chaque ligne est comparée à une référence naïve (le même pari pour un match moyen de la ligue) : à probas
voisines, Kelly garde la ligne où le modèle s'écarte le plus de la référence, et classe les pronostics par cet écart
multiplié par la qualité des données (« confiance »).

Tirs et tirs cadrés (total du match et par équipe) s'ajoutent dès que des profils les donnent (football-data, modèle
approfondi ou profils API-Football de kelly/apistats.py) ; les championnats africains (sans cotes ni autre source) sont
traités avec les seuls profils API-Football. Chaque pronostic porte sa « confiance » (c["conf"], qualité des données de
0 à 1) : les combinés à cote juste (combos.fair_*) n'utilisent que ceux dont la confiance et la proba sont assez hautes.

Ces pronostics n'ont ni cote ni mise : « compare chez 1xBet, joue si la cote >= X ». Ils n'entrent dans aucun combiné et
ne sont pas suivis pour la cote de clôture ; leurs résultats vont dans data/fair.json (règlement gratuit,
settle.settle_fair) pour mesurer si les probas de Kelly sont justes : les C.FAIR_TRACK meilleurs, plus jusqu'à
C.FAIR_TRACK_FREE pronostics de buts, 1N2, double chance et « les 2 marquent » suivis en silence (réglés avec la liste des
matchs de la veille, déjà téléchargée : aucune requête de plus). Le cumul par famille (kelly/calib.py) corrige ensuite les
probas des marchés où Kelly se montre trop sûre d'elle (c["p0"] = proba avant correction).
"""
import sys
import time
from datetime import datetime, timezone

from . import apistats, calib, catalogue, config as C, odds, prematch, stats, store, strength

FILE = "fair.json"
LINES = {"goals": (1.5, 2.5, 3.5), "corners": (7.5, 8.5, 9.5, 10.5, 11.5, 12.5), "cards": (2.5, 3.5, 4.5, 5.5, 6.5)}
FAMILIES = ("totals", "btts", "double_chance", prematch.CORNERS, prematch.CARDS)
FREE_MARKETS = ("totals", "btts", "double_chance", "h2h")       # réglés avec le seul score : suivis en nombre, gratuitement
# Tirs et tirs cadrés : total du match, puis une équipe (le suffixe _home / _away dit laquelle) ; (clé des chiffres d'avant-match,
# libellé, bornes plausibles de la moyenne attendue, dispersion de la binomiale négative)
STAT_SPECS = {"team_shots_total": ("exp_shots", "tirs", (12.0, 45.0), 0.03),
              "team_sot_total": ("exp_sot", "tirs cadrés", (3.0, 16.0), 0.04),
              "team_shots_home": ("exp_shots_home", "tirs", (4.0, 26.0), 0.05),
              "team_shots_away": ("exp_shots_away", "tirs", (4.0, 26.0), 0.05),
              "team_sot_home": ("exp_sot_home", "tirs cadrés", (1.2, 10.0), 0.05),
              "team_sot_away": ("exp_sot_away", "tirs cadrés", (1.2, 10.0), 0.05),
              # Fautes (football-data, modèle approfondi, FotMob, API-Football), touches et hors-jeu (FotMob seulement)
              "team_fouls_total": ("exp_fouls", "fautes", (12.0, 45.0), 0.03),
              "team_fouls_home": ("exp_fouls_home", "fautes", (5.0, 25.0), 0.05),
              "team_fouls_away": ("exp_fouls_away", "fautes", (5.0, 25.0), 0.05),
              "team_throws_total": ("exp_throws", "touches", (20.0, 70.0), 0.03),
              "team_throws_home": ("exp_throws_home", "touches", (8.0, 40.0), 0.05),
              "team_throws_away": ("exp_throws_away", "touches", (8.0, 40.0), 0.05),
              "team_offsides_total": ("exp_offsides", "hors-jeu", (1.0, 10.0), 0.10)}
STAT_MARKETS = tuple(STAT_SPECS)
ALL_FAMILIES = FAMILIES + STAT_MARKETS
EXP_KEY = {prematch.CORNERS: "exp_corners", prematch.CARDS: "exp_cards"} | {m: v[0] for m, v in STAT_SPECS.items()}
SIDE_MARKETS = {"team_shots": "shots", "team_sot": "sot", "team_fouls": "fouls"}         # marchés par équipe de strength.lines (côté dans l["side"])
DEEP_LINES = {"corners": (6.5, 13.5), "cards": (1.5, 7.5)}      # lignes de corners / cartons proposées avec le modèle approfondi
DEEP_MIN_Q = 0.35          # qualité des données minimale pour se fier au modèle approfondi
EVAL_MAX = 500             # matchs examinés au plus (le calcul est local, mais les profils sont cherchés par noms approchés)


def shrink(p):
    """Proba rapprochée de 50 % : le modèle de Poisson est trop sûr de lui sur les cas extrêmes."""
    return 0.5 + (p - 0.5) * C.FAIR_SHRINK


def stat_q(f, market):
    """Qualité (0 à 1) du modèle approfondi pour la statistique d'un marché : celle des corners, cartons, tirs... (d["qs"]) ; celle
    du modèle de buts pour buts, les 2 marquent et double chance."""
    d = f.get("deep") or {}
    stat = {prematch.CORNERS: "corners", prematch.CARDS: "cards"}.get(market) or (market.split("_")[1] if market in STAT_SPECS else "goals")
    q = (d.get("qs") or {}).get(stat)
    return d.get("q", 0.0) if q is None else q

def deep_options(f, home, away):
    """Options du modèle approfondi (f["deep"]) : [(marché, côté, ligne, proba brute, justification, référence naïve)] pour
    les familles réglables (buts 0,5-4,5, les 2 marquent, double chance, corners, cartons, tirs et tirs cadrés). [] si la
    qualité est trop faible."""
    d = f.get("deep")
    if not d or d.get("q", 0) < DEEP_MIN_Q:
        return []
    why = d.get("why") or {}
    lh, la = d["lh"], d["la"]
    short = lambda t: strength.shorten(t, 150)                  # noqa: E731 - le message Telegram a une taille limite
    tag = {"goals": short(f"modèle Kelly : {lh:.1f}-{la:.1f} buts attendus" + (f" ; {why['goals']}" if why.get("goals") else ""))}
    for kind, market, unit in (("corners", prematch.CORNERS, "corners"), ("cards", prematch.CARDS, "cartons")):
        s = d["stats"].get(kind)
        if s:
            tag[market] = short(f"modèle Kelly : {s['h'] + s['a']:.1f} {unit} attendus" + (f" ; {why[kind]}" if why.get(kind) else ""))
    out = []
    kinds = {prematch.CORNERS: "corners", prematch.CARDS: "cards"}
    for l in strength.lines(d, home, away):
        mk, pt = l["market"], l["point"]
        if mk in SIDE_MARKETS and l.get("side"):
            mk = f"{mk}_{l['side']}"
        if mk not in ALL_FAMILIES:
            continue
        if mk in kinds:
            s = d["stats"].get(kinds[mk])
            lo, hi = DEEP_LINES[kinds[mk]]
            if not s or not lo <= pt <= hi or not prematch.BOUNDS[kinds[mk]][0] <= s["h"] + s["a"] <= prematch.BOUNDS[kinds[mk]][1]:
                continue
            if l.get("q", 0) < DEEP_MIN_Q:                      # qualité de CETTE statistique (couverture, équipes), pas celle des buts
                continue
        elif mk in STAT_SPECS:
            s = d["stats"].get(l["stat"])
            mean = None if not s else (s["h"] + s["a"] if mk.endswith("_total") else s["h" if mk.endswith("_home") else "a"])
            lo, hi = STAT_SPECS[mk][2]
            if mean is None or not lo <= mean <= hi or l.get("q", 0) < DEEP_MIN_Q:
                continue
            tag[mk] = short(f"modèle Kelly : {mean:.1f} {STAT_SPECS[mk][1]} attendus" + (f" ; {why[l['stat']]}" if why.get(l["stat"]) else ""))
        out.append((mk, l["name"], pt, l["p"], tag.get(mk, tag["goals"]), l["base"]))
    return out


def _options(f, home, away):
    """(options à 5 éléments, références naïves {(marché, côté, ligne): proba}) ; voir options()."""
    # profils API-Football courts (accumulation jour après jour) : dès APISTATS_MIN_N matchs, avec la qualité réduite de features.aps_quality
    if min(f.get("n_home") or 0, f.get("n_away") or 0) < (C.APISTATS_MIN_N if f.get("aps_keys") else C.FAIR_MIN_N):
        return [], {}
    deep = deep_options(f, home, away)
    if deep:
        # le modèle approfondi ne connaît que ses statistiques : les profils API-Football complètent les autres (corners, tirs...)
        extra = _stat_options(f, home, away, skip={o[0] for o in deep}, only=set(f.get("aps_keys") or ()))
        return [o[:5] for o in deep] + extra, {(o[0], o[1], o[2]): o[5] for o in deep}
    return _simple_options(f, home, away), {}


def options(f, home, away):
    """Toutes les options (marché, côté, ligne, proba brute, justification) permises par les chiffres d'avant-match `f`
    (sources.match_features, ou dict {exp_corners, exp_cards, n_home, n_away} d'API-Football). [] si les données
    manquent ou si une équipe a trop peu de matchs récents."""
    return _options(f, home, away)[0]


def stat_n(f, market):
    """Matchs de données derrière le chiffre d'un marché : buts du classement FotMob (f["std_n"]), autres chiffres d'un profil court
    (FotMob matchs, API-Football : f["aps_n"]), sinon l'effectif de la source principale (n_home / n_away). Les buts d'un classement
    de 20 matchs ne rendent pas fiables des corners tirés de 3 matchs."""
    key = EXP_KEY.get(market, "exp_goals_home")
    own = set(f.get("aps_keys") or ())
    if key in own:
        n = f.get("std_n") if key.startswith("exp_goals") and f.get("std_n") else f.get("aps_n")
        if n:
            return min(n)
    return min(f.get("n_home") or 0, f.get("n_away") or 0)


def _stat_options(f, home, away, skip=(), only=None):
    """Options sur corners, cartons (Poisson, lignes fixes), tirs et tirs cadrés (binomiale négative, lignes autour de la moyenne).
    `skip` : marchés déjà traités ; `only` : si donné, seulement les chiffres dont la clé (exp_corners...) y figure."""
    out = []
    note = lambda market: (f" ({stat_n(f, market)} derniers matchs seulement)"        # noqa: E731 - effectif de la source de CE chiffre
                           if f.get("aps_keys") and stat_n(f, market) < C.APISTATS_LAST else "")
    for kind, market, unit in (("corners", prematch.CORNERS, "corners"), ("cards", prematch.CARDS, "cartons")):
        exp = f.get("exp_" + kind)
        if market in skip or exp is None or (only is not None and "exp_" + kind not in only):
            continue
        if not prematch.BOUNDS[kind][0] <= exp <= prematch.BOUNDS[kind][1]:
            continue
        for pt in LINES[kind]:
            for name in ("Over", "Under"):
                out.append((market, name, pt, prematch.over_p(name, prematch.poisson_over(exp, pt)),
                            f"stats réelles : {exp:.1f} {unit} attendus{note(market)}"))
    for market, (key, unit, (lo, hi), alpha) in STAT_SPECS.items():
        exp = f.get(key)
        if market in skip or exp is None or not lo <= exp <= hi or (only is not None and key not in only):
            continue
        pmf = strength.nb_pmf(exp, alpha)
        for pt in strength._lines_near(exp, alpha, lo=1):
            for name in ("Over", "Under"):
                out.append((market, name, pt, prematch.over_p(name, strength.p_over(pmf, pt)),
                            f"stats réelles : {exp:.1f} {unit} attendus{note(market)}"))
    return out


def _simple_options(f, home, away):
    """Options du modèle simple (moyennes glissantes, Poisson) : repli sans modèle approfondi."""
    out = []
    gm = prematch.goal_means(f)
    lo, hi = prematch.BOUNDS["goals"]
    if gm and all(lo <= x <= hi for x in gm[:2]):
        lh, la, label = gm
        for pt in LINES["goals"]:
            for name in ("Over", "Under"):
                out.append(("totals", name, pt, prematch.p_totals(lh, la, pt, name),
                            f"{label} : {lh + la:.1f} buts attendus ({lh:.1f}-{la:.1f})"))
        for name in ("Yes", "No"):
            out.append(("btts", name, None, prematch.p_btts(lh, la, name), f"{label} : {lh:.1f}-{la:.1f} buts attendus"))
        ph, pd, pa = prematch.p_1x2(lh, la)
        tag = f"{label} : {lh:.1f}-{la:.1f} buts attendus"
        out.append(("double_chance", f"{home} or Draw", None, ph + pd, tag))
        out.append(("double_chance", f"Draw or {away}", None, pd + pa, tag))
    return out + _stat_options(f, home, away)


def best_per_family(opts, families, bases=None):
    """Pour chaque marché, l'option dont la proba (rapprochée de 50 %) est dans C.FAIR_P_RANGE et la plus proche de
    C.FAIR_P_TARGET : une ligne trop facile (cote juste < 1.25) ou trop risquée n'est pas un pronostic. Avec `bases`
    ({(marché, côté, ligne): proba d'un match moyen}), à écart égal on préfère la ligne où le modèle s'éloigne le plus de
    la référence naïve (l'écart de proba compte pour moitié dans le choix)."""
    lo, hi = C.FAIR_P_RANGE
    best = {}
    for market, name, point, p, tag in opts:
        p = shrink(p)
        if market not in families or not lo <= p <= hi:
            continue
        gap = abs(p - C.FAIR_P_TARGET)
        base = (bases or {}).get((market, name, point))
        if base is not None:
            gap -= 0.5 * (p - shrink(base))
        if market not in best or gap < best[market][0]:
            best[market] = (gap, market, name, point, p, tag)
    return [b[1:] for b in best.values()]


def alternatives(opts, bases, market):
    """Lignes possibles d'un marché pour le choix à la cote 1xBet : [[côté, ligne, proba rapprochée de 50 %, référence ou None]],
    proba dans C.FAIR_P_RANGE seulement (même règle que best_per_family)."""
    lo, hi = C.FAIR_P_RANGE
    out = []
    for m, name, point, p, _ in opts:
        base = (bases or {}).get((m, name, point))
        if m == market and lo <= shrink(p) <= hi:
            out.append([name, point, round(shrink(p), 4), None if base is None else round(shrink(base), 4)])
    return out


def side_name(market, ev):
    """Équipe visée par un marché « par équipe » (suffixe _home / _away), sinon None."""
    if market in STAT_SPECS and not market.endswith("_total"):
        return ev["home_team"] if market.endswith("_home") else ev["away_team"]
    return None


def make_pick(ev, market, name, point, p, tag, src):
    """Pronostic d'un match ; `p` : proba brute (rapprochée de 50 %), corrigée par la fiabilité mesurée du marché (calib)."""
    p0, p = p, calib.calibrate(market, p, sport_key=ev.get("sport_key"))
    home, away = ev["home_team"], ev["away_team"]
    who = side_name(market, ev)
    return {"key": f'{ev["id"]}|{market}||{point if point is not None else ""}|{name}', "id": ev["id"],
            "sport_key": ev["sport_key"], "league": ev.get("league") or catalogue.name(ev["sport_key"]), "event": f"{home} - {away}",
            "home": home, "away": away, "commence": ev["commence_time"], "start": ev["commence_time"][11:16],
            "market": market, "name": name, "point": point, "who": who, "group": "soccer",
            "pick": odds.label(market, name, point, who, "soccer"), "prob": round(p, 4), "p0": round(p0, 4), "fair": round(1 / p, 2),
            "min_odds": round(1 / p * (1 + C.FAIR_MARGIN), 2), "why": tag, "src": src,
            "score": (p - 0.5) * C.MARKET_WEIGHT.get(market, 1.0)} | ({"xgid": ev["xgid"]} if ev.get("xgid") else {})


def _with_base(c, base, q):
    """Pronostic du modèle approfondi : score = moyenne de (proba - 50 %) et (proba - référence naïve), x poids du marché,
    x (0,5 + 0,5 x qualité) ; ajoute c["base"] (référence rapprochée) et c["conf"] (qualité des données)."""
    if base is None:
        return c
    b = calib.calibrate(c["market"], shrink(base), sport_key=c.get("sport_key"))
    w = C.MARKET_WEIGHT.get(c["market"], 1.0)
    c["base"], c["conf"] = round(b, 4), round(q or 0.0, 2)
    c["score"] = ((c["prob"] - 0.5) + (c["prob"] - b)) / 2 * w * (0.5 + 0.5 * (q or 0.0))
    return c


def simple_q(f, market):
    """Qualité (0 à 1) des chiffres d'un pronostic du modèle simple : celle du profil API-Football si c'est lui qui les donne,
    sinon croissante avec le nombre de matchs récents (5 matchs : 0,4 ; 10 : 0,6)."""
    if EXP_KEY.get(market, "exp_goals_home") in (f.get("aps_keys") or ()):
        return f.get("aps_q", 0.4)
    return min(0.7, 0.4 + 0.04 * (min(f.get("n_home") or 0, f.get("n_away") or 0) - C.FAIR_MIN_N))


def africa_event(f):
    """Match africain d'API-Football (stats._entry avec « lg ») sous la forme d'un événement du relevé gratuit."""
    ko = datetime.fromtimestamp(f["ts"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": f"af{f['id']}", "sport_key": "africa", "league": f["lg"], "home_team": f["home"], "away_team": f["away"],
            "commence_time": ko}


def same_match(a, b):
    """Même match vu par deux sources (relevé des cotes et liste API-Football) : coup d'envoi à 3 h près, les deux équipes reconnues."""
    ta, tb = (datetime.fromisoformat(x["commence_time"].replace("Z", "+00:00")) for x in (a, b))
    return (abs((ta - tb).total_seconds()) <= 3 * 3600 and stats.team_sim(a["home_team"], b["home_team"]) >= 0.8
            and stats.team_sim(a["away_team"], b["away_team"]) >= 0.8)


def make(scan, picks=(), day=None, deadline=None, africa=(), all_families=False):
    """Pronostics du jour, du meilleur au moins bon (au plus 2 par match). `scan` : relevé gratuit (odds.scan_today),
    `picks` : sélections avec cotes réelles (leurs marchés ne sont pas refaits : seulement corners/cartons des matchs
    payés qui n'ont pas de ligne chez les books). `africa` : matchs africains du jour (stats.africa_matches), analysés avec
    les seuls profils API-Football. `deadline` : instant time.monotonic au-delà duquel on s'arrête. `all_families` : garder le
    meilleur pronostic de CHAQUE marché du match (relevé 1xBet : la cote réelle choisit ensuite le pari, kelly/xprice.py) au lieu
    des 2 meilleurs plus tirs et tirs cadrés."""
    have = {}
    for c in picks:
        have.setdefault(c["id"], set()).add(c["market"])
    counts = {k: len(v) for k, v in scan["leagues"].items()}
    # Les profils API-Football (kelly/apistats.py) remplacent l'ancien repli « moyennes d'équipe » match par match.
    api_left, out, seen = (0 if apistats.active() else C.FAIR_API_EVENTS), [], 0
    events = [(key, ev) for key in catalogue.prioritise(counts, C.SPORTS) for ev in
              sorted(scan["leagues"][key], key=lambda e: e["commence_time"])]
    # un match africain déjà dans le relevé des cotes (qualifications CAF...) n'est pas analysé deux fois : deux jambes d'un même combiné
    events += [("africa", ev) for ev in (africa_event(f) for f in africa if f.get("status") not in stats.DONE)
               if not any(same_match(ev, e) for _, e in events)]
    for key, ev in events:
        if seen >= EVAL_MAX or (deadline and time.monotonic() > deadline):
            return _rank(out)
        seen += 1
        paid = ev["id"] in have
        families = {prematch.CORNERS, prematch.CARDS} - have.get(ev["id"], set()) if paid else set(ALL_FAMILIES)
        f = odds.match_numbers(ev["home_team"], ev["away_team"], key)
        (opts, bases), src = _options(f, ev["home_team"], ev["away_team"]), "football-data" if key != "africa" else "API-Football"
        if not opts and not paid and api_left > 0 and day is not None and stats.left_today() > C.FAIR_API_RESERVE:
            api_left -= 1
            try:
                exp = stats.expected_totals(day, ev["home_team"], ev["away_team"], ev["commence_time"])
            except Exception as e:               # noqa: BLE001
                print(f"[fair] API-Football ignoré : {type(e).__name__}", file=sys.stderr)
                exp = None
            if exp:
                opts = options({"exp_corners": exp["corners"], "exp_cards": exp["cards"],
                                "n_home": exp["n"], "n_away": exp["n"]}, ev["home_team"], ev["away_team"])
                src = "api"
        if f.get("aps_keys") and src == "football-data":
            src = "API-Football"
        picks_ = []
        for b in best_per_family(opts, families, bases):
            c = make_pick(ev, *b, src)
            c["alts"] = alternatives(opts, bases, b[0])        # autres lignes du marché : la meilleure à la cote 1xBet (xprice.py)
            c["n_min"] = stat_n(f, b[0])        # matchs de données de l'équipe la moins connue, pour la source de CE chiffre
            base = bases.get((b[0], b[1], b[2]))
            if base is not None:
                _with_base(c, base, stat_q(f, b[0]))          # qualité de la statistique du pari (pas celle des buts)
            else:
                c["conf"] = round(simple_q(f, b[0]), 2)
            picks_.append(c)
        best = sorted(picks_, key=lambda c: -c["score"])
        keep = best if all_families else best[:2]
        for prefix in ("team_shots_", "team_sot_"):          # tirs et tirs cadrés : toujours le meilleur de chaque, pour leurs combinés
            shot = next((c for c in best if c["market"].startswith(prefix)), None)
            if shot and shot not in keep:
                keep.append(shot)
        out += keep
    return _rank(out)


def _rank(pronos):
    return sorted(pronos, key=lambda c: -c["score"])


def spread(pronos, per_league=2):
    """Réordonne pour varier l'affichage : d'abord les meilleurs pronostics avec au plus `per_league` matchs par ligue,
    puis le reste (même ordre de score)."""
    first, rest, seen = [], [], {}
    for c in pronos:
        n = seen.setdefault(c["sport_key"], set())
        if c["id"] in n or len(n) < per_league:
            n.add(c["id"])
            first.append(c)
        else:
            rest.append(c)
    return first + rest


def fmt_prono(c):
    return (f'   → {c["pick"]} : proba Kelly {c["prob"]*100:.0f}% · cote juste {c["fair"]:.2f} → joue si la cote ≥ '
            f'{c["min_odds"]:.2f} chez 1xBet' + (f' ({c["why"]})' if c.get("why") else ""))


def fmt_pronos(pronos, limit=None):
    """Message « 🎲 PRONOSTICS KELLY » : un bloc par match, les paris sans cote de bookmaker clairement annoncés."""
    pronos = spread(pronos)
    shown = pronos[:limit or C.FAIR_MAX]
    lines = ["🎲 PRONOSTICS KELLY (sans cote de bookmaker)",
             "Matchs hors budget de cotes, analysés avec les stats gratuites. Kelly donne sa probabilité et la cote "
             "juste (1 ÷ proba) : compare chez 1xBet et ne joue que si la cote proposée est ≥ à celle indiquée "
             f"(marge de {C.FAIR_MARGIN*100:.0f}%). Hors combinés."]
    seen = []
    for c in shown:
        if c["id"] not in seen:
            seen.append(c["id"])
            lines.append(f'• {c["start"]} {c["event"]} [{c["league"]}]')
        lines.append(fmt_prono(c))
    if len(pronos) > len(shown):
        lines.append(f"(+ {len(pronos) - len(shown)} autres : /match <équipe> pour un match précis)")
    return "\n".join(lines)


# ---------- suivi (calibration) ----------

ROW = ("key", "id", "sport_key", "home", "away", "commence", "market", "name", "point", "prob", "p0", "xodds")   # fair.json : petites


def pick_label(r):
    who = side_name(r["market"], {"home_team": r["home"], "away_team": r["away"]})
    return odds.label(r["market"], r["name"], r.get("point"), who, "soccer")


def fair_odds(r):
    return round(1 / r["prob"], 2)


def load():
    rows = store.load(FILE, [])
    return rows if isinstance(rows, list) else []


def record(pronos, day, shown=None):
    """Enregistre dans data/fair.json les C.FAIR_TRACK meilleurs pronostics du jour, plus jusqu'à C.FAIR_TRACK_FREE pronostics
    de marchés réglés avec le seul score, suivis en silence (« x » : absents du message de résultats, comptés dans le bilan et la
    fiabilité). Fantômes sans cote : seuls leurs résultats servent, jamais la cote de clôture ni l'apprentissage. Garde
    C.FAIR_KEEP_DAYS jours. Un pronostic déjà suivi en silence (« x ») puis montré sort du silence ; une ligne qui change dans la
    journée (même match, même marché) met à jour le suivi existant au lieu d'en ajouter un second. -> nombre ajouté."""
    from datetime import date, timedelta
    rows = load()
    by_key = {r["key"]: r for r in rows}
    slot = {(r["date"], r["id"], r["market"]): r for r in rows}          # un suivi par match et marché (avant ce passage)
    n = 0
    main = list(shown) if shown is not None else pronos[:C.FAIR_TRACK]     # `shown` : pronostics montrés (un viable par match)
    rest = [c for c in pronos if c not in main]
    extra = [c for c in rest if c["market"] in FREE_MARKETS][:C.FAIR_TRACK_FREE]
    extra += [c for c in rest if c["market"].startswith(("team_shots_", "team_sot_"))][:C.FAIR_TRACK_SHOTS]
    for c in main + extra:
        old = by_key.get(c["key"])
        if old is None:
            old = slot.get((str(day), c["id"], c["market"]))
            if old is not None and old.get("result") is None:      # la ligne a changé (relevé 1xBet) : même suivi, mis à jour
                old.update({k: c.get(k) for k in ("key", "name", "point", "prob", "p0", "xodds")})
                by_key[c["key"]] = old
            elif old is None:
                by_key[c["key"]] = row = {k: c.get(k) for k in ROW} | {"date": str(day), "result": None} | ({"x": 1} if c in extra else {})
                rows.append(row)
                n += 1
                continue
            else:                                                   # déjà réglé : on n'y touche pas
                continue
        if c in main and old.get("result") is None:                 # suivi en silence le matin, annoncé depuis : il sort du silence
            old.pop("x", None)
    cut = str(date.fromisoformat(str(day)) - timedelta(days=C.FAIR_KEEP_DAYS))
    store.save(FILE, [r for r in rows if r["date"] >= cut])
    return n


def summary(rows, days=None, today=None):
    """Ligne de bilan des pronostics réglés : réussite et proba annoncée moyenne (la calibration), '' si rien de réglé."""
    from datetime import date, timedelta
    done = [r for r in rows if r.get("result") in ("win", "loss")]
    if days:
        cut = str((date.fromisoformat(str(today)) if today else date.today()) - timedelta(days=days))
        done = [r for r in done if r["date"] >= cut]
    if not done:
        return ""
    w = sum(1 for r in done if r["result"] == "win")
    p = sum(r["prob"] for r in done) / len(done)
    return (f"🎲 Pronostics sans cote : {w}/{len(done)} gagnés ({w/len(done)*100:.0f}%) pour une proba annoncée "
            f"moyenne de {p*100:.0f}%")


# ---------- combinés à cote juste : suivi (data/fair_combos.json) ----------

COMBOS_FILE = "fair_combos.json"
LEG_FIELDS = ROW + ("pick", "min_odds", "start", "league")        # une jambe de combiné garde de quoi être réglée seule
SENT_FIELDS = LEG_FIELDS + ("odds", "xref")        # + de quoi refaire le combiné et son code coupon (dernière version envoyée)


def load_combos():
    rows = store.load(COMBOS_FILE, [])
    return rows if isinstance(rows, list) else []


def combo_leg(c):
    """Jambe d'un combiné suivi : de quoi la régler seule et, pour un combiné aux cotes 1xBet, le refaire (cote, référence du pari)."""
    return {k: c.get(k) for k in SENT_FIELDS if c.get(k) is not None or k in LEG_FIELDS}


def combo_row(target, combo, day, stake=None, cur=None, coupon=None, at=None):
    """Ligne de suivi d'un combiné à cote juste (jambes, cote mini, proba ; mise sur le budget du jour et devise si elle existe ; code
    coupon 1xBet de la version envoyée, `coupon`, et heure « HH:MM » de ses cotes, `at`) ; le résultat vient plus tard (settle_fair_combos)."""
    legs, min_total, prob = combo
    return {"date": str(day), "tier": target, "min_odds": round(min_total, 2), "prob": round(prob, 5),
            "keys": [c["key"] for c in legs], "legs": [combo_leg(c) | {"result": None} for c in legs],
            "result": None} | ({} if stake is None else {"stake": stake} | ({"cur": cur} if stake else {})) \
        | ({"coupon": coupon} if coupon else {}) | ({"at": at} if at else {})                 # mise 0 : pari sans mise


def update_combo(row, combo, hhmm, coupon=None):
    """Combiné du matin renvoyé à jour : la ligne de suivi prend la nouvelle version (jambes, clés, cote, proba, code coupon `coupon`) ; la
    mise et la devise restent, les clés du matin sont gardées une fois (« orig_keys »), « updated » note l'heure. Un combiné déclaré mort qui revit
    redevient un combiné normal. Si les paris ou la cote totale changent, la note « joué à … » (/joue) de l'ancienne version tombe : c'est
    un autre ticket, l'utilisateur note la nouvelle version."""
    legs, min_total, prob = combo
    if (set(row.get("keys") or []) != {c["key"] for c in legs}
            or abs((row.get("min_odds") or 0) - round(min_total, 2)) > 1e-9):
        row.pop("played", None)
    row.setdefault("orig_keys", list(row.get("keys") or []))
    row.update(min_odds=round(min_total, 2), prob=round(prob, 5), keys=[c["key"] for c in legs], updated=hhmm,
               legs=[combo_leg(c) | {"result": None} for c in legs], at=hhmm)
    if coupon:
        row["coupon"] = coupon
    else:
        row.pop("coupon", None)                      # l'ancien code ne correspond plus à cette version
    if row.pop("dead", None):
        row["result"] = None
        if row.pop("auto_skipped", None):
            row.pop("skipped", None)
    return row


def kill_combo(row):
    """Combiné du matin déclaré injouable (« ne le joue pas ») : plus rien à suivre. Réglé « annulé » avec la marque « dead » (hors
    des résultats, du bilan et de l'argent) ; sur le budget du jour il passe aussi en « ❌ Pas joué » (money.skip)."""
    row["dead"] = True
    row["result"] = "void"
    if row.get("cur") and not row.get("skipped"):
        row["skipped"] = row["auto_skipped"] = True
    return row


def record_combos(sent, day, stakes=None, cur=None, coupons=None, at=None):
    """Enregistre les combinés à cote juste envoyés [(palier, combiné)], un seul par palier et par jour (le premier, comme
    settle.record_combos), garde C.FAIR_COMBO_KEEP_DAYS jours.
    `stakes` : {palier: mise sur le budget du jour}, `coupons` : {palier: code coupon envoyé}, `at` : heure « HH:MM » du relevé 1xBet. -> nombre ajouté. Fichier à part de bets.json (pas de bankroll)."""
    from datetime import date, timedelta
    rows = load_combos()
    n = 0
    for target, combo in sent:
        row = combo_row(target, combo, day, (stakes or {}).get(target), cur, (coupons or {}).get(target), at)
        if not any(r["date"] == row["date"] and r["tier"] == target for r in rows):      # relance du matin : un seul combiné par palier et par jour (le premier)
            rows.append(row)
            n += 1
    cut = str(date.fromisoformat(str(day)) - timedelta(days=C.FAIR_COMBO_KEEP_DAYS))
    store.save(COMBOS_FILE, [r for r in rows if r["date"] >= cut])
    return n
