"""Combinés LIVE (kelly/live.py) : lecture d'un match live 1xBet (format LiveFeed), probabilités recalculées avec le score, la minute et les cartons
rouges, garde-fous (fenêtre de minutes, championnats connus, écart avec 1xBet), combiné et boutons, journal et apprentissage À PART par tranche,
sélecteur Telegram. Hors ligne (1xBet, modèle et coupon simulés). Lancer : python -m tests.test_live"""
import os
import tempfile
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-live-")
os.environ.pop("TELEGRAM_TOKEN", None)

from kelly import app, calib, chat, custom, live, store  # noqa: E402
from kelly.sources import xbet  # noqa: E402

NOW = datetime(2026, 10, 10, 15, 30, tzinfo=timezone.utc)
DAY = "2026-10-10"
PRED = {"pred": {"lh": 1.6, "la": 0.9, "q": 1.0, "n": [60, 60]}}
# Cotes d'un match à 1-0 (63e) où 1xBet paie ~3 % de marge et voit Arsenal / « moins de 2,5 » à ~65 % quand Kelly les voit à ~80 % : l'ancienne grille
# (cotes quasi justes d'après Kelly) ne laisse plus rien passer, l'ancrage sur 1xBet et l'espérance de +2 % exigent un vrai écart de prix.
VALUE = [{"T": 1, "C": 1.49}, {"T": 2, "C": 4.41}, {"T": 3, "C": 7.4}, {"T": 4, "C": 1.12}, {"T": 5, "C": 1.245}, {"T": 6, "C": 2.77},
         {"T": 9, "P": 1.5, "C": 1.77}, {"T": 10, "P": 1.5, "C": 2.16}, {"T": 9, "P": 2.5, "C": 2.77}, {"T": 10, "P": 2.5, "C": 1.49},
         {"T": 180, "C": 3.03}, {"T": 181, "C": 1.43}]


def game(gid, minute, score=(1, 0), league="England. Premier League", home="Arsenal", away="Leeds", reds=(0, 0), period=None, odds=None):
    """Un match du flux LiveFeed de 1xBet (champs utilisés par live.parse)."""
    cells = odds if odds is not None else VALUE
    return {"I": gid, "LE": league, "O1E": home, "O2E": away, "S": int(NOW.timestamp()) - minute * 60 - 900,
            "SC": {"TS": minute * 60, "CP": period or (1 if minute < 45 else 2), "FS": {"S1": score[0], "S2": score[1]},
                   "ST": [{"Key": 0, "Value": [{"ID": 71, "S1": str(reds[0]), "S2": str(reds[1]), "N": "Red card"}]}]},
            "E": cells}


def test_basics():
    assert [live.tranche(m) for m in (29, 30, 44, 45, 60, 74, 75, 95)] == [None, "30-45", "30-45", "45-60", "60-75", "60-75", "75-90+", "75-90+"]
    assert abs(live.remaining(0, 1) - 1.0) < 1e-9 and abs(live.remaining(45, 2) - 0.55) < 1e-9 and live.remaining(94, 2) == 0
    assert 0 < live.remaining(80, 2) < 0.2
    m = live.parse(game(7, 63, score=(1, 0), reds=(0, 1)))
    assert m["minute"] == 63 and m["period"] == 2 and m["score"] == (1, 0) and m["reds"] == (0, 1) and m["home"] == "Arsenal"
    assert m["odds"][("totals", "Under", 2.5)] == (1.49, 10, 2.5) and m["odds"][("double_chance", "Arsenal or Draw", None)] == (1.12, 4, 0)
    assert ("h2h", "Draw", None) in m["odds"] and ("btts", "Yes", None) in m["odds"]
    assert live.parse({"SC": {}, "O1E": "A", "O2E": "B"}) is None, "sans minute : ignoré"
    assert live.league_key("England. Premier League") == "soccer_epl" and live.league_key("Czech Republic. 4 Liga") is None
    print("ok  tranches, temps restant, lecture d'un match live 1xBet, championnats connus")


def test_probs():
    p = live.probs(live.parse(game(1, 85, score=(0, 0))), PRED["pred"])
    assert p[("totals", "Under", 1.5)] > 0.9 and p[("h2h", "Draw", None)] > 0.6, p
    p2 = live.probs(live.parse(game(1, 85, score=(2, 0))), PRED["pred"])
    assert p2[("totals", "Over", 1.5)] == 1.0 and p2[("h2h", "Arsenal", None)] > 0.95
    early = live.probs(live.parse(game(1, 31, score=(0, 0))), PRED["pred"])[("totals", "Over", 1.5)]
    late = live.probs(live.parse(game(1, 75, score=(0, 0))), PRED["pred"])[("totals", "Over", 1.5)]
    assert early > late, "moins de temps, moins de buts"
    red = live.probs(live.parse(game(1, 60, score=(0, 0), reds=(1, 0))), PRED["pred"])[("h2h", "Leeds", None)]
    calm = live.probs(live.parse(game(1, 60, score=(0, 0))), PRED["pred"])[("h2h", "Leeds", None)]
    assert red > calm, "carton rouge pour Arsenal : Leeds a plus de chances"
    mp = live.market_probs(live.parse(game(1, 60)))
    assert abs(mp[("totals", "Over", 2.5)] + mp[("totals", "Under", 2.5)] - 1) < 1e-9 and abs(sum(mp[("h2h", k, None)] for k in ("Arsenal", "Draw", "Leeds")) - 1) < 1e-9
    print("ok  probabilités live : score, minute, cartons rouges ; 1xBet sans marge")


def test_legs_and_guards():
    raw = [game(1, 63), game(2, 20), game(3, 63, league="Czech Republic. 4 Liga"), game(4, 89)]
    keep, seen, count = live.legs(raw, now=NOW, data={}, model_for=lambda m, k: PRED if k else None)
    assert count == {"live": 4, "window": 2, "known": 1}, count                 # 20e et 89e hors fenêtre ; 4e division tchèque inconnue
    assert keep and all(c["id"] == "lv1" and c["prob"] >= 0.6 and c["odds"] >= 1.15 and abs(c["prob"] - c["px"]) <= 0.08
                        and c["prob"] * c["odds"] - 1 >= 0.02 for c in keep), keep
    assert all(c["tranche"] == "60-75" and c["live"] and c["xref"][0] == 1 for c in seen)
    assert seen and all(c.get("strategy") == "live" for c in seen + keep), "étiquette « live » : hors voies, mesurée à part"
    thin = live.legs([game(1, 63)], now=NOW, data={}, model_for=lambda m, k: {"pred": PRED["pred"] | {"q": 0.1}})
    assert thin[2]["known"] == 0, "données trop maigres : jamais"
    far = live.legs([game(1, 63, odds=[{"T": 9, "P": 1.5, "C": 4.0}, {"T": 10, "P": 1.5, "C": 1.2}])], now=NOW, data={}, model_for=lambda m, k: PRED if k else None)[0]
    assert not any(c["name"] == "Over" for c in far), "Kelly loin de 1xBet : 1xBet sait sans doute quelque chose"
    print("ok  garde-fous : fenêtre de minutes, championnats connus, données solides, écart avec 1xBet")


def test_combo_and_buttons():
    raw = [game(i, 40 + i * 5, home=f"Team{i}", away=f"Other{i}") for i in range(1, 7)]
    old = xbet.save_coupon
    xbet.save_coupon = lambda legs: "LIVE1"
    try:
        msg = live.combo(3.0, DAY, get=lambda u: {"Value": raw}, model_for=lambda m, k: PRED if k else None, now=NOW)
    finally:
        xbet.save_coupon = old
    text = str(msg)
    assert "🔴 COMBINÉ LIVE — cote" in text and "Kelly" in text and "1xBet" in text and "<code>LIVE1</code>" in text and "valide tout de suite" in text, text
    data = [b["callback_data"] for row in msg.markup["inline_keyboard"] for b in row if "callback_data" in b]
    assert data == [f"cjoue|m1|{DAY}"], "pas de « 🔀 » pour un combiné live"
    item = store.load(custom.FILE, {})["items"]["m1"]
    assert item["src"] == "live" and 3.0 <= item["total"] <= 3.75, item
    custom.track("m1", DAY)
    assert chat.refresh_button("vm1", DAY) is None, "un combiné live ne se revérifie pas"
    none = live.combo(15.0, DAY, get=lambda u: {"Value": [game(1, 20)]}, model_for=lambda m, k: PRED if k else None, now=NOW)
    assert none.startswith("🔴 Pas de combiné live à la cote 15") and "1 matchs en cours" in none, none
    print("ok  combiné live : cote, Kelly / 1xBet par pari, code coupon, « ✅ Je le joue » sans « 🔀 », pas de revérification")


def test_journal_and_separate_learning():
    store.save(live.JOURNAL, [])
    store.save(calib.FILE, {})
    keep, seen, _ = live.legs([game(1, 63)], now=NOW, data={}, model_for=lambda m, k: PRED if k else None)
    n = live.record(keep, NOW)
    assert n == len(keep) and 0 < n < len(seen) and live.record(keep, NOW) == 0, "seulement les paris retenus, une fois par jour et par tranche"
    from kelly import settle as S
    old = S._settle_fair_row
    S._settle_fair_row = lambda r, now, fdata, stats_for=None: r.update(result="win" if r["name"] in ("Over", "Yes", "Arsenal") else "loss")
    try:
        settled, learned = live.settle(NOW, fdata=object())
    finally:
        S._settle_fair_row = old
    data = live.calib_data()
    assert settled == learned == n and "goals|60-75" in data and "h2h|60-75" in data and not any(k.startswith("src|") for k in data), data
    assert store.load(live.CALIB_FILE, {})["v"] == live.CALIB_VERSION
    assert store.load(calib.FILE, {}) == {}, "l'apprentissage live ne touche jamais la calibration d'avant-match"
    assert live.settle(NOW, fdata=object()) == (0, 0)
    data["goals|60-75"] = [100, 20, 70.0]                                     # 20 % gagnés pour 70 % annoncés : trop confiante en 60-75
    assert live.factor("totals", "60-75", data) < 1 and live.factor("totals", "30-45", data) == 1.0
    assert any(line.startswith("buts 60-75 : 100 réglés") for line in live.calib_lines(data))
    print("ok  journal live (paris retenus seulement) et apprentissage À PART, par famille et par tranche, une seule correction")


def test_light_teams():
    """Équipes inconnues préparées à l'avance (kelly/livedata.py) : exclusions, buts attendus légers, avis mélangé au marché, jamais en coupe."""
    from kelly import config as C, livedata
    assert livedata.excluded("Czech Republic. U19 League") and livedata.excluded("Liga 2", "Dinamo II") and livedata.excluded("Regionalliga West")
    assert not livedata.excluded("Romania. Liga 2", "Metalul Buzau", "Targu Mures") and livedata.excluded("3 Liga", "Slavia Prague III")
    # le match se retrouve par son coup d'envoi même quand un nom diffère d'un site à l'autre (« CSC 1599 Selimbar » / « Viitorul Selimbar »)
    ko = int(NOW.timestamp())
    lists = {"2026-10-10": {"at": "2026-10-10T08:00", "games": [
        {"id": 5, "comp": "Liga 2", "start": NOW.isoformat(), "home": {"id": 11, "name": "Viitorul Selimbar"}, "away": {"id": 12, "name": "Unirea Slobozia"}},
        {"id": 6, "comp": "Liga 2", "start": NOW.isoformat(), "home": {"id": 13, "name": "Other"}, "away": {"id": 14, "name": "Team"}}]}}
    base = {"name": "x", "res": [[1, 1, 1]] * 10, "at": "2026-10-10T08:00"}
    db = {"lists": lists, "teams": {"11": base | {"name": "Viitorul Selimbar"}, "12": base | {"name": "Unirea Slobozia"}}}
    assert livedata.pred_for("CSC 1599 Selimbar", "Unirea Slobozia", db, start=ko), "retrouvé par le match"
    assert livedata.pred_for("CSC 1599 Selimbar", "Unirea Slobozia", db) is None, "sans heure : par le nom seul, introuvable"
    assert livedata.pred_for("CSC 1599 Selimbar", "Unirea Slobozia", db, start=ko + 3600) is None, "coup d'envoi trop différent"
    assert livedata.cup("Australia Cup") and livedata.cup("Club Friendlies") and not livedata.cup("Romania. Liga 2")
    teams = {"1": {"name": "Metalul Buzau", "res": [[3, 0, 1]] * 10, "at": "2026-10-10T08:00"},
             "2": {"name": "Targu Mures", "res": [[0, 2, 0]] * 10, "at": "2026-10-10T08:00"},
             "3": {"name": "Tiny FC", "res": [[1, 1, 1]] * 2, "at": "2026-10-10T08:00"}}
    p = livedata.pred_for("Metalul Buzau", "Targu Mures", {"teams": teams, "lists": {}})
    assert p["lh"] > 2 * p["la"] and p["q"] == 0.5 and p["n"] == [10, 10] and p["src"] == "365scores", p
    assert livedata.pred_for("Metalul Buzau", "Tiny FC", {"teams": teams, "lists": {}}) is None, "moins de 4 matchs : rien"
    assert livedata.pred_for("Unknown", "Targu Mures", {"teams": teams, "lists": {}}) is None
    old = livedata.pred_for
    livedata.pred_for = lambda h, a, data=None, start=None, league=None: {"lh": 3.0, "la": 0.3, "q": 0.5, "n": [10, 10], "src": "365scores"}
    try:
        g = game(9, 60, score=(0, 0), league="Romania. Liga 2", home="Metalul Buzau", away="Targu Mures")
        keep, seen, count = live.legs([g, game(8, 60, league="Australia Cup", home="A", away="B")], now=NOW, data={})
        assert count["known"] == 1, "la coupe est écartée"
        raw = live.probs(live.parse(g), livedata.pred_for("", ""))
        mp = live.market_probs(live.parse(g))
        h2h = next(c for c in seen if c["market"] == "h2h" and c["name"] == "Metalul Buzau")
        k = ("h2h", "Metalul Buzau", None)
        assert abs(h2h["p0"] - live.blend(raw[k], mp[k], C.LIVE_W_KELLY * C.LIVE_W_LIGHT)) < 1e-3 and h2h["src"] == "365scores", h2h
        assert abs(h2h["pk"] - raw[k]) < 1e-3, "pk : la proba brute de Kelly, gardée à côté de la proba ancrée"
    finally:
        livedata.pred_for = old
    print("ok  équipes inconnues : jeunes / réserves / régional et coupes exclus, buts attendus légers, avis mélangé à moitié avec 1xBet")


def test_prepare():
    """Préparation au passage horaire : liste du jour puis derniers résultats des équipes des matchs proches, hors exclusions, gardés quelques jours."""
    import json
    from kelly import livedata
    from kelly.sources import http, s365

    class Res:
        def __init__(self, obj):
            self.ok, self.content, self.error, self.stale = True, json.dumps(obj).encode(), None, False
    day_list = {"competitions": [{"id": 1, "name": "Liga 2"}, {"id": 2, "name": "U19 League"}],
                "games": [{"id": 10, "competitionId": 1, "startTime": "2026-10-10T16:00:00+00:00", "homeCompetitor": {"id": 101, "name": "Alpha"},
                           "awayCompetitor": {"id": 102, "name": "Beta"}},
                          {"id": 11, "competitionId": 2, "startTime": "2026-10-10T16:00:00+00:00", "homeCompetitor": {"id": 201, "name": "Gamma U19"},
                           "awayCompetitor": {"id": 202, "name": "Delta U19"}},
                          {"id": 12, "competitionId": 1, "startTime": "2026-10-11T12:00:00+00:00", "homeCompetitor": {"id": 301, "name": "Late"},
                           "awayCompetitor": {"id": 302, "name": "Later"}}]}
    results = {"games": [{"startTime": f"2026-10-0{i}T15:00:00+00:00", "homeCompetitor": {"id": 101, "score": 2.0}, "awayCompetitor": {"id": 9, "score": 1.0}}
                         for i in range(1, 9)]}
    calls = []
    old = (http.fetch, s365._via_proxy)
    http.fetch = lambda url, name, ttl, **k: calls.append(url) or Res(day_list if "allscores" in url else results)

    class NoProxy:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False
    s365._via_proxy = lambda: NoProxy()
    store.save(livedata.FILE, {})
    try:
        rep = livedata.prepare(now=NOW)
        rep2 = livedata.prepare(now=NOW)
    finally:
        http.fetch, s365._via_proxy = old
    d = livedata.load()
    assert rep["teams"] == 2 and set(d["teams"]) == {"101", "102"}, (rep, d["teams"])          # U19 exclus, match de demain trop loin
    assert d["teams"]["101"]["res"][0] == ["2026-10-08", 2, 1, 1] and len(d["teams"]["101"]["res"]) == 8
    assert rep2["teams"] == 0 and sum("allscores" in c for c in calls) == 1, "rien à refaire : liste et équipes en mémoire"
    # historique EN BASE : une relecture ajoute les nouveaux matchs sans doublon, une vieille équipe n'est jamais effacée
    old_res = [["2026-09-20", 0, 0, 1], [1, 1, 0]]
    merged = livedata.merge(old_res, [["2026-10-08", 2, 1, 1], ["2026-09-20", 0, 0, 1]])
    assert merged == [["2026-10-08", 2, 1, 1], ["2026-09-20", 0, 0, 1], [1, 1, 0]], merged
    d["teams"]["999"] = {"name": "Old", "comp": "", "at": "2026-09-01T10:00", "res": [["2026-08-30", 1, 0, 1]] * 5}
    store.save(livedata.FILE, d)
    old = (http.fetch, s365._via_proxy)
    http.fetch = lambda url, name, ttl, **k: Res(day_list if "allscores" in url else results)
    s365._via_proxy = lambda: NoProxy()
    try:
        livedata.prepare(now=NOW)
    finally:
        http.fetch, s365._via_proxy = old
    assert "999" in livedata.load()["teams"], "jamais effacée"
    print("ok  préparation : liste du jour, équipes des matchs proches, exclusions, rien relu inutilement, historique gardé en base")


def test_fetch_from_telegram():
    """Une commande Telegram est « hors ligne » (cache.offline) : la liste live doit quand même être lue chez 1xBet (sinon « réponse périmée »)."""
    from kelly import cache
    seen = []

    class Ses:
        why = None

        def call(self, fn, *a):
            return fn(*a)

        def close(self, what):
            seen.append("close")
    old = (xbet._Session, xbet._get_json)
    xbet._Session = Ses
    xbet._get_json = lambda url, ttl: seen.append(cache.OFFLINE) or {"Value": [game(1, 63)]}
    try:
        with cache.offline():
            raw, why = live.fetch()
            assert cache.OFFLINE, "le mode hors ligne est rétabli après la lecture"
    finally:
        xbet._Session, xbet._get_json = old
    assert why is None and len(raw) == 1 and seen == [False, "close"], seen
    print("ok  lecture live depuis une commande Telegram (hors ligne par ailleurs)")


def test_telegram_flow():
    assert chat.route("🔴 Live")[1].markup["inline_keyboard"][0][0]["callback_data"] == "live|3"
    assert chat.route("live")[1].startswith("🔴 Combiné live : quelle cote ?")
    assert chat.route("live 20") == ("/live 20", None)
    assert chat.on_callback("live|autre", 3).startswith("✏️ Écris ta cote live")
    assert chat.route("20") == ("/live 20", None) and chat.get_pending() is None
    seen = []
    old = live.combo                                      # app l'appelle via « LV » (même module)
    live.combo = lambda target, day, **k: seen.append(target) or "ok"
    try:
        assert chat.on_callback("live|5", 3) == "ok" and app._handle_command("/live 20") == "ok" and seen == [5.0, 20.0]
        assert "quelle cote" in str(app._handle_command("/live"))
    finally:
        live.combo = old
    print("ok  Telegram : bouton 🔴 Live, sélecteur de cote, « ✏️ Autre cote », « live 20 »")


def test_live_coupon_kind():
    """Ticket d'un combiné live : chaque pari avec Kind 1 et le ticket avec Live vrai (Kind 1) ; un ticket avant-match reste Kind 3, sans Live."""
    import json
    from kelly.sources import http, xbet
    posted = []

    class Direct:                                   # pas de proxy dans ce test : la session passe l'appel tel quel
        why = None
        call = staticmethod(lambda fn: fn())
        close = staticmethod(lambda *a, **k: None)
    old = http.post, xbet._Session
    http.post = lambda url, body, **kw: (posted.append(body), http.Result(url, json.dumps({"Success": True, "Value": "LV1"}).encode(), 200))[1]
    xbet._Session = Direct
    try:
        assert xbet.save_coupon([{"xref": [760263995, 10, 4.5], "xodds": 1.3, "live": True}, {"xref": [760263957, 6, 0], "xodds": 1.5, "live": True}]) == "LV1"
        assert xbet.save_coupon([{"xref": [900, 10, 3.5], "xodds": 1.55}]) == "LV1"
    finally:
        http.post, xbet._Session = old
    live_, pre = posted[0], posted[1]
    assert [e["Kind"] for e in live_["Events"]] == [1, 1] and live_["Live"] is True and live_["Kind"] == 1, live_
    assert [e["Kind"] for e in pre["Events"]] == [3] and pre["Live"] is False and pre["Kind"] == 3, pre
    print("ok  code coupon live : Kind 1 par pari et Live vrai sur le ticket ; avant-match inchangé (Kind 3)")


def main():
    test_live_coupon_kind()
    test_basics()
    test_probs()
    test_legs_and_guards()
    test_combo_and_buttons()
    test_journal_and_separate_learning()
    test_light_teams()
    test_prepare()
    test_fetch_from_telegram()
    test_telegram_flow()
    print("tests live : OK")


if __name__ == "__main__":
    main()
