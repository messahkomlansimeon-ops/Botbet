"""Familles hors voies (touches, hors-jeu, stats joueurs...) : jamais sur un ticket (combiné, code coupon), visibles ailleurs ; espérance d'un combiné de pur remplissage dite AU PRIX DU MARCHÉ.
Un pool fait seulement de touches et de hors-jeu ne produit AUCUN combiné, par chaque chemin : combiné à cote juste (fair_legs / fair_custom), paris réels (make_pool), combiné audacieux (_bold_pools,
_bold_combo), voie saison (season_pool, season_plan, season_auto_plan), live (lanes.ticket_ok) ; avec un témoin positif (le même pool plus des jambes de voie donne un combiné).
Lancer : python -m tests.test_ticket_families"""
from datetime import datetime, timezone

from tests import test_combi_kinds as K     # noqa: I001 - fixe DATA_DIR et les faux réseaux avant d'importer kelly
from tests import test_combi_track as T
from kelly import app, combos as CB, fmt, lanes, xalerts  # noqa: E402

DAY = T.DAY
NOW = datetime.now(timezone.utc)


def leg(i, market, **kw):
    return K.fouls(i) | {"id": f"t{i}", "home": f"TH{i}", "away": f"TA{i}", "event": f"TH{i} - TA{i}", "market": market,
                         "key": f"t{i}|{market}||22.5|Over", "pick": fmt.label(market, "Over", 22.5, None, "soccer")} | kw


OFF_LANE = ("team_throws_total", "team_offsides_total", "team_throws_home", "team_offsides_home", "team_throws_away", "player_shots_on_target")


def off_pool(n=6):
    return [leg(i, OFF_LANE[i % len(OFF_LANE)]) for i in range(n)]


def lane_pool(n=4):
    return [leg(50 + i, "team_fouls_total") for i in range(n)]


def test_ticket_ok():
    assert not any(lanes.ticket_ok(c) for c in off_pool()), "touches, hors-jeu, joueurs : hors voies"
    assert all(lanes.ticket_ok(c) for c in lane_pool()) and all(lanes.ticket_ok({"market": m}) for m in ("totals", "h2h", "btts", "alternate_totals_corners", "team_shots_home", "team_sot_total"))
    print("ok  lanes.ticket_ok : famille de voie seulement")


def test_no_combo_from_off_lane_only():
    T.reset()
    off, lane = off_pool(), lane_pool()
    assert CB.fair_legs(off) == [] and CB.make_pool(off) == [] and CB.fair_legs(off + lane) and len(CB.fair_legs(off + lane)) == len(lane)
    assert CB.fair_custom(off, 3) is None, "pool hors voies : pas de combiné à cote juste"
    assert CB.fair_custom(off + lane, 3) is not None, "témoin : avec des jambes de voie, le combiné existe"
    # combiné audacieux : pools puis refus
    mine, real = app._bold_pools([], off, None)
    assert mine == [] and real == []
    mine, real = app._bold_pools([], off + lane, None)
    assert {c["id"] for c in mine} == {c["id"] for c in lane}, "témoin : les jambes de voie passent, jamais les autres"
    with K.Fakes():
        out = app._bold_combo([], off, 3.0, DAY, None, NOW)
    assert str(out).startswith("❌ Pas de combiné") and "COMBINÉ AUDACIEUX" not in str(out), out
    # voie saison : une jambe hors voies, même `saison` avec valeur et cote 1xBet, n'entre ni dans le pool ni dans un combiné
    sea = [c | {"lane": "saison", "strategy": "saison", "sval": 1, "odds": 1.5} for c in off]
    assert CB.season_pool(sea) == [] and all(cb is None for _, cb in CB.season_plan(sea, [3.0])) and CB.season_auto_plan(sea) == []
    sea_ok = [c | {"lane": "saison", "strategy": "saison", "sval": 1, "odds": 1.5} for c in off + lane]
    assert len(CB.season_pool(sea_ok)) == len(lane) and any(cb for _, cb in CB.season_plan(sea_ok, [3.0])), "témoin saison"
    print("ok  un pool de touches et de hors-jeu ne produit aucun combiné (cote juste, audacieux, saison) ; témoin positif")


def test_real_picks_pool_and_alerts():
    """Paris réels (quality_legs -> make_pool, pari du jour, combinés réels, combi N) et alertes « monté » : touches et hors-jeu exclus ; le test prouve qu'il n'est pas vide en retirant le filtre."""
    picks = [T.pick(i, market="team_throws_total", key=f"e{i}|team_throws_total||22.5|Over") for i in range(3)]
    assert CB.make_pool(picks) == [] and len(CB.make_pool([T.pick(i) for i in range(3)])) == 3, "témoin : les mêmes paris en 1N2 sont des jambes"
    old = lanes.ticket_ok
    lanes.ticket_ok = lambda c: True
    try:
        assert len(CB.make_pool(picks)) == 3, "sans le filtre, ces paris seraient des jambes : le test n'est pas vide"
    finally:
        lanes.ticket_ok = old
    # alertes « 📈 monté » : une cote qui monte sur un pronostic de touches ne donne ni alerte ni code
    up = [leg(1, "team_throws_total", xodds0=1.5, xodds=1.9, xev=0.2, xkind="value"), leg(2, "team_fouls_total", xodds0=1.5, xodds=1.9, xev=0.2, xkind="value")]
    items = xalerts.rise_items(up, {"t1", "t2"}, "10:00", [], {})
    assert [i["key"] for i in items] == [up[1]["key"]], [i["key"] for i in items]
    print("ok  paris réels et alertes « monté » : hors voies exclus (test non vide)")


def test_season_auto_note_all_fill():
    legs = [fill_leg(1, 0.43) | {"strategy": "saison", "sval": 0, "lane": "saison"}, fill_leg(2, 0.47) | {"strategy": "saison", "sval": 0, "lane": "saison"}]
    with K.Fakes():
        msg = str(app.fair_combo_message(CB.season_tier(4.4, auto=True), legs, 4.41, 0.41, DAY, None, lambda *a, **k: "", None, "10:00", k=1))
    assert "espérance au prix du marché −" in msg and "espérance +" not in msg, msg
    print("ok  « saison auto » de pur remplissage : l'espérance de la note est au prix du marché")


def fill_leg(i, p_x=0.42):
    """Jambe de pur remplissage : Kelly la voit à 64 %, 1xBet (sans marge) à `p_x`, cote 2,10."""
    return K.fouls(i) | {"strategy": "fill", "prob": 0.64, "p_x": p_x, "xodds": 2.1, "odds": 2.1}


def test_all_fill_expectation_at_market_price():
    legs = [fill_leg(1, 0.43), fill_leg(2, 0.47)]
    total = 2.1 * 2.1
    pm = CB.market_prob(legs)
    assert pm is not None and abs((pm * total - 1) - (-0.109)) < 0.01, (pm, pm * total - 1)             # environ −10 %
    kelly_ev = f"{(0.64 * 0.64 * total - 1) * 100:+.0f}".replace("-", "−")
    line = fmt.combo_ev_line(legs, 0.64 * 0.64, total)
    assert "Espérance au prix du marché" in line and "−1" in line and "remplissage 2/2" in line and kelly_ev not in line and "Espérance du combiné" not in line, line
    chances = fmt.combo_chances(legs, 0.64 * 0.64, total)
    assert "Espérance au prix du marché" in chances and "📊 Espérance" not in chances and "−10,9 %" in chances, chances
    # sans proba du marché : dit non mesurable, jamais le chiffre de Kelly
    no_px = [fill_leg(1) | {"p_x": None, "xodds": None}, fill_leg(2) | {"p_x": None, "xodds": None}]      # ni proba du marché ni cote 1xBet
    miss = fmt.combo_ev_line(no_px, 0.64 * 0.64, total)
    assert "non mesurable" in miss and kelly_ev not in miss, miss
    # témoin : un combiné avec une jambe de valeur garde l'espérance d'avant
    mixed = [fill_leg(1), K.fouls(3) | {"strategy": "stats", "prob": 0.7, "p_x": 0.5, "xodds": 2.1, "odds": 2.1}]
    assert "📈 Espérance du combiné" in fmt.combo_ev_line(mixed, 0.64 * 0.7, total) and "au prix du marché" not in fmt.combo_ev_line(mixed, 0.64 * 0.7, total)
    print("ok  combiné de pur remplissage : espérance au prix du marché (≈ −10 %), jamais celle de Kelly")


if __name__ == "__main__":
    test_ticket_ok()
    test_no_combo_from_off_lane_only()
    test_all_fill_expectation_at_market_price()
    test_real_picks_pool_and_alerts()
    test_season_auto_note_all_fill()
    print("tests familles de tickets : OK")
