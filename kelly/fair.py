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

Ces pronostics n'ont ni cote ni mise : « compare chez 1xBet, joue si la cote >= X ». Ils n'entrent dans aucun combiné et
ne sont pas suivis pour la cote de clôture ; leurs résultats vont dans data/fair.json (règlement gratuit,
settle.settle_fair) pour mesurer si les probas de Kelly sont justes.
"""
import sys
import time

from . import catalogue, config as C, odds, prematch, stats, store, strength

FILE = "fair.json"
LINES = {"goals": (1.5, 2.5, 3.5), "corners": (7.5, 8.5, 9.5, 10.5, 11.5, 12.5), "cards": (2.5, 3.5, 4.5, 5.5, 6.5)}
FAMILIES = ("totals", "btts", "double_chance", prematch.CORNERS, prematch.CARDS)
DEEP_LINES = {"corners": (6.5, 13.5), "cards": (1.5, 7.5)}      # lignes de corners / cartons proposées avec le modèle approfondi
DEEP_MIN_Q = 0.35          # qualité des données minimale pour se fier au modèle approfondi
EVAL_MAX = 250             # matchs examinés au plus (le calcul est local, mais les profils sont cherchés par noms approchés)


def shrink(p):
    """Proba rapprochée de 50 % : le modèle de Poisson est trop sûr de lui sur les cas extrêmes."""
    return 0.5 + (p - 0.5) * C.FAIR_SHRINK


def deep_options(f, home, away):
    """Options du modèle approfondi (f["deep"]) : [(marché, côté, ligne, proba brute, justification, référence naïve)] pour
    les familles réglables (buts 0,5-4,5, les 2 marquent, double chance, corners, cartons). [] si la qualité est trop faible."""
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
        if mk not in FAMILIES:
            continue
        if mk in kinds:
            s = d["stats"].get(kinds[mk])
            lo, hi = DEEP_LINES[kinds[mk]]
            if not s or not lo <= pt <= hi or not prematch.BOUNDS[kinds[mk]][0] <= s["h"] + s["a"] <= prematch.BOUNDS[kinds[mk]][1]:
                continue
        out.append((mk, l["name"], pt, l["p"], tag.get(mk, tag["goals"]), l["base"]))
    return out


def _options(f, home, away):
    """(options à 5 éléments, références naïves {(marché, côté, ligne): proba}) ; voir options()."""
    if min(f.get("n_home") or 0, f.get("n_away") or 0) < C.FAIR_MIN_N:
        return [], {}
    deep = deep_options(f, home, away)
    if deep:
        return [o[:5] for o in deep], {(o[0], o[1], o[2]): o[5] for o in deep}
    return _simple_options(f, home, away), {}


def options(f, home, away):
    """Toutes les options (marché, côté, ligne, proba brute, justification) permises par les chiffres d'avant-match `f`
    (sources.match_features, ou dict {exp_corners, exp_cards, n_home, n_away} d'API-Football). [] si les données
    manquent ou si une équipe a trop peu de matchs récents."""
    return _options(f, home, away)[0]


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
    for kind, market, unit in (("corners", prematch.CORNERS, "corners"), ("cards", prematch.CARDS, "cartons")):
        exp = f.get("exp_" + kind)
        if exp is None or not prematch.BOUNDS[kind][0] <= exp <= prematch.BOUNDS[kind][1]:
            continue
        for pt in LINES[kind]:
            for name in ("Over", "Under"):
                out.append((market, name, pt, prematch.over_p(name, prematch.poisson_over(exp, pt)),
                            f"stats réelles : {exp:.1f} {unit} attendus"))
    return out


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


def make_pick(ev, market, name, point, p, tag, src):
    home, away = ev["home_team"], ev["away_team"]
    return {"key": f'{ev["id"]}|{market}||{point if point is not None else ""}|{name}', "id": ev["id"],
            "sport_key": ev["sport_key"], "league": catalogue.name(ev["sport_key"]), "event": f"{home} - {away}",
            "home": home, "away": away, "commence": ev["commence_time"], "start": ev["commence_time"][11:16],
            "market": market, "name": name, "point": point, "who": None, "group": "soccer",
            "pick": odds.label(market, name, point, None, "soccer"), "prob": round(p, 4), "fair": round(1 / p, 2),
            "min_odds": round(1 / p * (1 + C.FAIR_MARGIN), 2), "why": tag, "src": src,
            "score": (p - 0.5) * C.MARKET_WEIGHT.get(market, 1.0)}


def _with_base(c, base, q):
    """Pronostic du modèle approfondi : score = moyenne de (proba - 50 %) et (proba - référence naïve), x poids du marché,
    x (0,5 + 0,5 x qualité) ; ajoute c["base"] (référence rapprochée) et c["conf"] (qualité des données)."""
    if base is None:
        return c
    b = shrink(base)
    w = C.MARKET_WEIGHT.get(c["market"], 1.0)
    c["base"], c["conf"] = round(b, 4), round(q or 0.0, 2)
    c["score"] = ((c["prob"] - 0.5) + (c["prob"] - b)) / 2 * w * (0.5 + 0.5 * (q or 0.0))
    return c


def make(scan, picks=(), day=None, deadline=None):
    """Pronostics du jour, du meilleur au moins bon (au plus 2 par match). `scan` : relevé gratuit (odds.scan_today),
    `picks` : sélections avec cotes réelles (leurs marchés ne sont pas refaits : seulement corners/cartons des matchs
    payés qui n'ont pas de ligne chez les books). `deadline` : instant time.monotonic au-delà duquel on s'arrête."""
    have = {}
    for c in picks:
        have.setdefault(c["id"], set()).add(c["market"])
    counts = {k: len(v) for k, v in scan["leagues"].items()}
    api_left, out, seen = C.FAIR_API_EVENTS, [], 0
    for key in catalogue.prioritise(counts, C.SPORTS):
        for ev in sorted(scan["leagues"][key], key=lambda e: e["commence_time"]):
            if seen >= EVAL_MAX or (deadline and time.monotonic() > deadline):
                return _rank(out)
            seen += 1
            paid = ev["id"] in have
            families = {prematch.CORNERS, prematch.CARDS} - have.get(ev["id"], set()) if paid else set(FAMILIES)
            f = odds.match_numbers(ev["home_team"], ev["away_team"], key)
            (opts, bases), src = _options(f, ev["home_team"], ev["away_team"]), "football-data"
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
            q = (f.get("deep") or {}).get("q")
            mine = sorted((_with_base(make_pick(ev, *b, src), bases.get((b[0], b[1], b[2])), q)
                           for b in best_per_family(opts, families, bases)), key=lambda c: -c["score"])
            out += mine[:2]
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

ROW = ("key", "id", "sport_key", "home", "away", "commence", "market", "name", "point", "prob")   # lignes de fair.json : petites


def pick_label(r):
    return odds.label(r["market"], r["name"], r.get("point"), None, "soccer")


def fair_odds(r):
    return round(1 / r["prob"], 2)


def load():
    rows = store.load(FILE, [])
    return rows if isinstance(rows, list) else []


def record(pronos, day):
    """Enregistre les C.FAIR_TRACK meilleurs pronostics du jour dans data/fair.json (fantômes sans cote : seuls leurs
    résultats servent, jamais la cote de clôture ni l'apprentissage), garde C.FAIR_KEEP_DAYS jours. -> nombre ajouté."""
    from datetime import date, timedelta
    rows = load()
    known = {r["key"] for r in rows}
    n = 0
    for c in pronos[:C.FAIR_TRACK]:
        if c["key"] not in known:
            known.add(c["key"])
            rows.append({k: c.get(k) for k in ROW} | {"date": str(day), "result": None})
            n += 1
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
