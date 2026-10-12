"""Proba « vraie » : dé-marge de Shin (odds.shin_probs) et book de référence Pinnacle (odds.consensus / build_picks). Hors
ligne. Lancer : python -m tests.test_sharp"""
import math
import os
import random
import shutil
import tempfile
from datetime import date, datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-test-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-test-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-test-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)

from kelly import config as C, odds  # noqa: E402

DAY = date(2026, 10, 8)
UTC = timezone.utc


# ---------- solveur indépendant de la formule fermée ----------

def ref_shin(prices):
    """Shin par la RELATION DIRECTE du modèle, sans la formule fermée de odds._shin_p : pour z donné, p_i est la racine
    positive de (1-z) p^2 + z p = q_i^2 / S (trouvée par bissection), puis z est cherché par sécante-bissection pour que
    sum p_i = 1. Rend (probas, z)."""
    q = [1 / x for x in prices]
    s = sum(q)

    def p_of(a, z):
        lo, hi = 0.0, 1.0
        for _ in range(200):
            mid = (lo + hi) / 2
            if (1 - z) * mid * mid + z * mid < a:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    def total(z):
        return sum(p_of(x * x / s, z) for x in q)
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if total(mid) > 1 else (lo, mid)
    z = (lo + hi) / 2
    return [p_of(x * x / s, z) for x in q], z


def prop(prices):
    q = [1 / x for x in prices]
    return [x / sum(q) for x in q]


# ---------- 1. Shin ----------

def test_shin_math():
    rnd = random.Random(7)
    for _ in range(300):
        n = rnd.choice([2, 2, 3, 3, 4, 6])
        fair = [rnd.uniform(0.05, 1) for _ in range(n)]
        t = sum(fair)
        margin = rnd.uniform(0.0, 0.15)
        prices = [1 / (f / t * (1 + margin)) for f in fair]
        prices = [max(p, 1.01) for p in prices]
        ps = odds.shin_probs(prices)
        assert abs(sum(ps) - 1) < 1e-9 and all(0 < x < 1 for x in ps), (prices, ps)
        # ordre conservé : une cote plus basse = une proba plus haute
        assert [i for i, _ in sorted(enumerate(ps), key=lambda x: x[1])] == \
               [i for i, _ in sorted(enumerate(prices), key=lambda x: -x[1])], (prices, ps)
    # sans marge (et sous-rond) : simple normalisation
    assert all(abs(a - b) < 1e-12 for a, b in zip(odds.shin_probs([2.0, 4.0, 4.0]), [0.5, 0.25, 0.25]))
    assert odds.shin_z([2.0, 4.0, 4.0]) == 0.0
    sub = odds.shin_probs([2.1, 4.2, 4.2])                    # S < 1 : cotes « de valeur », jamais > 1 en cumulé
    assert abs(sum(sub) - 1) < 1e-12 and abs(sub[0] - 0.5) < 1e-12
    # marché symétrique : probas égales, z = (S - 1) / (n - 1) (résultat connu de la méthode)
    for prices in ([1.9, 1.9], [2.2, 2.2, 2.2], [3.6] * 4):
        ps = odds.shin_probs(prices)
        assert all(abs(x - 1 / len(prices)) < 1e-9 for x in ps), ps
        assert abs(odds.shin_z(prices) - (sum(1 / x for x in prices) - 1) / (len(prices) - 1)) < 1e-9
    print("Shin : somme 1, ordre, sans marge, symétrie : OK")


def test_shin_vs_reference():
    for prices in ([1.25, 6.0, 12.0], [2.0, 3.5, 4.0], [1.5, 2.6], [1.91, 1.91], [1.9, 3.4, 4.2, 9.0],
                   [1.1, 8.0, 21.0], [2.8, 3.2, 2.5]):
        got, want = odds.shin_probs(prices), ref_shin(prices)[0]
        assert all(abs(a - b) < 1e-9 for a, b in zip(got, want)), (prices, got, want)
        # relation du modèle : q_i = sqrt(S (z p_i + (1-z) p_i^2))
        z, s = odds.shin_z(prices), sum(1 / x for x in prices)
        for x, p in zip(prices, got):
            assert abs(1 / x - math.sqrt(s * (z * p + (1 - z) * p * p))) < 1e-9, (prices, z)
    # valeurs figées (calculées avec le solveur de référence ci-dessus) : 1,25 / 6 / 12
    got = odds.shin_probs([1.25, 6.0, 12.0])
    want = ref_shin([1.25, 6.0, 12.0])[0]
    assert [round(x, 4) for x in got] == [round(x, 4) for x in want] == [0.7778, 0.1520, 0.0701], (got, want)
    assert abs(odds.shin_z([1.25, 6.0, 12.0]) - ref_shin([1.25, 6.0, 12.0])[1]) < 1e-9
    print("Shin : égal au solveur indépendant : OK")


def test_favourite_longshot_bias():
    """Shin retire plus de marge aux outsiders qu'aux favoris : proba du favori au-dessus de la proportionnelle, celle des
    outsiders en dessous (un écart croissant avec la cote)."""
    for prices in ([1.25, 6.0, 12.0], [1.5, 3.0, 7.0], [1.3, 3.5, 11.0, 26.0], [1.6, 2.4]):
        sh, pr = odds.shin_probs(prices), prop(prices)
        order = sorted(range(len(prices)), key=lambda i: prices[i])
        assert sh[order[0]] > pr[order[0]], (prices, sh, pr)                 # favori : plus haut
        assert sh[order[-1]] < pr[order[-1]], (prices, sh, pr)               # plus grand outsider : plus bas
        rel = [sh[i] / pr[i] for i in order]
        assert rel == sorted(rel, reverse=True), (prices, rel)               # le raccourcissement croît avec la cote
    print("biais favori-outsider : OK")


# ---------- 2. build_picks ----------

def mk(key, *outs):
    return {"key": key, "outcomes": [dict(zip(("name", "price", "point"), o)) if len(o) == 3 else {"name": o[0], "price": o[1]}
                                     for o in outs if o]}


def book(key, *markets):
    return {"key": key, "title": key.capitalize(), "markets": list(markets)}


def event(books, eid="e1", sport="soccer_epl"):
    return {"id": eid, "sport_key": sport, "home_team": "Alpha", "away_team": "Beta",
            "commence_time": datetime(2026, 10, 8, 19, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "bookmakers": books}


SOFT = {"soft1": (2.00, 3.40, 3.80), "soft2": (2.05, 3.30, 3.70), "soft3": (1.95, 3.45, 3.90), "soft4": (2.02, 3.35, 3.75)}
PIN = (1.80, 3.60, 4.60)                                                # Pinnacle en désaccord : l'extérieur est moins probable


def h2h(prices):
    return mk("h2h", ("Alpha", prices[0]), ("Draw", prices[1]), ("Beta", prices[2]))


def picks_of(books, **kw):
    return {(c["market"], c["who"], c["point"], c["name"]): c for c in odds.build_picks([event(books)], DAY, **kw)}


def avg_shin(rows, i):
    return sum(odds.shin_probs(r)[i] for r in rows) / len(rows)


def test_pinnacle_reference():
    books = [book(k, h2h(v)) for k, v in SOFT.items()] + [book("pinnacle", h2h(PIN))]
    got = picks_of(books)
    w = C.SHARP_WEIGHT
    assert w == 0.7 and C.SHARP_BOOKS[0] == "pinnacle" and "betfair_ex_eu" in C.SHARP_BOOKS, (w, C.SHARP_BOOKS)       # + Betfair Exchange, Matchbook (aucun n'est dans ce relevé)
    for i, name in enumerate(("Alpha", "Draw", "Beta")):
        c = got[("h2h", None, None, name)]
        pin, soft = ref_shin(PIN)[0][i], sum(ref_shin(v)[0][i] for v in SOFT.values()) / len(SOFT)
        assert abs(c["prob_book"] - (w * pin + (1 - w) * soft)) < 1e-8, (name, c["prob_book"], w * pin + (1 - w) * soft)
        assert c["prob_src"] == "pinnacle" and c["n_sharp"] == 1 and c["n_books"] == 5, c
        assert abs(c["prob"] - c["prob_book"]) < 1e-12                    # sans modèle appris : calibration neutre
        spread = max(odds.shin_probs(v)[i] for v in list(SOFT.values()) + [PIN]) - min(
            odds.shin_probs(v)[i] for v in list(SOFT.values()) + [PIN])
        assert abs(c["spread"] - spread / c["prob_book"]) < 1e-8, "l'écart reste calculé sur tous les books"
        # cote typique inchangée (75e centile des 5 books) et valeur = proba x cote typique - 1
        ranked = sorted([v[i] for v in SOFT.values()] + [PIN[i]], reverse=True)
        assert c["odds"] == ranked[1] and c["best_odds"] == ranked[0], c
        assert abs(c["edge"] - (c["prob"] * c["odds"] - 1)) < 1e-12
    ps = [got[("h2h", None, None, n)]["prob_book"] for n in ("Alpha", "Draw", "Beta")]
    assert abs(sum(ps) - 1) < 1e-9, ps
    # Pinnacle tire l'extérieur vers le bas, l'ensemble des books mous le laissait plus haut
    soft_beta = avg_shin(list(SOFT.values()), 2)
    assert got[("h2h", None, None, "Beta")]["prob_book"] < soft_beta
    # SHARP_WEIGHT réglable : 1 = Pinnacle seul, 0 = les autres seuls
    C.SHARP_WEIGHT = 1.0
    try:
        c = picks_of(books)[("h2h", None, None, "Beta")]
        assert abs(c["prob_book"] - ref_shin(PIN)[0][2]) < 1e-8, c["prob_book"]
        C.SHARP_WEIGHT = 0.0
        c = picks_of(books)[("h2h", None, None, "Beta")]
        assert abs(c["prob_book"] - soft_beta) < 1e-8 and c["prob_src"] == "pinnacle", c["prob_book"]
    finally:
        C.SHARP_WEIGHT = w
    print("Pinnacle pondéré à SHARP_WEIGHT : OK")


def test_no_sharp_is_shin_consensus():
    books = [book(k, h2h(v)) for k, v in SOFT.items()]
    got = picks_of(books)
    for i, name in enumerate(("Alpha", "Draw", "Beta")):
        c = got[("h2h", None, None, name)]
        want = sum(ref_shin(v)[0][i] for v in SOFT.values()) / len(SOFT)
        assert abs(c["prob_book"] - want) < 1e-8 and c["prob_src"] == "consensus" and c["n_sharp"] == 0, c
    # Shin et non plus la proportionnelle : l'outsider est plus bas
    prop_beta = sum(prop(v)[2] for v in SOFT.values()) / len(SOFT)
    assert got[("h2h", None, None, "Beta")]["prob_book"] < prop_beta
    print("sans Pinnacle : consensus Shin : OK")


def test_min_bookmakers_and_incomplete_pinnacle():
    # Pinnacle compte parmi les books : 2 mous + Pinnacle = 3 -> des sélections ; 1 mou + Pinnacle = 2 -> aucune
    three = [book("soft1", h2h(SOFT["soft1"])), book("soft2", h2h(SOFT["soft2"])), book("pinnacle", h2h(PIN))]
    assert all(c["n_books"] == 3 and c["prob_src"] == "pinnacle" for c in picks_of(three).values()) and picks_of(three)
    assert picks_of(three[1:]) == {}
    # Pinnacle sans le Draw : marché incomplet écarté (règle des issues complètes) -> consensus des autres seulement
    no_draw = [book(k, h2h(v)) for k, v in SOFT.items()] + [book("pinnacle", mk("h2h", ("Alpha", 1.5), ("Beta", 2.9)))]
    got = picks_of(no_draw)
    assert all(c["prob_src"] == "consensus" and c["n_books"] == 4 for c in got.values()), got
    print("MIN_BOOKMAKERS et marché incomplet de Pinnacle : OK")


def test_totals_missing_line_uses_consensus():
    def tot(line, over, under):
        return mk("totals", ("Over", over, line), ("Under", under, line))
    soft = [book(f"soft{i}", tot(2.5, 1.90 + i * 0.02, 1.95 - i * 0.02), tot(3.5, 2.8 + i * 0.05, 1.42 - i * 0.01)) for i in range(3)]
    pin = book("pinnacle", tot(2.5, 2.10, 1.80))                          # Pinnacle n'affiche pas la ligne 3,5
    got = picks_of(soft + [pin])
    o25, o35 = got[("totals", None, 2.5, "Over")], got[("totals", None, 3.5, "Over")]
    assert o25["prob_src"] == "pinnacle" and o25["n_books"] == 4, o25
    w, pin25 = C.SHARP_WEIGHT, odds.shin_probs([2.10, 1.80])[0]
    soft25 = sum(odds.shin_probs([1.90 + i * 0.02, 1.95 - i * 0.02])[0] for i in range(3)) / 3
    assert abs(o25["prob_book"] - (w * pin25 + (1 - w) * soft25)) < 1e-9
    assert o35["prob_src"] == "consensus" and o35["n_books"] == 3 and o35["n_sharp"] == 0, o35
    soft35 = sum(odds.shin_probs([2.8 + i * 0.05, 1.42 - i * 0.01])[0] for i in range(3)) / 3
    assert abs(o35["prob_book"] - soft35) < 1e-9
    # Pinnacle n'affiche que l'Over d'une ligne : incomplet, la ligne reste au consensus
    half = book("pinnacle", mk("totals", ("Over", 2.10, 2.5)))
    assert picks_of(soft + [half])[("totals", None, 2.5, "Over")]["prob_src"] == "consensus"
    print("totaux : ligne absente chez Pinnacle -> consensus : OK")


def test_double_chance():
    def dc(a, b, c):
        return mk("double_chance", ("Alpha or Draw", a), ("Alpha or Beta", b), ("Draw or Beta", c))
    books = [book(k, h2h(v), dc(*[1.0 / (1 / v[0] + 1 / v[1]) * 0.95, 1.0 / (1 / v[0] + 1 / v[2]) * 0.95, 1.0 / (1 / v[1] + 1 / v[2]) * 0.95]))
             for k, v in SOFT.items()] + [book("pinnacle", h2h(PIN))]
    got = picks_of(books)
    h = {n: got[("h2h", None, None, n)]["prob_book"] for n in ("Alpha", "Draw", "Beta")}
    for name, want in (("Alpha or Draw", h["Alpha"] + h["Draw"]), ("Alpha or Beta", h["Alpha"] + h["Beta"]),
                       ("Draw or Beta", h["Draw"] + h["Beta"])):
        c = got[("double_chance", None, None, name)]
        assert abs(c["prob_book"] - want) < 1e-9 and c["prob_src"] == "pinnacle", (name, c["prob_book"], want)
    assert abs(sum(c["prob_book"] for k, c in got.items() if k[0] == "double_chance") - 2) < 1e-9
    # sans 1X2 : repli proportionnel de masse 2, moyenne des books
    only_dc = [book(k, dc(1.4, 1.35, 2.2)) for k in "abc"]
    pd = {k[3]: c for k, c in picks_of(only_dc).items()}
    assert abs(sum(c["prob_book"] for c in pd.values()) - 2) < 1e-9 and all(c["prob_src"] == "consensus" for c in pd.values())
    q = [1 / 1.4, 1 / 1.35, 1 / 2.2]
    assert abs(pd["Alpha or Draw"]["prob_book"] - 2 * q[0] / sum(q)) < 1e-9
    print("double chance : dérivée du 1X2 ou repli masse 2 : OK")


def test_exchange_as_sharp_book():
    old = C.SHARP_BOOKS
    exch = (1.85, 3.55, 4.40)
    books = [book(k, h2h(v)) for k, v in SOFT.items()] + [book("betfair_ex_eu", h2h(exch))]
    try:
        assert {"pinnacle", "betfair_ex_eu", "matchbook"} <= set(old), old                       # Betfair Exchange et Matchbook comptent par défaut (Pinnacle manque presque partout)
        C.SHARP_BOOKS = ("pinnacle",)
        assert picks_of(books)[("h2h", None, None, "Alpha")]["prob_src"] == "consensus"          # SHARP_BOOKS=pinnacle : exchange non listé, comme avant
        C.SHARP_BOOKS = old
        assert picks_of(books)[("h2h", None, None, "Alpha")]["prob_src"] == "sharp"              # défaut : l'exchange est un avis de référence
        C.SHARP_BOOKS = ("pinnacle", "betfair_ex_eu")
        c = picks_of(books)[("h2h", None, None, "Alpha")]
        pin = ref_shin(exch)[0][0]
        soft = sum(ref_shin(v)[0][0] for v in SOFT.values()) / len(SOFT)
        assert c["prob_src"] == "sharp" and c["n_sharp"] == 1 and abs(c["prob_book"] - (0.7 * pin + 0.3 * soft)) < 1e-8, c
        books.append(book("pinnacle", h2h(PIN)))
        c = picks_of(books)[("h2h", None, None, "Alpha")]
        sharp = (ref_shin(exch)[0][0] + ref_shin(PIN)[0][0]) / 2
        assert c["prob_src"] == "pinnacle" and c["n_sharp"] == 2 and abs(c["prob_book"] - (0.7 * sharp + 0.3 * soft)) < 1e-8, c
    finally:
        C.SHARP_BOOKS = old
    print("exchange en book de référence : OK")


def test_snapshot_and_steam():
    books = [book(k, h2h(v)) for k, v in SOFT.items()] + [book("pinnacle", h2h(PIN))]
    first = odds.build_picks([event(books)], DAY)
    snap = odds.snapshot(first)
    for c in first:
        assert snap[c["key"]]["pb"] == round(c["prob_book"], 4) == snap[c["key"]]["p"] and snap[c["key"]]["src"] == "pinnacle"
    moved = {k: dict(v, pb=v["pb"] * 0.8) for k, v in snap.items()}
    assert all(c["steam"] > 0.2 for c in odds.build_picks([event(books)], DAY, history=moved))            # même source : vrai mouvement
    other = {k: dict(v, src="consensus") for k, v in moved.items()}
    assert all(c["steam"] == 0.0 for c in odds.build_picks([event(books)], DAY, history=other))           # Pinnacle apparu : pas un steam
    legacy = {k: {kk: vv for kk, vv in v.items() if kk != "src"} for k, v in moved.items()}
    assert all(c["steam"] > 0.2 for c in odds.build_picks([event(books)], DAY, history=legacy))           # anciens relevés sans « src »
    print("snapshot et steam : OK")


def main():
    for name, fn in sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)):
        fn()
    for k in ("DATA_DIR", "SOURCES_CACHE_DIR", "ODDS_CACHE_DIR"):
        shutil.rmtree(os.environ[k], ignore_errors=True)
    print("test_sharp : tout est OK")


if __name__ == "__main__":
    main()
