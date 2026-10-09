"""Paris réels (The Odds API) remis à la cote 1xBet (xprice.price_picks), hors ligne : cote et valeur recalculées, cote typique gardée,
paris absents de 1xBet écartés des combinés / du pari du jour, comportement inchangé sans cote 1xBet, code coupon des combinés réels et du
pari du jour, suivi (cote du marché pour le CLV) et gains à la cote 1xBet.
Lancer : python -m tests.test_xbet_picks"""
import math
import os
import tempfile
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-xpicks-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-xpicks-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-xpicks-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ["XBET_PROXY_FILE"] = os.path.join(os.environ["DATA_DIR"], "pas-de-fichier.env")      # jamais le vrai proxy de la machine
os.environ["XBET_PROXY"] = "http://user:secret@proxy.test:80"

from kelly import app, combos as CB, config as C, fmt, settle, store, track, xprice  # noqa: E402
from kelly.sources import xbet  # noqa: E402

DAY = "2026-10-09"
TYP, XB, PROB = 1.6, 1.5, 0.7                       # cote typique des books, cote 1xBet, proba de consensus


def pick(n, market="h2h", name=None, point=None, **kw):
    home, away = f"Home{n}", f"Away{n}"
    name = name or home
    c = {"key": f"e{n}|{market}||{point if point is not None else ''}|{name}", "id": f"e{n}", "sport_key": "soccer_spain_la_liga",
         "group": "soccer", "event": f"{home} - {away}", "home": home, "away": away, "league": "Liga", "start": "19:00",
         "commence": "2026-10-09T19:00:00Z", "market": market, "who": None, "point": point, "name": name,
         "pick": fmt.label(market, name, point, None, "soccer"), "prob": PROB, "prob_book": PROB, "prob_src": "consensus",
         "n_sharp": 1, "odds": TYP, "best_odds": 1.8, "book": "Coolbet", "edge": PROB * TYP - 1, "spread": 0.02, "steam": 0.0,
         "n_books": 10, "weight": 1.0, "score": 0.12 + n / 100, "why": "cote plus haute qu'ailleurs (1,60 contre 1,43 en moyenne)"}
    return c | kw


def fake_fetch(priced, calls=None):
    """1xBet vu par les tests : `priced` = {id Kelly: {(marché, côté, ligne): cote}} ; les autres matchs sont introuvables."""
    def fetch(events, deadline=None, wanted=None):
        if calls is not None:
            calls.append(events)
        out = {}
        for k, ev in events:
            px = priced.get(ev["id"])
            if px is not None:
                gid = 9000 + int(ev["id"][1:])
                px = {(m, ev["home_team"] if n == "HOME" else n, p): v for (m, n, p), v in px.items()}
                out[ev["id"]] = {"gid": gid, "px": px, "refs": {k_: [gid, 1 + i, 0] for i, k_ in enumerate(px)}}
        return out
    return fetch


def world(n=6):
    """n matchs de 1X2 à la cote typique 1,60 ; 1xBet paie 1,50 sur les 4 premiers ; e5 : match trouvé mais pari absent (les 2 marquent
    seulement) ; e6 : match introuvable."""
    picks = [pick(i) for i in range(1, n + 1)]
    priced = {f"e{i}": {("h2h", "HOME", None): XB} for i in range(1, 5)} | {"e5": {("btts", "Yes", None): 1.8}}
    return picks, priced


def test_price_picks():
    picks, priced = world()
    picks.append(pick(7, sport_key="tennis_atp", group="tennis"))                  # championnat hors LEAGUES : jamais demandé à 1xBet
    calls = []
    st = xprice.price_picks(picks, fetch=fake_fetch(priced, calls), pronos=[{"id": "e2", "xgid": 4242}])
    assert st == {"matches": 5, "priced": 4, "none": 1, "missing": 2}, st
    assert {ev["id"] for _, ev in calls[0]} == {f"e{i}" for i in range(1, 7)}, "tennis non demandé"
    assert next(ev for _, ev in calls[0] if ev["id"] == "e2")["xgid"] == 4242, "id 1xBet déjà connu : réutilisé"
    c = picks[0]
    assert c["odds"] == XB and c["xodds"] == XB and c["book"] == "1xBet" and c["best_odds"] == XB, c
    assert c["typ_odds"] == TYP and c["typ_book"] == "Coolbet" and c["typ_edge"] == PROB * TYP - 1
    assert abs(c["edge"] - (PROB * XB - 1)) < 1e-9 and c["xref"] == [9001, 1, 0] and c["xgid"] == 9001
    assert abs(c["score"] - (0.13 + PROB * (XB - TYP))) < 1e-9, "le score suit l'écart de valeur"
    assert "qu'ailleurs" not in c["why"] and fmt.best_note(c) == "", "plus de cote d'un autre book"
    assert picks[4].get("xnone") == 1 and picks[4]["xgid"] == 9005 and "xodds" not in picks[4] and picks[4]["odds"] == TYP
    assert picks[5].get("xmiss") == 1 and "xnone" not in picks[5] and picks[5]["odds"] == TYP
    # relecture : la cote typique d'origine n'est pas perdue
    xprice.price_picks(picks, fetch=fake_fetch(priced | {"e1": {("h2h", "HOME", None): 1.45}}))
    assert picks[0]["odds"] == 1.45 and picks[0]["typ_odds"] == TYP and abs(picks[0]["edge"] - (PROB * 1.45 - 1)) < 1e-9
    # plus/moins de buts et corners : la ligne exacte
    t = pick(8, "totals", "Over", 2.5)
    k = pick(9, "alternate_totals_corners", "Over", 9.5)
    s = pick(10, "spreads", "Home10", -1.5)
    xprice.price_picks([t, k, s], fetch=fake_fetch({"e8": {("totals", "Over", 2.5): 1.9, ("totals", "Over", 3.5): 3.0},
                                                    "e9": {("alternate_totals_corners", "Over", 9.5): 1.99},
                                                    "e10": {("h2h", "HOME", None): 2.0}}))
    assert (t["odds"], k["odds"]) == (1.9, 1.99) and k["xref"][0] == 9009 and s.get("xnone") == 1, "handicap : 1xBet ne le lit pas"
    print("ok  paris réels remis à la cote 1xBet (cote, valeur, référence, cote typique gardée)")


def test_no_xbet():
    picks, _ = world()
    before = [dict(c) for c in picks]
    assert xprice.price_picks(picks, fetch=lambda *a, **k: {})["priced"] == 0
    assert xprice.price_picks(picks, fetch=lambda *a, **k: 1 / 0)["priced"] == 0
    assert picks == before, "1xBet injoignable : rien ne change"
    assert len(CB.make_pool(picks)) == 6
    old = C.XBET_ENABLED
    C.XBET_ENABLED = False
    try:
        assert xprice.price_picks(picks, fetch=lambda *a, **k: 1 / 0)["matches"] == 0
    finally:
        C.XBET_ENABLED = old
    print("ok  sans cote 1xBet : comportement inchangé")


def test_pool_and_plan():
    picks, priced = world()
    xprice.price_picks(picks, fetch=fake_fetch(priced))
    assert [c["id"] for c in CB.make_pool(picks)] == ["e1", "e2", "e3", "e4"], "absents de 1xBet : hors des combinés"
    # sans passer par price_picks : une cote 1xBet quelque part suffit à écarter les paris sans cote 1xBet
    raw = [pick(1, xodds=1.5, odds=1.5, edge=PROB * 1.5 - 1), pick(2), pick(3, xnone=1)]
    assert [c["id"] for c in CB.make_pool(raw)] == ["e1"]
    assert [c["id"] for c in CB.make_pool([pick(2), pick(3)])] == ["e2", "e3"]
    assert [c["id"] for c in CB.make_pool([pick(2), pick(3, xnone=1)])] == ["e2"], "pari déclaré absent : jamais"
    # valeur à la cote 1xBet sous le plancher : le pari sort par les filtres existants
    bad = [pick(1, xodds=1.1, odds=1.1, edge=PROB * 1.1 - 1, prob=0.5, prob_book=0.5), pick(2, xodds=1.5, odds=1.5, edge=0.05)]
    assert [c["id"] for c in CB.make_pool(bad)] == ["e2"]
    plan = dict(CB.plan(picks))
    sure = plan[C.COMBOS[0][0]]
    assert sure and {c["id"] for c in sure[0]} == {"e1", "e2", "e3", "e4"} and abs(sure[1] - XB ** 4) < 1e-6
    print("ok  paris sans cote 1xBet exclus des combinés, filtres de valeur à la cote 1xBet")


def test_build_day_coupon():
    picks, priced = world()
    xprice.price_picks(picks, fetch=fake_fetch(priced))
    asked = []
    old = xbet.save_coupon
    xbet.save_coupon = lambda legs: asked.append([c["key"] for c in legs]) or ("CODE%d" % len(asked))
    try:
        msgs, sent_picks, sent_combos = app.build_day(picks, DAY, odds_time="09:30")
    finally:
        xbet.save_coupon = old
    text = "\n".join(str(m) for m in msgs)
    assert "Code coupon 1xBet : <b>CODE1</b>" in text and "Charger le coupon" in text, text
    assert asked[0] == [picks[3]["key"]] and len(asked) == 2, asked                    # pari du jour d'abord, puis le combiné
    assert "cote 1,50" in text and "1,60" not in text and "Coolbet" not in text, "cotes 1xBet affichées, jamais la cote typique"
    assert "Cotes 1xBet de 09:30" in text, "heure du relevé rappelée"
    assert not any(c["id"] in ("e5", "e6") for c in sent_picks), "absents de 1xBet : ni pari du jour ni combiné"
    combo = sent_combos[0]
    assert combo["coupon"] == "CODE2" and abs(combo["odds"] - round(XB ** 4, 2)) < 1e-9
    assert [x["xodds"] for x in combo["xlegs"]] == [XB] * 4 and all(x["xref"] for x in combo["xlegs"])
    assert "legs" not in combo, "« legs » est la clé des combinés à vérifier (bankroll.played_gain)"
    top = sent_picks[0]
    assert top["coupon"] == "CODE1"
    # coupon impossible (échec / jambe sans référence) : le combiné part sans code
    xbet.save_coupon = lambda legs: None
    try:
        msgs2, _, combos2 = app.build_day(picks, DAY)
    finally:
        xbet.save_coupon = old
    assert "Code coupon" not in "\n".join(str(m) for m in msgs2) and combos2[0]["coupon"] is None
    plain, _ = world()                                                                    # sans cote 1xBet : aucun coupon, aucune nouvelle clé
    xbet.save_coupon = lambda legs: 1 / 0
    try:
        msgs3, _, combos3 = app.build_day(plain, DAY)
    finally:
        xbet.save_coupon = old
    assert "Code coupon" not in "\n".join(str(m) for m in msgs3) and all("coupon" not in c and "xlegs" not in c for c in combos3)
    print("ok  code coupon du combiné réel et du pari du jour")


def test_tracking_and_gains():
    picks, priced = world()
    xprice.price_picks(picks, fetch=fake_fetch(priced))
    store.save("tracked.json", [])
    now = datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc)
    keys = {picks[0]["key"], picks[1]["key"]}
    picks[0]["coupon"] = "ZZ1"
    track.record(picks, keys, DAY, now, single_key=picks[0]["key"])
    rows = {r["key"]: r for r in track.load()}
    r = rows[picks[0]["key"]]
    assert r["odds"] == TYP and r["xodds"] == XB and r["xref"] == [9001, 1, 0] and r["coupon"] == "ZZ1", "le suivi garde la cote du marché"
    assert r["book"] == "Coolbet" and abs(r["edge"] - (PROB * TYP - 1)) < 1e-9
    assert abs(r["features"]["log_odds"] - math.log(TYP)) < 1e-9 and abs(r["features"]["edge"] - (PROB * TYP - 1)) < 1e-9
    n = track.close(list(rows.values()), [{"key": picks[0]["key"], "odds": 1.55, "prob": 0.66}], now)
    assert n == 1 and r["clv"] == round(math.log(TYP / 1.55), 4), "CLV : cote du marché contre clôture du marché"
    assert settle._gain({"stake": 2, "result": "win", "odds": TYP, "xodds": XB}) == 1.0
    assert settle._gain({"stake": 2, "result": "win", "odds": 3.0}) == 4.0
    from kelly import money
    assert money.gain({"stake": 2, "cur": "FCFA", "result": "win", "odds": TYP, "xodds": XB}) == 1.0
    assert settle._rate([{"result": "win", "odds": TYP, "xodds": XB}])[2] == 50.0
    print("ok  suivi à la cote du marché, gains à la cote 1xBet")


def test_custom_combo():
    picks, priced = world()
    xprice.price_picks(picks, fetch=fake_fetch(priced))
    old = xbet.save_coupon
    xbet.save_coupon = lambda legs: "CUSTOM9"
    try:
        msg = str(app.custom_combo(picks, [], 5.0, DAY))
    finally:
        xbet.save_coupon = old
    assert "Code coupon 1xBet : <b>CUSTOM9</b>" in msg and "Home5" not in msg and "Home6" not in msg, msg
    print("ok  /combi sur mesure : seulement les paris aux cotes 1xBet, avec code")


def main():
    test_price_picks()
    test_no_xbet()
    test_pool_and_plan()
    test_build_day_coupon()
    test_tracking_and_gains()
    test_custom_combo()
    print("tests paris réels 1xBet : OK")


if __name__ == "__main__":
    main()
