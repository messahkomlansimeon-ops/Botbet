"""Prix 1xBet des pronostics Kelly (kelly/sources/xbet.py), le matin, juste après leur calcul.

Pour chaque match, Kelly relève les cotes 1xBet des marchés où elle a un pronostic, puis choisit, parmi les lignes de ce marché
(c["alts"], fair.alternatives), celle qui rapporte le plus à la cote 1xBet RÉELLE : proba corrigée (calib) >= C.VIABLE_MIN_P,
match plus favorable qu'un match moyen (référence), cote 1xBet >= cote mini de Kelly (1 / proba x (1 + C.FAIR_MARGIN)) ; entre
deux lignes, d'abord celles assez probables pour un combiné (C.FAIR_COMBO_MIN_P), puis la plus grande valeur x proba (niveau
modéré : à valeur égale, la plus probable). Le pronostic porte alors
c["xodds"] (cote 1xBet), c["xev"] (valeur : proba x cote - 1) et c["xp"] (proba de 1xBet sans sa marge, pour comparaison).
Ligne proposée par 1xBet mais trop mal payée : c["xodds"] < c["min_odds"], le pronostic n'est plus viable (combos.viable) ; ligne
trop faible (proba sous C.VIABLE_MIN_P ou sous un match moyen) : même chose, comptée à part (« weak »).
Match trouvé mais pari absent chez 1xBet (pas de tirs en Ligue 2...) : c["xnone"], injouable, plus viable. Match non trouvé ou pas
relevé : le pronostic reste « à vérifier » (cote mini) dans la liste, mais n'entre plus dans un combiné (combos.fair_legs).
Les paris RÉELS de The Odds API (combinés du matin, pari du jour) passent aussi à la cote 1xBet : price_picks.

Mode « mixte » (kelly/ensemble.py, C.ENSEMBLE_ENABLED et C.ENSEMBLE_ACTIVE_FAMILIES) : l'ensemble DÉCIDE dans les familles actives (buts, 1N2, double
chance, les 2 marquent) ; ailleurs (corners, cartons, tirs, fautes, touches, hors-jeu) la règle d'avant s'applique INCHANGÉE (proba du modèle face à
la cote 1xBet, marge C.FAIR_MARGIN), et le pronostic reçoit seulement ses avis « fantômes » (_shadow : c["p_model"], c["p_x"], c["p_sharp"],
c["p_ens"], jamais c["prob"] ni c["min_odds"]) pour l'affichage et le journal d'apprentissage.

Chemin « ensemble » (familles actives) : choose / price / apply remplacent la
proba du modèle par la proba retenue p_ens (combinaison en log-cotes de la proba sharp, de la proba 1xBet sans marge et de la proba du modèle,
poids appris par famille). Un pari est « de la valeur » ssi cote 1xBet x p_ens >= 1 + C.ENSEMBLE_MARGIN et sans veto (écart modèle-marché
trop grand quand le marché pèse beaucoup). Le pronostic prix porte alors :
  c["prob"]     = p_ens (c["p_ens"] : la même, nommée) : TOUT le reste de Kelly (viable, combinés, mises, bilan) lit cette proba ;
  c["p_model"]  = la proba du modèle de Kelly de la ligne choisie (ex-c["prob"], calibrée) ; c["p0"] reste la proba brute (calib.add la lit) ;
  c["p_x"]      = proba 1xBet sans marge (c["xp"] la reprend) ; c["p_sharp"] = proba sharp (si le match est couvert) ; c["ew"] = poids du modèle ;
  c["min_odds"] = (1 + marge) / p_ens ; un pari écarté (veto, prix, ligne faible) a c["min_odds"] > c["xodds"] (combos.viable le refuse) et, pour
  un veto, c["xveto"] = 1 (c["xwhy"] : raison).
"""
import sys
import time

from . import calib, config as C, ensemble, odds, store
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


def _alts(c):
    return c.get("alts") or [[c["name"], c["point"], c.get("p0", c.get("p_model", c["prob"])), None]]


def _ens_line(c, px, name, point, p0, base, price, data):
    """Décision de l'ensemble sur une ligne du pronostic `c` à la cote `price` -> (décision ensemble.value_of, proba du modèle corrigée, référence
    corrigée ou None). La proba 1xBet sans marge vient des cotes `px` du match, la proba sharp de c["psh"] (ensemble.attach)."""
    from datetime import datetime, timezone
    sd = calib.side(c | {"name": name})
    pm = calib.calibrate(c["market"], p0, sport_key=c.get("sport_key"), side=sd)
    b = None if base is None else calib.calibrate(c["market"], base, sport_key=c.get("sport_key"), side=sd)
    v = ensemble.value_of(c["market"], pm, ensemble.x_fair(px, c["market"], name, point, c.get("home"), c.get("away")),
                          ensemble.sharp_for(c, name, point, datetime.now(timezone.utc)), price, data=data)
    return v, pm, b


def line_rank(v):
    """Rang d'une ligne validée par l'ensemble (v : ensemble.value_of), à la cote 1xBet : d'abord les « valeur » (avis sharp) avant les « probable », puis celles assez
    probables pour un combiné, puis l'espérance AU PRIX 1xBet : valeur x proba pour une espérance positive (à valeur égale, la plus probable), l'espérance seule quand
    elle est négative (la moins mal payée gagne ; valeur x proba préférerait la proba la plus basse, d'où un biais vers « Moins de » et les lignes hautes)."""
    ev = v["ev"]
    return (v.get("kind") != "probable", v["p"] >= C.FAIR_COMBO_MIN_P, ev * v["p"] if ev > 0 else ev)


def _choose_ens(c, px):
    """choose() de l'ensemble : (tuple de choose, décision) ou None. Une ligne est retenue si le modèle la voit au moins aussi bonne qu'un match moyen
    (référence), si la proba retenue p_ens atteint C.VIABLE_MIN_P et si ensemble.value_of la valide (valeur et pas de veto) ; entre deux lignes,
    mêmes priorités que le chemin d'origine mais avec p_ens (jambe de combiné possible, puis valeur x proba)."""
    data = ensemble.load()
    best, key = None, None
    for name, point, p0, base in _alts(c):
        price = xbet.lookup(px, c["market"], name, point)
        if not price:
            continue
        v, pm, b = _ens_line(c, px, name, point, p0, base, price, data)
        if (b is not None and pm < b and not ensemble.model_silent(c["market"])) or not v["ok"] or v["p"] < C.VIABLE_MIN_P:       # famille figée : le modèle n'écarte aucune ligne
            continue
        rank = line_rank(v)
        if key is None or rank > key:
            best, key = ((name, point, v["p"], p0, b, price, v["ev"]), v), rank
    return best


def choose(c, px):
    """Meilleure ligne du pronostic `c` à la cote 1xBet (`px` : xbet.prices du match) -> (côté, ligne, proba, proba brute,
    référence corrigée, cote, valeur) ou None. Dans une famille où l'ensemble décide (ensemble.is_active), la proba est p_ens (voir _choose_ens)."""
    if ensemble.is_active(c["market"]):
        got = _choose_ens(c, px)
        return got[0] if got else None
    best, key = None, None
    for name, point, p0, base in c.get("alts") or [[c["name"], c["point"], c.get("p0", c["prob"]), None]]:
        price = xbet.lookup(px, c["market"], name, point)
        if not price:
            continue
        sd = calib.side(c | {"name": name})
        p = calib.calibrate(c["market"], p0, sport_key=c.get("sport_key"), side=sd)
        b = None if base is None else calib.calibrate(c["market"], base, sport_key=c.get("sport_key"), side=sd)
        if p < C.VIABLE_MIN_P or (b is not None and p < b) or price < round(1 / p * (1 + C.FAIR_MARGIN), 2):
            continue
        ev = p * price - 1
        rank = (p >= C.FAIR_COMBO_MIN_P, ev * p)             # une ligne de valeur qui peut entrer dans un combiné passe avant
        if key is None or rank > key:
            best, key = (name, point, p, p0, b, price, ev), rank
    return best


def _weak(c):
    """La ligne actuelle du pronostic échoue à choose() pour une autre raison que le prix : proba corrigée sous C.VIABLE_MIN_P ou sous
    celle d'un match moyen."""
    cur = next((a for a in c.get("alts") or [] if (a[0], a[1]) == (c["name"], c["point"])), None)
    if cur:
        sd = calib.side(c | {"name": cur[0]})
        p = calib.calibrate(c["market"], cur[2], sport_key=c.get("sport_key"), side=sd)
        b = None if cur[3] is None else calib.calibrate(c["market"], cur[3], sport_key=c.get("sport_key"), side=sd)
    else:                                           # prob et base du pronostic sont déjà corrigées
        p, b = c["prob"], c.get("base")
    return p < C.VIABLE_MIN_P or (b is not None and p < b)


def price(c, px, refs=None):
    """Met le pronostic `c` à la cote 1xBet (en place). -> "value" (ligne jouable chez 1xBet), "low" (1xBet paie trop peu), "weak" (ligne trop faible) ou None.
    `refs` (si fourni) : {(marché, côté, ligne): [GameId, Type, Param]} (xbet.prices) -> c["xref"], la référence du pari chez
    1xBet, pour enregistrer un ticket (un code coupon) au moment du message. "weak" : la ligne du pronostic est proposée mais elle
    n'est plus assez probable (pas un problème de prix). Famille où l'ensemble décide : voir _price_ens (c["prob"] = p_ens, c["p_model"]...) ; sinon la
    règle d'avant (_price_legacy) puis les avis fantômes (_shadow) quand l'ensemble est allumé."""
    if ensemble.is_active(c["market"]):
        return _price_ens(c, px, refs)
    r = _price_legacy(c, px, refs)
    if r is not None and C.ENSEMBLE_ENABLED:
        try:
            _shadow(c, px)
        except Exception as e:                      # noqa: BLE001 - un avis fantôme en échec ne change jamais la décision d'avant
            print(f"[ensemble] avis fantôme ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    return r


def _price_legacy(c, px, refs=None):
    """price() d'avant l'ensemble (ligne choisie par choose, proba du modèle corrigée, cote mini de Kelly) : inchangé."""
    pick = choose(c, px)
    if pick:
        name, point, p, p0, b, odd, ev = pick
        moved = (name, point) != (c["name"], c["point"])
        if moved:
            c.update(name=name, point=point, key=f'{c["id"]}|{c["market"]}||{point if point is not None else ""}|{name}',
                     pick=odds.label(c["market"], name, point, c.get("who"), "soccer"))
            if b is not None:
                c["base"] = round(b, 4)
        c.update(prob=round(p, 4), p0=round(p0, 4), fair=round(1 / p, 2), min_odds=round(1 / p * (1 + C.FAIR_MARGIN), 2),
                 xodds=odd, xev=round(ev, 4), xp=xbet.book_prob(px, c["market"], name, None if point is None else float(point)))
        if moved or c.get("xodds0") is None:        # cote du matin (alerte « mieux payé ») : la première lue, ou celle de la nouvelle ligne
            c["xodds0"] = odd
        ref = (refs or {}).get((c["market"], name, None if point is None else float(point)))
        if ref:
            c["xref"] = ref
        return "value"
    odd = xbet.lookup(px, c["market"], c["name"], c["point"])
    if odd:
        c.update(xodds=odd, xev=round(c["prob"] * odd - 1, 4),
                 xp=xbet.book_prob(px, c["market"], c["name"], None if c["point"] is None else float(c["point"])))
        ref = (refs or {}).get((c["market"], c["name"], None if c["point"] is None else float(c["point"])))
        if ref:
            c["xref"] = ref
        return "weak" if _weak(c) else "low"
    return None


def _shadow(c, px):
    """Avis de l'ensemble sur la ligne ACTUELLE du pronostic `c` (famille où il ne décide pas), à partir des cotes `px` du match : c["p_model"] (la proba du
    modèle, c["prob"] inchangée), c["p_x"] (1xBet sans marge, Shin), c["p_sharp"] (si un book de référence couvre le pari et que son relevé n'est pas
    périmé), c["p_ens"] (proba qu'aurait retenue l'ensemble), c["ew"], c["esrc"]. Ne touche ni c["prob"], ni c["min_odds"], ni c["xodds"] : la
    décision reste celle d'avant. Servent à l'affichage (ensemble.explain_c, sans « retenu ») et au journal d'apprentissage (ensemble.record)."""
    from datetime import datetime, timezone
    name, point = c["name"], c["point"]
    pm = c.get("prob")
    p_x = ensemble.x_fair(px, c["market"], name, point, c.get("home"), c.get("away"))
    p_s = ensemble.sharp_for(c, name, point, datetime.now(timezone.utc))
    p, parts = ensemble.combine(ensemble.family(c["market"]), pm, p_x, p_s)
    c.update(p_model=None if pm is None else round(pm, 4), ew=round(parts["wm"], 4), esrc=parts["src"])
    for k, val in (("p_x", p_x), ("p_sharp", p_s), ("p_ens", p)):
        if val is None:
            c.pop(k, None)
        else:
            c[k] = round(val, 4)


def _ens_fields(c, v, pm, p0, odd, price_ok):
    """Champs de l'ensemble posés sur le pronostic `c` pour la décision `v` (ensemble.value_of) à la cote `odd` : la proba retenue devient c["prob"],
    celle du modèle c["p_model"]. `price_ok` faux (pari écarté) : c["min_odds"] reste au-dessus de la cote 1xBet, pour que combos.viable, qui ne
    connaît que « cote 1xBet < cote mini », le refuse aussi."""
    parts = v["parts"]
    p = v["p"]
    if price_ok and v.get("kind"):                  # « value » (avis sharp) ou « probable » (sans avis sharp) : jamais « valeur » sans sharp (fmt.pronos)
        c["xkind"] = v["kind"]
    else:
        c.pop("xkind", None)
    if v.get("ev_x") is None:
        c.pop("xev_x", None)
    else:
        c["xev_x"] = round(v["ev_x"], 4)           # espérance au prix 1xBet sans marge : combos.stake_prob n'accepte une mise que si elle est positive
    min_odds = round(ensemble.min_price(p, market=c["market"]), 2) if v.get("kind") != "probable" else round((1 + C.ENSEMBLE_PROBABLE_MIN_EV) / p, 2)
    c.update(prob=round(p, 4), p_ens=round(p, 4), p_model=round(pm, 4), p0=round(p0, 4), fair=round(1 / p, 2),
             min_odds=min_odds if price_ok else max(min_odds, round(odd + 0.01, 2)), xodds=odd, xev=round(v["ev"], 4),
             xp=None if parts["p_x"] is None else round(parts["p_x"], 4), ew=round(parts["wm"], 4), esrc=parts["src"])
    for k, val in (("p_x", parts["p_x"]), ("p_sharp", parts["p_sharp"])):
        if val is None:
            c.pop(k, None)
        else:
            c[k] = round(val, 4)
    c.pop("xveto", None)
    c.pop("xwhy", None)
    if not price_ok:
        c["xwhy"] = v["why"] or "line"              # « line » : la ligne actuelle passerait, mais elle n'est pas parmi les lignes proposées (c["alts"])
        if v["why"] in ("veto", "no_market"):
            c["xveto"] = 1


def _price_ens(c, px, refs=None):
    """price() de l'ensemble (C.ENSEMBLE_ENABLED) : même contrat que le chemin d'origine ("value", "low", "weak" ou None), avec la proba retenue
    p_ens. "low" : 1xBet paie trop peu OU l'ensemble écarte le pari (veto : c["xveto"], raison dans c["xwhy"]) ; "weak" : ligne trop faible
    (p_ens sous C.VIABLE_MIN_P, ou modèle sous un match moyen)."""
    got = _choose_ens(c, px)
    if got:
        (name, point, p, p0, b, odd, ev), v = got
        moved = (name, point) != (c["name"], c["point"])
        if moved:
            c.update(name=name, point=point, key=f'{c["id"]}|{c["market"]}||{point if point is not None else ""}|{name}',
                     pick=odds.label(c["market"], name, point, c.get("who"), "soccer"))
            if b is not None:
                c["base"] = round(b, 4)
        _ens_fields(c, v, v["parts"]["p_model"], p0, odd, True)
        if moved or c.get("xodds0") is None:        # cote du matin (alerte « mieux payé ») : la première lue, ou celle de la nouvelle ligne
            c["xodds0"] = odd
        ref = (refs or {}).get((c["market"], name, None if point is None else float(point)))
        if ref:
            c["xref"] = ref
        return "value"
    odd = xbet.lookup(px, c["market"], c["name"], c["point"])
    if odd:
        cur = next((a for a in c.get("alts") or [] if (a[0], a[1]) == (c["name"], c["point"])), None) \
            or [c["name"], c["point"], c.get("p0", c.get("p_model", c["prob"])), None]            # ligne hors c["alts"] : sa proba brute, sans référence
        v, pm, b = _ens_line(c, px, c["name"], c["point"], cur[2], cur[3], odd, ensemble.load())
        if v["p"] is None:                          # famille figée SANS aucun avis de marché : pas de proba retenue, jamais celle du modèle seul (pari écarté, « no_market »)
            for k in ("xkind", "xev", "xev_x", "p_ens", "p_x", "p_sharp"):
                c.pop(k, None)                      # rien de périmé d'un relevé précédent
            c.update(p_model=round(pm, 4), xodds=odd, xwhy="no_market", xveto=1, min_odds=max(c.get("min_odds") or 0, round(odd + 0.01, 2)))
            ref = (refs or {}).get((c["market"], c["name"], None if c["point"] is None else float(c["point"])))
            if ref:
                c["xref"] = ref
            return "low"
        _ens_fields(c, v, pm, cur[2], odd, False)
        ref = (refs or {}).get((c["market"], c["name"], None if c["point"] is None else float(c["point"])))
        if ref:
            c["xref"] = ref
        return "weak" if (v["p"] is not None and v["p"] < C.VIABLE_MIN_P) or (b is not None and pm < b and not ensemble.model_silent(c["market"])) else "low"
    return None


def apply(pronos, fetch=None, picks=None):
    """Cotes 1xBet des pronostics du jour (en place, dans la limite de C.XBET_SECONDS et C.XBET_MAX_MATCHES). Ne lève jamais.
    -> {"matches": matchs trouvés chez 1xBet, "value": pronostics jouables, "low": pronostics trop mal payés, "weak":
    pronostics à ligne trop faible, "none": absents de 1xBet}.
    Ensemble allumé (C.ENSEMBLE_ENABLED) : `picks` (paris réels du jour, The Odds API) donne leur proba sharp aux pronostics des mêmes paris (ensemble.attach),
    et, s'il y a des pronostics d'une famille que l'ensemble décide, le résultat compte en plus "veto" (pronostics écartés par le désaccord
    modèle-marché ; déjà inclus dans "low")."""
    out = {"matches": 0, "value": 0, "low": 0, "weak": 0, "none": 0}
    if not C.XBET_ENABLED or not pronos:
        return out
    if C.ENSEMBLE_ENABLED:
        if any(ensemble.is_active(c["market"]) for c in pronos):
            out["veto"] = 0                         # « veto » n'existe que s'il y a des pronostics dont l'ensemble décide (sinon le bilan est celui d'avant)
        if picks:                                   # sans paris réels (relevé de l'après-midi) : les probas sharp du matin déjà posées restent
            ensemble.attach(pronos, picks, history=store.load("odds_history.json", {}))
    try:
        events, wanted = _events(pronos), _wanted(pronos)
        if fetch is None:                           # relevé du matin : les matchs à pronostics stats d'abord (xbet.prioritise_stats) ; un relevé en direct garde l'ordre de l'appelant
            events = xbet.prioritise_stats(events, wanted)
        found = (fetch or xbet.fetch_day)(events, deadline=time.monotonic() + C.XBET_SECONDS, wanted=wanted)
    except Exception as e:                          # noqa: BLE001 - une source en panne ne casse pas le matin
        print(f"[1xbet] cotes ignorées : {type(e).__name__}: {e}", file=sys.stderr)
        return out
    out["matches"] = len(found)
    for c in pronos:
        m = found.get(c["id"])
        if m:                                       # match relu chez 1xBet : les cotes d'un relevé précédent sont remplacées
            for k in ("xodds", "xev", "xp", "xnone", "xref") + (("p_x", "p_sharp", "p_ens", "ew", "esrc", "xveto", "xwhy", "xkind", "xev_x") if C.ENSEMBLE_ENABLED else ()):
                c.pop(k, None)
            c["xgid"] = m["gid"]                    # id 1xBet : le relevé de l'après-midi le relit sans menu ni liste (données)
            if m.get("cid"):
                c["xcid"] = m["cid"]                # id permanent : lien « ouvrir chez 1xBet » (xbet.match_url)
        r = price(c, m["px"], m.get("refs")) if m else None
        if r:
            out[r] += 1
            if "veto" in out and c.get("xveto"):
                out["veto"] += 1
        elif m:                                     # match trouvé, mais 1xBet ne propose pas ce pari : injouable
            c["xnone"] = 1
            out["none"] += 1
    print(f"[1xbet] {out['matches']} match(s) trouvé(s) chez 1xBet : {out['value']} pronostic(s) jouable(s) à la cote 1xBet, "
          f"{out['low']} trop mal payé(s)" + (f" (dont {out['veto']} écarté(s) par l'ensemble)" if "veto" in out else "")
          + f", {out['weak']} trop faible(s), {out['none']} absent(s) de 1xBet.", file=sys.stderr)
    return out


def _pick_price(c, px, refs, model=None):
    """Remet le pari réel `c` (The Odds API) au prix 1xBet de `px` (xbet.prices) -> True si 1xBet le propose. La cote typique d'origine
    reste dans c["typ_odds"] (avec sa valeur, son book et son score : le suivi et l'apprentissage les relisent, voir track), la valeur
    est recalculée comme build_picks (proba corrigée x cote - 1) et le score suit l'écart de cote (comme `rescore` avec un modèle appris)."""
    price = xbet.lookup(px, c["market"], c["name"], c["point"])
    if not price:
        return False
    if c.get("typ_odds") is None:                   # première lecture seulement : une relecture ne perd pas la cote d'origine
        c.update(typ_odds=c["odds"], typ_edge=c["edge"], typ_book=c.get("book"), typ_best=c.get("best_odds"),
                 typ_heur=c.get("heuristic", c["score"]))
        c["why"] = ", ".join(w for w in str(c.get("why") or "").split(", ") if w and not w.startswith("cote plus haute"))
    d = c.get("prob_book", c["prob"]) * (price - c["odds"]) * c.get("weight", 1.0)       # écart de la valeur brute de build_picks
    c.update(odds=price, best_odds=price, book="1xBet", xodds=price, edge=c["prob"] * price - 1, score=c["score"] + d)
    c.setdefault("xodds0", price)                   # cote du matin (alerte « mieux payé »), jamais écrasée par une relecture
    if "heuristic" in c:
        c["heuristic"] += d
        if model and model.get("adopted"):
            from . import learn
            c["score"] = learn.predict(model, learn.features(c))
    # même seuil que build_picks ; une relecture remplace l'ancienne mention (sa valeur a pu changer avec la cote)
    rest = [w for w in str(c.get("why") or "").split(", ") if w and "à la cote 1xBet" not in w]
    c["why"] = ", ".join(([f"valeur {c['edge'] * 100:+.0f} % à la cote 1xBet"] if c["edge"] > 0.03 else []) + rest)
    key = xbet.resolve(px, c["market"], c["name"], c["point"])          # nom de 1xBet (double chance : ordre des équipes)
    ref = (refs or {}).get(key) if key else None
    if ref:
        c["xref"] = ref
    else:
        c.pop("xref", None)
    if C.ENSEMBLE_ENABLED:                           # avis de ce pari réel pour l'ensemble et le journal d'apprentissage (ensemble.record)
        for k, val in (("p_x", ensemble.x_fair(px, c["market"], c["name"], c["point"], c.get("home"), c.get("away"))),
                       ("p_sharp", (ensemble.sharp_of(c) or (None,))[0])):
            if val is None:
                c.pop(k, None)
            else:
                c[k] = round(val, 4)
    return True


def price_picks(picks, fetch=None, pronos=(), model=None):
    """Prix 1xBet des paris RÉELS du jour (The Odds API : combinés du matin, pari du jour, /top), juste après leur calcul : l'utilisateur
    ne joue que chez 1xBet, qui ne paie pas la cote typique des autres books. Pour chaque pari dont le match est chez 1xBet et dont le
    marché y figure (1X2, double chance, plus/moins de buts, les 2 marquent, corners), c["odds"] devient la cote 1xBet (c["book"] =
    « 1xBet », c["typ_odds"] = cote typique d'origine, valeur recalculée) et c["xodds"] / c["xref"] / c["xgid"] sont posés : le coupon
    du combiné peut être enregistré. Aucune cote inventée : dès qu'au moins un pari est chiffré, les autres reçoivent c["xnone"]
    (match trouvé, pari absent) ou c["xmiss"] (match non trouvé) et ne servent plus (combos.quality_legs). Aucun pari chiffré (1xBet
    injoignable) : rien ne change. `pronos` : ids 1xBet déjà connus (le relevé des pronostics vient d'être fait : pas de menu ni de liste).
    `model` : modèle appris (le score est recalculé). Ne lève jamais. -> {"matches", "priced", "none", "missing"}."""
    out = {"matches": 0, "priced": 0, "none": 0, "missing": 0}
    if not C.XBET_ENABLED or not picks:
        return out
    gids = {c["id"]: c["xgid"] for c in pronos if c.get("xgid")}
    events = [(k, ev | ({"xgid": gids[ev["id"]]} if ev["id"] in gids else {})) for k, ev in _events(picks) if k in xbet.LEAGUES]
    try:
        found = (fetch or xbet.fetch_day)(events, deadline=time.monotonic() + C.XBET_SECONDS, wanted=_wanted(picks)) if events else {}
    except Exception as e:                          # noqa: BLE001 - une source en panne ne casse pas le matin
        print(f"[1xbet] cotes des paris réels ignorées : {type(e).__name__}: {e}", file=sys.stderr)
        return out
    out["matches"] = len(found)
    for c in picks:
        for k in ("xnone", "xmiss"):
            c.pop(k, None)
        m = found.get(c["id"])
        if m:
            c["xgid"] = m["gid"]
            if _pick_price(c, m["px"], m.get("refs"), model):
                out["priced"] += 1
    if out["priced"]:                               # 1xBet a chiffré au moins un pari : pas de cote typique dans les combinés
        for c in picks:
            if not c.get("xodds"):
                c["xnone" if c["id"] in found else "xmiss"] = 1
                out["none" if c["id"] in found else "missing"] += 1
    print(f"[1xbet] paris réels : {out['priced']} chiffré(s) à la cote 1xBet sur {out['matches']} match(s) trouvé(s), "
          f"{out['none']} absent(s) de 1xBet, {out['missing']} sans match chez 1xBet.", file=sys.stderr)
    return out


def live_check(ids, now=None, fetch=None, fresh_min=None):
    """Relit EN DIRECT chez 1xBet les matchs `ids` du jour (data/today.json : id 1xBet connu, un gameEvents + les sous-matchs utiles),
    remet leurs pronostics à la cote du moment (meilleure ligne, trop mal payé, absent), enregistre today.json. Les matchs qui
    commencent dans moins de C.XBET_SOON_MIN min sont laissés de côté (1xBet les retire de l'avant-match).
    `fresh_min` : cotes plus récentes reprises du cache (xbet.live_mode). -> (pronostics du jour à jour, ids relus, heure « HH:MM »
    du relevé). `fetch` : injectable pour les tests."""
    from datetime import datetime, timedelta, timezone
    from . import store
    now = now or datetime.now(timezone.utc)
    today = store.load("today.json", {})
    pronos = today.get("pronos") or []
    soon = now + timedelta(minutes=C.XBET_SOON_MIN)
    sel = [c for c in pronos if c["id"] in ids and datetime.fromisoformat(c["commence"].replace("Z", "+00:00")) > soon]
    got = set()

    def run(events, deadline=None, wanted=None):
        found = (fetch or xbet.fetch_day)(events, deadline=deadline, wanted=wanted, live=True, fresh_min=fresh_min)
        got.update(found)
        return found
    if sel:
        apply(sel, fetch=run)
        for c in sel:                               # l'heure de SA cote (fmt.pronos, fraîcheur de la fenêtre des stats) : celle de cette relecture
            if c["id"] in got and c.get("xodds"):
                c["xat"] = now.strftime("%H:%M")
        store.save("today.json", today)
    return pronos, got, now.strftime("%H:%M")


def live_found(items, fetch=None, fresh_min=None):
    """Relit EN DIRECT chez 1xBet les matchs des paris `items` (dicts : id, championnat, équipes, coup d'envoi, marché ; "xgid" connu : lu
    directement) -> {id Kelly: {"gid", "px", "refs", ...}} (xbet.fetch_day). Ne lève jamais : 1xBet en panne = {}. `fetch` : injectable."""
    try:
        return (fetch or xbet.fetch_day)(_events(items), deadline=time.monotonic() + C.XBET_SECONDS, wanted=_wanted(items),
                                         live=True, fresh_min=fresh_min) or {}
    except Exception as e:                          # noqa: BLE001 - 1xBet en panne : rien de relu
        print(f"[1xbet] vérification en direct impossible : {type(e).__name__}: {e}", file=sys.stderr)
        return {}


def live_picks(legs, now=None, fetch=None, fresh_min=None):
    """Relit EN DIRECT chez 1xBet les matchs des paris RÉELS `legs` (jambes d'un combiné ou pari du jour, tirés de data/today.json) et
    les remet au prix du moment, SANS toucher aux paris du jour : chaque jambe est relue sur une copie. Mêmes règles de fraîcheur que
    live_check (`fresh_min`, matchs qui commencent dans moins de C.XBET_SOON_MIN min laissés de côté). -> [(jambe d'origine, état,
    copie remise au prix)] avec état « ok » (prix relu), « gone » (match relu mais pari absent de 1xBet) ou « stale » (match pas relu : la
    jambe garde sa cote du dernier relevé), puis l'heure « HH:MM ». `fetch` : injectable pour les tests."""
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc)
    soon = now + timedelta(minutes=C.XBET_SOON_MIN)
    sel = [c for c in legs if datetime.fromisoformat(c["commence"].replace("Z", "+00:00")) > soon]
    found = live_found(sel, fetch, fresh_min) if sel else {}
    out = []
    for c in legs:
        m = found.get(c["id"])
        c2 = dict(c)
        if not m:
            out.append((c, "stale", c2))
            continue
        c2["xgid"] = m["gid"]
        out.append((c, "ok" if _pick_price(c2, m["px"], m.get("refs")) else "gone", c2))
    return out, now.strftime("%H:%M")
