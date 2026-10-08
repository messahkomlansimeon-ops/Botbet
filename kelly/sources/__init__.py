"""Sources de stats gratuites (football-data, Understat, FBref) : téléchargement poli, profils
compacts dans data/, auto-diagnostic. Points d'entrée : refresh_all(leagues), report(),
features.match_features(home, away, sport_key), features.player_features(name, team)."""
from datetime import datetime

from . import fbref, features, footballdata, state, understat
from .features import match_features, player_features

SOURCES = (footballdata, understat, fbref)       # FBref en dernier : la plus lente (6 s entre requêtes)
SOCCER_PREFIX = "soccer_"


def soccer_leagues(leagues=None):
    """Clés The Odds API de football à actualiser (par défaut : celles de config.SPORTS)."""
    if leagues is None:
        try:
            from .. import config
            leagues = list(getattr(config, "SPORTS", []))
        except Exception:
            leagues = []
    known = set(footballdata.SPORT_TO_DIV) | set(understat.SPORT_TO_LEAGUE) | set(fbref.SPORT_TO_COMP)
    out = []
    for k in leagues:
        out += sorted(x for x in known if x.startswith(k)) if k.endswith("_") else [k]
    return [k for k in dict.fromkeys(out) if k.startswith(SOCCER_PREFIX) and k in known]


def refresh_all(leagues=None):
    """Actualise toutes les sources (jamais d'exception ; une source bloquée n'arrête pas les autres).
    Renvoie {source: health}."""
    keys = soccer_leagues(leagues)
    out = {}
    for mod in SOURCES:
        try:
            out[mod.NAME] = mod.refresh(keys)
        except Exception as e:
            out[mod.NAME] = state.record_run(mod.NAME, 0, [f"erreur interne ({type(e).__name__}: {e})"])
    features.reload()
    return out


def health_all():
    return {m.NAME: m.health() for m in SOURCES}


def _short(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m %H:%M")
    except (TypeError, ValueError):
        return "?"


def report():
    """Statut court en français, une ligne par source (message du matin, --check)."""
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
    return "\n".join(lines)
