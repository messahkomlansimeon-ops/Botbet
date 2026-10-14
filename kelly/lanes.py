"""Voies (stratégies) : sharpgap, stats, fill. Interrupteurs on / ghost / off, ligues de la voie stats en fantôme, fraction de Kelly et cas « sans valeur » des combinés.

L'étiquette d'une ligne (c["strategy"]) est posée à sa construction (ensemble.strategy_of, xprice) et jamais recalculée. Ce module décide seulement ce qu'on a le droit d'en FAIRE :
`usable(c)` : la ligne peut-elle être choisie pour un pari ou une jambe de combiné ? Faux quand sa voie est en « ghost » ou « off » (une ligne en ghost reste calculée et
journalisée avec la référence 1xBet, elle n'est jamais sélectionnée), quand sa ligue est en fantôme (voie stats), ou quand c'est une jambe « fill » et que FILL_LEGS est coupé.
L'état de la coupure automatique de la voie stats est dans le document strategy_state.json ({"stats": {"mode": "on" | "ghost", "since", "leagues": {sport_key: {...}}}}) ;
il PRIME sur la configuration « on » et ne revient à « on » qu'à la main (reset)."""
import time

from . import config as C, ensemble, store

STATE_FILE = "strategy_state.json"
_memo = {"t": 0.0, "v": None}


def reset():
    _memo.update(t=0.0, v=None)


def state():
    """Document d'état de la coupure automatique (relu au plus toutes les 2 s : usable() est appelé dans des boucles)."""
    now = time.monotonic()
    if _memo["v"] is None or now - _memo["t"] > 2.0:
        d = store.load(STATE_FILE, {})
        d = d if isinstance(d, dict) else {}
        st = d.get("stats")
        if not isinstance(st, dict):                  # document abîmé ({"stats": "ghost"}...) : jamais un plantage de toute la sélection, l'état est lu comme vide
            d = {k: v for k, v in d.items() if k != "stats"}
        elif not isinstance(st.get("leagues") or {}, dict):
            d = d | {"stats": {k: v for k, v in st.items() if k != "leagues"}}
        _memo.update(t=now, v=d)
    return _memo["v"]


def lane_of_market(market):
    """Voie qui gère la famille d'un marché : « sharpgap », « stats », ou None (touches, hors-jeu, joueurs... : seulement des jambes « fill »)."""
    fam = ensemble.family(market or "")
    return "sharpgap" if fam in ensemble.SHARPGAP_FAMILIES else "stats" if fam in ensemble.STATS_LANE else None


def _outside_fd(sport_key):
    from .sources import footballdata as fd
    return bool(sport_key) and sport_key not in fd.STATS_SPORTS


def mode(lane, sport_key=None):
    """« on », « ghost » ou « off » pour la voie `lane` (et la ligue `sport_key` pour la voie stats)."""
    if lane == "sharpgap":
        return C.STRATEGY_SHARPGAP
    if lane != "stats":
        return "on"
    m = C.STRATEGY_STATS
    if m != "on":
        return m
    st = state().get("stats") or {}
    if st.get("mode") == "ghost":
        return "ghost"
    if sport_key and (sport_key in C.STATS_GHOST_LEAGUES or sport_key in (st.get("leagues") or {}) or (C.STATS_GHOST_OUTSIDE_FD and _outside_fd(sport_key))):
        return "ghost"
    return "on"


def usable(c):
    """La ligne `c` (pronostic ou pari réel) peut-elle être sélectionnée pour un pari ou une jambe de combiné ? Ensemble éteint : toujours (comportement d'avant)."""
    if not C.ENSEMBLE_ENABLED:
        return True
    lane = lane_of_market(c.get("market"))
    if lane and mode(lane, c.get("sport_key")) != "on":
        return False
    return not (c.get("strategy") == "fill" and not C.FILL_LEGS)


def gaps_allowed():
    """Les écarts sharp (sharpgap) peuvent-ils être annoncés, numérotés et suivis ? Ensemble éteint : comme avant (seul SHARPGAP_ENABLED décide)."""
    return (not C.ENSEMBLE_ENABLED) or mode("sharpgap") == "on"


def priced(c):
    """Faut-il lire / mettre au prix 1xBet cette ligne ? Faux seulement quand sa voie est « off » (rien de calculé) ; « ghost » est calculé comme « on »."""
    if not C.ENSEMBLE_ENABLED:
        return True
    lane = lane_of_market(c.get("market"))
    return not (lane and mode(lane, c.get("sport_key")) == "off")


def frac(lane):
    return C.KELLY_FRACTION_SHARPGAP if lane == "sharpgap" else C.KELLY_FRACTION_STATS


def item_fields(legs, single=False):
    """Champs de money.allocate pour un pari (`single`) ou un combiné de jambes `legs` : {"frac": fraction de Kelly de la voie la plus prudente parmi les jambes à valeur}, ou
    {"novalue": True} pour un combiné dont TOUTES les jambes sont « fill » (mise fixe, hors Kelly). {} quand une jambe n'a pas d'étiquette (ligne d'avant les étiquettes, combiné live) :
    le comportement d'avant. Un pari seul sans valeur garde Kelly comme avant."""
    legs = list(legs or [])
    labels = [(l or {}).get("strategy") for l in legs]
    if not legs or any(x is None for x in labels) or not C.ENSEMBLE_ENABLED:
        return {}
    value = [x for x in labels if x in ("sharpgap", "stats")]
    if value:
        return {"frac": min(frac(x) for x in value)}
    return {} if single else {"novalue": True}


def lines():
    """Lignes de status : état des voies, des ligues en fantôme et des mises."""
    st = state().get("stats") or {}
    out = [f"sharpgap : {C.STRATEGY_SHARPGAP} · stats : {C.STRATEGY_STATS}" + (" (coupée automatiquement : fantôme)" if st.get("mode") == "ghost" else "")
           + f" · jambes sans valeur : {'oui' if C.FILL_LEGS else 'non'}",
           f"Kelly : sharpgap {C.KELLY_FRACTION_SHARPGAP:g} · stats {C.KELLY_FRACTION_STATS:g} · combiné sans valeur : mise "
           + (f"fixe {C.NOVALUE_STAKE:g}" if C.NOVALUE_STAKE else "minimale (un pas d'arrondi)")]
    ghost = sorted(set(C.STATS_GHOST_LEAGUES) | set(st.get("leagues") or {}))
    out.append("Stats en fantôme : " + ("toutes les ligues hors football-data" if C.STATS_GHOST_OUTSIDE_FD else "aucune par défaut")
               + (f" + {', '.join(ghost[:6])}{'…' if len(ghost) > 6 else ''}" if ghost else ""))
    return out
