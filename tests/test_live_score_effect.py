"""Effet du score et des cartons rouges en direct (inplay.team_mults / chance(effect=), live_ledger.json, commit A du 17/10) : tableau des facteurs, bornes, égalité = 1, effet=False (défaut) = EXACTEMENT
le calcul d'avant (bit pour bit), sens de l'effet (l'équipe menée attaque, celle qui mène se replie, le rouge réduit l'équipe), parts domicile / extérieur, buts et cartons sans effet,
journal avec / sans effet (une fois par jambe, 30e minute, résultat à la fin), comparaison et verdict, constantes = ledger, interrupteur LIVE_SCORE_EFFECT dans le suivi. Hors ligne.
Lancer : python -m tests.test_live_score_effect"""
import hashlib
import json
import os
import tempfile
from datetime import timedelta

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-livefx-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

from tests import test_watch_chances as W  # noqa: E402
from kelly import config as C, inplay, liveeffect as LE, store, watch  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORNERS = dict(W.leg(0, "alternate_totals_corners", point=9.5, prob=0.58))


def test_multipliers():
    m = inplay.team_mults
    assert m("corners", (0, 0)) == (1.0, 1.0) and m("shots", (2, 2)) == (1.0, 1.0), "égalité : 1"
    assert m("corners", (1, 0)) == (0.90, 1.15), "l'équipe qui mène se replie (x0,90), la menée attaque (x1,15)"
    assert m("shots", (0, 1)) == (1.15, 0.90) and m("sot", (3, 1)) == (0.85, 1.25), "deux buts : x1,25 / x0,85"
    assert m("corners", (5, 0)) == m("corners", (2, 0)), "l'écart est plafonné à deux buts"
    assert m("corners", (0, 0), (1, 0)) == (0.80, 1.0) and m("shots", (0, 0), (0, 2)) == (1.0, 0.80 ** 2), "rouge : x0,80 par carton, équipe réduite seule"
    assert m("fouls", (0, 0), (1, 0)) == (1.15, 1.0) and m("fouls", (2, 0), (0, 0)) == (1.0, 1.0), "fautes : x1,15 au rouge, pas d'effet du score"
    a, b = m("corners", (0, 1), (1, 0))
    assert abs(a - 1.15 * 0.80) < 1e-12 and b == 0.90, "score et rouge se composent"
    for key in ("cards", "yellow", "throws", "offsides", "goals"):
        assert m(key, (2, 0), (1, 1)) == (1.0, 1.0), key
    lo, hi = inplay.MULT_BOUNDS
    for score in ((0, 0), (0, 3), (4, 0)):
        for reds in ((0, 0), (3, 0), (0, 4)):
            for key in ("corners", "shots", "sot", "fouls"):
                assert all(lo <= x <= hi for x in m(key, score, reds)), (key, score, reds)
    assert inplay.SCORE_MULT == {1: (1.15, 0.90), 2: (1.25, 0.85)} and inplay.RED_MULT == {"corners": 0.80, "shots": 0.80, "sot": 0.80, "fouls": 1.15}
    print("ok  facteurs : égalité 1, mené / menant, plafond à deux buts, rouge par carton, fautes, composition, bornes, aucun effet sur cartons / touches / hors-jeu / buts")


def test_shares_and_sides():
    assert inplay.shares("corners", None) == (0.5, 0.5) and inplay.shares("corners", {"stats": {"corners": {"h": 0, "a": 3}}}) == (0.5, 0.5)
    sh, sa = inplay.shares("corners", {"stats": {"corners": {"h": 6.0, "a": 4.0}}})
    assert (sh, sa) == (0.6, 0.4)
    pred = {"stats": {"corners": {"h": 6.0, "a": 4.0}}}
    mh, ma = inplay.team_mults("corners", (1, 0))
    assert inplay.side_mult("corners", "home", (1, 0), None, pred) == mh and inplay.side_mult("corners", "away", (1, 0), None, pred) == ma
    assert abs(inplay.side_mult("corners", "total", (1, 0), None, pred) - (0.6 * mh + 0.4 * ma)) < 1e-12
    assert abs(inplay.side_mult("corners", "total", (1, 0), None, None) - (mh + ma) / 2) < 1e-12, "sans parts : 50 / 50"
    print("ok  parts domicile / extérieur (modèle d'avant-match, sinon 50 / 50) et facteur par côté du pari")


def test_chance_bit_for_bit_and_direction():
    for score in ((0, 0), (1, 0), (0, 2)):
        for minute, period in ((20, 1), (50, 2), (80, 2)):
            for key, mk, side in (("corners", "alternate_totals_corners", "total"), ("corners", "team_corners_home", "home"), ("shots", "team_shots_away", "away"), ("fouls", "team_fouls_total", "total"),
                                  ("cards", "alternate_totals_cards", "total"), ("goals", "totals", None)):
                lg = W.leg(0, mk, point=5.5 if key != "goals" else 2.5, prob=0.55)
                stats = {key: (3, 2)} if key != "goals" else None
                for reds in ((0, 0), (1, 0)):
                    old = inplay.chance(lg, key, side, score, minute, period, reds, stats)
                    assert old == inplay.chance(lg, key, side, score, minute, period, reds, stats, None, False), "effect=False : bit pour bit"
                    if key in ("cards", "goals"):
                        assert inplay.chance(lg, key, side, score, minute, period, reds, stats, None, True) == old, "cartons et buts : jamais d'effet"
    lvl = inplay.chance(CORNERS, "corners", "total", (0, 0), 50, 2, (0, 0), {"corners": (3, 2)}, None, True)
    assert lvl == inplay.chance(CORNERS, "corners", "total", (0, 0), 50, 2, (0, 0), {"corners": (3, 2)}), "égalité sans rouge : identique"
    # côté domicile qui MÈNE de deux buts : ses corners restants baissent -> « plus de » moins probable ; l'équipe MENÉE (extérieur) : plus probable
    home = W.leg(0, "team_corners_home", point=4.5, prob=0.5)
    away = W.leg(0, "team_corners_away", point=4.5, prob=0.5)
    for lg, side, key in ((home, "home", "corners"), (away, "away", "corners")):
        base = inplay.chance(lg, key, side, (2, 0), 50, 2, (0, 0), {"corners": (2, 2)})[0]
        eff = inplay.chance(lg, key, side, (2, 0), 50, 2, (0, 0), {"corners": (2, 2)}, None, True)[0]
        assert (eff < base) if side == "home" else (eff > base), (side, base, eff)
    # rouge au domicile : ses tirs restants baissent
    sh = W.leg(0, "team_shots_home", point=9.5, prob=0.5)
    assert inplay.chance(sh, "shots", "home", (0, 0), 50, 2, (1, 0), {"shots": (6, 5)}, None, True)[0] < inplay.chance(sh, "shots", "home", (0, 0), 50, 2, (1, 0), {"shots": (6, 5)})[0]
    # « moins de » : l'effet joue dans l'autre sens (une équipe qui attaque davantage -> moins de chances de rester sous la ligne)
    un = W.leg(0, "team_corners_away", point=4.5, prob=0.5, name="Under")
    assert inplay.chance(un, "corners", "away", (2, 0), 50, 2, (0, 0), {"corners": (2, 2)}, None, True)[0] < inplay.chance(un, "corners", "away", (2, 0), 50, 2, (0, 0), {"corners": (2, 2)})[0]
    print("ok  effect=False = calcul d'avant (bit pour bit) ; égalité identique ; mené attaque, menant se replie, rouge réduit, « moins de » à l'envers ; cartons et buts intacts")


def test_ledger_matches_code():
    with open(os.path.join(ROOT, "live_ledger.json"), encoding="utf-8") as f:
        led = json.load(f)
    pre = led["prereg"]
    assert hashlib.sha256(json.dumps(pre, sort_keys=True, ensure_ascii=False).encode()).hexdigest() == led["prereg_sha"], "pré-déclaration inchangée depuis son commit"
    fx = pre["score_effect"]
    assert {int(k): tuple(v) for k, v in fx["score_mult"].items()} == inplay.SCORE_MULT and fx["red_mult"] == inplay.RED_MULT and tuple(fx["families_score"]) == inplay.SCORE_STATS
    assert tuple(fx["bounds"]) == inplay.MULT_BOUNDS and pre["journal"]["min_legs"] == LE.MIN_LEGS and tuple(pre["journal"]["families"]) == LE.FAMILIES
    assert pre["journal"]["min_minute"] == C.LIVE_MIN_MINUTE == 30 and (pre["switch"]["name"], pre["switch"]["default"]) == ("LIVE_SCORE_EFFECT", True) and C.LIVE_SCORE_EFFECT is True
    print("ok  constantes du code = live_ledger.json (empreinte de la pré-déclaration inchangée)")


def _stat_leg(i=0):
    return W.leg(i, "alternate_totals_corners", point=9.5, prob=0.58)


def test_journal_and_compare():
    store.save(LE.FILE, [])
    lg = _stat_leg()
    assert not LE.record(lg, "corners", "total", (0, 1), 29, (0, 0), 0.5, 0.45), "avant la 30e minute : rien"
    assert not LE.record(lg, "goals", None, (0, 1), 40, (0, 0), 0.5, 0.45) and not LE.record(lg, "cards", "total", (0, 1), 40, (0, 0), 0.5, 0.45), "familles sans effet : rien"
    assert LE.record(lg, "corners", "total", (0, 1), 31, (0, 0), 0.5, 0.45) and not LE.record(lg, "corners", "total", (0, 2), 60, (0, 0), 0.7, 0.6), "une seule ligne par jambe (la première)"
    r = LE.rows()
    assert len(r) == 1 and r[0]["minute"] == 31 and r[0]["p"] == 0.5 and r[0]["p_raw"] == 0.45 and r[0]["result"] is None
    assert not LE.settle("inconnue", "win") and not LE.settle(lg["key"], None) and LE.settle(lg["key"], "win") and not LE.settle(lg["key"], "loss"), "résultat posé une fois"
    assert LE.rows()[0]["result"] == "win"
    # comparaison : 40 jambes gagnées où l'effet monte la chance (p 0,7 > p_raw 0,6), 40 perdues où il la baisse : l'effet améliore
    data = []
    for i in range(80):
        win = i % 2 == 0
        data.append({"key": f"k{i}", "id": f"m{i // 2}", "fam": "corners", "p": 0.7 if win else 0.3, "p_raw": 0.6 if win else 0.4, "result": "win" if win else "loss"})
    c = LE.compare(data)
    assert c["corners"]["n"] == 80 and c["corners"]["ecart"] < 0 and c["all"]["n"] == 80 and abs(c["corners"]["realise"] - 0.5) < 1e-9
    assert LE.verdict(c["corners"]) == "pas assez de jambes", "80 < 200"
    big = [dict(x, key=x["key"] + f"-{j}", id=x["id"] + f"-{j}") for j in range(3) for x in data]
    assert LE.verdict(LE.compare(big)["corners"]) == "mieux"
    worse = [dict(x, p=x["p_raw"] - 0.1 if x["result"] == "win" else x["p_raw"] + 0.1) for x in big]
    assert LE.verdict(LE.compare(worse)["corners"]) == "pire"
    flat = [dict(x, p=x["p_raw"]) for x in big]
    assert LE.verdict(LE.compare(flat)["corners"]) == "indécis" or LE.compare(flat)["corners"]["ecart"] == 0
    assert LE.compare([]) == {} and LE.verdict(None) == "pas assez de jambes"
    # plafond du journal : les plus anciennes lignes réglées partent d'abord, les non réglées restent
    store.save(LE.FILE, [{"key": f"o{i}", "fam": "corners", "result": "win", "p": 0.5, "p_raw": 0.5} for i in range(LE.MAX_ROWS)] + [{"key": "open", "fam": "corners", "result": None, "p": 0.5, "p_raw": 0.5}])
    assert LE.record(_stat_leg(1), "shots", "home", (0, 0), 35, (0, 0), 0.5, 0.5)
    rows = LE.rows()
    assert len(rows) == LE.MAX_ROWS and any(x["key"] == "open" for x in rows) and rows[-1]["key"] == _stat_leg(1)["key"] and not any(x["key"] == "o0" for x in rows)
    print("ok  journal : première lecture à partir de la 30e, une ligne par jambe, résultat une fois ; comparaison avec / sans, verdict (200 jambes), plafond du fichier")


def test_watch_wiring():
    store.save(watch.FILE, {})
    store.save(LE.FILE, [])
    lg = W.leg(0, "alternate_totals_corners", point=9.5, prob=0.58)
    stats = {"corners": (3, 2)}
    old = C.LIVE_SCORE_EFFECT
    try:
        for flag in (True, False):
            store.save(watch.FILE, {})
            store.save(LE.FILE, [])
            C.LIVE_SCORE_EFFECT = flag
            with W.tickets([W.ticket("vm1", [lg])]):
                W.run(20, {0: W.live((0, 0), 20)}, stats)
                W.run(41, {0: W.live((0, 2), 41, red=(1, 0))}, stats)
                rows = LE.rows()
                assert len(rows) == 1 and rows[0]["minute"] == 41 and rows[0]["reds"] == [1, 0] and rows[0]["score"] == [0, 2] and rows[0]["result"] is None, rows
                if flag:
                    assert rows[0]["p"] != rows[0]["p_raw"], "l'effet change la chance affichée"
                else:
                    assert rows[0]["p"] == rows[0]["p_raw"], "LIVE_SCORE_EFFECT=0 : la chance d'avant"
                W.run(60, {0: W.live((0, 2), 60, red=(1, 0))}, {"corners": (9, 5)})
                assert len(LE.rows()) == 1, "une seule ligne par jambe"
                W.run(115, {0: {"live": False, "ended": True, "score": (0, 2), "minute": 90}}, {"corners": (10, 6)})
                assert LE.rows()[0]["result"] == "win", LE.rows()
    finally:
        C.LIVE_SCORE_EFFECT = old
    print("ok  suivi : chance avec effet affichée et chance sans effet notée à côté, une ligne par jambe, résultat à la fin ; LIVE_SCORE_EFFECT=0 : chances identiques")


def main():
    test_multipliers()
    test_shares_and_sides()
    test_chance_bit_for_bit_and_direction()
    test_ledger_matches_code()
    test_journal_and_compare()
    test_watch_wiring()
    print("tests effet du score en direct : OK")


if __name__ == "__main__":
    main()
