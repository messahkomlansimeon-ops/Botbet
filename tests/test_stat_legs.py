"""Jambes de stats (tirs, tirs cadrés, fautes, touches, hors-jeu) dans les combinés du matin et « combi N » : les matchs des championnats payés
(où 1xBet liste ces marchés) reçoivent des pronostics stats pour le relevé 1xBet, ces matchs passent en tête du relevé, today.json garde toute
jambe possible d'un combiné, et le palier « 🧮 COMBINÉ STATS » mélange les familles (un pari par match, mêmes règles de réutilisation).
Lancer : python -m tests.test_stat_legs"""
import os
import tempfile
from datetime import date

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-statlegs-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-statlegs-cache-")
os.environ["ODDS_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-statlegs-odds-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ["XBET_PROXY_FILE"] = os.path.join(os.environ["DATA_DIR"], "pas-de-fichier.env")
os.environ["XBET_PROXY"] = "http://user:secret@proxy.test:80"

from kelly import app, calib, combos as CB, config as C, fair, fmt, odds, played, store, xprice  # noqa: E402,F401

DAY = "2026-10-10"
STAT = ("team_fouls_total", "team_throws_home", "team_offsides_total", "team_shots_home", "team_sot_total")


def test_paid_match_gets_stat_families():
    """Match payé (cotes réelles de The Odds API) : jusqu'ici seulement corners et cartons, donc aucun pronostic stats pour Lens-Lyon, Dortmund-Brême
    ou Málaga-Espanyol, que 1xBet liste pourtant avec tirs, fautes, touches et hors-jeu (rejeu du 09/10). Relevé 1xBet : les stats s'ajoutent."""
    ev = {"id": "p1", "sport_key": "soccer_france_ligue_one", "home_team": "RC Lens", "away_team": "Lyon", "commence_time": f"{DAY}T18:45:00Z"}
    free = {"id": "f1", "sport_key": "soccer_france_ligue_two", "home_team": "Nancy", "away_team": "Guingamp", "commence_time": f"{DAY}T18:00:00Z"}
    opts = [("totals", "Over", 2.5, 0.70, "g"), ("alternate_totals_corners", "Over", 9.5, 0.66, "c"), ("alternate_totals_cards", "Under", 4.5, 0.66, "k")]
    opts += [(m, "Over", 7.5, 0.66, "s") for m in STAT]
    old_opts, old_num = fair._options, odds.match_numbers
    fair._options = lambda f, h, a: (opts, {})
    odds.match_numbers = lambda h, a, k: {"n_home": 8, "n_away": 8}
    picks = [{"id": "p1", "market": "h2h"}, {"id": "p1", "market": "totals"}]          # pari réel du match payé
    scan = {"leagues": {"soccer_france_ligue_one": [ev], "soccer_france_ligue_two": [free]}}
    try:
        every = fair.make(scan, picks, day=date.fromisoformat(DAY), all_families=True)
        plain = fair.make(scan, picks, day=date.fromisoformat(DAY))
        # une famille déjà présente dans les paris réels du match n'est pas refaite
        again = fair.make(scan, picks + [{"id": "p1", "market": "team_sot_total"}], day=date.fromisoformat(DAY), all_families=True)
        C.FAIR_STATS_PAID = False                     # interrupteur FAIR_STATS_PAID=0 : comportement d'avant
        try:
            off = fair.make(scan, picks, day=date.fromisoformat(DAY), all_families=True)
        finally:
            C.FAIR_STATS_PAID = True
    finally:
        fair._options, odds.match_numbers = old_opts, old_num
    assert sorted(c["market"] for c in off if c["id"] == "p1") == ["alternate_totals_cards", "alternate_totals_corners"]
    paid = sorted(c["market"] for c in every if c["id"] == "p1")
    assert paid == sorted(("alternate_totals_corners", "alternate_totals_cards") + STAT), paid         # pas de buts : les cotes réelles les ont déjà
    assert "totals" not in paid and "btts" not in paid and "double_chance" not in paid
    assert "team_sot_total" not in [c["market"] for c in again if c["id"] == "p1"], "déjà dans les paris réels du match"
    assert {c["market"] for c in plain if c["id"] == "p1"} <= {"alternate_totals_corners", "alternate_totals_cards", "team_shots_home", "team_sot_total"}, \
        "sans relevé 1xBet : comportement d'avant (corners, cartons, meilleur tirs, meilleur tirs cadrés)"
    assert "team_fouls_total" not in {c["market"] for c in plain if c["id"] == "p1"}
    assert {c["market"] for c in every if c["id"] == "f1"} >= set(STAT) | {"totals"}, "match non payé : toutes les familles, comme avant"
    print("ok  matchs payés : pronostics stats pour le relevé 1xBet")


def prono(i, market, p=0.66, name="Over", point=7.5, **kw):
    return {"key": f"m{i}|{market}||{point}|{name}", "id": f"m{i}", "sport_key": "soccer_epl", "home": f"H{i}", "away": f"A{i}",
            "commence": f"{DAY}T{12 + i % 8:02d}:00:00Z", "start": f"{12 + i % 8}:00", "league": "L", "market": market, "name": name,
            "point": point, "prob": p, "p0": p, "min_odds": round(1 / p * 1.05, 2), "conf": 0.6, "n_min": 8, "score": 0.1,
            "pick": fmt.label(market, name, point, None, "soccer"), "xodds": 1.95, "xev": round(p * 1.95 - 1, 4), "xgid": 9000 + i,
            "xref": [9500 + i, 12, point]} | kw


def test_fetch_order_stats_first():
    """Relevé 1xBet : les matchs dont on veut des sous-matchs stats (requêtes de plus) sont lus avant les autres, chacun dans l'ordre reçu ; si le délai
    tombe, ce sont eux qui valent le plus. Corners seuls ou rien : l'ordre d'avant."""
    from kelly.sources import state, xbet
    ev = lambda i: ("soccer_epl", {"id": f"m{i}", "home_team": f"H{i}", "away_team": f"A{i}", "commence_time": f"{DAY}T15:00:00Z", "xgid": 100 + i})  # noqa: E731
    events = [ev(1), ev(2), ev(3), ev(4), ev(5)]
    wanted = {"m1": {"totals", "alternate_totals_corners"}, "m2": {"team_fouls_total"}, "m3": {"alternate_totals_corners"},
              "m4": {"team_shots_home", "team_sot_total"}, "m5": {"team_offsides_total", "totals"}}
    assert [e["id"] for _, e in xbet.prioritise_stats(events, wanted)] == ["m2", "m4", "m5", "m1", "m3"]
    assert [e["id"] for _, e in xbet.prioritise_stats(events, None)] == ["m1", "m2", "m3", "m4", "m5"]
    assert [e["id"] for _, e in xbet.prioritise_stats(iter(events), {"m1": {"totals"}})] == ["m1", "m2", "m3", "m4", "m5"]
    # xprice.apply : relevé du matin (lecture par défaut) = stats d'abord ; relevé en direct (fetch fourni) = ordre de l'appelant
    pronos = [prono(1, "totals", score=0.9), prono(2, "team_fouls_total", score=0.2), prono(3, "alternate_totals_corners", score=0.5),
              prono(4, "team_shots_home", score=0.1)]
    seen = []
    old = xbet.fetch_day
    xbet.fetch_day = lambda events, deadline=None, wanted=None, **k: seen.append([e["id"] for _, e in events]) or {}      # aucun réseau
    try:
        xprice.apply(pronos)
        xprice.apply(pronos, fetch=lambda events, deadline=None, wanted=None: seen.append([e["id"] for _, e in events]) or {})
    finally:
        xbet.fetch_day = old
    assert seen == [["m2", "m4", "m1", "m3"], ["m1", "m3", "m2", "m4"]], seen
    print("ok  relevé 1xBet : matchs à sous-matchs stats lus d'abord")


def test_keep_pronos_keeps_every_combo_leg():
    """today.json : toute jambe possible d'un combiné reste, même au-delà de la limite de 300 pronostics lus (« combi N », relevé de 15:00)."""
    store.save(calib.FILE, {})
    calib._memo.clear()
    pronos = []
    for i in range(380):                              # beaucoup de pronostics trouvés chez 1xBet (xgid), classés devant, non viables (proba basse)
        pronos.append(prono(i, "totals", 0.5, point=2.5, name="Over", score=1.0 - i / 1000, xodds=None, xev=None, conf=0.1))
    best = prono(500, "team_sot_total", 0.70, score=0.05, xodds=2.5, xev=0.75)       # meilleur du match 500 : montré
    leg = prono(500, "team_fouls_total", 0.64, score=0.04, xodds=2.0, xev=0.28)       # autre jambe viable du même match, classée tout en bas
    pronos += [best, leg]
    assert leg["key"] in {c["key"] for c in CB.fair_legs(pronos)} and leg not in CB.best_per_match(pronos)
    kept = app.keep_pronos(pronos)
    assert leg in kept and best in kept and len(kept) < len(pronos), (len(kept), len(pronos))
    old = app.CB.fair_legs
    app.CB.fair_legs = lambda p: []                    # avant le correctif : la jambe est perdue au-delà de la limite
    try:
        assert leg not in app.keep_pronos(pronos)
    finally:
        app.CB.fair_legs = old
    print("ok  today.json garde toutes les jambes de combiné")


def test_stats_tier():
    """« 🧮 COMBINÉ STATS » : fautes, touches, hors-jeu, tirs et tirs cadrés mêlés, un pari par match, cote 5 à 7 ; titre, bouton, suivi."""
    assert [t[0] for t in C.FAIR_SHOTS_TIERS] == ["tc5", "tv5", "ts5"] and CB.shots_title("ts5") == "🧮 COMBINÉ STATS"
    pronos = []
    for i, m in enumerate(("team_fouls_total", "team_throws_home", "team_offsides_total", "team_fouls_home", "team_throws_total", "team_offsides_total")):
        pronos.append(prono(i, m, xodds=(2.1, 1.9, 1.6, 2.0, 1.7, 1.8)[i]))
    pronos += [prono(10, "totals"), prono(11, "alternate_totals_corners")]          # hors stats : jamais dans ce combiné
    plan = dict(CB.fair_shots_plan(pronos))
    assert plan["tc5"] is None and plan["tv5"] is None, "pas de tirs : les deux paliers d'avant ne sont pas bâtis"
    legs, total, prob = plan["ts5"]
    assert 5.0 <= total <= 7.0 and len(legs) >= 2 and len({c["id"] for c in legs}) == len(legs), "un seul pari par match"
    assert all(c["market"].startswith(("team_fouls_", "team_throws_", "team_offsides_")) for c in legs) and len({c["market"] for c in legs}) >= 2
    # tirs mêlés aux autres familles
    mixed = dict(CB.fair_shots_plan(pronos + [prono(20 + i, "team_shots_home", xodds=(2.1, 1.9, 1.6, 2.0)[i]) for i in range(4)]))
    assert mixed["tv5"] and all(c["market"].startswith("team_shots_") for c in mixed["tv5"][0]), "les tirs servent d'abord le palier « tirs »"
    assert mixed["ts5"] and not {c["id"] for c in mixed["ts5"][0]} & {c["id"] for c in mixed["tv5"][0]}, "jambes risquées : jamais deux combinés"
    # mêmes règles de réutilisation qu'ailleurs : un Usage partagé, une jambe risquée (proba < LEG_SAFE_PROB) dans un seul combiné
    u = CB.Usage()
    first = CB.fair_plan(pronos, usage=u)
    again = dict(CB.fair_shots_plan(pronos, usage=u))
    used = {c["id"] for _, cb in first if cb for c in cb[0]}
    assert not (again["ts5"] and {c["id"] for c in again["ts5"][0]} & used), "une jambe risquée prise ailleurs n'est pas reprise"
    store.save("bets.json", {"picks": [], "combos": []})
    saved = []
    old_save = app.xbet.save_coupon
    app.xbet.save_coupon = lambda legs: saved.append(len(legs)) or "TEST123"      # jamais de vrai coupon : aucun réseau
    try:
        msgs, _, _ = app.build_day([], date.fromisoformat(DAY), pronos=pronos, fair_combos=list(plan.items()))
    finally:
        app.xbet.save_coupon = old_save
    titles = [m.split("\n")[0] for m in msgs]
    assert "<b>🧮 COMBINÉ STATS — COTES 1XBET</b>" in titles and saved == [len(legs)], (titles, saved)
    fair.record_combos([(t, c) for t, c in plan.items() if c], DAY)
    t = played.find("vts5", DAY)
    assert not isinstance(t, str) and t is not None and "Stats" in played.todo(DAY), t
    assert played._fair_name("ts5") == "Stats" and played._fair_name("tc5") == "Tirs cadrés" and played._fair_name("tv5") == "Tirs vers le but"
    lo, hi, prefix = app._tier_range("ts5")
    assert (lo, hi) == (5.0, 7.0) and isinstance(prefix, tuple) and app._tier_range("tc5")[2] == "team_sot_"
    # relecture du combiné (patch_combo) : un remplaçant ne sort que des familles du palier
    spare = [prono(30, "totals"), prono(31, "team_throws_total")]
    res = CB.patch_combo(legs[:1], pronos[:0] + spare + legs[:1], 5.0, 7.0, fresh_ids={c["id"] for c in legs[:1] + spare}, prefix=prefix)
    assert res is None or all(c["market"].startswith(prefix) for c in res[0]), res
    print("ok  palier « COMBINÉ STATS »")


def test_price_to_combo_and_custom():
    """De bout en bout : pronostics stats d'un match payé -> cotes 1xBet (échelle du sous-match) -> jambes -> « combi 20 » avec des jambes de stats."""
    store.save(calib.FILE, {})
    calib._memo.clear()
    pronos, found = [], {}
    PRICE = (1.95, 2.1, 1.7, 2.0, 1.85, 2.2, 1.75, 1.9)
    fams = ("team_fouls_total", "team_shots_home", "team_sot_total", "team_throws_home", "team_offsides_total", "team_fouls_home", "team_shots_total",
            "team_sot_away")
    for i, m in enumerate(fams):
        c = prono(i, m, 0.66, xodds=None, xev=None, xgid=None, xref=None, name="Under", point=9.5,
                  alts=[["Under", 9.5, 0.66, 0.5], ["Under", 10.5, 0.74, 0.6]])
        for k in ("xodds", "xev", "xgid", "xref"):
            c.pop(k)
        pronos.append(c)
        found[f"m{i}"] = {"gid": 700 + i, "px": {(m, "Under", 9.5): PRICE[i], (m, "Over", 9.5): 1.8, (m, "Under", 10.5): 1.6},
                          "refs": {(m, "Under", 9.5): [800 + i, 12, 9.5], (m, "Under", 10.5): [800 + i, 12, 10.5]}}
    out = xprice.apply(pronos, fetch=lambda *a, **k: found)
    assert out["value"] == len(fams) and out["none"] == 0, out
    legs = CB.fair_legs(pronos)
    assert len(legs) == len(fams) and all(l["odds"] == l["xodds"] and l["xref"][0] >= 800 for l in legs), [l["key"] for l in legs]
    combo = CB.fair_custom(pronos, 20)
    assert combo and combo[1] >= 20 and all(c["market"].startswith("team_") for c in combo[0]), combo
    assert len({c["id"] for c in combo[0]}) == len(combo[0]), "un pari par match"
    print(f"ok  jambes de stats : prix 1xBet -> combinés ; combi 20 = {len(combo[0])} jambes, cote {combo[1]:.1f}")


if __name__ == "__main__":
    test_paid_match_gets_stat_families()
    test_fetch_order_stats_first()
    test_keep_pronos_keeps_every_combo_leg()
    test_stats_tier()
    test_price_to_combo_and_custom()
    print("tests jambes de stats : OK")
