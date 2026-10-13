"""⚠️ Ce matin : ce qui cloche dans le passage du matin, une ligne chacun, en tête des messages du matin (rien quand tout va bien) :
- journée inhabituelle : un compte (matchs au programme, championnats, paris avec cotes réelles, pronostics, matchs lus chez 1xBet) sous la moitié de sa
  médiane des derniers matins (MIN_DAYS matins connus au moins, médiane d'au moins MIN_MEDIAN) : une source a sans doute lâché ;
- relevé 1xBet coupé (délai, plafond, panne) ou format du site suspect (xbet._note_run) ;
- proxy qui refuse le mot de passe ou forfait Webshare épuisé (http.proxy_refusal).
Les comptes du jour sont gardés (day_counts.json, KEEP jours) : c'est la référence des matins suivants."""
import sys
from datetime import datetime, timedelta, timezone
from statistics import median

from . import fmt, store
from .sources import state

FILE = "day_counts.json"
KEEP, WINDOW, MIN_DAYS, MIN_MEDIAN, DROP = 21, 7, 4, 5, 0.5
LABELS = {"matches": "matchs au programme", "leagues": "championnats", "picks": "paris avec cotes réelles", "pronos": "pronostics",
          "xbet": "matchs lus chez 1xBet"}
FRESH_H = 3                     # relevé 1xBet ou refus du proxy de moins de 3 h : celui de ce matin


def counts(world, picks, pronos):
    """Comptes du matin -> {nom: nombre}."""
    from . import monde
    w = world or {}
    return {"matches": monde.total(w) if w else 0, "leagues": len(w.get("leagues") or {}), "picks": len(picks or []), "pronos": len(pronos or []),
            "xbet": sum(1 for c in list(pronos or []) + list(picks or []) if c.get("xodds"))}


def record(day, c):
    """Garde les comptes du jour `day` (les KEEP derniers jours) ; ne lève jamais."""
    try:
        d = store.load(FILE, {})
        d = d if isinstance(d, dict) else {}
        d[str(day)] = c
        store.save(FILE, {k: d[k] for k in sorted(d)[-KEEP:]})
    except Exception as e:                          # noqa: BLE001
        print(f"[matin] comptes du jour non gardés : {type(e).__name__}: {e}", file=sys.stderr)


def unusual(day, c, hist=None):
    """Comptes du jour sous la moitié de leur médiane des WINDOW derniers matins (avant `day`) -> [« 14 matchs au programme (d'habitude ~120) »]."""
    hist = hist if hist is not None else store.load(FILE, {})
    past = [v for k, v in sorted((hist or {}).items()) if k < str(day) and isinstance(v, dict)][-WINDOW:]
    if len(past) < MIN_DAYS:
        return []
    out = []
    for k, label in LABELS.items():
        vals = [v[k] for v in past if isinstance(v.get(k), (int, float))]
        if len(vals) < MIN_DAYS:
            continue
        med = median(vals)
        if med >= MIN_MEDIAN and c.get(k, 0) < DROP * med:
            out.append(f"{c.get(k, 0)} {label} (d'habitude ~{med:.0f})")
    return out


def _fresh(iso, now):
    try:
        t = datetime.fromisoformat(str(iso))
        return now - (t if t.tzinfo else t.replace(tzinfo=timezone.utc)) <= timedelta(hours=FRESH_H)
    except (TypeError, ValueError):
        return False


def xbet_lines(now):
    """Relevé 1xBet de ce matin coupé ou suspect -> lignes."""
    run = (state._all().get("xbet") or {}).get("run") or {}
    if not run or not _fresh(run.get("at"), now):
        return []
    out = []
    if run.get("cut"):
        out.append(f"relevé 1xBet coupé ({run['cut']}) : {run.get('read', 0)} matchs lus, {run.get('left', 0)} pas lus")
    if run.get("format"):
        out.append("1xBet répond sans cotes : le format de son site a peut-être changé (cotes 1xBet absentes ce matin)")
    return out


def proxy_lines(now):
    p = state._all().get("proxy") or {}
    if p.get("refused") and _fresh(p.get("refused_at"), now):
        return [f"{p['refused'].split(' (')[0].replace('proxy : ', 'le proxy : ')} : 1xBet et 365scores ne sont plus lus (Webshare, XBET_PROXY)"]
    return []


def message(day, c, now=None):
    """Le message « ⚠️ Ce matin » (Html) ou None si tout va bien ; garde les comptes du jour. Ne lève jamais."""
    from .telegram import Html
    now = now or datetime.now(timezone.utc)
    lines = []
    try:
        low = unusual(day, c)
        if low:
            lines.append("Journée inhabituelle : " + ", ".join(low) + ". Une source a peut-être lâché (🩺 status).")
        lines += xbet_lines(now) + proxy_lines(now)
    except Exception as e:                          # noqa: BLE001
        print(f"[matin] contrôle du jour ignoré : {type(e).__name__}: {e}", file=sys.stderr)
    record(day, c)
    if not lines:
        return None
    return Html("⚠️ " + fmt.b("Ce matin") + "\n" + "\n".join(fmt.esc("• " + x[0].upper() + x[1:]) for x in lines))
