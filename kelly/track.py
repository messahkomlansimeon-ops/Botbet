"""Suivi des paris : envoyés et fantômes, cote de clôture, CLV.

Un pari est « bon » s'il bat la cote de clôture (la plus précise qui existe :
tout l'argent est passé dessus). clv = log(cote prise / meilleure cote de
clôture) : positif = la cote a baissé après notre prise, on a pris mieux que
le marché final, quel que soit le résultat.
"""
import math
import os
from datetime import date, datetime, timedelta, timezone

from . import combos as CB, config as C, learn, store

FIELDS = ("key", "id", "sport_key", "group", "event", "home", "away", "league", "commence", "market",
          "who", "point", "name", "pick", "odds", "best_odds", "prob", "prob_book", "book", "edge", "spread", "steam", "weight",
          "score", "why", "n_books", "stake", "cur")


def load():
    return store.load("tracked.json", [])


def _archive(old):
    """Ajoute les lignes réglées anciennes à data/archive/tracked-AAAA.json (par année de leur date, sans doublon date+clé)."""
    by_year = {}
    for r in old:
        by_year.setdefault(str(r.get("date") or "")[:4] or "0000", []).append(r)
    os.makedirs(os.path.dirname(store.path("archive/x")), exist_ok=True)
    for year, rows in by_year.items():
        name = f"archive/tracked-{year}.json"
        cur = store.load(name, [])
        cur = cur if isinstance(cur, list) else []
        have = {(x.get("date"), x.get("key")) for x in cur}
        cur += [r for r in rows if (r.get("date"), r.get("key")) not in have]
        store.save(name, cur, compact=True)


def prune(rows):
    """-> (lignes gardées, lignes à archiver) : réglées (result non nul) ET plus vieilles que C.TRACK_KEEP_DAYS jours avant la
    dernière date suivie. Les lignes sans date lisible restent."""
    try:
        last = max(date.fromisoformat(str(r["date"])[:10]) for r in rows if r.get("date"))
    except (ValueError, KeyError):
        return rows, []
    cut = str(last - timedelta(days=C.TRACK_KEEP_DAYS))
    old = [r for r in rows if r.get("result") is not None and str(r.get("date") or "9999") < cut]
    if not old:
        return rows, []
    gone = {id(r) for r in old}
    return [r for r in rows if id(r) not in gone], old


def save(rows):
    """Écrit tracked.json court : les lignes réglées de plus de C.TRACK_KEEP_DAYS jours passent dans l'archive annuelle
    (data/archive/tracked-AAAA.json) ; l'apprentissage (learn.update) n'a besoin que des derniers mois."""
    keep, old = prune(rows)
    if old:
        try:
            _archive(old)
        except Exception:                 # archive impossible : on garde tout dans le fichier courant plutôt que de perdre des lignes
            keep = rows
    store.save("tracked.json", keep)


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def record(picks, sent_keys, day, now=None, single_key=None):
    """Enregistre les envoyés + les TRACK_TOP meilleurs candidats (3 max par match). `single_key` : clé du pari du jour (envoyé
    seul, même s'il figure aussi dans un combiné) ; sa ligne reçoit r["single"] = True. `prob_book` : proba brute des bookmakers,
    celle que la calibration (learn.calibrated) corrige."""
    now = now or datetime.now(timezone.utc)
    rows = load()
    known = {r["key"] for r in rows}
    ranked = sorted(CB.per_event(picks, k=3), key=lambda c: c["score"], reverse=True)
    chosen = [c for c in ranked[:C.TRACK_TOP]] + [c for c in picks if c["key"] in sent_keys]
    added = 0
    has_single = any(r.get("single") is True and r.get("date") == str(day) for r in rows)      # relance du matin : un seul pari du jour par date
    for c in chosen:
        if c["key"] in known:
            r = next(r for r in rows if r["key"] == c["key"])
            r["sent"] = r["sent"] or c["key"] in sent_keys
            if c["key"] == single_key and r.get("single") is not True:
                r["single"] = not has_single              # False : deuxième « top » du jour, que played.single_rows ne doit pas deviner
                has_single = has_single or r["single"]
            continue
        known.add(c["key"])
        single = c["key"] == single_key and not has_single
        row = {k: c.get(k) for k in FIELDS} | {
            "date": str(day), "sent": c["key"] in sent_keys, "taken_at": now.isoformat(timespec="minutes"),
            "features": learn.features(c, now), "close_odds": None, "close_prob": None, "closed_at": None,
            "clv": None, "result": None} | ({"single": True} if single else {})
        if c["key"] == single_key and not single:        # deuxième « top » du jour : ni pari du jour, ni mise du budget
            row |= {"single": False, "stake": None, "cur": None}
        rows.append(row)
        has_single = has_single or single
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
