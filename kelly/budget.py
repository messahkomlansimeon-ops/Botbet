"""Budget mensuel adaptatif des crédits The Odds API (forfait gratuit : 500/mois).

allocation du jour = (crédits restants + déjà dépensés aujourd'hui) / jours avant la remise
à zéro. Ce qui n'est pas dépensé un jour reste dans `remaining` : l'allocation des jours
suivants monte toute seule (week-ends chargés en matchs). Chaque usage (matin, clôture...)
n'a droit qu'à sa part de l'allocation (C.BUDGET_SHARES) ; un usage absent = 0 = désactivé.

État dans data/budget.json (commité par le workflow) : {day, spent: {usage: n}, remaining, updated}.
`spent` repart de zéro chaque jour UTC. `remaining` vient des en-têtes de l'API dès qu'ils sont
connus (odds.CREDITS), sinon de la dernière valeur stockée, sinon du forfait mensuel.
Un appel refusé est sauté par l'appelant (log stderr), jamais une erreur fatale.
"""
import calendar
from datetime import date, datetime, timezone

from . import config as C, store

FILE = "budget.json"


def _now():
    return datetime.now(timezone.utc)


def _reset_date(y, m):
    return date(y, m, min(max(C.ODDS_RESET_DAY, 1), calendar.monthrange(y, m)[1]))


def last_reset(today):
    """Dernière remise à zéro des crédits (<= aujourd'hui)."""
    c = _reset_date(today.year, today.month)
    if c <= today:
        return c
    return _reset_date(today.year - (today.month == 1), today.month - 1 or 12)


def next_reset(today):
    """Prochaine remise à zéro (> aujourd'hui)."""
    c = _reset_date(today.year, today.month)
    if c > today:
        return c
    return _reset_date(today.year + (today.month == 12), today.month % 12 + 1)


def days_left(now=None):
    """Jours (aujourd'hui compris) avant la remise à zéro : au moins 1."""
    today = (now or _now()).date()
    return max(1, (next_reset(today) - today).days)


def _live():
    from . import odds                       # import tardif : odds importe ce module
    return odds.CREDITS["remaining"]


def _state(now):
    s = store.load(FILE, {})
    s = s if isinstance(s, dict) else {}
    today = str(now.date())
    if s.get("day") != today:                # nouveau jour : dépenses remises à zéro
        rem = s.get("remaining")
        try:
            crossed = last_reset(now.date()) > date.fromisoformat(s["day"])
        except (KeyError, ValueError, TypeError):
            crossed = True
        s = {"day": today, "spent": {}, "remaining": C.ODDS_MONTHLY_CREDITS if rem is None or crossed else rem}
    if not isinstance(s.get("spent"), dict):
        s["spent"] = {}
    return s


def _save(s, now):
    s["updated"] = now.isoformat(timespec="minutes")
    store.save(FILE, s)


def remaining(now=None, s=None):
    live = _live()
    if live is not None:
        return live
    return (s or _state(now or _now()))["remaining"]


def allowance(now=None):
    """Crédits dépensables aujourd'hui, tous usages confondus."""
    now = now or _now()
    s = _state(now)
    return max(0, int((remaining(now, s) + sum(s["spent"].values())) / days_left(now)))


def share(use, now=None):
    """Part de l'allocation du jour réservée à `use` (0 si l'usage n'a pas de part)."""
    return int(allowance(now) * C.BUDGET_SHARES.get(use, 0) + 1e-9)


def spent(use, now=None):
    return _state(now or _now())["spent"].get(use, 0)


def left(use, now=None):
    """Crédits encore dépensables aujourd'hui pour `use`."""
    now = now or _now()
    return max(0, share(use, now) - spent(use, now))


def why_not(n, use, now=None):
    """None si dépenser `n` crédits pour `use` est permis, sinon la raison (français)."""
    if n <= 0:
        return None
    now = now or _now()
    rem = remaining(now)
    if rem <= 0:
        return "plus aucun crédit"
    floor = 0 if use == "closing" else C.MIN_CREDITS      # la réserve sert à clôturer les paris envoyés
    if rem - n < floor:
        return f"{rem} crédits restants (plancher {floor})"
    if n > left(use, now):
        return f"{left(use, now)} crédit(s) restant(s) sur la part « {use} » du jour ({share(use, now)})"
    return None


def can_spend(n, use, now=None):
    return why_not(n, use, now) is None


def spend(n, use, now=None):
    """Note une dépense réussie de `n` crédits pour `use` et sauve l'état."""
    now = now or _now()
    s = _state(now)
    s["spent"][use] = s["spent"].get(use, 0) + n
    live = _live()
    s["remaining"] = live if live is not None else max(0, s["remaining"] - n)
    _save(s, now)


def sync(now=None):
    """Recopie les crédits restants connus (en-têtes de l'API) dans l'état, s'ils ont changé."""
    now = now or _now()
    live = _live()
    s = _state(now)
    if live is not None and s["remaining"] != live:
        s["remaining"] = live
        _save(s, now)


def line(now=None):
    """Une ligne de bilan du jour (logs et --check)."""
    now = now or _now()
    s = _state(now)
    parts = [f"{k} {s['spent'].get(k, 0)}/{share(k, now)}" for k in C.BUDGET_SHARES if C.BUDGET_SHARES[k] > 0]
    return (f"dépensé aujourd'hui : {sum(s['spent'].values())} sur {allowance(now)} ({', '.join(parts)}) ; "
            f"{remaining(now, s)} crédits restants, {days_left(now)} jour(s) avant la remise à zéro")
