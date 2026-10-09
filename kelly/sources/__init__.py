import sys
from datetime import date, datetime

from .. import config as C
from . import betexplorer, fbref, features, footballdata, fotmob, http, s365, state, understat
from .features import match_features, player_features, team_profile

SOURCES = (footballdata, understat, fbref, fotmob, betexplorer)
SOCCER_PREFIX = "soccer_"
# Part du délai SOURCES_SECONDS de chaque source (poids) : sans partage, les sources de tête (football-data, FotMob) épuisaient tout le délai à froid
# et BetExplorer, en queue, n'était jamais actualisé. Le temps qu'une source n'utilise pas passe aux suivantes.
SLICE_WEIGHTS = {"footballdata": 3, "understat": 1, "fbref": 1, "fotmob": 4, "betexplorer": 3}
MONDE_MAX_AGE_DAYS = 3             # le relevé du dernier matin (data/monde.json) ne guide la priorité des ligues que s'il date de moins de 3 jours


def soccer_leagues(leagues=None):
    if leagues is None:
        try:
            from .. import config
            leagues = list(getattr(config, "SPORTS", []))
        except Exception:
            leagues = []
    known = (set(footballdata.SPORT_TO_DIV) | set(understat.SPORT_TO_LEAGUE) | set(fbref.SPORT_TO_COMP)
             | {k for k in fotmob.SPORT_TO_LEAGUE} | {k for k in betexplorer.PATHS})
    out = []
    for k in leagues:
        out += sorted(x for x in known if x.startswith(k)) if k.endswith("_") else [k]
    return [k for k in dict.fromkeys(out) if k.startswith(SOCCER_PREFIX) and k in known]


def priority_leagues(keys, explicit=()):
    """Ligues (parmi `keys`) à actualiser en premier : celles qui jouent aujourd'hui ou demain. Sources d'information, sans réseau :
    `explicit` (relevé du jour des matchs, quand l'appelant l'a déjà), le relevé du dernier matin (data/monde.json : les ligues qui jouaient
    jouent le plus souvent encore dans la semaine), et les pages de ligue FotMob déjà en cache (un match non joué prévu aujourd'hui ou demain)."""
    keys = set(keys)
    out = set(explicit or ()) & keys
    try:
        from .. import monde
        d = monde.load()
        if d and 0 <= (state.now().date() - date.fromisoformat(d["day"])).days <= MONDE_MAX_AGE_DAYS:
            out |= set(d.get("leagues") or {}) & keys
    except Exception:                                    # noqa: BLE001 - un indice, jamais bloquant
        pass
    if fotmob.enabled():
        for k in sorted(keys - out):
            try:
                if fotmob.playing_soon(k):
                    out.add(k)
            except Exception:                            # noqa: BLE001
                pass
    return out


def _slice(i, t_end):
    """Fin du délai de la source n° `i` : sa part (SLICE_WEIGHTS) de ce qui reste jusqu'à `t_end` (fin du délai global), le reliquat passe aux suivantes."""
    if t_end is None:
        return
    now = http.monotonic()
    w = [SLICE_WEIGHTS.get(m.NAME, 1) for m in SOURCES[i:]]
    http.set_deadline_at(now + max(0.0, t_end - now) * w[0] / sum(w))


def refresh_all(leagues=None, priority=()):
    """Actualise toutes les sources dans le délai global http.set_deadline (SOURCES_SECONDS). `priority` : clés des ligues qui jouent aujourd'hui
    (relevé des matchs du jour, si l'appelant l'a) ; en plus de lui, priority_leagues les cherche lui-même. À froid le délai ne suffit pas à
    tout : chaque source a sa part (SLICE_WEIGHTS), les ligues qui jouent bientôt passent d'abord puis la page la plus ancienne (state.order),
    de sorte que le reste est repris les jours suivants ; ce qui a été servi depuis un cache périmé est signalé (health, report)."""
    keys = soccer_leagues(leagues)
    out = {}
    try:                                                 # le dossier .cache/sources ne grossit plus sans fin
        n = http.prune(C.SOURCES_CACHE_KEEP_DAYS)
        if n:
            print(f"[sources] cache : {n} entrée(s) inutilisée(s) depuis {C.SOURCES_CACHE_KEEP_DAYS} jours effacée(s)", file=sys.stderr)
    except Exception:                                    # noqa: BLE001
        pass
    t_end = http.get_deadline()
    state.set_priority(priority_leagues(keys, priority))
    try:
        for i, mod in enumerate(SOURCES):
            _slice(i, t_end)
            try:
                out[mod.NAME] = mod.refresh(keys)
            except Exception as e:
                out[mod.NAME] = state.record_run(mod.NAME, 0, [f"erreur interne ({type(e).__name__}: {e})"])
    finally:
        http.set_deadline_at(t_end)
        state.set_priority(())
    features.reload()
    for mod in SOURCES:
        n = state.stale_count(mod.NAME)
        if n:
            print(f"[sources] {mod.LABEL} : {n} page(s) servie(s) depuis un cache périmé (délai ou accès refusé)", file=sys.stderr)
    return out


def health_all():
    return {m.NAME: m.health() for m in SOURCES}


def _short(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m %H:%M")
    except (TypeError, ValueError):
        return "?"


def report():
    lines = ["📡 Sources de stats"]
    for m in SOURCES:
        h = m.health()
        err = h["last_error"] or "accès refusé"
        if h["blocked_until"]:
            lines.append("⛔ " + (err if err.startswith(m.LABEL) else f"{m.LABEL} : {err}"))
        elif not getattr(m, "enabled", lambda: True)():
            lines.append(f"⚪ {m.LABEL} : désactivée")
        elif h["ok"] is None:
            lines.append(f"⚪ {m.LABEL} : pas encore lancée")
        elif h["ok"]:
            warn = f" ; ⚠️ {h['last_error']}" if h["last_error"] else ""
            lines.append(f"✅ {m.LABEL} : {h['rows']} lignes, maj {_short(h['last_success'])} UTC{warn}")
        else:
            lines.append(f"⚠️ {m.LABEL} : {h['last_error'] or 'échec'}")
    lines += _s365_lines()
    return "\n".join(lines)


def _s365_lines():
    """365scores (stats de match, s365.fill du passage horaire) n'est pas dans SOURCES (il n'a pas de rafraîchissement du matin) : une ligne
    seulement une fois lancé ou s'il est bloqué, pour ne pas alourdir le rapport d'une source qui n'a encore rien lu."""
    h = s365.health()
    if h["blocked_until"]:
        err = h["last_error"] or "accès refusé"
        return ["⛔ " + (err if err.startswith(s365.LABEL) else f"{s365.LABEL} : {err}")]
    if h["ok"] is None:
        return []
    warn = f" ; ⚠️ {h['last_error']}" if h["last_error"] else ""
    if h["ok"]:
        return [f"✅ {s365.LABEL} (stats de match) : {h['rows']} matchs lus, maj {_short(h['last_success'])} UTC{warn}"]
    return [f"⚠️ {s365.LABEL} : {h['last_error'] or 'échec'}"]
