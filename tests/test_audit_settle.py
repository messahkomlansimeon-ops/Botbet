"""Corrections de l'audit des données (hors ligne) : une lecture ratée de calib.json n'efface ni ne perd la fiabilité ; un match après prolongation
n'est jamais réglé sur 120 minutes (FotMob : page de match, liste des résultats) ; un total de corners ou de cartons auquel il manque une équipe
(null d'API-Football) n'est pas réglé comme si elle valait 0. Lancer : python -m tests.test_audit_settle"""
import json
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-auditsettle-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY", "STATS_API_KEY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

from kelly import calib, settle, stats, store  # noqa: E402
from kelly.sources import fmmatch, fotmob  # noqa: E402


def test_unreadable_calib():
    store.save(calib.FILE, {"shots": [200, 90, 130.0], "_t": "2026-10-09"})
    calib._memo.clear()
    assert calib.load()["shots"][0] == 200
    real_db, calls = store._db, {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:                                   # la lecture du contenu (la 1re : la version du document)
            raise OSError("database is locked")
        return real_db()
    calib._memo.pop("checked", None)
    calib._memo["sig"] = None                                 # le document « a changé » : il sera relu
    store._db = flaky
    try:
        assert calib.load()["shots"][0] == 200               # lecture ratée : les derniers bons cumuls, jamais {}
        rows = [{"market": "team_shots_total", "name": "Over", "home": "A", "away": "B", "p0": 0.65, "prob": 0.6, "result": "win"}]
        assert calib.add(rows) == 0 and not rows[0].get("calib")      # rien de compté ni de marqué : réessayé au prochain règlement
    finally:
        store._db = real_db
        store._unreadable.clear()
    calib._memo.clear()
    assert calib.load() == {"shots": [200, 90, 130.0], "_t": "2026-10-09"}
    assert calib.add(rows) == 1 and rows[0]["calib"]
    print("ok  lecture ratée de calib.json : derniers bons cumuls gardés, aucun résultat marqué compté sans être écrit")


def test_extra_time():
    pp = {"props": {"pageProps": {"general": {"finished": True, "matchId": 77, "matchTimeUTCDate": "2026-10-08T19:00:00Z"},
          "header": {"status": {"reason": {"short": "AET"}}, "teams": [{"name": "Inter", "id": 1, "score": 2}, {"name": "Arsenal", "id": 2, "score": 1}]},
          "content": {"stats": {"Periods": {"All": {"stats": [{"stats": [{"key": "corners", "stats": [7, 6]}, {"key": "total_shots", "stats": [18, 15]}]}]}}}}}}}
    page = lambda p: '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(p) + '</script>'      # noqa: E731
    assert fmmatch.parse_match(page(pp)) is None                                     # après prolongation : stats de 120 min, jamais lues
    pp["props"]["pageProps"]["header"]["status"]["reason"]["short"] = "FT"
    assert fmmatch.parse_match(page(pp))["s"]["corners"] == [7, 6]
    del pp["props"]["pageProps"]["header"]["status"]
    assert fmmatch.parse_match(page(pp)) is not None                                 # statut absent : comme avant
    with open(os.path.join(os.path.dirname(__file__), "fixtures", "sources", "fotmob_league47.json"), encoding="utf-8") as fh:
        raw = fh.read()
    def league(text):
        inner = json.dumps({"props": {"pageProps": json.loads(text)}}, ensure_ascii=False).replace("</", "<\\/")
        return fotmob.parse_league(f'<script id="__NEXT_DATA__" type="application/json">{inner}</script>')[0]["matches"]
    n_ft = len(league(raw))
    assert n_ft > 5 and '"reason": {"short": "FT"' in raw
    assert len(league(raw.replace('"reason": {"short": "FT"', '"reason": {"short": "AET"', 1))) == n_ft - 1      # score après prolongation : ignoré
    assert len(league(raw.replace('"awarded": false', '"awarded": true', 1))) == n_ft - 1                      # tapis vert : ignoré
    print("ok  prolongation : page de match FotMob ignorée (stats de 120 min) ; statut « FT » ou absent : lu comme avant")


def test_null_team_total():
    teams = [{"team": {"id": 1}, "statistics": [{"type": "Corner Kicks", "value": 5}, {"type": "Total Shots", "value": 12}, {"type": "Yellow Cards", "value": 2}]},
             {"team": {"id": 2}, "statistics": []}]
    e = stats._entry_from(teams)
    fst = dict(e, side={"home": e["teams"]["1"], "away": e["teams"]["2"]})
    b = {"home": "A", "away": "B", "market": "alternate_totals_corners", "name": "Under", "point": 8.5, "who": None}
    ev = {"scores": [{"name": "A", "score": "1"}, {"name": "B", "score": "0"}]}
    assert settle.settle_pick(b, ev, fst) is None and settle.settle_pick(dict(b, market="alternate_totals_cards", point=4.5), ev, fst) is None
    assert not settle._has_stat(b, fst) and settle.match_total(fst, "corners") is None
    both = dict(fst, side={"home": e["teams"]["1"], "away": e["teams"]["1"]}, corners=10)
    assert settle.settle_pick(b, ev, both) == "loss"                                 # les deux équipes connues : réglé
    print("ok  total de corners / cartons avec une équipe sans stats : non réglé (jamais compté 0), autre source essayée")


def test_combo_legs_once():
    leg = lambda k, r="win": {"key": k, "result": r, "prob": 0.7}      # noqa: E731
    rows = [{"date": "2026-10-12", "legs": [leg("a"), leg("b")]}, {"date": "2026-10-12", "legs": [leg("a"), leg("c")]},
            {"date": "2026-10-12", "custom": 1, "legs": [leg("d")]}, {"date": "2026-10-13", "legs": [leg("a")]}]
    got = settle._combo_legs(rows)
    assert [x["key"] for x in got] == ["a", "b", "c", "a"] and got[0] is rows[0]["legs"][0], got     # « a » une fois par jour, sur mesure exclu
    print("ok  prudence par jambe : chaque jambe une fois par jour, combinés sur mesure exclus")


def main():
    test_unreadable_calib()
    test_extra_time()
    test_null_team_total()
    test_combo_legs_once()
    print("tests corrections de l'audit des données : OK")


if __name__ == "__main__":
    main()
