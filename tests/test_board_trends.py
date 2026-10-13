"""📌 Tableau du jour et 📈 Tendances (hors ligne). Tableau : naît quand un match d'un coupon commence dans l'heure, épinglé sans notification,
MODIFIÉ sur place quand quelque chose change (jamais quand rien ne bouge), coupons en jeu d'abord avec chaque pari, perdus et gagnés en une ligne, se
resserre s'il est trop long ; le lendemain : nouveau tableau, l'ancien désépinglé ; message effacé : un nouveau. Tendances : modèle de Kelly pour les
championnats football-data, profils FotMob ailleurs (jamais via une coupe d'Europe), classements par championnat et par équipe.
Lancer : python -m tests.test_board_trends"""
import contextlib
import os
import tempfile
from datetime import date, datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-board-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

from kelly import app, chat, exposure, store, telegram, trends, watch  # noqa: E402
from kelly.sources import fmmatch, footballdata as fd  # noqa: E402

KO = datetime(2030, 1, 1, 19, 0, tzinfo=timezone.utc)
DAY = str(KO.date())


def leg(i, point=0.5, ko=KO):
    return {"id": f"e{i}", "key": f"e{i}|totals||{point}|Over", "home": f"H{i}", "away": f"A{i}", "market": "totals", "name": "Over",
            "point": point, "commence": ko.isoformat(), "pick": f"Plus de {str(point).replace('.', ',')} buts", "prob": 0.7}


def ticket(what, legs, coupon=None, day=DAY):
    return {"what": what, "tid": f"{day}|{what}", "name": f"Combi {what}", "stake": 1000, "odds": 4.0, "legs": legs, "coupon": coupon}


GAMES = {i: {"id": i, "start": KO.isoformat(), "home": {"id": 100 + i, "name": f"H{i}"}, "away": {"id": 200 + i, "name": f"A{i}"}} for i in range(4)}


@contextlib.contextmanager
def tickets(by_day):
    old = exposure.tickets
    exposure.tickets = lambda day: by_day.get(str(day), [])
    try:
        yield
    finally:
        exposure.tickets = old


@contextlib.contextmanager
def fake_telegram(fail_edit=False):
    calls, n = [], [100]

    def api(method, **kw):
        calls.append((method, kw))
        if method == "sendMessage":
            n[0] += 1
            return {"message_id": n[0]}
        if method in ("editMessageText", "editMessageReplyMarkup") and fail_edit:
            raise telegram.TelegramError("Bad Request: message to edit not found", 400)
        return True
    old = (telegram.api, telegram.creds)
    telegram.api, telegram.creds = api, lambda: ("tok", "chat")
    try:
        yield calls
    finally:
        telegram.api, telegram.creds = old


def follow(minutes, status):
    return watch.follow(KO + timedelta(minutes=minutes), games=GAMES, live_for=lambda ids: status, stats_for=lambda g: {}, final_for=lambda g: None,
                        detail_for=lambda g: None)


def live(score, minute):
    return {"live": True, "score": score, "minute": minute, "period": 1 if minute <= 45 else 2}


def test_board():
    store.save(watch.FILE, {})
    later = KO + timedelta(hours=2)
    ts = {DAY: [ticket("vm1", [leg(0), leg(1)], coupon="AB12"), ticket("vm2", [leg(2), leg(3, ko=later)], coupon="CD34")]}
    with tickets(ts), fake_telegram() as calls:
        watch.board(KO - timedelta(hours=3))
        assert not calls, "aucun match dans l'heure : pas encore de tableau"
        watch.board(KO - timedelta(minutes=30))
        assert [c[0] for c in calls] == ["sendMessage", "pinChatMessage"] and calls[1][1]["disable_notification"] is True, calls
        text = calls[0][1]["text"]
        assert text.startswith("📌 <b>Tableau du jour</b> · mardi 1er janvier · mis à jour 18h30 UTC"), text
        assert "🎫 <b>Combi vm1</b> · 🎟️ AB12 · 0 gagné, 2 à venir · si tout passe 4 000 FCFA\n   🕘 H0 – A0 19h00 · Plus de 0,5 buts" in text, text
        assert "💰 En jeu : mise 2 000 FCFA · gains possibles 8 000 FCFA" in text, text
        calls.clear()
        watch.board(KO - timedelta(minutes=20))
        assert not calls, "rien n'a changé : pas de modification (seule l'heure aurait bougé)"
        follow(20, {i: live((0, 0), 20) for i in range(3)})
        follow(31, {0: live((1, 0), 31), 1: live((0, 0), 31), 2: live((0, 0), 31)})
        watch.board(KO + timedelta(minutes=31))
        assert [c[0] for c in calls] == ["editMessageText"] and calls[0][1]["message_id"] == 101, calls          # modifié sur place
        text = calls[0][1]["text"]
        assert "   ✅ H0 – A0 1-0 · Plus de 0,5 buts\n   ⏳ H1 – A1 0-0 (31e) · Plus de 0,5 buts" in text, text
        calls.clear()
        follow(115, {0: {"live": False, "ended": True, "score": (1, 0), "minute": 90}, 1: {"live": False, "ended": True, "score": (0, 0), "minute": 90},
                     2: {"live": False, "ended": True, "score": (2, 1), "minute": 90}})
        watch.board(KO + timedelta(minutes=115))
        text = calls[-1][1]["text"]
        assert "🎫 <b>Combi vm2</b> · 🎟️ CD34 · 1 gagné, 1 à venir" in text and "🎫 <b>Combi vm1</b> · 🎟️ AB12 · ❌ perdu (H1 – A1 0-0 · Plus de 0,5 buts)" in text, text
        assert text.index("Combi vm2") < text.index("Combi vm1"), "en jeu d'abord, puis gagnés, puis perdus"
    nxt = KO + timedelta(days=1)
    ts2 = ts | {str(nxt.date()): [ticket("vm1", [leg(5, ko=nxt)], day=str(nxt.date()))]}
    with tickets(ts2), fake_telegram() as calls:
        watch.board(nxt - timedelta(minutes=30))
        assert [c[0] for c in calls] == ["unpinChatMessage", "sendMessage", "pinChatMessage"] and calls[0][1]["message_id"] == 101, calls      # la veille désépinglée
    with tickets(ts2), fake_telegram(fail_edit=True) as calls:
        follow(24 * 60 + 10, {5: live((0, 0), 10)})
        d = store.load(watch.FILE, {})
        d["board"]["hash"] = "x"                                              # quelque chose a changé
        store.save(watch.FILE, d)
        watch.board(nxt + timedelta(minutes=10))
        assert [c[0] for c in calls][-2:] == ["sendMessage", "pinChatMessage"], calls     # message effacé : un nouveau tableau
    print("ok  📌 tableau du jour : né dans l'heure du 1er match, épinglé sans bruit, modifié sur place seulement quand ça change, en jeu d'abord ;"
          " le lendemain l'ancien désépinglé ; message effacé : un nouveau")


def test_board_compact():
    store.save(watch.FILE, {})
    big = {DAY: [ticket(f"vm{i}", [leg(10 * i + j) for j in range(15)]) for i in range(12)]}
    with tickets(big), fake_telegram() as calls:
        watch.board(KO - timedelta(minutes=10))
    text = calls[0][1]["text"]
    assert len(text) <= watch.BOARD_MAX and text.count("🎫") == 12 and "🕘" not in text, len(text)      # trop long : une ligne par coupon
    print("ok  📌 tableau trop long : il se resserre (une ligne par coupon), jamais tronqué")


def test_trends():
    today = date(2026, 10, 13)
    fd_teams = {f"T{i}": {"div": "E0", "last": "2026-10-04", "all": {"n": 8}} for i in range(12)} | {"Old": {"div": "E0", "last": "2025-05-01"}}
    model = {"stats": {"corners": {"m": 5.0, "h": 1.2, "t": {f"T{i}": [0.8 + i * 0.04, 1.2 - i * 0.03, 7.0, 8] for i in range(12)} | {"Old": [2, 2, 1, 9]}}}}
    fm_teams = {"Racing Cordoba": {"sk": ["soccer_xb_argentina_2"], "last": "2026-10-10", "all": {"n": 9, "corners_for": 6.0, "corners_against": 4.0}},
                "Manchester United": {"sk": ["soccer_epl", "soccer_uefa_champs_league"], "last": "2026-10-10",
                                      "all": {"n": 9, "corners_for": 7.2, "corners_against": 3.0}}}
    old = (fd.profiles, fmmatch.profiles)
    fd.profiles, fmmatch.profiles = (lambda: {"teams": fd_teams}), (lambda: {"teams": fm_teams})
    try:
        ts = trends.teams(today, model_for=lambda div: model if div == "E0" else None)
        assert set(ts) == {f"T{i}" for i in range(12)} | {"Racing Cordoba"}, set(ts)      # ni l'équipe ancienne, ni un club vu en coupe d'Europe
        assert ts["T0"]["corners"] == (round(5.0 * 0.8 * 1.1, 2), round(5.0 * 1.2 * 1.1, 2)) and ts["T0"]["sk"] == "soccer_epl"
        out = telegram.plain(trends.report(["corners", "premier"], data=ts))
        assert out.startswith("📈 Tendances · 🚩 Corners · Premier League\n") and "⬆️ En obtiennent le plus : T11 " in out and "⬇️ En obtiennent le moins : T0 " in out, out
        glob = telegram.plain(trends.report([], data=ts))
        assert "Premier League" in glob and "T11" in glob and "Tendances · 🚩 Corners" in glob, glob
        assert "Aucun championnat" in telegram.plain(trends.report(["corners", "zzz"], data=ts))
        assert chat.route("📈 Tendances")[0] == "/tendances" and chat.route("tendances tirs premier")[0] == "/tendances tirs premier"
        assert ["📈 Tendances" in row for row in chat.MENU].count(True) == 1 and "tendances" in str(app.HELP)
    finally:
        fd.profiles, fmmatch.profiles = old
    print("ok  📈 tendances : modèle de Kelly (football-data), FotMob ailleurs (jamais via une coupe d'Europe), par championnat et par équipe ; bouton du menu")


def main():
    test_board()
    test_board_compact()
    test_trends()
    print("tests tableau du jour et tendances : OK")


if __name__ == "__main__":
    main()
