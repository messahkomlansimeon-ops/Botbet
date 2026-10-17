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
             line("g", "m4", ko_in(55), 41),                                         # pas encore (avance 55 >= 50) : le passage suivant (10 min plus tard) tombera à 45
             line("h", "m5", ko, 51, "totals"), line("i", "m5", ko, 52, "h2h"),       # buts : le match principal reste ouvert, jamais lus
             dict(line("j", None, ko, 61), id=None)])                                # sans id de match : ignorée
    rd = Reader()
    rep = CR.run(NOW, read=rd)
    assert rd.calls == [["11", "12"]], rd.calls                                      # ni les buts (totals, h2h), ni la ligne sans id                                      # un seul appel : les deux sous-matchs du match, jamais celui des touches
    assert rep["planned"] == 1 and rep["read"] == 1 and rep["ok"] == 2 and rep["bytes"] == 12800 and rep["requests"] == 2, rep
    rep = CR.run(NOW + timedelta(minutes=2), read=rd)
    assert len(rd.calls) == 1 and rep["planned"] == 0, "déjà lu : jamais deux fois"
    CR.run(NOW + timedelta(minutes=10), read=rd)                                     # m4 à 45 min : lu à son tour (45 min)
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
        xbet._events = lambda gid: (_ for _ in ()).throw(xbet.XbetError("réponse inattendue (pas un objet JSON)"))
        got, _, _ = xbet.read_closing(["11"])
        assert got == {"11": None}, "sous-match fermé : noté sans relevé, ne lève pas"
        xbet._events = lambda gid: (_ for _ in ()).throw(xbet.XbetError("ReadTimeout"))
        assert xbet.read_closing(["11"])[0] == {}, "panne : absent, relu plus tard"
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
        CR.run = lambda now=None, **kw: got.append(now)             # le passage ne donne pas son heure de début  # noqa
        watch._tickets = lambda now: None
        watch.board = lambda now: None
        watch.opportunity = lambda now: None
        watch._lock = lambda: L()
        watch.tick(NOW)
    finally:
        CR.run, watch._tickets, watch.board, watch.opportunity, watch._lock = keep
    assert got == [None], "le passage --watch lance le relevé de clôture, sans l'heure de son début (périmée après des minutes de tickets)"
    # section G : couverture ≤ 60 min et part du relevé de clôture
    ko = ko_in(60)
    rows = [line(f"k{i}", f"m{i}", ko, i, strategy="stats", xodds=2.0, xclose={"odds": 2.1, "lead_min": lead, "susp": False, **({"src": src} if src else {})})
            for i, (lead, src) in enumerate([(40, "close_read"), (55, "close_read"), (80, "cache"), (45, None), (200, "cache")])]
    out = "\n".join(A.section_g(rows))
    assert "Couverture ≤ 60 min : 3/5 = 60%" in out and "2/4 lignes (50%)" in out and "1 sans source" in out, out
    print("ok  crochet du passage --watch ; section G : couverture ≤ 60 min et part close_read")


def test_failures_and_retry():
    ko = ko_in(40)
    put(ens=[line("a", "m1", ko, 11), line("b", "m1", ko, 12, SOT), line("c", "m2", ko, 21)], sea=[line("d|saison", "m1", ko, 11)])      # m1 : deux sous-matchs, l'un partagé par deux journaux
    calls = []

    def flaky(gids, deadline=None):
        calls.append(list(gids))
        n = len(calls)
        if n == 1:
            return {}, (0, 0), "délai dépassé"                                        # panne totale (délai global du passage épuisé) : RIEN n'est enregistré
        if n == 2:
            return {"11": NOW.timestamp(), "21": None}, (100, 2), None               # 11 relevé, 21 fermé (réponse « plus de sous-match »), 12 en panne
        return {g: NOW.timestamp() for g in gids}, (100, 1), None
    CR.run(NOW, read=flaky)
    assert calls == [["11", "12", "21"]], calls                                        # 11 une seule fois malgré les deux journaux
    assert store.load(CR.FILE, {}).get("days") in (None, {}), "panne : rien d'enregistré, rien de compté dans le plafond"
    CR.run(NOW + timedelta(minutes=1), read=flaky)
    CR.run(NOW + timedelta(minutes=2), read=flaky)
    assert calls[2] == ["12"], calls                                                   # seul le sous-match resté en panne est relu ; 21 (fermé) et 11 (relevé) jamais
    CR.run(NOW + timedelta(minutes=3), read=flaky)
    assert len(calls) == 3, "tout a répondu : plus rien à lire"
    d = store.load(CR.FILE, {})["days"]["2026-10-17"]["done"]
    assert d["m1"].keys() == {"11", "12"} and d["m2"] == {"21": None}, d
    print("ok  panne = rien d'enregistré et relu ; sous-match fermé noté sans relevé ; seul le sous-match en panne est relu ; sous-match partagé lu une fois")


def test_boundaries_and_kelly_day():
    for lead, want in ((34.9, False), (35, True), (49.9, True), (50, False)):
        put(ens=[line("a", "m1", NOW + timedelta(minutes=lead), 11)])
        rd = Reader()
        CR.run(NOW, read=rd)
        assert bool(rd.calls) is want, (lead, rd.calls)
    # jour Kelly : un match de 04:10 UTC vu à 03:25 (jour Kelly précédent) est lu ; un match déjà commencé jamais
    now = datetime(2026, 10, 18, 3, 25, tzinfo=timezone.utc)
    put(ens=[line("a", "m1", datetime(2026, 10, 18, 4, 10, tzinfo=timezone.utc), 11), line("b", "m2", now - timedelta(minutes=5), 12)])
    rd = Reader(at=now.timestamp())
    CR.run(now, read=rd)
    assert rd.calls == [["11"]], rd.calls
    assert CR.kelly_day(datetime(2026, 10, 18, 3, 59, tzinfo=timezone.utc)) == "2026-10-17" and CR.kelly_day(datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)) == "2026-10-18"
    print("ok  bornes 35 incluse / 50 exclue ; jour Kelly (04:00 UTC) ; match de 04:10 vu à 03:25 lu ; match commencé ignoré")


def test_priority_hours_lines_and_purge():
    assert CR.PRIORITY_HOURS == (0, 1, 2, 3, 16, 19, 22, 23)
    base = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    ranks = [CR.rank_key((f"m{h}", {"ko": base.replace(hour=h), "lines": 1})) for h in (0, 3, 16, 19, 22, 23)]
    assert all(not r[0] for r in ranks), "chaque heure de la liste est prioritaire"
    assert all(CR.rank_key((f"m{h}", {"ko": base.replace(hour=h), "lines": 1}))[0] for h in (4, 14, 15, 17, 18, 20, 21)), "les autres non"
    a = CR.rank_key(("a", {"ko": base.replace(hour=14), "lines": 5}))
    b = CR.rank_key(("b", {"ko": base.replace(hour=14), "lines": 2}))
    assert a < b, "à tranche égale, le plus de lignes d'abord"
    # les lignes sont bien comptées (3 lignes contre 1) dans matches()
    ko = ko_in(40)
    put(ens=[line("x1", "mx", ko, 1), line("x2", "mx", ko, 1), line("x3", "mx", ko, 1), line("y1", "my", ko, 2)])
    assert CR.matches(NOW)["mx"]["lines"] == 3 and CR.matches(NOW)["my"]["lines"] == 1
    # registre purgé à KEEP_DAYS jours, ts non numériques ignorés par l'index
    days = {f"2026-09-{d:02d}": {"done": {}, "bytes": 0, "requests": 0} for d in range(1, 21)}
    store.save(CR.FILE, {"days": days, "starts": {"7": "oops"}})
    CR._record(NOW, {"mz": {"5": 1.0}}, (0, 0))
    assert len(store.load(CR.FILE, {})["days"]) == CR.KEEP_DAYS
    store.save(CR.FILE, {"days": {"2026-10-17": {"done": {"m": {"5": "texte", "6": 2.0}}}}})
    assert CR.index() == {"6": [2.0]}, CR.index()
    print("ok  tranches prioritaires une à une, plus de lignes d'abord, lignes comptées, registre purgé, index filtré")


def test_src_start_and_newer_cache():
    idx = {"11": [100.0], "start:11": [1000.0]}
    assert CR.src_of([11, 9, 0], 100.4, idx) == "close_read"
    assert CR.src_of([11, 9, 0], 1010.0, idx) == "close_read", "tombé dans le cache pendant la lecture, avant l'enregistrement"
    assert CR.src_of([11, 9, 0], 999.0, idx) == "close_read" and CR.src_of([11, 9, 0], 998.0, idx) == "cache"
    assert CR.src_of([11, 9, 0], 1000 + CR.SECONDS + 31, idx) == "cache", "relevé de cache PLUS récent que la lecture : pas la nôtre"
    assert CR.src_of([11, 9, 0], 500.0, idx) == "cache" and CR.src_of([12, 9, 0], 100.0, idx) == "cache" and CR.src_of(None, 1.0, idx) == "cache"
    print("ok  source : instant exact, pendant la lecture, relevé de cache plus récent ou plus ancien = « cache »")


def test_read_closing_ttl():
    from types import SimpleNamespace
    ttls, old = [], (xbet._Session, http.fetch, http._read)

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
        http.fetch = lambda url, source, ttl, headers=None, check_robots=True: ttls.append(ttl) or SimpleNamespace(ok=True, stale=False, error=None, content='{"eventGroups": []}')
        http._read = lambda key: ({"fetched_at": 55.0}, b"{}")
        got, _, _ = xbet.read_closing(["7"])
        assert got == {"7": 55.0} and ttls == [0], (got, ttls)                       # TTL 0 : toujours relu sur le réseau, jamais repris du cache
        # panne réseau : absent du résultat (relu plus tard) ; réponse vide : « fermé » (None)
        http.fetch = lambda *a, **k: SimpleNamespace(ok=False, stale=False, error="ReadTimeout", content="")
        assert xbet.read_closing(["7"])[0] == {}
        http.fetch = lambda *a, **k: SimpleNamespace(ok=True, stale=False, error=None, content="[]")
        http.invalidate = lambda *a, **k: None
        assert xbet.read_closing(["7"])[0] == {"7": None}
    finally:
        xbet._Session, http.fetch, http._read = old
    print("ok  read_closing : TTL 0, panne absente du résultat, sous-match fermé = None")


def main():
    test_failures_and_retry()
    test_boundaries_and_kelly_day()
    test_priority_hours_lines_and_purge()
    test_src_start_and_newer_cache()
    test_read_closing_ttl()
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
