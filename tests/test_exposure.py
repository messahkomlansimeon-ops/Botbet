"""Matchs répétés dans tes tickets joués (kelly/exposure.py) : « combi » écarte les matchs de tes tickets du jour (règles de réutilisation) et le dit,
« 🔀 Un autre sans mes matchs » n'en reprend aucun, la note d'un ticket alerte sur les matchs partagés, « 🧾 Mes paris » montre les matchs dont
dépendent plusieurs tickets. Hors ligne. Lancer : python -m tests.test_exposure"""
from tests import test_combi_track as T     # noqa: I001 - fixe DATA_DIR et les faux réseaux avant d'importer kelly
from tests import test_combi_kinds as K     # noqa: E402
from kelly import app, chat, custom, exposure, money, played, store  # noqa: E402

DAY = T.DAY


def setup():
    """Pronostics du jour : 4 « moins de 3,5 buts » (m0-m3) et 4 « plus de 22,5 fautes » (f0-f3) ; budget 5 000 FCFA."""
    K.setup()
    money.set_daily(5000)


def play(legs, odds=None):
    """Un combi sur mesure sur `legs`, suivi et noté joué (100 FCFA) -> son identifiant « m<n> »."""
    cid = T.keep_custom(legs, stake=100, cur="FCFA")
    row = next(r for r in __import__("kelly").fair.load_combos() if str(r["tier"]) == cid)
    if not row.get("played"):
        played.command([f"v{cid}", str(odds or row["min_odds"]).replace(".", ","), "100"], DAY)
    return cid


def test_tickets_and_summary():
    setup()
    play(T.pronos(4))                                  # m0..m3
    play(T.pronos(3) + [K.fouls(0)])                   # m0..m2 + f0
    ts = exposure.tickets(DAY)
    assert [t["what"] for t in ts] == ["vm1", "vm2"] and all(t["stake"] == 100 for t in ts), ts
    assert exposure.played_ids(DAY) == {"m0", "m1", "m2", "m3", "f0"}
    lines = exposure.summary_lines(DAY)
    assert lines[0].startswith("🔗 Matchs dans plusieurs tickets (au moins un ticket gagnant : ") and "• H0 – A0 : 2 tickets · 200 FCFA" in lines, lines
    assert "🔗 Matchs dans plusieurs tickets" in played.todo(DAY)
    p = exposure.chance_any(ts)
    assert 0.72 ** 4 * 0.9 < p < 0.72 ** 3 + 0.01, p                 # deux tickets très liés : à peine plus que le meilleur seul
    print("ok  tickets joués du jour, matchs partagés et argent en jeu dans « Mes paris »")


def test_overlap_alert():
    setup()
    play(T.pronos(4))
    out = played.command(["vm2", "1"], DAY)            # rien à noter : pas d'alerte
    assert "en commun" not in out
    cid = T.keep_custom(T.pronos(2) + [K.fouls(0), K.fouls(1)], stake=100, cur="FCFA")
    out = played.command([f"v{cid}", "5", "100"], DAY)
    assert out.startswith("✅ Noté") and "\n\n🔗 2 matchs partagés avec 1 autre ticket\n   H0 – A0, H1 – A1\n   S'ils ratent, plusieurs tickets tombent ensemble.\n" in out, out
    assert "🎯 Chances : ce ticket " in out and " · au moins un de tes 2 tickets " in out, out
    cid = T.keep_custom([K.fouls(2), K.fouls(3)], stake=100, cur="FCFA")
    assert "en commun" not in played.command([f"v{cid}", "2,25", "100"], DAY), "aucun match partagé : pas d'alerte"
    print("ok  alerte à la note d'un ticket qui partage des matchs avec tes autres tickets")


def test_combi_warns_played():
    """combi garde les meilleurs paris même s'ils sont dans tes tickets joués (rien n'est retiré) et le dit ; « 🔀 Un autre sans mes matchs » pour varier."""
    setup()
    play([K.fouls(i, prob=0.65) for i in range(4)])    # tes matchs f0..f3 joués
    with K.Fakes():
        out = app.handle_command("combi 5 fautes")
    item = store.load(custom.FILE, {})["items"]["m2"]
    assert {l["id"] for l in item["legs"]} & {"f0", "f1", "f2", "f3"}, "rien n'est retiré"
    assert "</b> · 🔗1\n" in out and "<i>🔗 déjà dans N de tes tickets</i>" in out and "🔀 Un autre sans mes matchs" in str(out.markup), out
    assert "Laissés de côté" not in out and "cnew|m2|" in str(out.markup)
    setup()
    with K.Fakes():
        out = app.handle_command("combi 5 fautes")
    assert "🔗" not in out, "aucun ticket joué : pas d'avertissement"
    print("ok  combi : les meilleurs paris restent proposés, ceux déjà dans tes tickets sont signalés")


def test_another():
    setup()
    with K.Fakes():
        first = app.handle_command("combi 2")
    item = store.load(custom.FILE, {})["items"]["m1"]
    shown = {l["id"] for l in item["legs"]}
    with K.Fakes():
        out = chat.on_callback("cnew|m1|" + DAY, 4)
    second = store.load(custom.FILE, {})["items"].get("m2")
    assert second and not {l["id"] for l in second["legs"]} & shown, (first, out)
    assert "🔀 Sans les" in out and "du combiné précédent" in out, out
    assert app.another_combo("m9", DAY) == custom.GONE
    # la vérification en direct ne remet jamais un match évité comme remplaçant
    seen = {}
    old = app._live_fair
    app._live_fair = lambda legs, lo, hi, *a, **k: (seen.update(k), (None, "10:00", True))[1]
    try:
        with K.Fakes():
            app.custom_combo([], app.upcoming(store.load("today.json", {})["pronos"]), 2.0, DAY, avoid={"m0"})
    finally:
        app._live_fair = old
    assert seen.get("avoid") == {"m0"}, seen
    # échec sans tes matchs : « 🔀 Cote X sans mes matchs » (le plus haut) et « 🎯 Cote N avec mes matchs », jamais un « /combi X » qui les reprendrait
    with K.Fakes():
        out = app.custom_combo([], app.upcoming(store.load("today.json", {})["pronos"]), 500.0, DAY, avoid={"m0"}, again="m1")
    data = T.buttons(out)
    assert data[0].startswith(f"cnew|m1|{DAY}|") and data[-1] == "cgo|500|" and "\n❌ Pas de combiné à la cote 500 sans tes matchs.\n📉 Avec les bons paris du jour, la cote monte au plus à" in out, (data, out)
    assert "demande au plus /combi" not in out
    print("ok  « 🔀 Un autre sans mes matchs » : aucun match du combi affiché ni de tes tickets")


def main():
    test_tickets_and_summary()
    test_overlap_alert()
    test_combi_warns_played()
    test_another()
    print("tests matchs partagés : OK")


if __name__ == "__main__":
    main()
