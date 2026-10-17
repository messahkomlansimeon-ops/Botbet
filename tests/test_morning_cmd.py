"""Commande Telegram « matin » (kelly/morningcmd.py) : relance si le matin n'est pas fait, réponse « déjà fait à HHhMM, écris matin force » sinon, « matin force » le refait, une seule demande à la
fois, verrou « daily » contre deux lancements simultanés (bot.py --daily), le runner prend la demande une fois (avec --force si demandé) ; le mot « matin » est reconnu par le chat. Hors ligne.
Lancer : python -m tests.test_morning_cmd"""
import contextlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-morningcmd-")
os.environ["QUIET_HOURS"] = "off"
for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "XBET_PROXY", "S365_PROXY", "XDG_RUNTIME_DIR"):
    os.environ.pop(k, None)

from kelly import envfile  # noqa: E402

envfile.load = lambda *a, **k: []
import bot  # noqa: E402
import termux_runner as R  # noqa: E402
from kelly import app, chat, morningcmd as MC, passlock, store  # noqa: E402

NOW = datetime(2026, 10, 18, 9, 30, tzinfo=timezone.utc)


def reset(done_day=None, at="2026-10-18T04:12:00+00:00"):
    for f in (MC.REQUEST,):
        with contextlib.suppress(OSError):
            os.remove(store.path(f))
    store.save(app.DAILY_FILE, {"day": done_day, "at": at} if done_day else {})


def test_replies():
    reset()
    r = MC.handle([], NOW)
    assert "Je lance le matin" in r and MC.read_request() == {"at": MC.read_request()["at"], "force": False}, r
    assert "déjà demandé" in MC.handle([], NOW) and MC.read_request()["force"] is False, "une seule demande à la fois"
    assert "je le passe en « force »" in MC.handle(["force"], NOW) and MC.read_request()["force"] is True, "« matin force » promeut la demande en attente"
    assert "déjà demandé" in MC.handle([], NOW) and "(force)" in MC.handle([], NOW)
    reset()
    MC.handle([], NOW)
    assert "je le passe en « force »" in MC.handle(["forcé"], NOW), "« matin forcé » est lu comme force"
    reset("2026-10-18")
    r = MC.handle([], NOW)
    assert r == "Le matin est déjà fait à 04h12, écris « matin force » pour le refaire." and MC.read_request() is None, r
    r = MC.handle(["force"], NOW)
    assert "Je refais le matin (force)" in r and "pas de doublon" in r and "joués ou qui sont réglés restent tels quels" in r and MC.read_request()["force"] is True, r
    reset("2026-10-17")                                   # fait HIER (09h30 UTC, après DAY_END_HOUR) : le matin du jour n'est pas fait -> relance
    assert "Je lance le matin" in MC.handle([], NOW) and MC.read_request()["force"] is False
    reset("2026-10-17")                                   # 02h00 UTC : le jour Kelly d'hier court encore et est fait -> « déjà fait »
    early = datetime(2026, 10, 18, 2, 0, tzinfo=timezone.utc)
    r = MC.handle([], early)
    assert "Le matin du 2026-10-17 (fait à 04h12 le 18/10)" in r and "couvre encore le jour en cours" in r and "04h00 UTC" in r and MC.read_request() is None, r
    r = MC.handle(["force"], early)
    assert "n'est pas proposé avant cette heure" in r and MC.read_request() is None, "avant 04h UTC la veille faite : pas de force (il ferait sauter le vrai matin)"
    # force sur une demande normale en attente : la demande passe en force, jamais deux fichiers
    reset()
    MC.write_request(False)
    assert MC.write_request(True) is True and MC.read_request()["force"] is True and MC.write_request(False) is True, "force l'emporte"
    print("ok  réponses : lance si pas fait, « déjà fait à 04h12 » sinon, force refait, une seule demande, jour Kelly")


def test_lock_and_running():
    reset()
    with passlock.hold("daily") as got:
        assert got
        with passlock.hold("daily") as second:
            assert second is False, "second lancement simultané refusé"
        r = MC.handle([], NOW)
        assert "tourne déjà" in r and MC.read_request() is None, r
    assert passlock.busy("daily") is False
    # bot.py --daily : le verrou tenu -> sortie 0 sans rien lancer
    ran = []
    real = app.daily_entry
    app.daily_entry = lambda sample=False, force=False: ran.append(1) or 0
    try:
        with passlock.hold("daily"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                try:
                    bot.main(["--daily"])
                except SystemExit as e:
                    assert e.code == bot.BUSY_RC == R.DAILY_BUSY_RC == 74, "occupé : un code à part, JAMAIS 0 (le runner compterait le matin réussi)"
            assert not ran and "un autre passage du matin tourne déjà" in err.getvalue()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                bot.main(["--daily", "--force"])
            except SystemExit as e:
                assert e.code == 0
        assert ran == [1], "verrou libre : le matin tourne"
        assert passlock.busy("daily") is False, "verrou rendu à la sortie"
    finally:
        app.daily_entry = real
    print("ok  verrou « daily » : second lancement refusé, bot.py --daily se retire, verrou rendu")


def test_runner_busy_rc_not_counted():
    st = R.new_state()
    now = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
    R.mark_done(st, ["--daily"], now, 1000.0, R.DAILY_BUSY_RC)
    assert st["daily_ok_day"] == "" and st["daily_tries"] == 0 and st["daily_try_day"] == "2026-10-18" and st["daily_try_at"] == 1000.0, st
    due = R.due_tasks(now + timedelta(seconds=R.DAILY_RETRY_GAP + 1), st, 1000.0 + R.DAILY_RETRY_GAP + 1)
    assert ["--daily"] in due, "re-regardé plus tard"
    R.mark_done(st, ["--daily"], now, 2000.0, 1)
    assert st["daily_tries"] == 1 and st["daily_ok_day"] == ""
    R.mark_done(st, ["--daily"], now, 3000.0, 0)
    assert st["daily_ok_day"] == "2026-10-18"
    print("ok  runner : un matin ignoré parce qu'un autre tourne (code 74) n'est ni réussi ni compté comme essai")


def test_runner_takes_request_once():
    reset()
    assert R.take_daily_request() is None
    MC.write_request(False)
    assert R.take_daily_request() == {"force": False} and R.take_daily_request() is None, "prise une seule fois, fichier supprimé"
    MC.write_request(True)
    assert R.take_daily_request() == {"force": True}
    p = store.path(MC.REQUEST)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"at": (datetime.now(timezone.utc) - timedelta(minutes=50)).isoformat(), "force": True}, f)
    assert R.take_daily_request() is None and not os.path.exists(p), "demande vieille de 50 min : ignorée"
    with open(p, "w", encoding="utf-8") as f:
        f.write("{pas du json")
    assert R.take_daily_request() is None
    # la boucle : la demande lance --daily (--force) avant les autres passages, et un --daily du plan n'est pas doublé
    logs = tempfile.mkdtemp(prefix="kelly-mc-")
    real_logs = R.LOGS
    R.LOGS = logs
    ran = []
    r = R.Runner(serve_cmd=[sys.executable, "-c", "import time; time.sleep(30)"], tick=0.1, state=R.new_state(), watch_every=999,
                 plan=lambda now, s, e: [["--daily"], ["--track", "--own-lock"]], watch_thread=True)
    r.run = lambda args, **k: ran.append(args) or 0
    try:
        MC.write_request(True)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            r._pass()
        assert ran == [["--daily", "--force"], ["--track", "--own-lock"]], ran
        ran.clear()
        MC.write_request(False)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            r._pass()
        assert ran == [["--daily"], ["--track", "--own-lock"]], ran
        ran.clear()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            r._pass()
        assert ran == [["--daily"], ["--track", "--own-lock"]], "sans demande : le plan comme avant"
    finally:
        r.shutdown()
        R.LOGS = real_logs
    print("ok  runner : la demande est prise une fois (force transmis), périmée ou illisible ignorée, pas de matin en double")


def test_chat_word():
    assert chat.WORDS_ARGS["matin"] == "/matin"
    assert chat._command_words(["matin"]) == "/matin" and chat._command_words(["Matin", "force"]) == "/matin force" and chat.route("matin force") == ("/matin force", None)
    print("ok  mot « matin » reconnu")


def main():
    test_replies()
    test_lock_and_running()
    test_runner_busy_rc_not_counted()
    test_runner_takes_request_once()
    test_chat_word()
    print("tests commande matin : OK")


if __name__ == "__main__":
    main()
