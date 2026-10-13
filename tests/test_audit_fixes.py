"""Corrections de l'audit de la base réelle du 13/10 (hors ligne) : le règlement reprend les stats finales vues en direct par le suivi des matchs (m3 du
12/10 : corners de Racing de Córdoba – Los Andes introuvables au règlement, vus par 365scores pendant le match) ; la liste du jour de 365scores garde le
score final des matchs terminés, et règle les petits championnats ; 🩺 status donne l'heure de FIN du matin. Lancer : python -m tests.test_audit_fixes"""
from tests import test_proxy_backup as P      # fixe DATA_DIR (dossier temporaire)

import json  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
from datetime import date, datetime, timedelta, timezone  # noqa: E402

from kelly import livedata, settle, status, store, watch  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 13, 4, 30, tzinfo=UTC)


def leg(market="alternate_totals_corners", name="Under", point=8.5):
    return {"key": f"xb1|{market}||{point}|{name}", "id": "xb1", "sport_key": "soccer_xb_argentina_2", "home": "Racing De Cordoba",
            "away": "CA Los Andes", "commence": "2026-10-12T20:00:00Z", "market": market, "name": name, "point": point, "prob": 0.6, "result": None}


def test_watch_final_stats():
    store.save(watch.FILE, {"v": watch.V, "legs": {"2026-10-12|vm3|xb1|alternate_totals_corners||8.5|Under": {"state": "win", "gid": 77, "at": NOW.isoformat()}},
                            "games": {"77": {"done": 1, "score": "1-0", "fst": {"corners": [3, 4], "shots": [9, 6], "cards": [2, 3]}, "at": NOW.isoformat()}}})
    fst = watch.final_stats({"id": "xb1"})
    assert fst["corners"] == 7 and fst["side"]["home"]["shots"] == 9 and "cards" not in fst, fst      # cartons : jamais (365scores ne compte pas comme 1xBet)
    assert watch.final_stats({"id": "xb2"}) is None
    old = (settle.football_score, settle.FootballData.score, settle.FootballData.fotmob_score, settle.FootballData.stats, settle.fmmatch.match_stats,
           settle.s365.match_stats, settle.s365_score)
    settle.football_score = lambda b: None
    settle.FootballData.score = lambda self, b: None
    settle.FootballData.fotmob_score = lambda self, b: None
    settle.FootballData.stats = lambda self, b: None
    settle.fmmatch.match_stats = lambda b: None
    settle.s365.match_stats = lambda b: None
    settle.s365_score = lambda b, now=None: None
    try:
        b = leg()
        assert settle._settle_fair_row(b, NOW, settle.FootballData()) and b["result"] == "win" and b["final"], b      # 1-0 (suivi), 7 corners (suivi)
        b2 = leg(point=6.5)
        settle._settle_fair_row(b2, NOW, settle.FootballData())
        assert b2["result"] == "loss", b2
        d = store.load(watch.FILE, {})
        d["games"]["77"]["et"] = 1                                                # prolongation : rien de repris
        store.save(watch.FILE, d)
        b3 = leg(point=9.5)
        settle._settle_fair_row(b3, NOW, settle.FootballData())
        assert b3["result"] is None, b3
    finally:
        (settle.football_score, settle.FootballData.score, settle.FootballData.fotmob_score, settle.FootballData.stats, settle.fmmatch.match_stats,
         settle.s365.match_stats, settle.s365_score) = old
    print("ok  règlement : stats finales vues en direct par le suivi des matchs reprises quand aucune source ne les a (jamais après prolongation)")


def test_s365_final_scores():
    g = {"id": 5, "startTime": "2026-10-11T20:00:00+00:00", "statusGroup": 4, "statusText": "Ended", "homeCompetitor": {"id": 1, "name": "Brusque", "score": 2},
         "awayCompetitor": {"id": 2, "name": "Paysandu", "score": 1}}
    assert livedata._final(g) == {"sc": [2, 1]}
    assert livedata._final(g | {"statusText": "After Penalties", "shortStatusText": "Pen"}) == {}           # prolongation / tirs au but : jamais
    assert livedata._final(g | {"statusGroup": 3}) == {}                                                    # en cours
    settle._S365_DAYS[date(2026, 10, 11)] = {5: {"id": 5, "start": "2026-10-11T20:00:00+00:00", "home": {"name": "Brusque"}, "away": {"name": "Paysandu"},
                                                 "sc": [2, 1]}}
    try:
        b = {"id": "x9", "home": "Brusque", "away": "Paysandu", "commence": "2026-10-11T20:00:00Z"}
        ev = settle.s365_score(b, NOW)
        assert ev and [s["score"] for s in ev["scores"]] == ["2", "1"], ev
        assert settle.s365_score(b | {"commence": "2026-10-13T03:00:00Z"}, NOW) is None                     # pas encore fini
    finally:
        settle._S365_DAYS.clear()
    print("ok  liste du jour de 365scores : score final des matchs terminés (jamais après prolongation), règle les petits championnats")


def test_status_morning_end():
    root = tempfile.mkdtemp(prefix="kelly-root-")
    os.makedirs(os.path.join(root, "logs"))
    open(os.path.join(root, "logs", "runner.lock"), "w").close()
    json.dump({"watch_at": 0}, open(os.path.join(root, "logs", "runner_state.json"), "w"))
    store.save("daily_done.json", {"day": "2030-01-01", "at": "2030-01-01T04:00:08+00:00"})
    store.save("progress.json", {"kind": "daily", "started": "2030-01-01T04:00:05+00:00", "finished": "2030-01-01T04:36:14+00:00"})
    lines = status.phone_lines(datetime(2030, 1, 1, 12, 0, tzinfo=UTC), root=root)
    assert "🌅 Matin fait à 04h36 UTC" in lines, lines
    print("ok  status : heure de FIN du matin")


def main():
    test_watch_final_stats()
    test_s365_final_scores()
    test_status_morning_end()
    print("tests corrections de l'audit : OK")


if __name__ == "__main__":
    main()
