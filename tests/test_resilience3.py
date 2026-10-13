"""Résilience, 3e envoi (hors ligne) : décalage Telegram gardé en mémoire (une commande jamais retraitée quand la base ne peut plus écrire) ; messages du
direct périmés après 2 h dans la file ; secours FotMob quand la liste du jour de 365scores est vide ; source bloquée à répétition : pause de plus en plus
longue (1xBet et 365scores jamais plus de 24 h) ; stats à 0 là où il y en a toujours = manquantes ; fiabilité et pronostics écrits ensemble ; réglage
du dimanche rattrapé. Lancer : python -m tests.test_resilience3"""
from tests import test_proxy_backup as P      # fixe DATA_DIR (dossier temporaire), tickets et matchs FotMob de test

import time  # noqa: E402
from datetime import date, datetime, timedelta, timezone  # noqa: E402

import termux_runner as R  # noqa: E402
from kelly import calib, config as C, fair, settle, store, telegram, watch  # noqa: E402
from kelly.sources import state  # noqa: E402

UTC = timezone.utc


def test_offset_in_memory():
    seen, offsets = [], []
    upd = [{"update_id": 41, "message": {"text": "bilan", "chat": {"id": "c"}}}]

    def api(method, **kw):
        if method == "getUpdates":
            offsets.append(kw["offset"])
            return [u for u in upd if u["update_id"] >= kw["offset"]]
        return {}
    old = (telegram.api, telegram.creds, store.save)
    telegram.api, telegram.creds = api, lambda: ("t", "c")

    def save(name, obj, compact=False):
        if name == "telegram.json":
            raise OSError("disque plein")
        return old[2](name, obj, compact)
    store.save = save
    try:
        telegram.poll(lambda text: seen.append(text))
        telegram.poll(lambda text: seen.append(text))
    finally:
        telegram.api, telegram.creds, store.save = old
    assert seen == ["bilan"] and offsets[-1] == 42, (seen, offsets)              # jamais retraitée, même sans pouvoir écrire le décalage
    print("ok  décalage Telegram gardé en mémoire : une commande n'est jamais retraitée quand la base refuse d'écrire (disque plein)")


def test_live_ttl():
    now = time.time()
    store.save(telegram.OUTBOX, [{"text": "⚽ But ! 63e", "html": False, "markup": None, "chat_id": None, "ts": now - 3 * 3600, "ttl": 7200},
                                 {"text": "📊 Résultats d'hier", "html": False, "markup": None, "chat_id": None, "ts": now - 3 * 3600}])
    sent = []
    old = telegram._deliver
    telegram._deliver = lambda text, html, markup, target, reply_to=None: sent.append(text) or {"message_id": 1}
    try:
        telegram._flush(store.load(telegram.OUTBOX, []), "c")
    finally:
        telegram._deliver = old
    assert len(sent) == 1 and sent[0].endswith("📊 Résultats d'hier"), sent        # le but de 3 h : abandonné ; le reste part
    msg = telegram.Html("x")
    msg.ttl = 60
    calls = []
    old_e = telegram._enqueue
    telegram._enqueue = lambda *a, **k: calls.append((a, k))
    old_d, old_c = telegram._deliver, telegram.creds
    telegram._deliver = lambda *a, **k: (_ for _ in ()).throw(telegram.TelegramError("réseau", 502))
    telegram.creds = lambda: ("t", "c")
    try:
        store.save(telegram.OUTBOX, [])
        telegram.send(msg)
    finally:
        telegram._enqueue, telegram._deliver, telegram.creds = old_e, old_d, old_c
    assert calls and calls[0][0][-1] == 60, calls
    print("ok  file d'attente : un message du direct (but, mi-temps) abandonné après 2 h, les autres gardés 36 h")


def test_backup_on_empty_list():
    store.save(watch.FILE, {})
    ts = [P.ticket("vm1", [P.leg(0)], coupon="AB12")]
    with P.tickets(ts):
        msg = watch.follow(P.KO + timedelta(minutes=30), games={}, live_for=lambda ids: {}, stats_for=lambda g: {}, final_for=lambda g: None,
                           detail_for=lambda g: None, fm_for=lambda: ({"fm90": P.fm(0, (1, 0), 30)}, ""))
    h = store.load(watch.FILE, {})["health"]
    assert h.get("backup") and "liste des matchs du jour de 365scores vide" in (h.get("why") or ""), h
    assert "introuvable" not in str(msg or ""), msg                            # sans liste du jour, jamais « match introuvable » à tort
    print("ok  liste du jour de 365scores vide alors que tes matchs ont commencé : secours FotMob (plus de « tout va bien » à tort)")


def test_block_escalates():
    store.save(state.FILE, {})
    t0 = state.now()

    def hours(name):
        return (datetime.fromisoformat(state.health(name)["blocked_until"]) - t0).total_seconds() / 3600
    state.block("fbref", "HTTP 403")
    assert 23.9 < hours("fbref") <= 24.1
    state.block("fbref", "HTTP 403")
    assert 47.9 < hours("fbref") <= 48.1
    for _ in range(6):
        state.block("fbref", "HTTP 403")
    assert hours("fbref") <= 24 * 7 + 0.1
    for _ in range(3):
        state.block("xbet", "HTTP 403")
    assert hours("xbet") <= 24.1                                                # 1xBet : jamais plus de 24 h
    store.save(state.FILE, {"fbref": {"blocks": 5}})
    state.record_run("fbref", 10, [])
    assert state._all()["fbref"]["blocks"] == 0                                 # une réussite remet le compte à zéro
    state.block("fbref", "HTTP 403")
    assert hours("fbref") <= 24.1
    store.save(state.FILE, {})
    print("ok  source bloquée à répétition : 24 h, 48 h, 96 h… au plus 7 jours (FBref) ; 1xBet et 365scores jamais plus de 24 h ; réussite = remise à zéro")


def test_zero_stats():
    corners = {"market": "alternate_totals_corners", "name": "Under", "point": 9.5}
    assert settle.zero_stats(corners, {"corners": 0}) and not settle.zero_stats(corners, {"corners": 7})
    assert not settle.zero_stats({"market": "alternate_totals_cards", "name": "Under", "point": 3.5}, {"cards": 0})     # 0 carton : possible
    shots = {"market": "team_shots_total", "name": "Over", "point": 20.5}
    assert settle.zero_stats(shots, {"side": {"home": {"shots": 0}, "away": {"shots": 0}}})
    assert not settle.zero_stats({"market": "team_shots_home", "name": "Over", "point": 9.5}, {"side": {"home": {"shots": 0}, "away": {"shots": 12}}})
    # pronostic suivi : pas réglé (rien d'appris de faux), réessayé ensuite
    now = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)
    row = {"key": "e1|alternate_totals_corners||9.5|Under", "id": "e1", "sport_key": "soccer_epl", "home": "A", "away": "B",
           "commence": "2030-01-01T19:00:00Z", "market": "alternate_totals_corners", "name": "Under", "point": 9.5, "prob": 0.6, "p0": 0.6,
           "date": "2030-01-01", "result": None}
    fdata = settle.FootballData()
    old = (settle.football_score, settle.match_stats, fdata.score)
    settle.football_score = lambda b: {"id": "e1", "completed": True, "scores": [{"name": "A", "score": "1"}, {"name": "B", "score": "0"}]}
    settle.match_stats = lambda b, fd, sf=None: {"corners": 0}
    try:
        import os
        os.environ["STATS_API_KEY"] = "x"
        assert not settle._settle_fair_row(row, now, fdata) and row["result"] is None
    finally:
        settle.football_score, settle.match_stats = old[0], old[1]
        os.environ.pop("STATS_API_KEY", None)
    print("ok  corners, tirs, fautes, touches du match à 0 = stats manquantes : pari non réglé, rien d'appris de faux (0 carton reste possible)")


def test_calib_and_fair_together():
    store.save(fair.FILE, [{"key": "e1|totals||2.5|Over", "id": "e1", "sport_key": "soccer_epl", "home": "A", "away": "B", "commence": "2030-01-01T19:00:00Z",
                            "market": "totals", "name": "Over", "point": 2.5, "prob": 0.6, "p0": 0.6, "date": "2030-01-01", "result": "win"}])
    store.save(calib.FILE, {})
    calib.reset()
    seen = []
    real = store.save_many
    store.save_many = lambda docs: seen.append(sorted(docs)) or real(docs)
    try:
        settle.settle_fair(datetime(2030, 1, 2, 12, 0, tzinfo=UTC))
    finally:
        store.save_many = real
    assert seen == [sorted([calib.FILE, fair.FILE])], seen
    assert store.load(calib.FILE, {})["goals"][0] == 1 and store.load(fair.FILE, [])[0]["calib"] is True
    settle.settle_fair(datetime(2030, 1, 2, 13, 0, tzinfo=UTC))
    assert store.load(calib.FILE, {})["goals"][0] == 1                         # jamais compté deux fois
    print("ok  fiabilité et pronostics « comptés » écrits d'un coup (un arrêt entre les deux ne compte plus deux fois)")


def test_tune_catchup():
    names = lambda now, st: [a[0] for a in R.due_tasks(now, st, 1e9)]      # noqa: E731
    wed = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
    st = R.new_state() | {"tune_day": "2026-10-04"}                          # dimanche manqué (le 11)
    assert "--tune" in names(wed, st) and "--tune" not in names(wed.replace(hour=15), st)
    st["tune_day"] = "2026-10-11"
    assert "--tune" not in names(wed, st)
    assert "--tune" in names(datetime(2026, 10, 18, 2, 30, tzinfo=UTC), st)     # le dimanche suivant, comme avant
    print("ok  réglage du dimanche manqué : rattrapé dans la semaine entre 9h et 14h UTC")


def main():
    test_offset_in_memory()
    test_live_ttl()
    test_backup_on_empty_list()
    test_block_escalates()
    test_zero_stats()
    test_calib_and_fair_together()
    test_tune_catchup()
    print("tests résilience (3) : OK")


if __name__ == "__main__":
    main()
