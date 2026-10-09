"""Prix 1xBet des pronostics Kelly (kelly/sources/xbet.py), le matin, juste après leur calcul.

Pour chaque match, Kelly relève les cotes 1xBet des marchés où elle a un pronostic, puis choisit, parmi les lignes de ce marché
(c["alts"], fair.alternatives), celle qui rapporte le plus à la cote 1xBet RÉELLE : proba corrigée (calib) >= C.VIABLE_MIN_P,
match plus favorable qu'un match moyen (référence), cote 1xBet >= cote mini de Kelly (1 / proba x (1 + C.FAIR_MARGIN)) ; entre
deux lignes, la plus grande valeur x proba (niveau modéré : à valeur égale, la plus probable). Le pronostic porte alors
c["xodds"] (cote 1xBet), c["xev"] (valeur : proba x cote - 1) et c["xp"] (proba de 1xBet sans sa marge, pour comparaison).
Ligne proposée par 1xBet mais trop mal payée : c["xodds"] < c["min_odds"], le pronostic n'est plus viable (combos.viable).
Match trouvé mais pari absent chez 1xBet (pas de tirs en Ligue 2...) : c["xnone"], injouable, plus viable. Match non trouvé ou pas
relevé : le pronostic reste « à vérifier » (cote mini) dans la liste, mais n'entre plus dans un combiné (combos.fair_legs).
"""
import sys
import time

from . import calib, config as C, odds
from .sources import xbet


def _events(pronos):
    """Matchs des pronostics, du mieux classé au moins bien classé : [(clé du championnat, événement)]."""
    seen, out = set(), []
    for c in sorted(pronos, key=lambda c: -c.get("score", 0)):
        if c["id"] not in seen:
            seen.add(c["id"])
            out.append((c["sport_key"], {"id": c["id"], "home_team": c["home"], "away_team": c["away"],
                                         "commence_time": c["commence"]} | ({"xgid": c["xgid"]} if c.get("xgid") else {})))
    return out


def _wanted(pronos):
    out = {}
    for c in pronos:
        out.setdefault(c["id"], set()).add(c["market"])
    return out


def choose(c, px):
    """Meilleure ligne du pronostic `c` à la cote 1xBet (`px` : xbet.prices du match) -> (côté, ligne, proba, proba brute,
    référence corrigée, cote, valeur) ou None."""
    best, key = None, None
    for name, point, p0, base in c.get("alts") or [[c["name"], c["point"], c.get("p0", c["prob"]), None]]:
        price = xbet.lookup(px, c["market"], name, point)
        if not price:
            continue
        p = calib.calibrate(c["market"], p0, sport_key=c.get("sport_key"))
        b = None if base is None else calib.calibrate(c["market"], base, sport_key=c.get("sport_key"))
        if p < C.VIABLE_MIN_P or (b is not None and p < b) or price < round(1 / p * (1 + C.FAIR_MARGIN), 2):
            continue
        ev = p * price - 1
        if key is None or ev * p > key:
            best, key = (name, point, p, p0, b, price, ev), ev * p
    return best


def price(c, px):
    """Met le pronostic `c` à la cote 1xBet (en place). -> "value" (ligne jouable chez 1xBet), "low" (1xBet paie trop peu) ou None."""
    pick = choose(c, px)
    if pick:
        name, point, p, p0, b, odd, ev = pick
        if (name, point) != (c["name"], c["point"]):
            c.update(name=name, point=point, key=f'{c["id"]}|{c["market"]}||{point if point is not None else ""}|{name}',
                     pick=odds.label(c["market"], name, point, c.get("who"), "soccer"))
            if b is not None:
                c["base"] = round(b, 4)
        c.update(prob=round(p, 4), p0=round(p0, 4), fair=round(1 / p, 2), min_odds=round(1 / p * (1 + C.FAIR_MARGIN), 2),
                 xodds=odd, xev=round(ev, 4), xp=xbet.book_prob(px, c["market"], name, None if point is None else float(point)))
        return "value"
    odd = xbet.lookup(px, c["market"], c["name"], c["point"])
    if odd:
        c.update(xodds=odd, xev=round(c["prob"] * odd - 1, 4),
                 xp=xbet.book_prob(px, c["market"], c["name"], None if c["point"] is None else float(c["point"])))
        return "low"
    return None


def apply(pronos, fetch=None):
    """Cotes 1xBet des pronostics du jour (en place, dans la limite de C.XBET_SECONDS et C.XBET_MAX_MATCHES). Ne lève jamais.
    -> {"matches": matchs trouvés chez 1xBet, "value": pronostics jouables, "low": pronostics trop mal payés}."""
    out = {"matches": 0, "value": 0, "low": 0, "none": 0}
    if not C.XBET_ENABLED or not pronos:
        return out
    try:
        found = (fetch or xbet.fetch_day)(_events(pronos), deadline=time.monotonic() + C.XBET_SECONDS, wanted=_wanted(pronos))
    except Exception as e:                          # noqa: BLE001 - une source en panne ne casse pas le matin
        print(f"[1xbet] cotes ignorées : {type(e).__name__}: {e}", file=sys.stderr)
        return out
    out["matches"] = len(found)
    for c in pronos:
        m = found.get(c["id"])
        if m:                                       # match relu chez 1xBet : les cotes d'un relevé précédent sont remplacées
            for k in ("xodds", "xev", "xp", "xnone"):
                c.pop(k, None)
            c["xgid"] = m["gid"]                    # id 1xBet : le relevé de l'après-midi le relit sans menu ni liste (données)
        r = price(c, m["px"]) if m else None
        if r:
            out[r] += 1
        elif m:                                     # match trouvé, mais 1xBet ne propose pas ce pari : injouable
            c["xnone"] = 1
            out["none"] += 1
    print(f"[1xbet] {out['matches']} match(s) trouvé(s) chez 1xBet : {out['value']} pronostic(s) jouable(s) à la cote 1xBet, "
          f"{out['low']} trop mal payé(s), {out['none']} absent(s) de 1xBet.", file=sys.stderr)
    return out
