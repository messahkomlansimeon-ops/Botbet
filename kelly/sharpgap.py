"""Écarts « sharp » : 1xBet paie plus que la cote juste des books de référence.

Pour les matchs et marchés couverts par The Odds API, les paris réels du jour (data/today.json, "picks") portent la proba « vraie » des books de
référence (Pinnacle pesé, marge de Shin retirée : c["prob_book"], c["prob_src"] « pinnacle » ou « sharp », c["n_sharp"], voir odds.consensus) et,
une fois remis au prix 1xBet (xprice.price_picks), la cote 1xBet c["xodds"] et sa référence c["xref"] (de quoi enregistrer un coupon). L'écart est

    edge = cote 1xBet x proba sharp - 1

C'est la seule source de valeur que l'audit n'a pas démentie : 1xBet est un book mou, en retard sur les books de référence ; ce n'est PAS un
avis du modèle de Kelly. Un candidat doit passer ces filtres (chacun compté dans scan()["rejected"]) :
  - no_sharp / few_sharp : proba venant d'un book de référence, Pinnacle présent ou au moins C.SHARPGAP_MIN_SHARP books de référence ;
  - no_xodds : le pari a une cote 1xBet (c["xodds"], sinon celle du même pari chez un pronostic relevé par xprice.apply) ;
  - soon : le match commence dans plus de C.SHARPGAP_SOON_MIN minutes ;
  - age_unknown / stale : le relevé sharp a un âge connu (c["sharp_at"], sinon la date du pari dans odds_history.json, voir sharp_age) d'au plus
    C.SHARPGAP_MAX_AGE_MIN minutes : les cotes du matin sont périmées l'après-midi (un book de référence bouge, 1xBet aussi) ;
  - x_stale : relevé 1xBet pas plus vieux que C.SHARPGAP_MAX_X_AGE_MIN minutes (âge inconnu : accepté, la cote est revérifiée en direct avant
    d'envoyer un coupon) ;
  - edge : écart >= C.SHARPGAP_EDGE ; suspect : écart > C.SHARPGAP_MAX_EDGE (cote en retard ou pari mal apparié : écarté, pas annoncé).
Les candidats sont classés par écart décroissant, un seul par match et marché (per_match) pour ne pas empiler des paris corrélés. Chaque candidat
est un dict prêt à afficher (describe()), à enregistrer en coupon (xref, xodds, xgid) et à suivre (key, id, market...). Les refus pour âge ou
cote 1xBet périmée qui auraient eu un écart suffisant sont gardés dans scan()["refused"] (de quoi dire « 3 écarts refusés : relevé sharp trop vieux »).
"""
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import config as C, ensemble, store

REASONS = ("no_sharp", "few_sharp", "no_xodds", "soon", "age_unknown", "stale", "x_stale", "edge", "suspect")


def _dt(iso):
    """Instant (aware, UTC) d'un texte ISO, « Z » compris ; None si illisible."""
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def sharp_age(c, now, history=None):
    """Âge en minutes du relevé sharp du pari `c` : c["sharp_at"] (heure ISO du relevé The Odds API, posée par odds.build_picks en phase 2), sinon
    la date de la dernière entrée du pari dans odds_history.json (`history` : {clé: {"t": ISO}}, snapshot() à chaque relevé) ; None si inconnue."""
    t = _dt(c.get("sharp_at")) if c.get("sharp_at") else None
    if t is None:
        h = (history or {}).get(c.get("key"))
        t = _dt(h.get("t")) if isinstance(h, dict) and h.get("t") else None
    return None if t is None else max(0.0, (now - t).total_seconds() / 60)


def x_age(today, c, now):
    """Âge en minutes du relevé 1xBet du pari : c["x_at"] (ISO) ou l'heure « HH:MM » (UTC) de today["picks_at"] / today["xbet_at"] ; None si inconnu."""
    t = _dt(c.get("x_at")) if c.get("x_at") else None
    if t is None:
        hhmm = today.get("picks_at") or today.get("xbet_at")
        try:
            h, m = (int(x) for x in str(hhmm).split(":")[:2])
            t = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if t > now:
                t -= timedelta(days=1)
        except (TypeError, ValueError):
            t = None
    return None if t is None else max(0.0, (now - t).total_seconds() / 60)


def _xprices(pronos):
    """{identité du pari: (cote, référence, id 1xBet)} des pronostics relevés chez 1xBet : le même pari que le pari réel, au même prix."""
    out = {}
    for c in pronos or []:
        if c.get("xodds"):
            out[ensemble._kid(c)] = (c["xodds"], c.get("xref"), c.get("xgid"))
    return out


def scan(today, now=None, history=None, min_edge=None, max_age=None, per_match=True, use_pronos=True):
    """Écarts sharp des paris réels de `today` (data/today.json) à l'instant `now` -> {"cands": [...], "rejected": Counter par raison,
    "refused": [candidats d'écart suffisant refusés pour âge ou relevé 1xBet périmé, "why" en plus], "n_picks": nombre de paris examinés}."""
    now = now or datetime.now(timezone.utc)
    min_edge = C.SHARPGAP_EDGE if min_edge is None else min_edge
    max_age = C.SHARPGAP_MAX_AGE_MIN if max_age is None else max_age
    history = store.load("odds_history.json", {}) if history is None else history
    xp = _xprices(today.get("pronos")) if use_pronos else {}
    out, rej, refused = [], Counter(), []
    picks = today.get("picks") or []
    for c in picks:
        s = ensemble.sharp_of(c)
        if not s:
            rej["no_sharp"] += 1
            continue
        p, src, n_sharp = s
        if not (src == "pinnacle" or n_sharp >= C.SHARPGAP_MIN_SHARP):
            rej["few_sharp"] += 1
            continue
        xodds, xref, xgid = c.get("xodds"), c.get("xref"), c.get("xgid")
        if not xodds and use_pronos:
            xodds, xref, xgid = xp.get(ensemble._kid(c), (None, None, None))
        if not xodds or xodds <= 1:
            rej["no_xodds"] += 1
            continue
        start = _dt(c.get("commence"))
        mins = None if start is None else (start - now).total_seconds() / 60
        if mins is None or mins < C.SHARPGAP_SOON_MIN:
            rej["soon"] += 1
            continue
        edge = xodds * p - 1
        age, xage = sharp_age(c, now, history), x_age(today, c, now)
        cand = {"key": c["key"], "id": c["id"], "sport_key": c.get("sport_key"), "group": c.get("group", "soccer"), "event": c.get("event"),
                "home": c.get("home"), "away": c.get("away"), "league": c.get("league"), "start": c.get("start"), "commence": c["commence"],
                "market": c["market"], "who": c.get("who"), "name": c["name"], "point": c.get("point"), "pick": c.get("pick"),
                "p_sharp": round(p, 4), "fair_odds": round(1 / p, 2), "src": src, "n_sharp": n_sharp, "n_books": c.get("n_books"),
                "xodds": xodds, "xref": xref, "xgid": xgid, "xcid": c.get("xcid"), "edge": round(edge, 4),
                "kelly_f": round(edge / (xodds - 1), 4), "age_min": None if age is None else round(age),
                "x_age_min": None if xage is None else round(xage), "mins_to_start": round(mins), "book": c.get("book"),
                "best_odds": c.get("best_odds"), "typ_odds": c.get("typ_odds")}
        why = ("age_unknown" if age is None else "stale" if age > max_age
               else "x_stale" if xage is not None and xage > C.SHARPGAP_MAX_X_AGE_MIN else "")
        if edge < min_edge:
            rej["edge"] += 1
            continue
        if why:
            rej[why] += 1
            refused.append(cand | {"why": why})
            continue
        if edge > C.SHARPGAP_MAX_EDGE:
            rej["suspect"] += 1
            continue
        out.append(cand)
    out.sort(key=lambda x: (-x["edge"], -(x["n_sharp"] or 0), x["age_min"]))
    if per_match:
        seen, keep = set(), []
        for x in out:
            k = (x["id"], x["market"])
            if k not in seen:
                seen.add(k)
                keep.append(x)
        out = keep
    refused.sort(key=lambda x: -x["edge"])
    return {"cands": out, "rejected": rej, "refused": refused, "n_picks": len(picks)}


def find(today, now=None, history=None, **kw):
    """Candidats d'écart sharp, du plus grand écart au plus petit (voir scan pour les filtres et les options)."""
    return scan(today, now, history, **kw)["cands"]


def _n(x, d=2):
    return f"{x:.{d}f}".replace(".", ",")


def describe(x):
    """Textes prêts à afficher (sans HTML) : {"title", "line", "age"}. title : le pari ; line : « 1xBet 1,95 · juste 1,81 (Pinnacle) · +7,7 % » ;
    age : « relevé sharp il y a 12 min · match dans 95 min »."""
    book = "Pinnacle" if x["src"] == "pinnacle" else f"{x['n_sharp']} books de référence"
    age = "âge du relevé sharp inconnu" if x.get("age_min") is None else f"relevé sharp il y a {x['age_min']} min"
    return {"title": x.get("pick") or x["name"],
            "line": f"1xBet {_n(x['xodds'])} · juste {_n(x['fair_odds'])} ({book}) · {x['edge'] * 100:+.1f} %".replace(".", ","),
            "age": f"{age} · match dans {x['mins_to_start']} min"}
