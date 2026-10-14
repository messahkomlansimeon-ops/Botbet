"""Suivi des tickets pendant les matchs, opportunités live et heures calmes (kelly/watch.py, kelly/quiet.py) ; « ⚠️ en danger » seulement pour les
combinés pas encore joués (kelly/xalerts.py). Hors ligne (365scores, 1xBet et Telegram simulés). Lancer : python -m tests.test_watch"""
from datetime import datetime, timedelta, timezone

from tests import test_combi_track as T     # noqa: I001 - fixe DATA_DIR et les faux réseaux avant d'importer kelly
from tests import test_combi_kinds as K     # noqa: E402
from kelly import config as C, played, quiet, store, telegram, watch  # noqa: E402

DAY = T.DAY
KO = datetime.fromisoformat(T.KO.replace("Z", "+00:00"))         # 2030-01-01 19:00 UTC


def capture():
    sent = []
    old = telegram.send
    telegram.send = lambda text, *a, **k: sent.append(str(text)) or True
    return sent, lambda: setattr(telegram, "send", old)


def test_quiet():
    C.QUIET_HOURS = "23-7"
    assert quiet.is_quiet(KO.replace(hour=23)) and quiet.is_quiet(KO.replace(hour=3)) and not quiet.is_quiet(KO.replace(hour=7))
    assert not quiet.is_quiet(KO.replace(hour=15))
    store.save(quiet.FILE, [])
    sent, undo = capture()
    try:
        quiet.send(telegram.Html("<b>💰 alerte</b>"), KO.replace(hour=2))
        quiet.send("📈 deux", KO.replace(hour=3))
        assert sent == [] and len(store.load(quiet.FILE, [])) == 2, "la nuit : gardées"
        assert quiet.flush(KO.replace(hour=5)) == 0, "encore la nuit"
        assert quiet.flush(KO.replace(hour=7)) == 2 and sent[0].startswith("🌙 2 alertes de la nuit") and sent[1] == "<b>💰 alerte</b>"
        quiet.send("tout de suite", KO.replace(hour=15))
        assert sent[-1] == "tout de suite" and store.load(quiet.FILE, []) == []
        C.QUIET_HOURS = "off"
        assert not quiet.is_quiet(KO.replace(hour=2))
    finally:
        undo()
        C.QUIET_HOURS = "23-7"
    print("ok  heures calmes : alertes de la nuit gardées, envoyées au réveil d'un coup")


def test_states():
    leg = {"market": "totals", "name": "Under", "point": 2.5, "home": "A", "away": "B"}
    assert watch.state(leg, 1, 0, {}, False) == ("live", "1-0") and watch.margin(leg, 1, 0) == "encore 1 but de marge"
    assert watch.state(leg, 2, 1, {}, False)[0] == "loss" and watch.state(leg, 1, 1, {}, True)[0] == "win"
    over = leg | {"name": "Over"}
    assert watch.state(over, 2, 1, {}, False)[0] == "win" and watch.margin(over, 1, 0) == "il manque 2 buts"
    assert watch.state({"market": "btts", "name": "Yes", "home": "A", "away": "B"}, 1, 1, {}, False)[0] == "win"
    assert watch.state({"market": "double_chance", "name": "A or Draw", "home": "A", "away": "B"}, 0, 0, {}, True)[0] == "win"
    assert watch.state({"market": "h2h", "name": "B", "home": "A", "away": "B"}, 2, 0, {}, False)[0] == "live", "1N2 : décidé à la fin seulement"
    shots = {"market": "team_shots_away", "name": "Under", "point": 12.5, "home": "A", "away": "B"}
    assert watch.state(shots, 0, 0, {"shots": (5, 13)}, False) == ("loss", "13 tirs")
    assert watch.state({"market": "alternate_totals_corners", "name": "Over", "point": 9.5}, 0, 0, {"corners": (6, 4)}, False)[0] == "win"
    assert watch.state(shots, 0, 0, {}, False) == (None, ""), "pas de stats : rien dit"
    assert watch.state(shots | {"point": 0.5}, 0, 0, {"shots": (0, 1)}, True) == ("loss", "1 tir"), "singulier"
    g = {"statusGroup": 4, "statusText": "Suspended", "gameTime": 25.0, "homeCompetitor": {"score": 0.0}, "awayCompetitor": {"score": 0.0}}
    st = watch._status(g)
    assert {k: st[k] for k in ("live", "paused", "ended", "score", "minute")} == {"live": False, "paused": True, "ended": False, "score": (0, 0),
                                                                                  "minute": 25}, "suspendu : pas fini"
    assert watch._status(g | {"statusText": "Ended", "gameTime": 90.0})["ended"] and watch._status(g | {"statusGroup": 3})["live"]
    print("ok  états des paris : buts, les 2 marquent, 1N2 / double chance à la fin, stats en direct")


def test_more_markets():
    """Corners d'une équipe et cartons jaunes suivis en direct (avant : jamais, et un ticket qui en avait ne pouvait pas être « 🏆 gagné ») ; après un but,
    tous les paris de buts disent où ils en sont (avant : seulement plus / moins de buts) ; cartons jaunes lus à part des rouges."""
    tc = {"market": "team_corners_home", "name": "Over", "point": 4.5, "home": "A", "away": "B"}
    assert watch.need(tc) == ("corners", "home") and watch.state(tc, 0, 0, {"corners": (5, 1)}, False) == ("win", "5 corners")
    assert watch.state(tc | {"market": "team_corners_away"}, 0, 0, {"corners": (5, 1)}, True) == ("loss", "1 corner")
    yl = {"market": "team_yellow_total", "name": "Under", "point": 3.5, "home": "A", "away": "B"}
    assert watch.need(yl) == ("yellow", "total") and watch.state(yl, 0, 0, {"yellow": (2, 2), "cards": (3, 2)}, False) == ("loss", "4 cartons jaunes")
    h2h = {"market": "h2h", "name": "A", "home": "A", "away": "B"}
    assert watch.margin(h2h, 1, 0) == "pour toi pour l'instant" and watch.margin(h2h, 0, 1) == "pas pour toi pour l'instant"
    assert watch.margin({"market": "double_chance", "name": "Draw or B", "home": "A", "away": "B"}, 1, 1) == "pour toi pour l'instant"
    assert watch.margin({"market": "btts", "name": "Yes", "home": "A", "away": "B"}, 1, 0) == "il manque un but de B"
    assert watch.margin({"market": "team_totals_home", "name": "Over", "point": 1.5, "home": "A", "away": "B"}, 1, 3) == "il manque 1 but"
    from kelly.sources import http, s365
    payload = {"statistics": [{"name": "Yellow Cards", "competitorId": 1, "value": "2"}, {"name": "Red Cards", "competitorId": 1, "value": "1"},
                              {"name": "Yellow Cards", "competitorId": 2, "value": "3"}, {"name": "Corners", "competitorId": 2, "value": "4"}]}
    old = http.fetch, s365.read_json
    http.fetch, s365.read_json = (lambda *a, **k: http.Result("u", b"{}", 200)), (lambda res: payload)      # réponse fraîche (une vieille copie : rien)
    try:
        st = watch._stats(s365, {"id": 9, "home": {"id": 1}, "away": {"id": 2}})
    finally:
        http.fetch, s365.read_json = old
    assert st["yellow"] == (2, 3) and st["cards"] == (3, 3), st
    print("ok  corners d'une équipe et jaunes suivis en direct ; après un but, tous les paris de buts disent où ils en sont")


def setup_tickets():
    K.setup()
    store.save(watch.FILE, {})
    legs = [T.prono(0, pick="Moins de 3,5 buts"), T.prono(1, pick="Moins de 3,5 buts"), K.fouls(0, prob=0.65)]
    cid = T.keep_custom(legs, stake=100, cur="FCFA")
    row = next(r for r in __import__("kelly").fair.load_combos() if str(r["tier"]) == cid)
    played.command([f"v{cid}", str(row["min_odds"]).replace(".", ","), "100"], DAY)
    games = {i: {"id": i, "start": T.KO, "home": {"id": 100 + i, "name": h}, "away": {"id": 200 + i, "name": a}}
             for i, (h, a) in enumerate((("H0", "A0"), ("H1", "A1"), ("FH0", "FA0")))}
    return f"v{cid}", games


def test_follow():
    what, games = setup_tickets()
    live = {0: {"live": True, "score": (1, 0), "minute": 30}, 1: {"live": True, "score": (0, 0), "minute": 30},
            2: {"live": True, "score": (0, 0), "minute": 30}}
    kw = {"games": games, "live_for": lambda ids: live, "stats_for": lambda g: {"fouls": (10, 8)}, "final_for": lambda g: None}
    msg = watch.follow(KO + timedelta(minutes=30), **kw)
    assert msg is None, "premier relevé : rien de décidé, aucun but vu avant (pas de message)"
    live[0] = {"live": True, "score": (2, 0), "minute": 55}
    live[2] = {"live": True, "score": (0, 0), "minute": 55}
    kw["stats_for"] = lambda g: {"fouls": (14, 10)}
    msg = str(watch.follow(KO + timedelta(minutes=60), **kw))
    assert "⚽ H0 2-0 A0 (55e)\n   Moins de 3,5 buts : encore 1 but de marge" in msg, msg          # un bloc par match : titre, puis les paris
    assert "✅ FH0 – FA0 : Plus de 22,5 fautes au total (24 fautes, 0-0)" in msg and "🎫 <b>" in msg and "</b> · 🎟️ C1 · 📊 1/3 gagnés, 2 en cours" in msg, msg
    assert "✅ FH0 – FA0 : Plus de 22,5 fautes au total (24 fautes, 0-0) · 🎟️ C1" in msg, "chaque pari dit de quel coupon il vient"
    assert msg.count("FH0 – FA0") == 1, "un pari dans plusieurs tickets : dit une fois"
    assert watch.follow(KO + timedelta(minutes=62), **kw) is None, "rien de neuf : pas de message"
    live[0] = {"live": True, "score": (3, 1), "minute": 80}
    msg = str(watch.follow(KO + timedelta(minutes=85), **kw))
    assert "❌ H0 – A0 : Moins de 3,5 buts (3-1)" in msg and "❌ Ticket perdu." in msg, msg
    live[1] = {"live": True, "score": (5, 0), "minute": 88}
    assert watch.follow(KO + timedelta(minutes=90), **kw) is None, "défaut : plus aucune alerte sur un match qui n'appartient qu'à un coupon perdu"
    old, C.WATCH_AFTER_LOSS = C.WATCH_AFTER_LOSS, True
    try:
        what, games = setup_tickets()
        live2 = {0: {"live": True, "score": (4, 0), "minute": 60}, 1: {"live": True, "score": (0, 0), "minute": 60}, 2: {"live": True, "score": (0, 0), "minute": 60}}
        kw3 = kw | {"games": games, "live_for": lambda ids: live2}
        assert "❌ Ticket perdu." in str(watch.follow(KO + timedelta(minutes=60), **kw3))
        live2[1] = {"live": True, "score": (5, 0), "minute": 88}
        msg = str(watch.follow(KO + timedelta(minutes=90), **kw3))        # WATCH_AFTER_LOSS=1 : les autres matchs du ticket perdu restent suivis, sans répéter « Ticket perdu »
        assert msg == "⚽ H1 5-0 A1 (88e)\n❌ H1 – A1 : Moins de 3,5 buts (5-0) · 🎟️ C1 (perdu)", msg
        assert watch.follow(KO + timedelta(minutes=92), **kw3) is None, "rien de neuf"
    finally:
        C.WATCH_AFTER_LOSS = old
    # match suspendu (groupe 4 chez 365scores, comme un match fini) : rien n'est tranché, même longtemps après le coup d'envoi
    what, games = setup_tickets()
    paused = {i: {"live": False, "paused": True, "score": (0, 0), "minute": 25} for i in range(3)}
    kw2 = kw | {"live_for": lambda ids: paused, "stats_for": lambda g: {"fouls": (1, 1)}}
    assert watch.follow(KO + timedelta(minutes=150), **kw2) is None, "suspendu : « moins de » pas gagné, « plus de » pas perdu"
    assert all(v.get("state") == "live" for v in store.load(watch.FILE, {})["legs"].values()), store.load(watch.FILE, {})["legs"]
    print("ok  suivi des tickets : ⚽ but avec la marge, ✅ pari décidé en cours de match, ❌ ticket perdu une seule fois")


def test_follow_won_after_end():
    what, games = setup_tickets()
    live = {}
    kw = {"games": games, "live_for": lambda ids: live, "stats_for": lambda g: {"fouls": (12, 12)},
          "final_for": lambda g: {0: (1, 1), 1: (0, 0), 2: (0, 0)}[g["id"]]}
    out = watch.follow(KO + timedelta(minutes=130), **kw)             # sortis de la liste en cours : matchs finis, scores par les résultats
    msg, won = str(out), str(out.more[0])
    assert msg.count("✅") == 3 and "Ticket GAGNÉ" not in msg and len(out.more) == 1, msg            # le coupon gagné : un message à part
    assert won.startswith("🎉🏆 <b>COUPON GAGNÉ !</b> 🏆🎉\n🎫 <b>Combi sur mesure (cote 5)</b> · 🎟️ C1\n✅ H0 – A0 · Moins de 3,5 buts (1-1)") and won.count("✅") == 3, won
    assert "💰 100 FCFA × 3,60 = <b>360 FCFA</b> encaissés (+260 FCFA)" in won, won
    assert out.more[0].markup["inline_keyboard"][0][0]["text"] == "🎫 C1" and out.more[0].markup["inline_keyboard"][0][0]["callback_data"].startswith("tk|")
    print("ok  match fini : score final retrouvé, 🎉 coupon gagné dans un message à part (paris, scores, gain), bouton du coupon")


def test_opportunity():
    from kelly import live as L
    from tests.test_live import PRED, game
    store.save(watch.FILE, {})
    now = KO.replace(hour=16)
    current = [{"statusGroup": 3, "gameTime": 50}] * 9
    raw = {"Value": [game(i, 50, home=f"T{i}", away=f"O{i}") for i in range(6)]}
    old = L.model
    L.model = lambda m, key: PRED
    try:
        msg = watch.opportunity(now, current=current, get=lambda u: raw)
        assert msg and "🔴 Opportunité live" in str(msg) and T.buttons(msg)[0] == "live|3", msg
        assert watch.opportunity(now + timedelta(minutes=30), current=current, get=lambda u: raw) is None, "trop tôt après la précédente"
        assert watch.opportunity(now + timedelta(hours=3), current=current[:3], get=lambda u: raw) is None, "pas assez de matchs en cours"
        assert watch.opportunity(KO.replace(hour=9), current=current, get=lambda u: raw) is None, "hors des heures"
    finally:
        L.model = old
    print("ok  opportunités live : contrôle léger, heures, écart entre deux, boutons de cote")


def test_danger_only_unplayed():
    from kelly import xalerts
    x = {"date": DAY, "played": {"odds": 5, "stake": 100}}
    leg = T.prono(0)
    class Gone:
        def get(self, c):
            return ("gone", None, None, "10:00")
    items = xalerts.danger_items([("Le combi", "combi", "vm1", x, [leg])], Gone(), DAY)
    assert items == [], "ticket déjà joué : pas d'alerte « en danger »"
    items = xalerts.danger_items([("Le combi", "combi", "vm1", {"date": DAY}, [leg])], Gone(), DAY)
    assert len(items) == 1 and "n'est plus jouable" in items[0]["text"], "pas encore joué : l'alerte part"
    print("ok  « ⚠️ en danger » seulement pour les combinés pas encore joués")


def main():
    test_quiet()
    test_states()
    test_more_markets()
    test_follow()
    test_follow_won_after_end()
    test_opportunity()
    test_danger_only_unplayed()
    print("tests suivi : OK")


if __name__ == "__main__":
    main()
