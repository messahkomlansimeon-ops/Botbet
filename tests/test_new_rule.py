"""Compteur « depuis la correction » (hors ligne) : les pronostics de corners, cartons, jaunes, tirs, tirs cadrés et fautes choisis avec la nouvelle règle
(mélange avec 1xBet) portent « rule » 2 et sont comptés à part, avec leur gain réel à la cote 1xBet ; dès 30 résultats, l'exclusion des combinés se décide
sur eux seuls : justes ET gain prouvé (au pire de la marge d'erreur, comptée match par match, pas pire que -10 %), la famille revient ; justes mais gain
incertain, elle reste écartée ; une famille admise n'est écartée que sur une perte prouvée ; avant, l'ancienne règle décide et le bilan montre l'avancée.
Lancer : python -m tests.test_new_rule"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-newrule-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("XBET_PROXY", None)
os.environ.pop("S365_PROXY", None)
os.environ["QUIET_HOURS"] = "off"

from kelly import calib, combos as CB, config as C, fair, settle, status, store  # noqa: E402

DAY = "2026-10-13"
OLD = {"shots": [67, 30, 43.5]}                       # ancienne règle : 45 % réalisés pour 65 % annoncés


def prono(i, market="team_shots_total", **kw):
    return {"key": f"m{i}|{market}||24.5|Over", "id": f"m{i}", "sport_key": "soccer_epl", "home": f"H{i}", "away": f"A{i}",
            "commence": f"{DAY}T15:00:00Z", "market": market, "name": "Over", "point": 24.5, "prob": 0.63, "p0": 0.65, "p_ens": 0.63, "xodds": 1.72,
            "score": 0.1} | kw


def leg():
    return {"market": "team_shots_total", "name": "Over", "home": "A", "away": "B", "sport_key": "soccer_epl", "p0": 0.70, "prob": 0.65, "conf": 0.9,
            "n_min": 10}


def reset(data=None):
    store.save(calib.FILE, data or {})
    calib._memo.clear()


def rows(fam, n, wins, odds, per_match=1):
    """Résultats de la nouvelle règle (data["_new_rows"]) : `n` lignes à la cote `odds`, les `wins` premières gagnées, `per_match` lignes par match."""
    return [[DAY, f"g{i // per_match}", fam, odds, 1 if i < wins else 0] for i in range(n)]


def test_tagged_and_counted():
    store.save(fair.FILE, [])
    pr = [prono(1), prono(2, "team_throws_total"), prono(4, xwhy="price"), prono(5, xodds=None, p_ens=None)]
    fair.record(pr, DAY, shown=pr)
    rows = {r["id"]: r for r in fair.load()}
    assert rows["m1"].get("rule") == C.RULE and "rule" not in rows["m2"], rows                          # touches : règle d'avant
    assert "rule" not in rows["m4"] and "rule" not in rows["m5"], rows        # ligne écartée, match absent de 1xBet : la règle n'a rien choisi
    reset(dict(OLD))
    settled = [dict(r, result="win") for r in fair.load()] + [dict(prono(3), date=DAY, result="loss")]    # sans « rule » : ancienne règle
    assert calib.add(settled) == 5
    d = calib.load()
    assert calib.new_count("shots") == 1 and "throws" not in calib.new_rule(), d                         # seul le tir de la nouvelle règle
    assert calib.new_rule()["shots"][2] == 0.63 and d["shots"][2] > 43.5 + 3 * 0.65 - 0.01, d             # compteur : proba annoncée ; cumul : brute
    assert d["_new_rows"] == [[DAY, "m1", "shots", 1.72, 1]], d                                             # et son gain réel à la cote 1xBet
    print("ok  pronostics de la nouvelle règle marqués (« rule ») et comptés à part ; touches et anciens pronostics : non")


def test_back_or_not():
    reset(dict(OLD))
    assert not CB.data_ok(leg()) and calib.new_rule_note("shots") == " · nouvelle règle 0/30"
    out = "\n".join(settle._reliability_lines()[0])
    assert "🔴 Tirs · 67 · trop confiante (45 % / 65 %) → écartés des combinés · nouvelle règle 0/30" in out, out
    reset(dict(OLD) | {"_new": {"shots": [29, 19, 18.9]}})
    assert not CB.data_ok(leg()) and calib.new_rule_note("shots") == " · nouvelle règle 29/30"           # 29 : l'ancienne règle décide encore
    reset(dict(OLD) | {"_new": {"shots": [30, 19, 19.5]}, "_new_rows": rows("shots", 30, 19, 1.72)})   # 63 % pour 65 % : juste, gain +9 % ± 20 %
    assert calib.viable_factor("team_shots_total") >= C.VIABLE_MIN_K and not CB.data_ok(leg())          # juste, mais gain pas encore prouvé
    note = calib.new_rule_note("shots")
    assert note == " · nouvelle règle : 30 · 63 % / 65 % · gain +9 % (± 20 %, 30 matchs) → gain pas encore prouvé", note
    reset(dict(OLD) | {"_new": {"shots": [30, 21, 19.5]}, "_new_rows": rows("shots", 30, 21, 1.72)})   # 70 % : gain +20 % ± 19 %, au pire +2 %
    assert CB.data_ok(leg()) and calib.in_combos("shots")
    note = calib.new_rule_note("shots")
    assert note == " · nouvelle règle : 30 · 70 % / 65 % · gain +20 % (± 19 %, 30 matchs) → de retour dans les combinés", note
    line = next(x for x in status.learning_lines() if x.startswith(("🔴 Tirs", "🟢 Tirs")))
    assert "écartés" not in line and line.endswith("→ de retour dans les combinés"), line
    assert calib.factor("team_shots_total", base=1.0) < C.VIABLE_MIN_K                                   # la correction des % garde tout l'historique
    reset(dict(OLD) | {"_new": {"shots": [30, 14, 19.5]}, "_new_rows": rows("shots", 30, 14, 1.72)})   # nouvelle règle aussi mauvaise : toujours écartés
    assert not CB.data_ok(leg()) and "de retour" not in calib.new_rule_note("shots")
    assert "→ écartés des combinés" in "\n".join(settle._reliability_lines()[0])
    # corners : seuil de la famille (60) ; 30 résultats ne suffisent pas à juger
    reset({"corners": [100, 45, 66.0], "_new": {"corners": [30, 19, 19.5]}})
    corner = leg() | {"market": "alternate_totals_corners"}
    assert not CB.data_ok(corner) and calib.new_rule_note("corners") == " · nouvelle règle 30/60"
    # famille admise (corners justes) : écartée seulement sur une perte prouvée (65 % à la cote 1,20 : -22 %, au mieux -12 %)
    good = {"corners": [100, 66, 66.0]}
    reset(good | {"_new": {"corners": [60, 39, 39.0]}, "_new_rows": rows("corners", 60, 39, 1.30)})       # -15 % ± 10 % : pas prouvé, reste
    assert CB.data_ok(corner) and "→" not in calib.new_rule_note("corners")
    reset(good | {"_new": {"corners": [60, 39, 39.0]}, "_new_rows": rows("corners", 60, 39, 1.20)})
    assert not CB.data_ok(corner) and not calib.in_combos("corners") and calib.new_rule_note("corners").endswith("→ perte prouvée"), calib.new_rule_note("corners")
    assert "Corners · 100 · juste (66 % / 66 %) → écartés des combinés" in "\n".join(settle._reliability_lines()[0])
    print("ok  dès 30 résultats de la nouvelle règle : justes et gain prouvé, de retour ; gain incertain ou mauvais, toujours écartés ; perte prouvée, écartés")


def test_roi_counted_by_match():
    one = calib.new_roi("shots", {"_new_rows": rows("shots", 30, 19, 1.72)})
    two = calib.new_roi("shots", {"_new_rows": rows("shots", 30, 19, 1.72, per_match=2)})        # mêmes résultats, 2 lignes par match
    assert one["n"] == two["n"] == 30 and one["m"] == 30 and two["m"] == 15 and abs(one["roi"] - two["roi"]) < 1e-12, (one, two)
    assert two["hw"] > one["hw"], (one, two)                                    # lignes corrélées (gagnées ou perdues ensemble) : la marge s'élargit
    assert calib.new_roi("shots", {"_new_rows": rows("corners", 5, 3, 1.5)}) is None
    store.save(calib.FILE, {"_new_rows": rows("shots", C.NEW_RULE_ROWS, 1, 1.5)})
    calib._memo.clear()
    calib.add([dict(prono(9), date=DAY, result="win", rule=C.RULE)])
    assert len(calib.load()["_new_rows"]) == C.NEW_RULE_ROWS and calib.load()["_new_rows"][-1][1] == "m9"     # bornés : les plus récents
    print("ok  gain réel compté match par match (lignes d'un même match : marge plus large), résultats bornés")


def test_detail_line():
    from datetime import datetime, timezone
    from kelly import app, track
    reset(dict(OLD) | {"_new": {"shots": [12, 8, 7.8]}})
    day = str(datetime.now(timezone.utc).date())
    track.save([{"key": f"k{i}", "id": f"e{i}", "date": day, "market": "totals", "sent": True, "odds": 2.0, "prob": 0.55, "result": "win" if i % 2 else "loss"}
                for i in range(4)])
    store.save("bets.json", {"picks": [], "combos": []})
    out = str(app.handle_command("/bilan détail"))
    assert "🔴 Tirs · 67 · trop confiante (45 % / 65 %) · ×0,40 → écartés des combinés · nouvelle règle 12/30" in out, out
    print("ok  bilan détail : avancée du compteur après la correction")


def main():
    test_tagged_and_counted()
    test_back_or_not()
    test_roi_counted_by_match()
    test_detail_line()
    print("tests compteur depuis la correction : OK")


if __name__ == "__main__":
    main()
