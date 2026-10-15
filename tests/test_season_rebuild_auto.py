"""« saison auto » / « 🔄 Reconstruire » (hors ligne, faux 1xBet) : la construction automatique du jour est relancée SANS rien détruire avant d'avoir un remplaçant ; les combinés du matin non joués sont
fermés (« reconstruit »), un combiné noté joué est gardé et ses matchs restent consommés, au plus 4 combinés actifs, écart minimal entre deux reconstructions, mises dans ce qu'il reste du jour ; « saison »
liste les combinés avec leur bouton « Rouvrir » (sans réseau) et la mention « construit avant la dernière mise à jour ». Lancer : python -m tests.test_season_rebuild_auto"""
import itertools
from datetime import timedelta

from tests import test_notes_fcfa as N      # fixe DATA_DIR (dossier temporaire), monde de test
from tests import test_season_lane as T
from kelly import app, chat, combos as CB, config as C, fair, lanes, money, played, seasonlane as SL, store, telegram  # noqa: E402
from kelly.sources import xbet  # noqa: E402

DAY, NOW = str(T.DAY), T.NOW
KO = T.KO


def leg(i, p, odds, sval=True):
    return {"id": f"m{i}", "key": f"m{i}|ou25||2.5|Over|saison", "lane": "saison", "strategy": "saison", "sval": sval, "prob": p, "odds": odds, "xodds": odds, "xref": f"r{i}", "xgid": i,
            "market": "ou25", "name": "Over", "point": 2.5, "home": f"H{i}", "away": f"A{i}", "sport_key": "soccer_x", "league": "X", "commence": KO, "pick": "Plus de 2,5 buts", "min_odds": odds,
            "conf": 0.8, "start": KO}


def pool(n=30, fill=0, seed=0):
    legs = [leg(i, 0.72 - 0.004 * i, round((1.04 + 0.002 * (i % 7)) / (0.72 - 0.004 * i), 2)) for i in range(n)]
    return legs + [leg(n + j, 0.74, 1.30, sval=False) for j in range(fill)]


INFO = {"events": 30, "cup_events": 0, "predicted": 30, "read": 30, "cup_read": 0, "no_price": 0, "unread": 0, "no_model": 0, "other": 0, "few": set(), "unknown": set()}
CODES = itertools.count(1)
STATE = {"legs": [], "info": dict(INFO), "calls": 0}


def fake_rebuild(now, fetch=None):
    STATE["calls"] += 1
    return list(STATE["legs"]), dict(STATE["info"])


def setup(daily=20000):
    N.reset()
    C.NOVALUE_SEND = False
    C.SEASON_SEND_FILL = True
    C.SEASON_AUTO, C.SEASON_AUTO_MIN_EV, C.SEASON_AUTO_MAX_ODDS, C.SEASON_AUTO_MAX_COMBOS, C.SEASON_AUTO_MIN_P = True, -0.15, 150.0, 4, 0.25
    C.SEASON_REBUILD_GAP = 5
    C.STRATEGY_SEASON = "on"
    store.save(fair.GHOST_COMBOS_FILE, [])
    store.save("fair_combos.json", [])
    store.remove(app.SEASON_REBUILD_FILE) if app.SEASON_REBUILD_FILE in store.names("") else None
    money.set_daily(daily)
    SL.rebuild = fake_rebuild
    xbet.save_coupon = lambda legs: f"CODE{next(CODES)}"
    telegram.send = lambda *a, **k: True
    STATE.update(legs=pool(), info=dict(INFO), calls=0)
    # le matin : quatre combinés `saison` enregistrés
    legs = pool()
    plan = [(t, c) for t, c in CB.season_plan(legs, usage=CB.Usage()) if c]
    assert len(plan) == 4, [t for t, _ in plan]
    fair.record_combos(plan, DAY, {t: 500 for t, _ in plan}, money.cur(), coupons={t: f"M{n}" for n, (t, _) in enumerate(plan)}, at="09:30")
    return plan


def today():
    return {"day": DAY, "season": [], "picks": [], "pronos": []}


def rows_of(day=DAY):
    return [r for r in fair.load_combos() if str(r.get("date")) == day]


def test_replace_unplayed():
    old = setup()
    old_tiers = [t for t, _ in old]
    STATE["legs"] = pool(seed=1)[5:]                 # d'autres jambes (les premières sont parties)
    reply = app.season_command(["auto"], today(), now=NOW)
    rows = rows_of()
    closed = [r for r in rows if r.get("closed")]
    active = [r for r in rows if not r.get("closed")]
    assert {r["tier"] for r in closed} == set(old_tiers) and all(r["dead"] and r["result"] == "void" and r["closed"] == "reconstruit" and r["closed_at"] == NOW.strftime("%H:%M") for r in closed)
    assert 1 <= len(active) <= 4 and all(r["origin"] == "reconstruit" and r["built_at"] and r["sbuild"] == CB.season_signature() for r in active), active
    assert not ({r["tier"] for r in active} & set(old_tiers)), "aucun palier repris à un combiné fermé"
    assert len(reply.more) == len(active) and "remplacé" in str(reply) and "🔎" in str(reply), str(reply)
    ids = [l["id"] for r in active for l in r["legs"]]
    assert len(ids) == len(set(ids)), "aucune jambe partagée"
    assert set(app._recorded_season(DAY)) == {r["tier"] for r in active}, "une relance du matin ne renverrait pas les combinés fermés"
    usage = app.day_usage(DAY)
    assert not any(usage.by_leg.get(CB.leg_id(l)) for r in closed for l in r["legs"] if l["key"] not in {x["key"] for a in active for x in a["legs"]}), "les matchs des combinés fermés sont libérés"
    spent = sum(r.get("stake") or 0 for r in active)
    assert spent <= money.daily() - 0, spent
    print("ok  « saison auto » : combinés du matin non joués fermés (« reconstruit », heure), nouveaux combinés enregistrés et envoyés, rien de repris aux fermés, aucune jambe partagée")


def test_noted_kept_and_cap():
    old = setup()
    rows = fair.load_combos()
    noted = next(r for r in rows if r["tier"] == old[0][0])
    noted["played"] = {"odds": noted["min_odds"], "stake": 500, "at": "x"}
    store.save(fair.COMBOS_FILE, rows)
    kept_ids = {l["id"] for l in noted["legs"]}
    STATE["legs"] = pool()
    app.season_command(["auto"], today(), now=NOW)
    rows = rows_of()
    active = [r for r in rows if not r.get("closed")]
    assert any(r["tier"] == old[0][0] and not r.get("closed") and not r.get("dead") for r in rows), "le combiné noté joué est gardé"
    assert len(active) <= C.SEASON_AUTO_MAX_COMBOS, "plafond de 4 combinés actifs, gardés compris"
    new = [r for r in active if r["tier"] != old[0][0]]
    assert new and not (kept_ids & {l["id"] for r in new for l in r["legs"]}), "les matchs du combiné gardé restent consommés"
    # quatre combinés notés : refus, rien ne change
    for r in rows:
        if not r.get("closed"):
            r["played"] = {"odds": r["min_odds"], "stake": 100, "at": "x"}
    rows = [r for r in rows if not r.get("closed")]
    while len(rows) < 4:
        extra = dict(rows[0], tier=f"ssa{90 + len(rows)}")
        rows.append(extra)
    store.save(fair.COMBOS_FILE, rows)
    store.save(app.SEASON_REBUILD_FILE, {})
    before = store.load(fair.COMBOS_FILE, [])
    out = app.season_command(["auto"], today(), now=NOW + timedelta(hours=1))
    assert "plafond" in str(out) and store.load(fair.COMBOS_FILE, []) == before
    print("ok  combiné noté joué gardé (ses matchs restent consommés), plafond de 4 combinés actifs gardés compris, quatre notés = refus sans rien changer")


def test_failure_keeps_old():
    setup()
    before = store.load(fair.COMBOS_FILE, [])
    STATE["legs"], STATE["info"] = [], dict(INFO, read=0)
    out = str(app.season_command(["auto"], today(), now=NOW))
    assert "restent" in out and store.load(fair.COMBOS_FILE, []) == before, out
    STATE["legs"], STATE["info"] = pool(1), dict(INFO)
    store.save(app.SEASON_REBUILD_FILE, {})
    out = str(app.season_command(["auto"], today(), now=NOW + timedelta(hours=1)))
    assert "restent" in out and store.load(fair.COMBOS_FILE, []) == before, "moins de 2 jambes : rien n'est détruit"
    print("ok  1xBet muet ou moins de 2 jambes : les combinés du matin restent intacts, la ligne 🔎 explique")


def test_gap_off_and_buttons():
    setup()
    STATE["legs"] = pool()
    app.season_command(["auto"], today(), now=NOW)
    again = str(app.season_command(["auto"], today(), now=NOW + timedelta(minutes=2)))
    assert "attends encore" in again and STATE["calls"] == 1, again
    app.season_command(["auto"], today(), now=NOW + timedelta(minutes=6))
    assert STATE["calls"] == 2
    C.STRATEGY_SEASON = "off"
    try:
        assert "éteinte" in app.season_command(["auto"], today(), now=NOW + timedelta(hours=2))
    finally:
        C.STRATEGY_SEASON = "on"
    C.SEASON_AUTO = False
    try:
        assert "éteinte" in app.season_command(["auto"], today(), now=NOW + timedelta(hours=2))
    finally:
        C.SEASON_AUTO = True
    store.save("today.json", today())
    assert "autre jour" in chat.on_callback("sa|rb|2020-01-01")
    assert chat.route("saison auto")[0] == "/saison auto"
    print("ok  écart minimal entre deux reconstructions, voie éteinte / mode fixe refusés, bouton d'un autre jour refusé, mot « saison auto » routé")


def test_listing_buttons_open_and_old_mention():
    setup()
    rows = fair.load_combos()
    rows[0].pop("sbuild", None)                      # construit avant la mise à jour : pas d'empreinte
    rows[1]["sbuild"] = "autre"
    store.save(fair.COMBOS_FILE, rows)
    rows_closed = fair.load_combos()
    rows_closed[3]["closed"], rows_closed[3]["dead"], rows_closed[3]["closed_at"] = "reconstruit", True, "08:00"
    store.save(fair.COMBOS_FILE, rows_closed)
    out = app.season_command([], today(), now=NOW)
    txt = str(out)
    labels = [b["text"] for row in out.markup["inline_keyboard"] for b in row]
    datas = [b.get("callback_data") for row in out.markup["inline_keyboard"] for b in row]
    assert txt.count("construit avant la dernière mise à jour") == 2 and "1 combiné remplacé" in txt, txt
    assert sum(1 for t in labels if t.startswith("🔓 Rouvrir")) == 3 and "🔄 Reconstruire" in labels and f"sa|rb|{DAY}" in datas
    assert all(len(d.encode()) <= 64 for d in datas if d), datas
    # rouvrir : aucun réseau
    SL.rebuild = lambda *a, **k: (_ for _ in ()).throw(AssertionError("réseau"))
    xbet.save_coupon = lambda legs: (_ for _ in ()).throw(AssertionError("réseau"))
    tier = fair.load_combos()[0]["tier"]
    msg = chat.on_callback(f"so|{tier}|{DAY}")
    assert "SAISON · COMBINÉ" in str(msg) and "Construit avant la dernière mise à jour" in str(msg) and msg.markup, str(msg)[:400]
    closed_msg = chat.on_callback(f"so|{fair.load_combos()[3]['tier']}|{DAY}")
    assert "remplacé à 08:00" in str(closed_msg)
    print("ok  « saison » : un bouton Rouvrir par combiné actif (sans réseau), 🔄 Reconstruire, mention « construit avant la dernière mise à jour », remplacés comptés")


def test_closed_row_guards_and_reopen_on_note():
    old = setup()
    STATE["legs"] = pool()[3:]
    app.season_command(["auto"], today(), now=NOW)
    closed = next(r for r in rows_of() if r.get("closed"))
    out = app.refresh_combo(closed["tier"], DAY, now=NOW)
    assert "remplacé" in out, out
    t = played.find(f"v{closed['tier']}", DAY)
    assert t is not None and not isinstance(t, str)
    played.note(t, closed["min_odds"], 500)
    back = next(r for r in rows_of() if r["tier"] == closed["tier"])
    assert back["played"] and not back.get("dead") and not back.get("closed") and back["result"] is None and back.get("reopened"), back
    print("ok  combiné remplacé : « Vérifier » refusé avec l'heure ; noté joué malgré tout, il est rouvert et suivi (la note est la vérité)")


def test_stakes_within_day():
    setup(daily=3000)
    rows = fair.load_combos()
    for r in rows:
        r["stake"] = 700                             # 2 800 engagés ce matin sur 3 000
    store.save(fair.COMBOS_FILE, rows)
    STATE["legs"] = pool()
    app.season_command(["auto"], today(), now=NOW)
    new = [r for r in rows_of() if not r.get("closed")]
    # tout est remplaçable : les mises engagées retombent, le budget du jour est entier
    assert sum(r.get("stake") or 0 for r in new) <= 3000 + 1e-6
    rows = rows_of()
    noted = [r for r in rows if not r.get("closed")][:1]
    for r in noted:
        r["played"] = {"odds": 2.0, "stake": 2800, "at": "x"}
        r["stake"] = 2800
    store.save(fair.COMBOS_FILE, rows)
    store.save(app.SEASON_REBUILD_FILE, {})
    app.season_command(["auto"], today(), now=NOW + timedelta(hours=1))
    active = [r for r in rows_of() if not r.get("closed")]
    assert sum(r.get("stake") or 0 for r in active) <= 3000 + 1e-6, [(r["tier"], r.get("stake")) for r in active]
    print("ok  mises : jamais plus que ce qu'il reste du budget du jour (combinés gardés comptés)")


def test_review_fixes():
    old = setup()
    # un combiné déjà réglé n'est jamais remplacé ni son résultat écrasé
    rows = fair.load_combos()
    rows[0]["result"] = "loss"
    store.save(fair.COMBOS_FILE, rows)
    STATE["legs"] = pool()
    app.season_command(["auto"], today(), now=NOW)
    r0 = next(r for r in rows_of() if r["tier"] == old[0][0])
    assert r0["result"] == "loss" and not r0.get("closed"), r0
    assert sum(1 for r in rows_of() if not r.get("closed")) <= 4
    # « Mes paris » ne montre pas les combinés remplacés
    todo = str(played.todo(DAY))
    closed = [r for r in rows_of() if r.get("closed")]
    assert closed and "plus jouable" not in todo and todo.count("Saison en cours") == len([r for r in rows_of() if not r.get("closed")]), todo
    # un plan fait seulement de combinés sans valeur (fantômes) : rien n'est détruit
    old = setup()
    C.SEASON_SEND_FILL = False
    before = store.load(fair.COMBOS_FILE, [])
    STATE["legs"] = pool(0, fill=20)
    out = str(app.season_command(["auto"], today(), now=NOW))
    C.SEASON_SEND_FILL = True
    assert "à envoyer" in out and store.load(fair.COMBOS_FILE, []) == before, out
    # un fantôme fermé n'empêche pas d'enregistrer le même fantôme reconstruit
    legs = pool(0, fill=4)
    combo = ([dict(l) for l in legs[:2]], 1.7, 0.5)
    fair.ghost_record("ssa1.7", combo, DAY, "saison")
    g = fair.load_ghost()
    g[0]["closed"] = "reconstruit"
    store.save(fair.GHOST_COMBOS_FILE, g)
    fair.ghost_record("ssa1.7", combo, DAY, "saison")
    assert len(fair.load_ghost()) == 2 and sum(1 for x in fair.load_ghost() if not x.get("closed")) == 1
    # la mise engagée d'un combiné gardé est la mise NOTÉE
    r = {"date": DAY, "tier": "ssa9", "stake": 500, "played": {"odds": 3, "stake": 2000, "at": "x"}}
    assert app._committed(DAY, [r]) >= 2000
    # « Rouvrir » un combiné noté : l'état réel dans les boutons
    old = setup()
    rows = fair.load_combos()
    rows[0]["played"] = {"odds": rows[0]["min_odds"], "stake": 500, "at": "x"}
    store.save(fair.COMBOS_FILE, rows)
    msg = chat.on_callback(f"so|{rows[0]['tier']}|{DAY}")
    labels = [b["text"] for row in msg.markup["inline_keyboard"] for b in row]
    assert not any(t.startswith("✅ Je l'ai joué") for t in labels) and any("Joué à" in t for t in labels), labels
    print("ok  correctifs de relecture : combiné réglé gardé, plan de fantômes seuls = rien détruit, fantôme fermé ré-enregistrable, « Mes paris » sans remplacés, mise notée comptée, « Rouvrir » montre l'état réel")


def test_concurrency_and_failure():
    old = setup()
    victim = old[1][0]

    def noted_during(now, fetch=None):                # l'utilisateur note un combiné PENDANT la relecture 1xBet
        rows = fair.load_combos()
        next(r for r in rows if r["tier"] == victim)["played"] = {"odds": 3.0, "stake": 700, "at": "x"}
        store.save(fair.COMBOS_FILE, rows)
        return pool(), dict(INFO)
    SL.rebuild = noted_during
    app.season_command(["auto"], today(), now=NOW)
    v = next(r for r in rows_of() if r["tier"] == victim)
    assert v["played"]["stake"] == 700 and not v.get("closed") and not v.get("dead"), v
    # une panne après la fermeture : les combinés du matin sont rouverts
    old = setup()
    real = fair.record_combos

    def boom(*a, **k):
        raise OSError("base refusée")
    fair.record_combos = boom
    try:
        out = str(app.season_command(["auto"], today(), now=NOW))
    finally:
        fair.record_combos = real
    rows = rows_of()
    assert "interrompue" in out and all(not r.get("closed") and not r.get("dead") and r["result"] is None for r in rows) and len(rows) == 4, (out, rows)
    # panne APRÈS l'enregistrement des nouveaux (marquage impossible) : les nouveaux partent aussi, les fantômes sont rouverts
    old = setup()
    fair.ghost_record("ssa1.7", ([dict(l) for l in pool(0, fill=4)[:2]], 1.7, 0.5), DAY, "saison")
    real_update = store.update
    calls = []

    def flaky(name, fn, default):
        calls.append(name)
        if name == fair.COMBOS_FILE and len(calls) == 2:          # le 2e appel : le marquage des nouveaux
            raise OSError("base refusée")
        return real_update(name, fn, default)
    store.update = flaky
    try:
        out = str(app.season_command(["auto"], today(), now=NOW))
    finally:
        store.update = real_update
    rows = rows_of()
    assert "interrompue" in out and len(rows) == 4 and all(not r.get("closed") and not r.get("origin") for r in rows), (out, [(r["tier"], r.get("closed"), r.get("origin")) for r in rows])
    assert all(not g.get("closed") for g in fair.load_ghost()), "fantômes rouverts"
    print("ok  note arrivée pendant la relecture : jamais fermée ni écrasée ; panne après la fermeture : les combinés du matin sont rouverts")


def main():
    for t in (test_replace_unplayed, test_noted_kept_and_cap, test_failure_keeps_old, test_gap_off_and_buttons, test_listing_buttons_open_and_old_mention,
              test_closed_row_guards_and_reopen_on_note, test_stakes_within_day, test_review_fixes, test_concurrency_and_failure):
        t()
    print("tests saison auto (reconstruction) : OK")


if __name__ == "__main__":
    main()
