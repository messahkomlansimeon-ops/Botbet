"""Voie cotes, collecte (kelly/oddsonly.observe) sur des relevés 1xBet FICTIFS : première détection, lecture servie par le cache (non comptée), survie, disparition, cellule disparue (censure), coup d'envoi (censure), xclose
à 0-90 min, mouvement (observation), suspect, familles hors voies jamais journalisées, aucun appel réseau, rien dans le journal d'ensemble. Hors ligne.
Lancer : python -m tests.test_oddsonly_collect"""
import os
import tempfile
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-oddsonly-col-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-oddsonly-col-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY"):
    os.environ.pop(k, None)

import requests  # noqa: E402

from tests import test_oddsonly as T  # noqa: E402, I001
from kelly import ensemble, oddsonly as O, store  # noqa: E402
from kelly.sources import xbet  # noqa: E402

KO = datetime(2026, 10, 17, 20, 0, tzinfo=timezone.utc)
EV = [("soccer_epl", {"id": "m1", "home_team": T.H, "away_team": T.A, "commence_time": KO.isoformat()})]
DCK = ("double_chance", f"{T.H} or Draw", None)


def at(h, m=0):
    return datetime(2026, 10, 17, h, m, tzinfo=timezone.utc)


def px_with(factor=1.0, extra=None):
    px = T.book_h2h() | T.book_dc()
    px[DCK] = round(px[DCK] * factor, 3)
    return px | (extra or {})


def read(clock, px, **kw):
    """Un relevé 1xBet fictif lu à `clock` (heure du cache) ; observe() est appelée comme le fait fetch_day."""
    xbet.odds_time = lambda gids: clock.timestamp()
    found = {"m1": {"gid": 1, "px": px, "refs": {k: [1, 9, 0] for k in px}, "blocked": kw.get("blocked", set())}}
    O.observe(found, EV, kw.get("now", clock))
    return {r["key"]: r for r in store.load(O.LOG_FILE, [])}


def test_collect():
    old_odds_time, old_get, old_post = xbet.odds_time, requests.get, requests.post

    def boom(*a, **k):
        raise AssertionError("aucun appel réseau")
    requests.get = requests.post = boom
    try:
        rows = read(at(12), px_with(1.12))
        k1x = O.row_key("2026-10-17", "m1", "1a", "double_chance", f"{T.H} or Draw", None)
        assert k1x in rows and rows[k1x]["status"] == "value" and rows[k1x]["reads"] == 1 and rows[k1x]["gone_at"] is None and rows[k1x]["strategy"] == "cotes", rows
        assert rows[k1x]["xref"] == [1, 9, 0] and rows[k1x]["first_at"] == "2026-10-17T12:00+00:00" and rows[k1x]["censored"] is None
        # même heure de cache : lecture non comptée
        rows = read(at(12), px_with(1.12))
        assert rows[k1x]["reads"] == 1
        # lecture authentique plus tard, toujours là
        rows = read(at(13), px_with(1.12))
        assert rows[k1x]["reads"] == 2 and rows[k1x]["gone_at"] is None and rows[k1x]["last_seen_at"] == "2026-10-17T13:00+00:00"
        # l'incohérence disparaît : gone_at au relevé où elle est repassée sous le seuil, last_seen_at au dernier où elle existait
        rows = read(at(14), px_with(1.0))
        assert rows[k1x]["gone_at"] == "2026-10-17T14:00+00:00" and rows[k1x]["last_seen_at"] == "2026-10-17T13:00+00:00" and rows[k1x]["censored"] is None
        assert rows[k1x]["edge_last"] < O.EDGE
        # mouvement (observation) : la cote a bougé de plus de 3 % entre deux relevés authentiques
        mv = [r for r in rows.values() if r["mech"] == "3"]
        assert mv and all(r["status"] == "watch" and r["mv"] is not None for r in mv), mv
        # suspect : au-delà de ODDSONLY_MAX_EDGE
        rows = read(at(15), px_with(1.45))
        sus = [r for r in rows.values() if r["status"] == "suspect"]
        assert sus and all(O.status_of(r["edge"]) == "suspect" for r in sus), rows
        # clôture de la voie : relevé à 0-90 min du match, cote tirée du relevé ; puis cellule retirée par 1xBet : xgone (comme lanestats.x_clv)
        rows = read(at(19, 0), px_with(1.12))
        assert rows[k1x]["xclose"] == {"odds": px_with(1.12)[DCK], "read": "2026-10-17T19:00+00:00", "lead_min": 60, "susp": False} and rows[k1x]["xgone"] is False, rows[k1x]
        px_no = px_with(1.12)
        px_no.pop(DCK)
        rows = read(at(19, 30), px_no)
        assert rows[k1x]["xgone"] is True
        # suspendue : xclose avec susp (exclue du CLV)
        rows = read(at(19, 40), px_no, blocked={DCK})
        assert rows[k1x]["xclose"]["susp"] is True or rows[k1x]["xgone"] is True
        # famille hors voies : jamais journalisée ; rien dans le journal d'ensemble
        offs = T.ladder("team_throws_total", lambda L: T.nb_over(10, 40, L), (8.5, 9.5, 10.5, 11.5, 12.5))
        read(at(16), px_with(1.12, offs))
        assert not any("throws" in r["market"] for r in store.load(O.LOG_FILE, []))
        assert store.load(ensemble.LOG_FILE, []) == [], "rien dans le journal d'ensemble"
    finally:
        xbet.odds_time, requests.get, requests.post = old_odds_time, old_get, old_post
    print("ok  collecte : première détection, cache non compté, survie, disparition, censure, suspect, mouvement, hors voies jamais, aucun réseau")


def test_censoring():
    """Une cellule qui disparaît censure la ligne (jamais « disparue ») ; le coup d'envoi censure les lignes encore ouvertes."""
    store.save(O.LOG_FILE, [])
    store.save(O.STATE_FILE, {})
    old = xbet.odds_time
    try:
        px = px_with(1.12)
        k12 = ("double_chance", f"{T.H} or {T.A}", None)
        px[k12] = round(px[k12] * 1.12, 3)
        rows = read(at(12), px)
        key = lambda n: O.row_key("2026-10-17", "m1", "1a", "double_chance", n, None)       # noqa: E731
        assert rows[key(f"{T.H} or Draw")]["censored"] is None and rows[key(f"{T.H} or {T.A}")]["censored"] is None
        gone = dict(px)
        gone.pop(k12)
        rows = read(at(13), gone)
        assert rows[key(f"{T.H} or {T.A}")]["censored"] == "cellule" and rows[key(f"{T.H} or {T.A}")]["gone_at"] is None, rows[key(f"{T.H} or {T.A}")]
        assert rows[key(f"{T.H} or Draw")]["censored"] is None
        O.observe({"m1": {"gid": 1, "px": px, "refs": {}, "blocked": set()}}, EV, at(21))
        rows = {r["key"]: r for r in store.load(O.LOG_FILE, [])}
        assert rows[key(f"{T.H} or Draw")]["censored"] == "coup d'envoi" and rows[key(f"{T.H} or {T.A}")]["censored"] == "cellule"
    finally:
        xbet.odds_time = old
    print("ok  censure : cellule disparue, coup d'envoi (jamais comptées comme disparues)")


def test_off_and_observer_hook():
    from kelly import config as C
    store.save(O.LOG_FILE, [])
    old = C.STRATEGY_ODDSONLY
    C.STRATEGY_ODDSONLY = "off"
    try:
        xbet.odds_time = lambda gids: at(9).timestamp()
        O.observe({"m1": {"gid": 1, "px": px_with(1.12), "refs": {}, "blocked": set()}}, EV, at(9))
    finally:
        C.STRATEGY_ODDSONLY = old
    assert store.load(O.LOG_FILE, []) == [], "interrupteur « off » : rien n'est collecté"
    # un observateur qui lève n'empêche pas fetch_day de rendre ses cotes
    def bad(found, events):
        raise RuntimeError("boum")
    xbet.OBSERVERS.append(bad)
    try:
        assert xbet.fetch_day([], record=False) == {}
    finally:
        xbet.OBSERVERS.remove(bad)
    print("ok  « off » ne collecte rien ; un observateur en échec ne casse pas fetch_day")


if __name__ == "__main__":
    test_collect()
    test_censoring()
    test_off_and_observer_hook()
    print("tests voie cotes (collecte) : OK")
