"""Alertes 1xBet (kelly/xalerts.py), hors ligne : 💰 1xBet en retard sur le marché (avec mise et code coupon, relecture plafonnée),
⚠️ mes paris en danger (combiné réel dont un pari a quitté 1xBet, bouton « Vérifier »), 📈 mieux payé qu'au matin (code coupon),
✅ bonne nouvelle (pari joué), dédoublonnage entre passages, ordre et plafond des alertes d'un message, cote du matin (xodds0).
Lancer : python -m tests.test_alerts"""
import contextlib
import io
import json
import os
import tempfile
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-alerts-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-alerts-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-alerts-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ["XBET_PROXY_FILE"] = os.path.join(os.environ["DATA_DIR"], "pas-de-fichier.env")      # jamais le vrai proxy de la machine
os.environ["XBET_PROXY"] = "http://user:secret@proxy.test:80"

from kelly import app, calib, config as C, fair, fmt, money, odds, settle, store, telegram, track, xalerts, xprice  # noqa: E402
from kelly.sources import http, state, xbet  # noqa: E402

DAY = "2030-01-01"
KO = f"{DAY}T19:00:00Z"
TYP, XB, PROB = 1.6, 1.5, 0.7


def reset():
    """Données du jour vides, fiabilité neutre, pas de budget, aucune alerte déjà envoyée."""
    for f in ("tracked.json", "fair.json", fair.COMBOS_FILE):
        store.save(f, [])
    store.save("bets.json", {"picks": [], "combos": []})
    for f in ("today.json", app.COMBO_UPDATES, money.FILE, calib.FILE, "odds_history.json", xalerts.FILE, "pending.json"):
        store.save(f, {})
    calib._memo.clear()
    C.XALERTS_ENABLED, C.XALERT_LIVE_MAX = True, 3


def at(h, m=0):
    return datetime(2030, 1, 1, h, m, tzinfo=timezone.utc)


def pick(n, **kw):
    """Pari réel (The Odds API) du match e<n> : 1X2 de l'équipe à domicile à la cote typique TYP."""
    home, away = f"Home{n}", f"Away{n}"
    c = {"key": f"e{n}|h2h||{home}", "id": f"e{n}", "sport_key": "soccer_spain_la_liga", "group": "soccer", "event": f"{home} - {away}",
         "home": home, "away": away, "league": "Liga", "start": "19:00", "commence": KO, "market": "h2h", "who": None, "point": None,
         "name": home, "pick": fmt.label("h2h", home, None, None, "soccer"), "prob": PROB, "prob_book": PROB, "prob_src": "consensus",
         "n_sharp": 1, "odds": TYP, "best_odds": 1.8, "book": "Coolbet", "edge": PROB * TYP - 1, "spread": 0.02, "steam": 0.0,
         "n_books": 10, "weight": 1.0, "score": 0.12 + n / 100, "why": ""}
    return c | kw


def prono(i, **kw):
    """Pronostic 💎 : moins de 3,5 buts à 1,55 chez 1xBet (cote du matin 1,55), match m<i>."""
    c = {"id": f"m{i}", "sport_key": "soccer_epl", "home": f"H{i}", "away": f"A{i}", "commence": KO, "start": "19:00", "league": "L",
         "event": f"H{i} - A{i}", "market": "totals", "name": "Under", "point": 3.5, "who": None, "prob": 0.72, "p0": 0.72,
         "min_odds": 1.46, "conf": 0.6, "n_min": 8, "score": 0.2 - i / 1000, "key": f"m{i}|totals||3.5|Under",
         "pick": fmt.label("totals", "Under", 3.5, None, "soccer"), "xodds": 1.55, "xodds0": 1.55, "xev": 0.12, "xgid": 900 + i,
         "xref": [900 + i, 10, 3.5],
         "psh": {"under|3.5": [0.70, "pinnacle", 1]}}      # Pinnacle voit 70 % : sans avis du marché, l'ensemble (buts) ne validerait jamais ce pari
    return c | kw


def fake_fetch(priced, calls=None):
    """1xBet vu par les tests : `priced` = {id Kelly: {(marché, côté, ligne): cote}} ; les autres matchs sont introuvables. « HOME » = l'équipe
    à domicile. `calls` reçoit les ids des matchs demandés à chaque appel."""
    def fetch(events, deadline=None, wanted=None, live=False, fresh_min=None, max_matches=None):
        if calls is not None:
            calls.append([ev["id"] for _, ev in events])
        out = {}
        for k, ev in events:
            px = priced.get(ev["id"])
            if px is not None:
                gid = 9000 + int(ev["id"][1:])
                px = {(m, ev["home_team"] if n == "HOME" else n, p): v for (m, n, p), v in px.items()}
                out[ev["id"]] = {"gid": gid, "px": px, "refs": {k_: [gid, 1 + i, 0] for i, k_ in enumerate(px)}}
        return out
    return fetch


def cache_event(gid, cells, age=0):
    """Lecture 1xBet gardée dans le cache du disque (relevé qui vient d'avoir lieu) : cells = [(type, cote)]."""
    u = xbet._events_url(gid)
    body = json.dumps({"eventGroups": [{"events": [[{"type": t, "cf": cf, "parameter": 0} for t, cf in cells]]}]}).encode()
    http._write(http._key(u, xbet.JSON_H), {"url": u, "fetched_at": state.clock() - age, "status": 200}, body)


class Fakes:
    """Messages capturés, code coupon simulé (les paris seuls enregistrés sont gardés dans `coupons`) : aucun réseau."""

    def __init__(self, code="K7Q2X"):
        self.sent, self.coupons, self.code = [], [], code

    def __enter__(self):
        self.old = (xbet.save_coupon, telegram.send)
        xbet.save_coupon = lambda legs: (self.coupons.append(legs), self.code)[1]
        telegram.send = lambda text, *a, **k: (self.sent.append(text), True)[1]
        return self

    def __exit__(self, *a):
        xbet.save_coupon, telegram.send = self.old

    def alerts(self):
        return [m for m in self.sent if "Alertes Kelly" in str(m)]


def buttons(msg):
    return [b["callback_data"] for row in msg.markup["inline_keyboard"] for b in row if "callback_data" in b]


def setup_singles(picks, priced, sent=(1,)):
    """Paris réels remis au prix 1xBet, ceux de `sent` (numéros) envoyés et suivis, today.json du jour."""
    reset()
    xprice.price_picks(picks, fetch=fake_fetch(priced))
    track.record(picks, {picks[n - 1]["key"] for n in sent}, DAY, at(4), single_key=picks[sent[0] - 1]["key"])
    store.save("today.json", {"day": DAY, "picks": picks, "pronos": [], "picks_at": "09:30"})


def run_track(closings, xprices, calls=None, fetch=None, now=None):
    """Passage horaire à 18:00 : cotes de clôture simulées (`closings`, une liste de paris), 1xBet simulé (`xprices`)."""
    old = odds.build_picks
    odds.build_picks = lambda evs, day, *a, **k: [c for c in closings if c["id"] == evs[0]["id"]]
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return app.run_track(events_for=lambda sk, eid, m: {"commence_time": KO, "id": eid}, now=now or at(18),
                                 xfetch=fetch or fake_fetch(xprices, calls))
    finally:
        odds.build_picks = old


def moved(n, odds_now=1.55, pb=0.65):
    """Pari du marché à la clôture : la cote a chuté et les books lui donnent plus de chances (find_alerts : mouvement fort)."""
    return pick(n, odds=odds_now, prob=pb, prob_book=pb)


def history(*ns):
    store.save("odds_history.json", {f"e{n}|h2h||Home{n}": {"o": 1.9, "p": 0.52, "pb": 0.52, "t": "2030-01-01T08:00+00:00"} for n in ns})


# ---------- cote du matin ----------

def test_morning_price_kept():
    reset()
    c = prono(1, xodds=None, xodds0=None, xev=None, xref=None)
    xprice.price(c, {("totals", "Under", 3.5): 1.55})
    assert c["xodds"] == 1.55 and c["xodds0"] == 1.55
    xprice.price(c, {("totals", "Under", 3.5): 1.70})
    assert c["xodds"] == 1.70 and c["xodds0"] == 1.55, "relevé suivant : la cote du matin reste"
    # la ligne change : l'ancienne cote ne se compare plus à la nouvelle
    d = prono(2, alts=[["Over", 2.5, 0.72, None]], name="Under", point=3.5, psh={"over|2.5": [0.62, "pinnacle", 1]})
    xprice.price(d, {("totals", "Over", 2.5): 2.0})
    assert (d["name"], d["point"]) == ("Over", 2.5) and d["xodds0"] == 2.0, d
    # paris réels : la première lecture seulement
    p = pick(1)
    xprice.price_picks([p, pick(2)], fetch=fake_fetch({"e1": {("h2h", "HOME", None): 1.5}}))
    assert p["xodds"] == 1.5 and p["xodds0"] == 1.5
    xprice.price_picks([p], fetch=fake_fetch({"e1": {("h2h", "HOME", None): 1.8}}))
    assert p["xodds"] == 1.8 and p["xodds0"] == 1.5 and p["typ_odds"] == TYP
    print("ok  cote 1xBet du matin (xodds0) : gardée aux relevés suivants, refaite si la ligne change")


# ---------- 💰 1xBet en retard ----------

def test_lag():
    picks = [pick(1), pick(2)]
    setup_singles(picks, {"e1": {("h2h", "HOME", None): 1.80}, "e2": {("h2h", "HOME", None): 1.5}})
    history(1)
    calls = []
    with Fakes() as f:
        run_track([moved(1)], {"e1": {("h2h", "HOME", None): 1.80}}, calls)
    assert len(f.sent) == 1 and "La cote chute" not in str(f.sent[0]), f.sent
    msg = str(f.sent[0])
    assert "💰 1xBet est en retard : joue maintenant" in msg and "Home1 – Away1" in msg and "19h00" in msg and "Liga" in msg, msg
    assert "Home1 gagne — 1xBet paie <b>1,80</b>" in msg and "(lu à 18:00)" in msg, msg
    assert "1,90 ➜ <b>1,55</b>" in msg and "52 % ➜ 65 % de chances" in msg and "valeur <b>+17 %</b>" in msg, msg
    assert "💡 Mise conseillée : règle ton budget du jour avec « 💰 Mon budget »" in msg and " u</b>" not in msg, msg          # sans budget : jamais d'unités
    assert "Code : <code>K7Q2X</code>" in msg, msg
    assert len(f.coupons) == 1 and f.coupons[0][0]["xodds"] == 1.80 and f.coupons[0][0]["xref"] == [9001, 1, 0], f.coupons
    assert calls == [["e1"]], f"un seul match relu en direct : {calls}"
    # même état au passage suivant (les clôtures sont relevées de nouveau) : la même alerte n'est pas renvoyée
    rows = track.load()
    for r in rows:
        r["closed_at"] = None
    track.save(rows)
    history(1)
    with Fakes() as f2:
        run_track([moved(1)], {"e1": {("h2h", "HOME", None): 1.80}}, calls)
    assert not f2.sent and not f2.coupons, f2.sent
    # budget du jour : la mise est prise dessus
    setup_singles([pick(1), pick(2)], {"e1": {("h2h", "HOME", None): 1.80}})
    history(1)
    money.set_daily(5000)
    with Fakes() as f:
        run_track([moved(1)], {"e1": {("h2h", "HOME", None): 1.80}})
    assert "💰 Mise conseillée : <b>1 250 FCFA (sur ton budget du jour)</b>" in str(f.sent[0]), f.sent
    print("ok  F1 💰 1xBet en retard : valeur +17 %, cote 1xBet et heure, mouvement du marché, mise (budget, sinon règle ton budget), code coupon du pari seul")


def test_no_lag_when_xbet_followed():
    picks = [pick(1)]
    setup_singles(picks, {"e1": {("h2h", "HOME", None): 1.80}})
    history(1)
    calls = []
    with Fakes() as f:
        run_track([moved(1)], {"e1": {("h2h", "HOME", None): 1.50}}, calls)         # 0,65 x 1,50 - 1 = -2,5 % : 1xBet a suivi
        assert not f.sent and not f.coupons and calls == [["e1"]], (f.sent, calls)
        # pas de référence 1xBet pour ce pari (jamais chiffré, pas d'id 1xBet du match) : 1xBet n'est pas relu
        setup_singles([pick(1)], {})
        history(1)
        run_track([moved(1)], {"e1": {("h2h", "HOME", None): 1.80}}, calls)
        assert not f.sent and calls == [["e1"]], (f.sent, calls)
        # 1xBet ne propose plus le pari, ou ne répond pas : rien non plus, et aucune erreur
        setup_singles([pick(1)], {"e1": {("h2h", "HOME", None): 1.80}})
        history(1)
        run_track([moved(1)], {"e1": {("btts", "Yes", None): 1.8}})
        setup_singles([pick(1)], {"e1": {("h2h", "HOME", None): 1.80}})
        history(1)

        def boom(*a, **k):
            raise RuntimeError("1xBet en panne")
        run_track([moved(1)], None, fetch=boom)
        # alertes coupées : aucun message
        setup_singles([pick(1)], {"e1": {("h2h", "HOME", None): 1.80}})
        history(1)
        C.XALERTS_ENABLED = False
        run_track([moved(1)], {"e1": {("h2h", "HOME", None): 1.80}})
    assert not f.sent, f.sent
    C.XALERTS_ENABLED = True
    print("ok  F1 pas d'alerte « joue » si 1xBet a déjà suivi, s'il est muet ou s'il ne propose plus le pari ; XALERTS_ENABLED=0 coupe tout")


def test_live_cap():
    picks = [pick(n) for n in range(1, 6)]
    px = {f"e{n}": {("h2h", "HOME", None): 1.80} for n in range(1, 6)}
    setup_singles(picks, px, sent=(1, 2, 3, 4, 5))
    history(1, 2, 3, 4, 5)
    calls = []
    with Fakes() as f:
        run_track([moved(n) for n in range(1, 6)], px, calls)
    read = {i for c in calls for i in c}
    assert len(read) == C.XALERT_LIVE_MAX == 3 and all(len(c) == 1 for c in calls), calls
    assert len(f.sent) == 1 and str(f.sent[0]).count("1xBet paie") == 3, f.sent
    assert len(f.coupons) == 3
    print("ok  F1 relectures 1xBet plafonnées à XALERT_LIVE_MAX = 3 matchs par passage (un message, 3 alertes)")


# ---------- ✅ bonne nouvelle ----------

def test_good_news():
    played_pick = pick(1, odds=2.2, edge=PROB * 2.2 - 1, stake=1000, cur="FCFA")
    setup_singles([played_pick, pick(2)], {"e1": {("h2h", "HOME", None): 2.10}, "e2": {("h2h", "HOME", None): 1.5}})
    money.set_daily(5000)
    row = next(r for r in track.load() if r["key"] == played_pick["key"])
    assert row["odds"] == 2.2 and row["xodds"] == 2.10 and row["cur"] == "FCFA", row
    with Fakes() as f:
        # une mise conseillée (budget du jour) n'est pas un pari joué : tant que tu ne l'as pas noté, aucune bonne nouvelle
        run_track([pick(1, odds=1.86, prob=0.6, prob_book=0.6)], {"e1": {("h2h", "HOME", None): 2.10}})
        assert not f.sent, f.sent
        rows = track.load()
        row = next(r for r in rows if r["key"] == played_pick["key"])
        row["played"], row["closed_at"] = {"odds": 2.10, "stake": 1000}, None
        track.save(rows)
        run_track([pick(1, odds=1.86, prob=0.6, prob_book=0.6)], {"e1": {("h2h", "HOME", None): 2.10}})
        msg = str(f.sent[0])
        assert "✅ Bonne nouvelle" in msg and "Home1 – Away1" in msg, msg
        assert "tu as pris <b>2,10</b>, le marché est maintenant à <b>1,86</b> (+13 % de valeur prise)" in msg, msg
        assert "💰" not in msg and not f.coupons, "pari joué : pas de « joue maintenant »"
        # noté joué à 2,05 : c'est cette cote qui compte
        store.save(xalerts.FILE, {})
        rows = track.load()
        row = next(r for r in rows if r["key"] == played_pick["key"])
        row["played"], row["closed_at"] = {"odds": 2.05, "stake": 1000}, None
        track.save(rows)
        run_track([pick(1, odds=1.86, prob=0.6, prob_book=0.6)], {})
        assert "tu as pris <b>2,05</b>" in str(f.sent[1]) and "+10 % de valeur prise" in str(f.sent[1]), f.sent[1]
        # pari pas joué (« ❌ Pas joué »), ou marché qui a peu bougé : rien
        store.save(xalerts.FILE, {})
        rows = track.load()
        row = next(r for r in rows if r["key"] == played_pick["key"])
        row["skipped"], row["closed_at"] = True, None
        track.save(rows)
        n = len(f.sent)
        run_track([pick(1, odds=1.86, prob=0.6, prob_book=0.6)], {})
        rows = track.load()
        row = next(r for r in rows if r["key"] == played_pick["key"])
        row["skipped"], row["closed_at"] = False, None
        track.save(rows)
        run_track([pick(1, odds=2.1, prob=0.6, prob_book=0.6)], {})
    assert len(f.sent) == n, f.sent[n:]
    print("ok  F4 ✅ bonne nouvelle : pari joué dont le marché a baissé de 10 % depuis le matin (cote prise, valeur prise), rien sinon")


# ---------- ⚠️ mes paris en danger / 📈 mieux payé ----------

def morning_real():
    """Le matin complet sur des paris réels à la cote 1xBet (pari du jour e4, combiné Sûr e1..e4) et trois pronostics 💎."""
    reset()
    picks = [pick(i) for i in range(1, 5)]
    xprice.price_picks(picks, fetch=fake_fetch({f"e{i}": {("h2h", "HOME", None): XB} for i in range(1, 5)}))
    codes = iter(f"CODE{n}" for n in range(1, 20))
    with Fakes() as f:
        xbet.save_coupon = lambda legs: next(codes)
        msgs, sent_picks, sent_combos = app.build_day(picks, DAY, odds_time="09:30")
    track.record(picks, {c["key"] for c in sent_picks}, DAY, at(4), single_key=app.single_key(sent_picks))
    settle.record_combos(sent_combos, DAY)
    pronos = [prono(i) for i in (7, 8, 9)]
    store.save("today.json", {"day": DAY, "picks": picks, "pronos": pronos, "picks_at": "09:30", "xbet_at": "09:30"})
    return picks


def refresh_fetch(calls=None, m7=1.55, e3=1.5):
    """Relevé de 15:00 : trois pronostics (m7 à `m7`) ; e3 n'est lu qu'en direct par les alertes."""
    px = {f"m{i}": {("totals", "Under", 3.5): 1.55} for i in (7, 8, 9)} | {"m7": {("totals", "Under", 3.5): m7}}
    return fake_fetch(px | {"e3": {("h2h", "HOME", None): e3}}, calls)


def refresh(fetch, now=None):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        app.run_xbet_refresh(now=now or at(15), announce=True, fetch=fetch)


def test_danger_and_rise():
    morning_real()
    # relevé de 15:00 : e1 n'est plus chez 1xBet, e2 paie 1,20 (plus d'avantage), e4 monte à 1,95 ; e3 pas en cache (relu en direct) ; m7 monte à 1,70
    cache_event(9001, [(2, 3.4)])
    cache_event(9002, [(1, 1.2)])
    cache_event(9004, [(1, 1.95)])
    calls = []
    with Fakes() as f:
        refresh(refresh_fetch(calls, m7=1.70))
        al = f.alerts()
        assert len(al) == 1, f.sent
        msg = str(al[0])
        assert msg.index("⚠️ Mes paris en danger") < msg.index("📈 Mieux payé chez 1xBet qu'au premier relevé"), msg
        assert "Le combiné Sûr n'est plus jouable tel quel" in msg, msg
        assert "❌ Plus chez 1xBet : Home1 – Away1 (19h00 · Liga) — Home1 gagne" in msg, msg
        assert "📉 Moins bien payé : Home2 – Away2" in msg and "1,50 ➜ <b>1,20</b> chez 1xBet" in msg, msg
        assert "Home3" not in msg, "e3 relu en direct, inchangé"
        assert "Si tu ne l'as pas encore joué : touche « 🎟️ Vérifier ce combiné » ou demande un combi." in msg, msg
        assert buttons(al[0]) == [f"xmajr|sur|{DAY}"], buttons(al[0])
        assert "Le pari du jour" not in msg, "le pari du jour (e4) a monté, il n'est pas en danger"
        # 📈 : le pronostic 💎 m7 (1,55 ➜ 1,70, relevé de 15:00) et le pari du jour e4 (1,50 ➜ 1,95, lu dans le cache), chacun avec son code coupon
        assert "H7 – A7" in msg and "1xBet payait 1,55 au premier relevé du jour, il paie maintenant <b>1,70</b> (+10 %, lu à 15:00)" in msg, msg
        assert "Home4 – Away4" in msg and "1xBet payait 1,50 au premier relevé du jour, il paie maintenant <b>1,95</b> (+30 %" in msg, msg
        assert "H8" not in msg and "H9" not in msg, "cote inchangée : pas d'alerte"
        assert msg.count("Code : <code>K7Q2X</code>") == 2 and len(f.coupons) == 2, f.coupons
        assert sorted(c[0]["xodds"] for c in f.coupons) == [1.70, 1.95], f.coupons
        assert [c for c in calls if any(i.startswith("e") for i in c)] == [["e3"]], f"seul e3 relu en direct : {calls}"
        # dédoublonnage : même relevé, même état -> rien
        n = len(f.sent)
        refresh(refresh_fetch(m7=1.70), at(15, 30))
        assert len(f.alerts()) == 1 and len(f.sent) == n, f.sent[n:]
        # m7 bouge encore de plus de 5 % : seule cette alerte revient ; un petit mouvement (< 5 %) ne revient pas
        refresh(refresh_fetch(m7=1.74), at(16))
        assert len(f.alerts()) == 1, f.alerts()
        refresh(refresh_fetch(m7=1.95), at(16, 30))
        last = str(f.alerts()[-1])
        assert len(f.alerts()) == 2 and "H7 – A7" in last and "⚠️" not in last and "Home4" not in last and "<b>1,95</b>" in last, last
    print("ok  F2 ⚠️ combiné réel (pari retiré / moins bien payé) avec bouton « Vérifier », F3 📈 mieux payé (💎 et pari réel) avec codes coupon, "
          "ordre ⚠️ puis 📈, seul e3 relu en direct, dédoublonnage")


def test_danger_pari_du_jour():
    picks = morning_real()
    cache_event(9004, [(2, 3.0)])                   # le pari du jour (e4) n'est plus chez 1xBet
    with Fakes() as f:
        refresh(refresh_fetch())
        msg = str(f.alerts()[0])
    assert "Le pari du jour n'est plus jouable tel quel" in msg and "Le combiné Sûr n'est plus jouable tel quel" in msg, msg
    assert f"xmajr|pari|{DAY}" in buttons(f.alerts()[0]) and f"xmajr|sur|{DAY}" in buttons(f.alerts()[0])
    # un combiné dont un match commence dans moins de 10 min (retiré de l'avant-match par 1xBet) n'est pas jugé
    store.save(xalerts.FILE, {})
    with Fakes() as f:
        refresh(refresh_fetch(), datetime(2030, 1, 1, 18, 55, tzinfo=timezone.utc))
    assert not f.alerts(), f.sent
    # un pari que l'utilisateur a retiré (« ❌ Pas joué ») n'est plus surveillé
    store.save(xalerts.FILE, {})
    bets = store.load("bets.json", {})
    bets["combos"][0]["skipped"] = True
    store.save("bets.json", bets)
    rows = track.load()
    for r in rows:
        r["skipped"] = True
    track.save(rows)
    with Fakes() as f:
        refresh(refresh_fetch())
    assert not f.alerts(), f.sent
    print("ok  F2 ⚠️ pari du jour retiré par 1xBet, matchs imminents et paris retirés ignorés")


def test_fair_combo_changed():
    reset()
    legs = [prono(i) for i in range(4)]
    fair.record_combos([(5, (legs, 1.55 ** 4, 0.72 ** 4))], DAY, coupons={5: "FAIRC"}, at="04:12")
    store.save("today.json", {"day": DAY, "picks": [], "pronos": [prono(i) for i in range(7)]})
    px = {f"m{i}": ({} if i == 1 else {("totals", "Under", 3.5): 1.55}) for i in range(7)}       # m1 : plus de plus/moins chez 1xBet
    with Fakes() as f:
        refresh(fake_fetch(px))
    msg = str(f.sent[0])
    assert msg.startswith("⚠️ Ton combiné a changé") and "🔄 COMBINÉ À JOUR 15:00" in msg and "❌ Retiré : H1 – A1" in msg, msg
    print("ok  F2 combiné 💎 renvoyé : « ⚠️ Ton combiné a changé » quand un pari est retiré")


# ---------- un message par passage ----------

def test_grouping_and_max():
    reset()
    items = ([xalerts._item("good", f"g{i}", 1.5, f"BONNE{i}", rank=i) for i in range(3)]
             + [xalerts._item("rise", f"r{i}", 1.5, f"HAUSSE{i}", rank=i) for i in range(2)]
             + [xalerts._item("danger", "d0", 1.5, "DANGER0"), xalerts._item("lag", "l0", 1.5, "RETARD0")])
    with Fakes() as f:
        msg = str(xalerts.send(items, DAY, at(17)))
    assert len(f.sent) == 1 and msg == str(f.sent[0])
    assert msg.index("RETARD0") < msg.index("DANGER0") < msg.index("HAUSSE1") < msg.index("HAUSSE0") < msg.index("BONNE2") < msg.index("BONNE1"), msg
    assert msg.index("💰 1xBet est en retard") < msg.index("⚠️ Mes paris en danger") < msg.index("📈 Mieux payé") < msg.index("✅ Bonne nouvelle"), msg
    assert sum(msg.count(w) for w in ("RETARD", "DANGER", "HAUSSE", "BONNE")) == C.XALERT_MAX_ITEMS == 6 and "BONNE0" not in msg, msg
    assert "+ 1 autre alerte au prochain passage" in msg, msg
    assert len(store.load(xalerts.FILE, {})["items"]) == 6, "seules les alertes montrées sont retenues"
    # celle qui n'a pas été montrée part au passage suivant, les 6 déjà envoyées jamais
    with Fakes() as f:
        again = str(xalerts.send(items, DAY, at(18)))
        assert "BONNE0" in again and again.count("BONNE") == 1 and "RETARD" not in again and "autre alerte" not in again, again
        # une alerte déjà envoyée ne revient que si sa cote a bougé d'au moins le seuil (5 %, 10 % pour ✅)
        assert xalerts.send([xalerts._item("rise", "r0", 1.54, "X")], DAY, at(19)) is None
        assert xalerts.send([xalerts._item("rise", "r0", 1.60, "HAUSSE0 BIS")], DAY, at(19)) is not None
        assert xalerts.send([xalerts._item("good", "g1", 1.4, "X")], DAY, at(19)) is None
        assert xalerts.send([xalerts._item("good", "g1", 1.3, "BONNE1 BIS")], DAY, at(19)) is not None
        # nouveau jour : tout est de nouveau possible
        assert xalerts.send(items[:1], "2030-01-02", at(17)) is not None
    print("ok  un message par passage : 💰 ⚠️ 📈 ✅ dans l'ordre, 6 alertes au plus, le reste au passage suivant, dédoublonnage par pari et par jour")


def main():
    test_morning_price_kept()
    test_lag()
    test_no_lag_when_xbet_followed()
    test_live_cap()
    test_good_news()
    test_danger_and_rise()
    test_danger_pari_du_jour()
    test_fair_combo_changed()
    test_grouping_and_max()
    print("tests alertes 1xBet : OK")


if __name__ == "__main__":
    main()
