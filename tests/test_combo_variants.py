"""Variantes du constructeur de combinés en fantôme (kelly/combovariants.py) : pool du jour gardé et archivé, quatre variantes reconstruites à pool égal, jamais envoyées ni dans les documents de production,
mesure jointe au journal, règle pré-déclarée. Lancer : python -m tests.test_combo_variants"""
from tests import test_season_combos as S      # fixe DATA_DIR, monde de test, jambes `saison` fictives
from tests import test_combi_track as T
from kelly import combos as CB, combovariants as V, config as C, ensemble, fair, lanestats, store  # noqa: E402

DAY = T.DAY


def world():
    S.setup()
    store.save(fair.COMBOS_FILE, [])
    store.save(fair.GHOST_COMBOS_FILE, [])
    store.save("bets.json", {"picks": [], "combos": []})
    sea = S.make_legs()
    pronos = [T.prono(i, prob=0.62 + (i % 5) / 50, xodds=1.7 + (i % 4) / 10, min_odds=1.5) for i in range(14)]
    picks = [T.pick(i) for i in range(1, 9)]
    return picks, pronos, sea


def test_pool_and_archive():
    picks, pronos, sea = world()
    n, rows = V.run(DAY, picks, pronos, sea)
    assert n > 0 and rows > 0
    doc = store.load(f"pool_{DAY}.json", None)
    assert doc and doc["cols"] == list(V.POOL_COLS) and set(doc["voies"]) == {"reels", "fair", "saison"} and doc["voies"]["saison"], doc.keys()
    row = doc["voies"]["saison"][0]
    assert len(row) == len(V.POOL_COLS) and row[V.POOL_COLS.index("prob")] and row[V.POOL_COLS.index("xodds")], row
    assert store.load(f"arch/pool/{DAY}.json", None) == doc, "archive du jour"
    # anciens pools : data/ garde 3 jours, l'archive 90 jours
    store.save("pool_2020-01-01.json", {"day": "2020-01-01"})
    store.save("arch/pool/2020-01-01.json", {"day": "2020-01-01"})
    store.save("arch/pool/2029-12-20.json", {"day": "2029-12-20"})
    V.record_pool(DAY, V.pools(picks, pronos, sea))
    assert not store.exists("pool_2020-01-01.json") and not store.exists("arch/pool/2020-01-01.json") and store.exists("arch/pool/2029-12-20.json")
    print("ok  pool complet de chaque voie gardé (data/pool_<jour>.json + arch/pool), anciens pools élagués (3 jours / 90 jours)")


def test_variants_isolated():
    picks, pronos, sea = world()
    before = {n: store.load(n, None) for n in (fair.COMBOS_FILE, fair.GHOST_COMBOS_FILE, "bets.json", "tracked.json", "today.json")}
    old_cfg = (C.COMBO_OBJECTIVE, C.COMBO_MAX_FILL_SHARE)
    V.run(DAY, picks, pronos, sea)
    rows = store.load(V.FILE, [])
    assert {r["variant"] for r in rows} == {"prob", "prob+cap", "ev", "ev+cap"} and {r["voie"] for r in rows} >= {"fair", "saison"}, {(r["voie"], r["variant"]) for r in rows}
    assert any(r["built"] for r in rows) and all(("legs" in r) == bool(r["built"]) for r in rows)
    per = {}
    for r in rows:
        per.setdefault((r["voie"], r["tier"]), set()).add(r["variant"])
    assert all(v == {"prob", "prob+cap", "ev", "ev+cap"} for v in per.values()), "chaque palier est reconstruit pour les quatre variantes"
    assert (C.COMBO_OBJECTIVE, C.COMBO_MAX_FILL_SHARE) == old_cfg, "réglages remis"
    after = {n: store.load(n, None) for n in before}
    assert after == before, "aucun document de production touché (fair_combos, fantômes, bets, tracked, today)"
    # une relance du même jour remplace, ne double pas
    n1 = len(store.load(V.FILE, []))
    V.run(DAY, picks, pronos, sea)
    assert len(store.load(V.FILE, [])) == n1
    # les variantes ne consomment pas l'Usage de la production
    u = CB.Usage()
    V.build(DAY, V.pools(picks, pronos, sea))
    assert not u.by_leg
    # une exception pendant un palier remet quand même les réglages
    real = CB.plan
    CB.plan = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        V.build(DAY, V.pools(picks, pronos, sea))
    finally:
        CB.plan = real
    assert (C.COMBO_OBJECTIVE, C.COMBO_MAX_FILL_SHARE) == old_cfg
    print("ok  quatre variantes à pool égal, jamais dans la production (aucun document touché, hors Usage), relance sans doublon, réglages toujours remis")


def test_measure_and_rule():
    picks, pronos, sea = world()
    V.run(DAY, picks, pronos, sea)
    rows = store.load(V.FILE, [])
    built = [r for r in rows if r["built"] and r["variant"] == "prob"]
    assert built
    # journal : toutes les jambes gagnent, dernier prix 1xBet un peu plus bas que le prix pris (CLV positif)
    log = []
    for r in rows:
        for l in r.get("legs") or []:
            log.append({"key": l["key"], "id": l["id"], "date": DAY, "result": "win", "xodds": l["xodds"], "strategy": l.get("strategy") or "stats",
                        "xclose": {"odds": l["xodds"] / 1.05, "lead_min": 30, "susp": False}})
    seen = {}
    for x in log:
        seen[x["key"]] = x
    store.save(ensemble.LOG_FILE, list(seen.values()))
    lanestats._memo["key"] = None
    out = V.lines(None, None)
    assert out and out[1].startswith("toutes voies") or out[0].startswith("fantômes"), out[:3]
    text = "\n".join(out)
    assert "prob (production)" in text and "ev+cap" in text and "voie fair" in text and "palier" in text and "pas encore lisible" in text, text
    summ = V.summarize(rows, V._journal())
    p = summ["prob"]
    assert 0 < p["settled"] <= p["built"] and p["wins"] == p["settled"] and p["clv"][1] is not None and abs(p["clv"][1] - __import__("math").log(1.05)) < 1e-6, p
    # règle pré-déclarée : lisible après 150 paliers ; remplace si CLV >= et IC exclut une dégradation et paliers >= 70 %
    fake = {n: {"built": 300, "tried": 300, "settled": 0, "wins": 0, "gain": 0, "legs": 1, "fill": 0, "clv": (100, 0.010, 0.004)} for n, _, _ in V.VARIANTS}
    fake["ev"]["clv"] = (100, 0.030, 0.004)
    fake["ev+cap"]["clv"] = (100, 0.030, 0.004)
    fake["ev+cap"]["built"] = 160
    fake["prob+cap"]["clv"] = (100, 0.011, 0.004)
    v = V.verdict(fake, rows)
    assert v["ev"].startswith("REMPLACE") and v["ev+cap"].startswith("ne remplace pas") and "plus de 30 %" in v["ev+cap"] and v["prob+cap"].startswith("ne remplace pas"), v
    print("ok  mesure jointe au journal (réussite, gain, CLV ± IC, remplissage, par voie et palier) ; règle pré-déclarée : lisible à 150 paliers / 21 jours, remplacement seulement si l'IC exclut une dégradation")


def test_ev_line_display():
    """Sous chaque combiné envoyé : « Espérance du combiné : +4 % (valeur 3/5, remplissage 2/5) » = Π(p x cote) − 1, toujours dite, même négative."""
    from kelly import fmt
    legs = [{"key": f"k{i}", "id": f"e{i}", "prob": 0.8, "odds": 1.5, "xodds": 1.5, "strategy": "fill" if i < 2 else "stats", "market": "totals", "name": "Over", "point": 2.5,
             "home": "A", "away": "B", "pick": "x", "start": "19:00", "league": "L", "event": "A - B", "group": "soccer"} for i in range(5)]
    prob, odds = 0.8 ** 5 * 0.95, 1.5 ** 5
    line = fmt.combo_ev_line(legs, prob, odds)
    assert line == f"📈 Espérance du combiné : {fmt.signed((prob * odds - 1) * 100, ' %')} (valeur 3/5, remplissage 2/5)", line
    neg = fmt.combo_ev_line(legs, 0.2, 2.0)
    assert "−" in neg or "-" in neg, neg
    assert "à la cote mini" in fmt.combo_ev_line(legs, prob, odds, mini=True) and "mini" not in line
    assert fmt.combo_ev_line([], 0.5, 2.0) == "" and fmt.combo_ev_line(legs, None, 2.0) == ""
    msg = str(fmt.combo("🛡️ SÛR", legs, odds, prob))
    assert "📈 Espérance du combiné" in msg and "(valeur 3/5, remplissage 2/5)" in msg, msg[:400]
    print("ok  affichage : espérance du combiné (valeur / remplissage) sous chaque combiné, négative aussi, cote mini dite")


def main():
    test_pool_and_archive()
    test_variants_isolated()
    test_measure_and_rule()
    test_ev_line_display()
    print("tests variantes du constructeur : OK")


if __name__ == "__main__":
    main()
