"""Suivi des paris : envoyés et fantômes, cote de clôture, CLV.

Un pari est « bon » s'il bat la cote de clôture (la plus précise qui existe :
tout l'argent est passé dessus). clv = log(cote prise / meilleure cote de
clôture) : positif = la cote a baissé après notre prise, on a pris mieux que
le marché final, quel que soit le résultat.
"""
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone

from . import combos as CB, config as C, ensemble, learn, money, sharpgap, store

FIELDS = ("key", "id", "sport_key", "group", "event", "home", "away", "league", "commence", "market",
          "who", "point", "name", "pick", "odds", "best_odds", "prob", "prob_book", "prob_src", "book", "edge", "spread", "steam", "weight",
          "score", "why", "n_books", "stake", "cur", "xodds", "xref", "coupon", "xat")


def load():
    return store.load("tracked.json", [])


def _archive(old):
    """Ajoute les lignes réglées anciennes à data/archive/tracked-AAAA.json (par année de leur date, sans doublon date+clé)."""
    by_year = {}
    for r in old:
        by_year.setdefault(str(r.get("date") or "")[:4] or "0000", []).append(r)
    for year, rows in by_year.items():
        name = f"archive/tracked-{year}.json"
        cur = store.load(name, [])
        cur = cur if isinstance(cur, list) else []
        have = {(x.get("date"), x.get("key")) for x in cur}
        cur += [r for r in rows if (r.get("date"), r.get("key")) not in have]
        store.save(name, cur, compact=True)


def prune(rows):
    """-> (lignes gardées, lignes à archiver) : réglées (result non nul) ET plus vieilles que C.TRACK_KEEP_DAYS jours avant la
    dernière date suivie. Les lignes sans date lisible restent, et une ligne NOTÉE (r["played"]) aussi : l'argent (money.result,
    « depuis le début ») lit tracked.json, pas l'archive."""
    try:
        last = max(date.fromisoformat(str(r["date"])[:10]) for r in rows if r.get("date"))
    except (ValueError, KeyError):
        return rows, []
    cut = str(last - timedelta(days=C.TRACK_KEEP_DAYS))
    old = [r for r in rows if r.get("result") is not None and str(r.get("date") or "9999") < cut and not r.get("played")]
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


def _typical(c):
    """Le pari tel que The Odds API le voyait au choix (cote typique, valeur, book, score d'origine) quand xprice.price_picks l'a remis à
    la cote 1xBet. Les lignes de suivi gardent CE prix du marché dans "odds" (le CLV et l'apprentissage le comparent à la clôture, qui
    vient de The Odds API) ; la cote 1xBet est à part, dans "xodds" (settle, bankroll et money la prennent pour les gains)."""
    if c.get("typ_odds") is None:
        return c
    return c | {"odds": c["typ_odds"], "edge": c.get("typ_edge", c["edge"]), "book": c.get("typ_book"),
                "best_odds": c.get("typ_best", c["best_odds"]), "heuristic": c.get("typ_heur", c.get("heuristic", c["score"]))}


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
        row = {k: _typical(c).get(k) for k in FIELDS} | {
            "date": str(day), "sent": c["key"] in sent_keys, "taken_at": now.isoformat(timespec="minutes"),
            "features": learn.features(_typical(c), now), "close_odds": None, "close_prob": None, "closed_at": None,
            "clv": None, "result": None} | ({"single": True} if single else {})
        if c["key"] == single_key and not single:        # deuxième « top » du jour : ni pari du jour, ni mise du budget
            row |= {"single": False, "stake": None, "cur": None}
        rows.append(row)
        has_single = has_single or single
        added += 1
    save(rows)
    return added


def record_gap(entries, day, now=None, read_at=None):
    """Enregistre les paris « 💰 1xBet paie plus que le marché » annoncés (sharpgap) : entries = [(pari de today.json avec toutes ses données, candidat
    sharpgap avec "n" et "batch" (sharpgap.reserve), mise conseillée en FCFA, 0 si trop petite, None sans budget du jour ; code coupon ou None)]. Un pari déjà suivi (jambe d'un combiné, fantôme,
    pari du jour) reçoit la marque ; sinon une ligne neuve, comme track.record, ENVOYÉE (r["sent"] : sa clôture est relevée dans le budget « closing » et son
    règlement est celui des autres paris). Marques : r["sharpgap"] = True, r["gap_n"] (numéro « g<n> » du jour), r["gap"] (écart, proba sharp, cote juste, heure du
    relevé sharp...), r["single"] = False (jamais le « pari du jour » : played.single_rows l'ignore). Prix 1xBet de la ligne : le candidat (relu en direct), sauf
    pour le pari du jour, dont la ligne garde son prix. Un candidat sans numéro ("n" absent : l'écart est aussi le pari du jour, déjà suivi) est seulement marqué.
    Mise : celle du budget du jour (None : pas de budget, la mise sera calculée à la note). `read_at` : heure « HH:MM » de la lecture
    des cotes 1xBet (le matin : la fin du relevé ; sinon maintenant). -> nombre de lignes marquées."""
    now = now or datetime.now(timezone.utc)
    rows = load()
    by_key = {r["key"]: r for r in rows}
    n = 0
    for pick, cand, stake, code in entries:
        mark = {"sharpgap": True, "gap": {k: cand.get(k) for k in ("edge", "p_sharp", "src", "n_sharp", "fair_odds", "xodds", "sharp_at", "batch")}
                                          | {"at": now.strftime("%H:%M")}}
        r = by_key.get(cand["key"])
        if not cand.get("n"):                                # écart qui est AUSSI le pari du jour : seulement marqué (mesure contre Pinnacle), pas de numéro « g<n> »
            if r is not None:
                r.update(mark)
                n += 1
            continue
        mark |= {"gap_n": cand["n"], "sent": True}
        if r is None:
            r = {k: _typical(pick).get(k) for k in FIELDS} | {
                "date": str(day), "taken_at": now.isoformat(timespec="minutes"), "features": learn.features(_typical(pick), now),
                "close_odds": None, "close_prob": None, "closed_at": None, "clv": None, "result": None, "single": False}
            rows.append(r)
            by_key[r["key"]] = r
        r.setdefault("single", False)
        r.update(mark)
        if r.get("single") is not True:                      # le pari du jour garde SON prix et SA mise
            r.update(xodds=cand["xodds"], xref=cand.get("xref"), xat=read_at or now.strftime("%H:%M"), stake=stake, cur=(money.cur() if stake else None))
            if code:
                r["coupon"] = code
        n += 1
    save(rows)
    return n


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
            rk = (tiers.get(c["tier"], len(tiers)), -(c.get("stake") or 0))
            for k in c["keys"]:
                out[k] = min(out.get(k, rk), rk)
    return out


def _audit_close(r, c):
    """Champs de l'audit du CLV (tools/audit_clv.py), écrits à la clôture sans AUCUNE requête en plus : `close_book` = l'avis des books de référence à la
    clôture (proba démarginée, source, nombre de books sharp), et `xclose` = le prix 1xBet de ce pari lu dans le CACHE DISQUE (xbet.cached_cell, jamais
    de réseau) avec l'âge du relevé en minutes : un prix plus vieux que la clôture n'est pas une clôture, l'audit lit l'âge. Ne lève jamais."""
    try:
        if c.get("prob_book") is not None:
            r["close_book"] = {"p": round(c["prob_book"], 4), "src": c.get("prob_src"), "n_sharp": c.get("n_sharp")}
        if r.get("xref"):
            from .sources import state, xbet
            cell = xbet.cached_cell(r["xref"])
            if cell is not None:
                age = round((state.clock() - cell.at) / 60) if isinstance(cell.at, (int, float)) else None
                lead = None
                if isinstance(cell.at, (int, float)) and r.get("commence"):
                    lead = round((datetime.fromisoformat(str(r["commence"]).replace("Z", "+00:00")).timestamp() - cell.at) / 60)
                r["xclose"] = {"odds": cell.cf, "age_min": age, "lead_min": lead, "susp": bool(cell.blocked)}
    except Exception as e:                          # noqa: BLE001 - un champ d'audit ne bloque jamais une clôture
        print(f"[suivi] champs d'audit de clôture ignorés : {type(e).__name__}: {e}", file=sys.stderr)


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
        _audit_close(r, c)
        s = ensemble.sharp_of(c) if r.get("sharpgap") and r.get("xodds") else None
        if s:                                               # pari « 1xBet en retard » : sa cote contre la clôture sans marge du book de référence
            r["gap_edge_close"], r["gap_close_src"] = sharpgap.close_edge(r["xodds"], s[0]), s[1]
        n += 1
    return n


def mark_missed(rows, missed, now):
    for r in missed:
        r["closed_at"] = "missed"


def with_clv(rows):
    return [r for r in rows if r.get("clv") is not None]
