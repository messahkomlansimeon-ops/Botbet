"""Relecture du SOIR (python bot.py --evening, app.run_evening) : les matchs du jour Kelly pas encore commencés sont réanalysés (toutes les familles,
stats comprises) depuis les sources en cache, remis aux cotes 1xBet et fusionnés dans today.json ; rien d'autre ne change ; un seul message ; aucun appel
payant ; idempotent ; ne lève jamais. Lancer : python -m tests.test_evening"""
import copy
import os
import sys
import tempfile
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-evening-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-evening-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-evening-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ["XBET_PROXY_FILE"] = os.path.join(os.environ["DATA_DIR"], "pas-de-fichier.env")
os.environ["XBET_PROXY"] = "http://user:secret@proxy.test:80"

import requests  # noqa: E402

from kelly import apistats, app, calib, combos as CB, config as C, fair, odds, stats, store, telegram  # noqa: E402,F401

DAY = "2026-10-09"
NOW = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)
KO = {"A": "23:00", "B": "23:30", "C": "20:00", "D": "21:10", "E": "23:45"}       # A, B, E à venir ; C commencé ; D imminent (10 min)


def prono(mid, market, name, point, p=0.66, odds_=None, **kw):
    c = {"key": f"{mid}|{market}||{point if point is not None else ''}|{name}", "id": mid, "sport_key": "soccer_argentina_primera_division",
         "home": f"H{mid}", "away": f"A{mid}", "event": f"H{mid} - A{mid}", "league": "Primera", "commence": f"{DAY}T{KO[mid]}:00Z",
         "start": KO[mid], "market": market, "name": name, "point": point, "who": None, "group": "soccer", "pick": f"{market} {name} {point}",
         "prob": p, "p0": p, "fair": round(1 / p, 2), "min_odds": round(1 / p * 1.05, 2), "why": "x", "src": "football-data", "conf": 0.6,
         "n_min": 8, "xgid": 100 + ord(mid)}
    if odds_:
        c |= {"xodds": odds_, "xev": round(p * odds_ - 1, 4), "xodds0": odds_, "xat": "18:00"}
    return c | kw


def setup():
    store.save(calib.FILE, {})
    calib._memo.clear()
    old = [prono("A", "totals", "Over", 2.5, 0.64, 1.9), prono("A", "btts", "Yes", None, 0.6, 1.8),
           prono("B", "totals", "Over", 2.5, 0.64, 1.9), prono("C", "totals", "Over", 2.5, 0.64, 1.9),
           prono("D", "totals", "Over", 2.5, 0.64, 1.9), prono("E", "totals", "Over", 2.5, 0.64, 1.9)]
    pick = {"key": "Z|h2h||H", "id": "Z", "sport_key": "soccer_epl", "market": "h2h", "name": "H", "point": None, "odds": 2.0, "prob": 0.5,
            "home": "Z1", "away": "Z2", "commence": f"{DAY}T22:00:00Z", "book": "x", "weight": 1.0, "edge": 0.0, "score": 0.1}
    pickb = pick | {"key": "B|alternate_totals_corners||9.5|Over", "id": "B", "market": "alternate_totals_corners", "name": "Over", "point": 9.5,
                    "prob_book": 0.62, "prob_src": "pinnacle", "n_sharp": 1}          # pari réel du match B : B est un match « payé », avis sharp sur ses corners
    today = {"day": DAY, "picks": [pick, pickb], "pronos": old, "announced": ["A|totals", "B|totals"], "xbet_at": "18:00"}
    store.save("today.json", today)
    kA = old[0]["key"]
    store.save(fair.COMBOS_FILE, [{"date": DAY, "tier": 5, "min_odds": 5.0, "prob": 0.4, "keys": [kA], "legs": [{"key": kA, "id": "A", "result": None}],
                                   "result": None}])
    store.save("bets.json", {"picks": [], "combos": [{"date": DAY, "tier": "🎯 Sûr", "keys": ["Z|h2h||H"], "odds": 2.0, "prob": 0.5}]})
    store.save("tracked.json", [{"key": "Z|h2h||H", "date": DAY, "result": None}])
    rows = [{"key": old[0]["key"], "id": "A", "market": "totals", "date": DAY, "result": None, "name": "Over", "point": 2.5, "prob": 0.64, "p0": 0.64}]
    rows += [{"key": f"A|{m}||10.5|Under", "id": "A", "market": m, "date": DAY, "result": None, "name": "Under", "point": 10.5, "prob": 0.7, "p0": 0.7}
             for m in ("team_fouls_total", "team_shots_home", "team_sot_total", "team_throws_home", "team_offsides_total")]    # suivis de A déjà là (autre ligne)
    store.save(fair.FILE, rows)
    return old


def lad(m, side="Under", line=9.5, price=1.95):
    return {(m, side, line): price}


def px_for(mid):
    px = {("totals", "Over", 2.5): 1.95, ("totals", "Under", 2.5): 1.85}
    for m in ("team_fouls_total", "team_shots_home", "team_sot_total", "team_throws_home", "team_offsides_total"):
        px |= lad(m) | lad(m, "Over", 9.5, 1.8)
    return {"gid": 100 + ord(mid), "px": px, "refs": {k: [200 + ord(mid), 12, k[2]] for k in px}, "cid": 5}


STAT_OPTS = [(m, "Under", 9.5, 0.66, "stats") for m in ("team_fouls_total", "team_shots_home", "team_sot_total", "team_throws_home", "team_offsides_total")]
OPTS = [("totals", "Over", 2.5, 0.70, "g"), ("alternate_totals_corners", "Over", 9.5, 0.66, "c")] + STAT_OPTS


class Net:
    """Remplace tout accès réseau : un seul appel serait un appel de trop (aucun appel payant, aucun quota API-Football)."""
    calls = []

    @staticmethod
    def boom(*a, **k):
        Net.calls.append((a, k))
        raise AssertionError(f"accès réseau interdit : {a}")


def with_fakes(fn, found_ids=("A", "B")):
    sent, seen = [], {}
    old = (fair._options, odds.match_numbers, requests.get, requests.post, telegram.send, app.log_opinions)
    fair._options = lambda f, h, a: ([], {}) if h == "HE" else (OPTS, {})           # E : rien à proposer (le repli API-Football serait tenté si un jour était passé)
    odds.match_numbers = lambda h, a, k: {"n_home": 8, "n_away": 8}
    old_api = (stats.left_today, stats.expected_totals, apistats.active)
    stats.left_today, apistats.active = (lambda: 999), (lambda: False)
    stats.expected_totals = Net.boom                                                  # quota API-Football : jamais touché
    requests.get = requests.post = Net.boom
    telegram.send = lambda m, *a, **k: sent.append(str(m)) or True
    rows = []
    app.log_opinions = lambda r, day, now=None: rows.append(list(r)) or len(r)

    def fetch(events, deadline=None, wanted=None):
        seen.update(order=[e["id"] for _, e in events], wanted=wanted, deadline=deadline)
        return {i: px_for(i) for i in found_ids if i in {e["id"] for _, e in events}}
    try:
        return fn(fetch), sent, seen, rows
    finally:
        fair._options, odds.match_numbers, requests.get, requests.post, telegram.send, app.log_opinions = old
        stats.left_today, stats.expected_totals, apistats.active = old_api


def snapshot():
    return {n: copy.deepcopy(store.load(n, None)) for n in ("bets.json", "tracked.json", fair.COMBOS_FILE, fair.FILE)}


def test_evening_merge():
    old = setup()
    before_today, before = copy.deepcopy(store.load("today.json", {})), snapshot()
    n, sent, seen, rows = with_fakes(lambda fetch: app.run_evening(NOW, fetch=fetch))
    assert Net.calls == [], Net.calls
    today = store.load("today.json", {})
    by = {}
    for c in today["pronos"]:
        by.setdefault(c["id"], []).append(c)
    # matchs commencé (C), imminent (D) et non relu chez 1xBet (E) : pronostics identiques, cotes comprises
    for mid in ("C", "D", "E"):
        assert by[mid] == [c for c in old if c["id"] == mid], mid
    assert "C" not in seen["order"] and "D" not in seen["order"], seen["order"]
    # matchs relus (A, B) : réanalysés avec les stats, remis aux cotes 1xBet ; la jambe d'un combiné envoyé (A totals) reste telle quelle
    stats = {m for m in ("team_fouls_total", "team_shots_home", "team_sot_total", "team_throws_home", "team_offsides_total")}
    for mid in ("A", "B"):
        got = {c["market"] for c in by[mid]}
        assert stats <= got, (mid, got)
        assert all(c.get("xodds") and c.get("xat") == "21:00" for c in by[mid] if c["market"] in stats), mid
    assert old[0] in by["A"], "jambe d'un combiné du jour conservée à l'identique"
    assert sum(1 for c in by["A"] if c["market"] == "totals") == 1, "pas de doublon de marché à côté d'une jambe conservée"
    assert not any(c["market"] == "btts" for c in by["A"]), "btts de A : non protégé, remplacé par la nouvelle analyse (qui n'en a pas)"
    assert sum(len(v) for v in by.values()) == len(today["pronos"]) and len({c["key"] for c in today["pronos"]}) == len(today["pronos"])
    # rien d'autre ne change : paris du jour, combinés, suivis, notes ; le suivi gratuit ne gagne que de NOUVELLES lignes
    assert today["picks"] == before_today["picks"] and today["day"] == DAY
    after = snapshot()
    for name in ("bets.json", "tracked.json", fair.COMBOS_FILE):
        assert after[name] == before[name], name
    assert all(r in after[fair.FILE] for r in before[fair.FILE]) and len(after[fair.FILE]) > len(before[fair.FILE]), after[fair.FILE]
    assert not any(r["id"] in ("C", "D", "E") for r in after[fair.FILE] if r not in before[fair.FILE])
    # un seul message, avec le nombre de 💎 ; aucun combiné
    assert n == 2 and len(sent) == 1 and "🌙 Pronostics de ce soir mis à jour : 2 💎" in sent[0], (n, [m[:80] for m in sent])
    assert "COMBINÉ" not in sent[0] and set(today["announced"]) >= {"A|totals", "B|totals"} and len(today["announced"]) > 2
    assert len(rows) == 1 and rows[0] and all(c["id"] in ("A", "B") for c in rows[0]), "journal des avis : les pronostics relus seulement"
    print("ok  relecture du soir : seuls les matchs à venir relus sont remplacés")
    return today


def test_evening_idempotent():
    first = store.load("today.json", {})
    snap = snapshot()
    n, sent, seen, rows = with_fakes(lambda fetch: app.run_evening(NOW, fetch=fetch))
    second = store.load("today.json", {})
    assert n == 0 and sent == [], "2e passage : plus rien de neuf, donc aucun message"
    assert [c["key"] for c in second["pronos"]] == [c["key"] for c in first["pronos"]], "mêmes pronostics, dans le même ordre, sans doublon"
    assert second["announced"] == first["announced"] and snapshot() == snap, "suivis et combinés inchangés"
    assert Net.calls == []
    print("ok  deux passages = un seul message, aucun doublon")


def test_evening_never_raises_and_changes_nothing():
    setup()
    before_today, before = copy.deepcopy(store.load("today.json", {})), snapshot()

    def boom(events, deadline=None, wanted=None):
        raise RuntimeError("1xBet en panne")
    n, sent, _, _ = with_fakes(lambda fetch: app.run_evening(NOW, fetch=boom))
    assert n == 0 and sent == [] and store.load("today.json", {}) == before_today and snapshot() == before
    n, sent, _, _ = with_fakes(lambda fetch: app.run_evening(NOW, fetch=fetch), found_ids=())             # 1xBet ne relit aucun match
    assert n == 0 and sent == [] and store.load("today.json", {}) == before_today and snapshot() == before
    # pas de today.json du jour (autre jour) ou rien à venir : rien ne bouge, aucune erreur
    store.save("today.json", before_today | {"day": "2026-10-01"})
    n, sent, _, _ = with_fakes(lambda fetch: app.run_evening(NOW, fetch=fetch))
    assert n == 0 and sent == [] and store.load("today.json", {})["day"] == "2026-10-01"
    store.save("today.json", before_today)
    late = datetime(2026, 10, 10, 0, 30, tzinfo=timezone.utc)
    n, sent, _, _ = with_fakes(lambda fetch: app.run_evening(late, fetch=fetch))                          # tous les matchs finis ou commencés
    assert n == 0 and sent == [] and store.load("today.json", {}) == before_today
    store.save("today.json", None)
    assert app.run_evening(NOW) == 0                                                                       # document illisible : pas d'exception
    print("ok  relecture du soir : jamais d'exception, rien ne change en cas de panne")


def test_bot_entry():
    sys.argv.append("--sample")                    # bot.py ne charge jamais le vrai .env en mode test
    try:
        import bot
    finally:
        sys.argv.remove("--sample")
    calls = []
    old = app.run_evening
    app.run_evening = lambda *a, **k: calls.append(1) or 0
    try:
        bot.main(["--evening"])
        bot.main(["--evening", "--no-lock"])
    finally:
        app.run_evening = old
    assert calls == [1, 1]
    print("ok  python bot.py --evening")


if __name__ == "__main__":
    test_evening_merge()
    test_evening_idempotent()
    test_evening_never_raises_and_changes_nothing()
    test_bot_entry()
    print("tests relecture du soir : OK")
