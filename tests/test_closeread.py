"""Relevé de clôture des marchés de stats (kelly/closeread.py) sur des journaux et des lectures FICTIFS : fenêtre 35-60 min lue au passage où le suivant serait trop tard, une lecture par sous-match
même si plusieurs lignes le partagent, jamais deux fois, familles hors voies / réglées / sans référence ignorées, trois journaux, plafond du jour et priorité des tranches horaires (rang du JOUR),
silence au-delà de 85 % du mois, aucun observateur de fetch_day, source `close_read` marquée par mark_x_close (et conservée), relevé d'après le coup d'envoi jamais pris pour une clôture, archive avec
source (et anciennes lignes à 3 éléments), crochet du passage --watch. Hors ligne, aucune requête. Lancer : python -m tests.test_closeread"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-closeread-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-closeread-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)

from kelly import closeread as CR, config as C, cupstats, ensemble, seasonlane, store  # noqa: E402
from kelly.sources import http, xbet  # noqa: E402

NOW = datetime(2026, 10, 17, 18, 0, tzinfo=timezone.utc)
CORN, SOT, THROW = "team_corners_total", "team_sot_total", "team_throws_total"


def line(key, mid, ko, gid, market=CORN, **kw):
    return {"key": key, "id": mid, "market": market, "name": "Over", "point": 4.5, "commence": ko.isoformat(), "xref": [gid, 9, 4.5], "result": None} | kw


def put(ens=(), sea=(), cup=()):
    store.save(ensemble.LOG_FILE, list(ens))
    store.save(seasonlane.LOG, list(sea))
    store.save(cupstats.LOG, list(cup))
    store.save(CR.FILE, {})


class Reader:
    """Lecture fictive : enregistre les appels, rend l'instant `at` pour chaque sous-match."""

    def __init__(self, at=None, spent=(6400, 1)):
        self.calls, self.at, self.spent = [], at, spent

    def __call__(self, gids, deadline=None):
        self.calls.append(list(gids))
        return {g: (self.at if self.at is not None else NOW.timestamp()) for g in gids}, (self.spent[0] * len(gids), self.spent[1] * len(gids)), None


def ko_in(minutes, now=NOW):
    return now + timedelta(minutes=minutes)


def test_window_and_sharing():
    ko = ko_in(40)
    put(ens=[line("a", "m1", ko, 11), line("b", "m1", ko, 11), line("c", "m1", ko, 12, SOT),
             line("t", "m1", ko, 13, THROW),                                         # hors voie : jamais lue
             line("s", "m1", ko, 14, result="win"),                                  # réglée : ignorée
             dict(line("n", "m1", ko, 15), xref=None),                               # sans référence 1xBet
             line("e", "m2", ko_in(70), 21),                                         # trop loin
             line("f", "m3", ko_in(30), 31),                                         # 1xBet a fermé (< 35 min)
             line("g", "m4", ko_in(50), 41)])                                        # pas encore : le passage suivant (10 min plus tard) tombera à 40
    rd = Reader()
    rep = CR.run(NOW, read=rd)
    assert rd.calls == [["11", "12"]], rd.calls                                      # un seul appel : les deux sous-matchs du match, jamais celui des touches
    assert rep["planned"] == 1 and rep["read"] == 1 and rep["ok"] == 2 and rep["bytes"] == 12800 and rep["requests"] == 2, rep
    rep = CR.run(NOW + timedelta(minutes=2), read=rd)
    assert len(rd.calls) == 1 and rep["planned"] == 0, "déjà lu : jamais deux fois"
    CR.run(NOW + timedelta(minutes=10), read=rd)                                     # m4 à 40 min : lu à son tour
    assert rd.calls[-1] == ["41"], rd.calls
    d = store.load(CR.FILE, {})["days"]["2026-10-17"]
    assert set(d["done"]) == {"m1", "m4"} and d["requests"] == 3 and d["bytes"] == 19200, d
    print("ok  fenêtre 35-60 min lue au passage où le suivant serait trop tard ; un appel par sous-match ; touches, réglées, sans référence, trop loin / trop tard ignorées ; jamais deux fois")


def test_three_journals_and_switch():
    ko = ko_in(36)
    put(ens=[line("a", "m1", ko, 11)], sea=[line("b|saison", "m2", ko, 22)], cup=[line("c", "m3", ko, 33)])
    rd = Reader()
    CR.run(NOW, read=rd)
    assert sorted(rd.calls[0]) == ["11", "22", "33"], rd.calls
    put(ens=[line("a", "m1", ko, 11)])
    C.XCLOSE_READ = False
    try:
        rd2 = Reader()
        CR.run(NOW, read=rd2)
        assert rd2.calls == [], "interrupteur XCLOSE_READ éteint : rien"
    finally:
        C.XCLOSE_READ = True
    print("ok  trois journaux (ensemble, saison, coupes) ; interrupteur XCLOSE_READ")


def test_cap_and_priority():
    # 3 matchs du même jour Kelly : 19h (tranche prioritaire), 22h (prioritaire), 14h (non prioritaire, mais lu à ce passage). Plafond 2 : le non prioritaire n'est jamais lu.
    now = datetime(2026, 10, 17, 13, 20, tzinfo=timezone.utc)
    m14, m19, m22 = datetime(2026, 10, 17, 14, 0, tzinfo=timezone.utc), datetime(2026, 10, 17, 19, 0, tzinfo=timezone.utc), datetime(2026, 10, 17, 22, 0, tzinfo=timezone.utc)
    ens = [line("a", "m14", m14, 1), line("a2", "m14", m14, 1), line("a3", "m14", m14, 1), line("b", "m19", m19, 2), line("c", "m22", m22, 3)]
    put(ens=ens)
    old = C.XCLOSE_READS_DAY
    try:
        C.XCLOSE_READS_DAY = 2
        rd = Reader()
        CR.run(now, read=rd)                                                          # m14 est à 40 min ; il a le plus de lignes, mais sa tranche n'est pas prioritaire : rang 2 >= plafond 2
        assert rd.calls == [], rd.calls
        C.XCLOSE_READS_DAY = 3
        CR.run(now, read=rd)
        assert rd.calls == [["1"]], rd.calls                                          # une place de plus : il passe
        put(ens=ens)
        C.XCLOSE_READS_DAY = 2
        CR.run(m19 - timedelta(minutes=40), read=rd)                                  # m19 (prioritaire) à son tour : rang 0
        assert rd.calls[-1] == ["2"], rd.calls
        store.save(CR.FILE, {"days": {"2026-10-17": {"done": {"x": {"9": 1.0}, "y": {"8": 1.0}}, "bytes": 0, "requests": 0}}})
        n = len(rd.calls)
        CR.run(m22 - timedelta(minutes=40), read=rd)
        assert len(rd.calls) == n, "plafond du jour atteint : plus aucun match"
    finally:
        C.XCLOSE_READS_DAY = old
    print("ok  plafond par jour Kelly et priorité des tranches horaires jugée sur tous les matchs restants du jour")


def test_month_silence_and_no_observer():
    put(ens=[line("a", "m1", ko_in(40), 11)])
    old_usage, old_read = xbet.usage, xbet.read_closing
    calls = []
    try:
        xbet.usage = lambda today=None: {"month": "2026-10", "bytes": C.XBET_MONTH_MB * 1e6 * 0.9, "requests": 1, "days": {}}
        xbet.read_closing = lambda gids, deadline=None: calls.append(gids) or ({g: NOW.timestamp() for g in gids}, (6400, 1), None)
        rep = CR.run(NOW)
        assert rep["silenced"] and calls == [], (rep, calls)
        assert any("muet" in x and "85" in x for x in CR.report_lines("2026-10-17")), CR.report_lines("2026-10-17")
        xbet.usage = lambda today=None: {"month": "2026-10", "bytes": 1e6, "requests": 1, "days": {}}
        rep = CR.run(NOW)
        assert not rep["silenced"] and calls == [["11"]], (rep, calls)
        assert not any("muet" in x for x in CR.report_lines("2026-10-17")), "le relevé a repris : plus de mention"
    finally:
        xbet.usage, xbet.read_closing = old_usage, old_read
    # read_closing n'appelle jamais les observateurs de fetch_day (voie « cotes »)
    seen, old = [], (xbet._Session, xbet._events, http._read, xbet.OBSERVERS[:])

    class FakeSession:
        why, start = None, {"bytes": 0, "requests": 0}

        def full(self):
            return False

        def call(self, fn, *a):
            return fn(*a)

        def close(self, what):
            pass
    try:
        xbet._Session = FakeSession
        xbet._events = lambda gid: {}
        http._read = lambda key: ({"fetched_at": 1234.5}, b"{}")
        xbet.OBSERVERS[:] = [lambda out, events: seen.append(1)]
        got, spent, why = xbet.read_closing(["11", "12"])
        assert got == {"11": 1234.5, "12": 1234.5} and why is None and seen == [], (got, seen)
        xbet._events = lambda gid: (_ for _ in ()).throw(xbet.XbetError("réponse inattendue"))
        got, _, _ = xbet.read_closing(["11"])
        assert got == {"11": None}, "sous-match fermé : noté sans relevé, ne lève pas"
    finally:
        xbet._Session, xbet._events, http._read = old[0], old[1], old[2]
        xbet.OBSERVERS[:] = old[3]
    print("ok  silence au-delà de 85 % du mois (et mention), reprise ; read_closing sans observateur, sous-match fermé sans exception")


def test_src_marked_and_kept():
    ko = ko_in(60)
    row = line("a", "m1", ko, 11, xodds=2.0)
    put(ens=[row, line("b", "m2", ko, 12, xodds=2.0)])
    read_at = ko.timestamp() - 38 * 60
    store.save(CR.FILE, {"days": {"2026-10-17": {"done": {"m1": {"11": read_at}}, "bytes": 0, "requests": 0}}})
    old = xbet.cached_cell
    try:
        xbet.cached_cell = lambda ref: xbet.Cell(2.2 if ref[0] == 11 else 2.1, read_at if ref[0] == 11 else ko.timestamp() - 75 * 60, False)
        ensemble.mark_x_close(NOW)
        log = {r["key"]: r for r in store.load(ensemble.LOG_FILE, [])}
        assert log["a"]["xclose"]["src"] == "close_read" and log["a"]["xclose"]["lead_min"] == 38, log["a"]
        assert log["b"]["xclose"]["src"] == "cache" and log["b"]["xclose"]["lead_min"] == 75, log["b"]
        # même relevé relu plus tard : la source reste, rien n'est réécrit
        assert ensemble.mark_x_close(NOW + timedelta(minutes=30)) == 0
        assert {r["key"]: r for r in store.load(ensemble.LOG_FILE, [])}["a"]["xclose"]["src"] == "close_read"
        # après le coup d'envoi : le relevé de clôture (KO - 38) est encore pris ; un relevé d'APRÈS le coup d'envoi (suivi en direct, même cache) n'est jamais une clôture
        store.save(ensemble.LOG_FILE, [dict(line("c", "m3", ko, 12, xodds=2.0))])
        xbet.cached_cell = lambda ref: xbet.Cell(1.5, ko.timestamp() + 20 * 60, False)
        assert ensemble.mark_x_close(ko + timedelta(minutes=25)) == 0
        assert "xclose" not in store.load(ensemble.LOG_FILE, [])[0], "relevé en direct : pas une clôture"
        xbet.cached_cell = lambda ref: xbet.Cell(2.4, ko.timestamp() - 36 * 60, False)
        assert ensemble.mark_x_close(ko + timedelta(minutes=15)) == 1, "match déjà commencé : la clôture lue à KO - 36 est encore notée"
        assert store.load(ensemble.LOG_FILE, [])[0]["xclose"]["lead_min"] == 36
    finally:
        xbet.cached_cell = old
    print("ok  src close_read / cache marquée puis conservée ; relevé d'après le coup d'envoi ignoré ; clôture d'avant lue après le coup d'envoi")


def test_archive_roundtrip():
    r = line("a", "m1", ko_in(60), 11, xodds=2.0, xclose={"odds": 2.2, "lead_min": 38, "susp": False, "src": "close_read"}, strategy="stats", sport_key="soccer_epl")
    back = ensemble.unpack_archive_row("a", "2026-10-17", ensemble._pack(r))
    assert back["xclose"] == {"odds": 2.2, "lead_min": 38, "susp": False, "src": "close_read"}, back
    old = ensemble._pack(r)
    old[ensemble.ARCH_COLS.index("xclose")] = [2.2, 38, 0]                           # archive d'avant le 17/10 : trois éléments
    assert "src" not in ensemble.unpack_archive_row("a", "2026-10-17", old)["xclose"]
    r2 = dict(r, xclose={"odds": 2.2, "lead_min": 38, "susp": False})                # sans source
    assert "src" not in ensemble.unpack_archive_row("a", "2026-10-17", ensemble._pack(r2))["xclose"]
    print("ok  archive : source conservée, anciennes lignes à 3 éléments toujours lisibles")


def test_watch_hook_and_audit():
    from kelly import watch
    from tools import audit_clv as A
    got = []
    keep = (CR.run, watch._tickets, watch.board, watch.opportunity, watch._lock)

    class L:
        def close(self):
            pass
    try:
        CR.run = lambda now=None, **kw: got.append(now)
        watch._tickets = lambda now: None
        watch.board = lambda now: None
        watch.opportunity = lambda now: None
        watch._lock = lambda: L()
        watch.tick(NOW)
    finally:
        CR.run, watch._tickets, watch.board, watch.opportunity, watch._lock = keep
    assert got == [NOW], "le passage --watch lance le relevé de clôture"
    # section G : couverture ≤ 60 min et part du relevé de clôture
    ko = ko_in(60)
    rows = [line(f"k{i}", f"m{i}", ko, i, strategy="stats", xodds=2.0, xclose={"odds": 2.1, "lead_min": lead, "susp": False, **({"src": src} if src else {})})
            for i, (lead, src) in enumerate([(40, "close_read"), (55, "close_read"), (80, "cache"), (45, None), (200, "cache")])]
    out = "\n".join(A.section_g(rows))
    assert "Couverture ≤ 60 min : 3/5 = 60%" in out and "2/4 lignes (50%)" in out and "1 sans source" in out, out
    print("ok  crochet du passage --watch ; section G : couverture ≤ 60 min et part close_read")


def main():
    test_window_and_sharing()
    test_three_journals_and_switch()
    test_cap_and_priority()
    test_month_silence_and_no_observer()
    test_src_marked_and_kept()
    test_archive_roundtrip()
    test_watch_hook_and_audit()
    print("tests relevé de clôture : OK")


if __name__ == "__main__":
    main()
