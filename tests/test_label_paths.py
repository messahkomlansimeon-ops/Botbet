"""Toute ligne jouable chez 1xBet porte une étiquette `strategy` (fill à défaut), sur chaque chemin de construction de combiné (matin, un par match, audacieux / complément, à la
demande) ; compteur « jambes sans étiquette » à 0 pour les lignes créées. Le combiné LIVE n'en a pas encore (à étiqueter après le CLV par jambe). Hors ligne.
Lancer : python -m tests.test_label_paths"""
from tests import test_notes_fcfa as N      # fixe DATA_DIR (dossier temporaire), monde de test, réglages des jeux d'essai
from kelly import app, combos as CB, ensemble as E, fair, lanestats, money, store, xprice  # noqa: E402


def test_every_playable_line_is_labelled():
    """Supervision du 14/10 : toute ligne jouable chez 1xBet (y compris « low », jambes COMBI_FILL_* / COMBI_BOLD_*) reçoit une étiquette (fill à défaut) ; chaque jambe de chaque
    chemin de construction de combiné en porte une. Seul le combiné LIVE en est dépourvu pour l'instant (décision : à étiqueter après le CLV par jambe)."""
    E.FILE and store.save(E.FILE, {})
    E._memo.clear()
    N.reset()
    money.set_daily(5000)
    ps = [N.unpriced(i) for i in range(9)]
    priced = {f"m{i}": {("totals", "Under", 3.5): (1.55 if i < 5 else 1.30)} for i in range(8)}            # m5..m7 : 1xBet paie trop peu ; m8 : absent de 1xBet
    xprice.apply(ps, fetch=N.fake_fetch(priced))
    assert [c.get("strategy") for c in ps] == ["sharpgap"] * 5 + ["fill"] * 3 + [None], [(c.get("strategy"), c.get("xkind"), c.get("xwhy")) for c in ps]
    assert all(bool(c.get("strategy")) == bool(c.get("xodds")) for c in ps), "une étiquette si et seulement si la ligne porte un prix 1xBet"
    paths = {
        "matin (fair_plan)": [l for _, combo in CB.fair_plan(ps) if combo for l in combo[0]],
        "un par match (best_per_match)": CB.best_per_match(ps),
        "audacieux / complément (_bold_pools)": app._bold_pools([], ps, "", min_p=0.0, min_ev=-1)[0],
        "à la demande (fair_legs)": CB.fair_legs(ps),
    }
    for name, legs in paths.items():
        assert legs and all(l.get("strategy") for l in legs), (name, [(l.get("id"), l.get("strategy")) for l in legs])
    row = fair.combo_row(5, ([c for c in ps if c.get("xodds")][:3], 4.0, 0.3), "2026-10-14")
    assert all(l.get("strategy") for l in row["legs"]) and "has_value" in row, row
    store.save("fair_combos.json", [row | {"date": __import__("datetime").date.today().isoformat()}])
    n, total = lanestats.unlabeled()
    assert n == 0 and total >= 3 and lanestats.unlabeled_line() == f"jambes sans étiquette : 0 / {total} (lignes du jour)", (n, total)
    store.save("fair_combos.json", [])
    print("ok  toute ligne jouable chez 1xBet a une étiquette (fill à défaut) sur tous les chemins de combinés ; compteur de jambes sans étiquette à 0 ; live : à faire après le CLV par jambe")


def test_discarded_and_other_families():
    """Revue du 14/10 : une ligne écartée par l'ensemble (« weak », xwhy) ou sous le prix mini est `fill`, jamais sharpgap / stats ; les compteurs ignorent le live, documents abîmés compris."""
    from kelly import config as C
    c = N.unpriced(0) | {"market": "alternate_totals_corners", "name": "Under", "point": 9.5, "key": "m0|alternate_totals_corners||9.5|Under", "xodds": 1.8, "xwhy": "line"}
    ps = [N.unpriced(i) for i in range(3)]
    xprice.apply(ps, fetch=N.fake_fetch({f"m{i}": {("totals", "Under", 3.5): 1.30} for i in range(3)}))
    assert [p["strategy"] for p in ps] == ["fill"] * 3 and all(p["xodds"] for p in ps)
    orig = xprice._price
    try:
        def fake(c_, px_, refs_=None):                       # ligne « weak » : prix posé, xkind retiré, motif d'écart (comme _ens_fields(price_ok=False))
            c_.update(xodds=1.8, xwhy="line")
            c_.pop("xkind", None)
            return "weak"
        xprice._price = fake
        w = c.copy(); w.pop("xkind", None)
        assert xprice.price(w, {}, None) == "weak" and w["strategy"] == "fill", w.get("strategy")
    finally:
        xprice._price = orig
    # compteur : live ignoré, documents abîmés ignorés, ensemble éteint = pas de ligne
    store.save("fair_combos.json", [{"date": __import__("datetime").date.today().isoformat(), "legs": [{"xodds": 1.5, "live": True}, {"xodds": 1.5, "strategy": "fill"}]}, "pas un dict", None])
    assert lanestats.unlabeled() == (0, 1) and lanestats.unlabeled_line().startswith("jambes sans étiquette : 0 / 1")
    C.ENSEMBLE_ENABLED = False
    try:
        assert lanestats.unlabeled_line() == ""
    finally:
        C.ENSEMBLE_ENABLED = True
    store.save("fair_combos.json", [])
    print("ok  ligne écartée = fill, compteur : live et documents abîmés ignorés, ensemble éteint = pas de ligne")


def main():
    test_every_playable_line_is_labelled()
    test_discarded_and_other_families()
    print("tests chemins étiquetés : OK")


if __name__ == "__main__":
    main()
