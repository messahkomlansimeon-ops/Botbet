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
        else:                                         # une ligue dont la valeur n'est pas un dict ({"x": "oops"}) reste une ligue coupée, sans date
            lg = st.get("leagues") or {}
            if any(not isinstance(v, dict) for v in lg.values()):
                d = d | {"stats": st | {"leagues": {k: (v if isinstance(v, dict) else {}) for k, v in lg.items()}}}
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
    return "ghost" if league_ghost(sport_key) else "on"


def league_ghost(sport_key):
    """La ligue est-elle en fantôme pour la voie stats (liste de config, coupure automatique de la ligue, ou hors football-data sans retour décidé à la main) ? Ne regarde pas
    l'interrupteur global : sert aussi à dire quelles lignes comptent pour la coupure de la voie entière."""
    if not sport_key:
        return False
    st = state().get("stats") or {}
    if sport_key in C.STATS_GHOST_LEAGUES or sport_key in (st.get("leagues") or {}):
        return True
    return bool(C.STATS_GHOST_OUTSIDE_FD and _outside_fd(sport_key) and sport_key not in C.STATS_ON_LEAGUES)


def season_mode():
    """« on », « ghost » ou « off » de la voie `saison` (config.STRATEGY_SEASON). Ensemble éteint : « off » (la voie n'existe pas sans étiquettes)."""
    return C.STRATEGY_SEASON if C.ENSEMBLE_ENABLED else "off"


def usable(c):
    """La ligne `c` (pronostic ou pari réel) peut-elle être sélectionnée pour un pari ou une jambe de combiné ? Ensemble éteint : toujours (comportement d'avant)."""
    if not C.ENSEMBLE_ENABLED:
        return True
    if c.get("strategy") == "saison" or c.get("lane") == "saison":              # voie à part : ses interrupteurs, jamais ceux de la voie stats (ligues en fantôme, coupure automatique)
        return season_mode() == "on" and bool(c.get("sval") or C.FILL_LEGS)
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
    {"novalue": True} pour un combiné dont TOUTES les jambes sont « fill » (ou des jambes `saison` de remplissage) (par défaut aucune mise : suivi en fantôme ; mise fixe optionnelle NOVALUE_STAKE, hors Kelly). {} quand une jambe n'a pas d'étiquette (ligne d'avant les étiquettes, combiné live) :
    le comportement d'avant. Un pari seul sans valeur garde Kelly comme avant."""
    legs = list(legs or [])
    labels = [(l or {}).get("strategy") for l in legs]
    if not legs or any(x is None for x in labels) or "live" in labels or not C.ENSEMBLE_ENABLED:
        return {}                                   # jambe live : hors voies, le dimensionnement live reste celui d'avant
    if all((l or {}).get("lane") == "saison" or x == "saison" for l, x in zip(legs, labels)):      # combiné de la voie `saison` : part de mise propre à la voie, plafond propre éventuel
        cap = {"cap": C.SEASON_TICKET_MAX} if C.SEASON_TICKET_MAX > 0 else {}
        if any((l or {}).get("sval") for l in legs):
            return {"frac": C.KELLY_FRACTION_SEASON} | cap
        if single:
            return {}
        # remplissage seul : envoyé « sans valeur », mise = part de la voie (plancher de confiance : sans espérance positive Kelly ne mise pas, ici la voie le fait par décision), plafonds ordinaires
        return {"frac": C.KELLY_FRACTION_SEASON, "flat": True} | cap if C.SEASON_SEND_FILL else {"novalue": True}
    value = [x for x in labels if x in ("sharpgap", "stats")]
    if value:
        return {"frac": min(frac(x) for x in value)}
    return {} if single else {"novalue": True}


def lines():
    """Lignes de status : état des voies, des ligues en fantôme et des mises."""
    st = state().get("stats") or {}
    out = [f"sharpgap : {C.STRATEGY_SHARPGAP} · stats : {C.STRATEGY_STATS}" + (" (coupée automatiquement : fantôme)" if st.get("mode") == "ghost" else "")
           + f" · jambes sans valeur : {'oui' if C.FILL_LEGS else 'non'}",
           f"Part de mise : sharpgap {C.KELLY_FRACTION_SHARPGAP:g} · stats {C.KELLY_FRACTION_STATS:g} (part de ta limite du jour, pas du Kelly) · combiné sans valeur : mise "
           + (f"fixe {C.NOVALUE_STAKE:g} (« sans valeur »)" if C.NOVALUE_STAKE > 0 else "aucune (suivi en fantôme)")]
    out.append(f"saison : {season_mode()} · poids du modèle {C.SEASON_MODEL_WEIGHT * 100:.0f} % · marge {C.SEASON_MARGIN * 100:g} % · garde-fous : valeur ≤ +{C.SEASON_MAX_EDGE * 100:g} %, désaccord ≤ {C.SEASON_MAX_DISAGREE * 100:g} points · paliers {', '.join(f'{t:g}' for t in C.SEASON_TIERS)} · part de mise "
               f"{C.KELLY_FRACTION_SEASON:g}" + (f" · plafond ticket {C.SEASON_TICKET_MAX:g}" if C.SEASON_TICKET_MAX > 0 else "") + f" · combinés de remplissage : {'envoyés (sans valeur)' if C.SEASON_SEND_FILL else 'en fantôme'}"
               + (" · ligues : " + ", ".join(sorted(C.SEASON_LEAGUES)[:4]) if C.SEASON_LEAGUES else " · toutes les ligues couvertes"))
    from . import seasonlane
    out[-1] += " · " + seasonlane.state_text()      # la même ligne « saison » : ce qui est suivi exactement (ligues, équipes, équipes avec l'adversaire enregistré)
    ghost = sorted(set(C.STATS_GHOST_LEAGUES) | set(st.get("leagues") or {}))
    out.append("Stats en fantôme : " + ("toutes les ligues hors football-data" if C.STATS_GHOST_OUTSIDE_FD else "aucune par défaut")
               + (f" + {', '.join(ghost[:6])}{'…' if len(ghost) > 6 else ''}" if ghost else ""))
    return out


def write_state(doc):
    store.save(STATE_FILE, doc)
    reset()


def resume(arg):
    """Remise à la main : « stats » (voie entière, redevient « on » ; la mesure repart de ce jour) ou une clé de ligue (sort de la liste de coupure automatique). -> message."""
    from datetime import date
    doc = dict(state())
    st = dict(doc.get("stats") or {})
    if arg == "stats":
        if st.get("mode") != "ghost":
            return "La voie stats n'est pas coupée automatiquement."
        st.pop("mode", None)
        st.pop("reason", None)
        st["watch"] = {}                            # la chaîne des deux lundis repart de zéro
        st["since_ok"] = str(date.today())          # la coupure ne regarde plus que les lignes réglées à partir d'aujourd'hui
        write_state(doc | {"stats": st})
        return "✅ Voie stats remise à « on ». La coupure automatique repart de zéro à partir d'aujourd'hui."
    leagues = dict(st.get("leagues") or {})
    if arg in leagues:
        leagues.pop(arg)
        st["leagues"] = leagues
        st["back_confirmed"] = {k: v for k, v in (st.get("back_confirmed") or {}).items() if k != arg}
        st["watch"] = {k: v for k, v in (st.get("watch") or {}).items() if not k.endswith(":" + arg)}
        st["returned"] = dict(st.get("returned") or {}) | {arg: str(date.today())}       # la ligue repart de zéro : ni son passé ni sa période fantôme ne comptent
        write_state(doc | {"stats": st})
        return f"✅ {arg} sort des ligues en fantôme (coupure automatique)."
    if arg and (arg in C.STATS_GHOST_LEAGUES or (_outside_fd(arg) and C.STATS_GHOST_OUTSIDE_FD and arg not in C.STATS_ON_LEAGUES)):
        return (f"{arg} est en fantôme par la configuration : retire-la de STATS_GHOST_LEAGUES, ou ajoute-la à STATS_ON_LEAGUES, puis « kelly restart ».")
    return f"{arg or '?'} : rien à remettre."


def command(args):
    """« voies » : l'état ; « voies stats on » : remet la voie stats ; « voies <ligue> on » : sort une ligue de la coupure automatique."""
    words = (" ".join(map(str, args)) if isinstance(args, (list, tuple)) else str(args or "")).lower().split()      # Telegram passe une LISTE de mots
    if len(words) >= 2 and words[-1] == "on":
        return resume(words[0] if words[0] != "ligue" or len(words) < 3 else words[1])
    from . import lanestats
    return "\n".join(["🎛️ Voies"] + lines() + lanestats.cut_lines())
