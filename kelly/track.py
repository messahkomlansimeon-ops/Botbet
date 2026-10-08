"""Suivi des paris : envoyés et fantômes, cote de clôture, CLV.

Un pari est « bon » s'il bat la cote de clôture (la plus précise qui existe :
tout l'argent est passé dessus). clv = log(cote prise / meilleure cote de
clôture) : positif = la cote a baissé après notre prise, on a pris mieux que
le marché final, quel que soit le résultat.
"""
import math
from datetime import datetime, timedelta, timezone

from . import combos as CB, config as C, learn, store

FIELDS = ("key", "id", "sport_key", "group", "event", "home", "away", "league", "commence", "market",
          "who", "point", "name", "pick", "odds", "best_odds", "prob", "book", "edge", "spread", "steam", "weight",
          "score", "why", "n_books")


def load():
    return store.load("tracked.json", [])


def save(rows):
    store.save("tracked.json", rows)


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def record(picks, sent_keys, day, now=None):
    """Enregistre les envoyés + les TRACK_TOP meilleurs candidats (3 max par match)."""
    now = now or datetime.now(timezone.utc)
    rows = load()
    known = {r["key"] for r in rows}
    ranked = sorted(CB.per_event(picks, k=3), key=lambda c: c["score"], reverse=True)
    chosen = [c for c in ranked[:C.TRACK_TOP]] + [c for c in picks if c["key"] in sent_keys]
    added = 0
    for c in chosen:
        if c["key"] in known:
            r = next(r for r in rows if r["key"] == c["key"])
            r["sent"] = r["sent"] or c["key"] in sent_keys
            continue
        known.add(c["key"])
        rows.append({k: c.get(k) for k in FIELDS} | {
            "date": str(day), "sent": c["key"] in sent_keys, "taken_at": now.isoformat(timespec="minutes"),
            "features": learn.features(c, now), "close_odds": None, "close_prob": None, "closed_at": None,
            "clv": None, "result": None})
        added += 1
    save(rows)
    return added


def pending_closing(rows, now):
    """Paris sans clôture dont le match démarre dans la fenêtre (ou est passé : « missed »).
    Gratuit : seuls les paris envoyés sont à clôturer (les fantômes servent par leurs résultats),
    sauf C.CLOSE_GHOSTS."""
    due, missed = [], []
    for r in rows:
        if r["closed_at"]:
            continue
        start = _ts(r["commence"])
        if start <= now:
            missed.append(r)
        elif start <= now + timedelta(minutes=C.CLOSE_WINDOW_MIN) and (r.get("sent") or C.CLOSE_GHOSTS):
            due.append(r)
    return due, missed


def ranks(combos):
    """clé du pari -> (palier, -mise) du combiné en cours qui le contient et passe en premier (Sûr d'abord,
    puis grosse mise). Un pari envoyé hors combiné (le pari du jour) passe avant : (-1, 0)."""
    tiers = {t: i for i, (t, _, _) in enumerate(C.COMBOS)}
    out = {}
    for c in combos:
        if c.get("result") is None:
            rk = (tiers.get(c["tier"], len(tiers)), -c.get("stake", 0))
            for k in c["keys"]:
                out[k] = min(out.get(k, rk), rk)
    return out


def close(rows, closing_picks, now):
    """Note la clôture des paris suivis à partir des sélections recalculées."""
    by_key = {c["key"]: c for c in closing_picks}
    n = 0
    for r in rows:
        c = by_key.get(r["key"])
        if r["closed_at"] or not c:
            continue
        r["close_odds"], r["close_prob"] = c["odds"], round(c["prob"], 4)
        r["closed_at"] = now.isoformat(timespec="minutes")
        r["clv"] = round(math.log(r["odds"] / c["odds"]), 4)
        n += 1
    return n


def mark_missed(rows, missed, now):
    for r in missed:
        r["closed_at"] = "missed"


def with_clv(rows):
    return [r for r in rows if r.get("clv") is not None]
