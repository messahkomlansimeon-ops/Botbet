"""Compteur « depuis la correction » (hors ligne) : les pronostics de corners, cartons, jaunes, tirs, tirs cadrés et fautes choisis avec la nouvelle règle
(mélange avec 1xBet) portent « rule » 2 et sont comptés à part ; dès 30 résultats, l'exclusion des combinés se décide sur eux seuls : bons, la famille
revient ; mauvais, elle reste écartée ; avant, l'ancienne règle décide et le bilan montre l'avancée. Lancer : python -m tests.test_new_rule"""
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
    print("ok  pronostics de la nouvelle règle marqués (« rule ») et comptés à part ; touches et anciens pronostics : non")


def test_back_or_not():
    reset(dict(OLD))
    assert not CB.data_ok(leg()) and calib.new_rule_note("shots") == " · nouvelle règle 0/30"
    out = "\n".join(settle._reliability_lines()[0])
    assert "🔴 Tirs · 67 · trop confiante (45 % / 65 %) → écartés des combinés · nouvelle règle 0/30" in out, out
    reset(dict(OLD) | {"_new": {"shots": [29, 19, 18.9]}})
    assert not CB.data_ok(leg()) and calib.new_rule_note("shots") == " · nouvelle règle 29/30"           # 29 : l'ancienne règle décide encore
    reset(dict(OLD) | {"_new": {"shots": [30, 19, 19.5]}})                                               # 63 % réalisés pour 65 % annoncés : juste
    assert CB.data_ok(leg()) and calib.viable_factor("team_shots_total") >= C.VIABLE_MIN_K
    note = calib.new_rule_note("shots")
    assert note == " · nouvelle règle : 30 · 63 % / 65 % → de retour dans les combinés", note
    line = next(x for x in status.learning_lines() if x.startswith(("🔴 Tirs", "🟢 Tirs")))
    assert "écartés" not in line and line.endswith("→ de retour dans les combinés"), line
    assert calib.factor("team_shots_total", base=1.0) < C.VIABLE_MIN_K                                   # la correction des % garde tout l'historique
    reset(dict(OLD) | {"_new": {"shots": [30, 14, 19.5]}})                                               # nouvelle règle aussi mauvaise : toujours écartés
    assert not CB.data_ok(leg()) and "de retour" not in calib.new_rule_note("shots")
    assert "→ écartés des combinés" in "\n".join(settle._reliability_lines()[0])
    # corners : seuil de la famille (60) ; 30 résultats ne suffisent pas à juger
    reset({"corners": [100, 45, 66.0], "_new": {"corners": [30, 19, 19.5]}})
    corner = leg() | {"market": "alternate_totals_corners"}
    assert not CB.data_ok(corner) and calib.new_rule_note("corners") == " · nouvelle règle 30/60"
    print("ok  dès 30 résultats de la nouvelle règle : bons, de retour dans les combinés ; mauvais, toujours écartés ; avancée affichée")


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
    test_detail_line()
    print("tests compteur depuis la correction : OK")


if __name__ == "__main__":
    main()
