"""Live et joueurs : proba live ancrée sur 1xBet (mélange en logit, poids de Kelly <= 0,3), garde-fous resserrés (espérance +2 %, écart 8 points, pas de
pari après la 80e), journal et calibration sur les seuls paris retenus (un côté par marché, une correction, ancien format ignoré), combinés live de 2 à 4
paris à la cote 20 au plus, aucune mise si l'espérance au prix de 1xBet est <= 0, minute et score gardés par pari ; marchés joueur coupés (PROP_MARKETS,
PLAYER_WEIGHT) et stat d'un joueur par identifiant et par équipe. Hors ligne. Lancer : python -m tests.test_live_players"""
import os
import tempfile
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-liveplayers-")
os.environ["QUIET_HOURS"] = "off"
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
    os.environ.pop(k, None)

from kelly import config as C, custom, live, money, odds, players, store  # noqa: E402
from kelly import calib, combos as CB, settle as S  # noqa: E402
from kelly.sources import xbet  # noqa: E402
from tests.test_live import DAY, NOW, PRED, VALUE, game  # noqa: E402

KNOWN = lambda m, k: PRED if k else None            # noqa: E731 - tous les championnats connus avec les mêmes buts attendus
ODDS_X = [{"T": 1, "C": 1.30}, {"T": 2, "C": 4.6}, {"T": 3, "C": 11.0}, {"T": 4, "C": 1.05}, {"T": 5, "C": 1.22}, {"T": 6, "C": 3.6},
          {"T": 9, "P": 1.5, "C": 1.55}, {"T": 10, "P": 1.5, "C": 2.40}, {"T": 9, "P": 2.5, "C": 3.10}, {"T": 10, "P": 2.5, "C": 1.36},
          {"T": 180, "C": 3.0}, {"T": 181, "C": 1.38}]     # cotes quasi justes d'après Kelly (l'ancienne grille de test) : 1xBet n'est pas en retard


def raws(n, minute=63, odds=None):
    return [game(i, minute, home=f"Team{i}", away=f"Other{i}", odds=odds) for i in range(1, n + 1)]


def test_settings():
    assert C.LIVE_MIN_EV == 0.02 and C.LIVE_MAX_GAP == 0.08 and C.LIVE_MAX_MINUTE == 80, (C.LIVE_MIN_EV, C.LIVE_MAX_GAP, C.LIVE_MAX_MINUTE)
    assert 0 < C.LIVE_W_KELLY <= 0.3 and (C.LIVE_COMBO_MIN_LEGS, C.LIVE_COMBO_MAX_LEGS, C.LIVE_COMBO_MAX_ODDS) == (2, 4, 20.0)
    assert not any(m.startswith("player_") for ms in C.PROP_MARKETS.values() for m in ms), C.PROP_MARKETS
    assert "alternate_totals_corners" in C.PROP_MARKETS["soccer"] and "btts" in C.PROP_MARKETS["soccer"]
    assert C.PLAYER_WEIGHT == 1.0 and odds.market_weight("player_shots") == 1.0 and odds.market_weight("alternate_totals_corners") == 1.6
    print("ok  réglages : +2 % d'espérance, 8 points d'écart, 80e minute, poids de Kelly <= 0,3, combinés 2-4 paris / cote 20, plus de player_*, PLAYER_WEIGHT 1")


def test_blend():
    assert abs(live.blend(0.9, 0.5, 0.0) - 0.5) < 1e-9 and abs(live.blend(0.9, 0.5, 1.0) - 0.9) < 1e-9
    b = live.blend(0.9, 0.6, 0.3)
    assert 0.6 < b < 0.75, b          # bien plus près de 1xBet que de Kelly (le milieu serait 0,75)
    assert abs(live.blend(0.8, 0.6, 0.3) + live.blend(0.2, 0.4, 0.3) - 1) < 1e-9, "symétrique : plus / moins d'une ligne s'additionnent à 1"
    assert 0 < live.blend(1.0, 0.0, 0.3) < 1 and live.blend(0.7, 0.7, 0.3) - 0.7 < 1e-9
    assert live.kelly_weight("modèle") == C.LIVE_W_KELLY and live.kelly_weight("365scores") < live.kelly_weight("profils") < live.kelly_weight("modèle")
    print("ok  mélange en logit : Kelly pèse au plus 30 %, moins avec des données légères")


def test_guards():
    keep, seen, count = live.legs(raws(1), now=NOW, data={}, model_for=KNOWN)
    assert {(c["market"], c["name"]) for c in keep} == {("h2h", "Team1"), ("totals", "Under")} and count["known"] == 1, [(c["market"], c["name"]) for c in keep]
    for c in keep:
        assert c["pk"] > c["prob"] > c["px"] and abs(c["prob"] - c["px"]) <= 0.08 and c["prob"] * c["odds"] - 1 >= 0.02, c
    # cotes quasi justes d'après Kelly : plus rien (avant : espérance -5 % acceptée)
    assert live.legs(raws(1, odds=ODDS_X), now=NOW, data={}, model_for=KNOWN)[0] == []
    # Kelly très loin de 1xBet (95 % contre ~40 %) : 1xBet sait quelque chose, même ancré le pari est écarté
    far = [{"T": 9, "P": 1.5, "C": 4.0}, {"T": 10, "P": 1.5, "C": 1.2}, {"T": 1, "C": 3.0}, {"T": 2, "C": 3.5}, {"T": 3, "C": 2.9}]
    assert live.legs(raws(1, odds=far), now=NOW, data={}, model_for=KNOWN)[0] == []
    # pas de proba de 1xBet (un seul côté coté) : pas de pari
    lonely = [{"T": 10, "P": 2.5, "C": 1.49}]
    assert live.legs(raws(1, odds=lonely), now=NOW, data={}, model_for=KNOWN)[0] == []
    # la 80e est la dernière minute ; la 81e est hors fenêtre
    assert live.legs([game(1, 80)], now=NOW, data={}, model_for=KNOWN)[2]["window"] == 1
    assert live.legs([game(1, 81)], now=NOW, data={}, model_for=KNOWN)[2] == {"live": 1, "window": 0, "known": 0}
    old, C.LIVE_MAX_MINUTE = C.LIVE_MAX_MINUTE, 70
    try:
        assert live.legs([game(1, 75)], now=NOW, data={}, model_for=KNOWN)[2]["window"] == 0, "la minute maximale est réglable"
    finally:
        C.LIVE_MAX_MINUTE = old
    # une seule correction : les anciennes clés « src|… » ne comptent plus
    data = {"src|365scores": [200, 60, 140.0]}
    assert live.factor("totals", "60-75", data) == 1.0
    print("ok  garde-fous : proba ancrée, +2 % d'espérance, 8 points d'écart, 1xBet obligatoire, dernière minute 80 réglable")


def test_one_side_per_market():
    many = [{"id": "lv1", "market": "totals", "point": 2.5, "name": "Over", "prob": 0.6, "odds": 1.8}, {"id": "lv1", "market": "totals", "point": 2.5, "name": "Under", "prob": 0.7, "odds": 1.6},
            {"id": "lv1", "market": "totals", "point": 1.5, "name": "Over", "prob": 0.7, "odds": 1.5}]
    out = live._one_side(many)
    assert [c["name"] for c in out] == ["Under", "Over"] and out[1]["point"] == 1.5, out
    print("ok  un seul côté par match, marché et ligne (le plus rentable)")


def test_record_only_retained():
    store.save(live.JOURNAL, [])
    keep, seen, _ = live.legs(raws(2), now=NOW, data={}, model_for=KNOWN)
    assert len(seen) == 24 and len(keep) == 4
    assert live.record(keep, NOW) == 4 and live.record(keep, NOW) == 0, "rien d'autre que les paris retenus, une fois par match et par tranche"
    rows = store.load(live.JOURNAL, [])
    assert len(rows) == 4 and all(r["v"] == live.CALIB_VERSION and r["pk"] and r["minute"] == 63 and r["score"] == "1-0" for r in rows), rows
    other_side = dict(next(c for c in keep if c["market"] == "totals"), name="Over", key="x")
    assert live.record([other_side], NOW) == 0, "l'autre côté du même marché n'est jamais noté dans la même tranche"
    later, _, _ = live.legs([game(1, 77, home="Team1", away="Other1")], now=NOW, data={}, model_for=KNOWN)
    assert later == [] or live.record(later, NOW) == len(later), "une autre tranche peut être notée"
    print("ok  journal : seulement les paris retenus, un côté par marché, une fois par match et par tranche")


def test_calibration_reset_and_single_correction():
    # ancien fichier (sans version, deux côtés de chaque marché + clés « src|… ») : ignoré, puis remplacé
    store.save(live.CALIB_FILE, {"goals|60-75": [582, 291, 291.0], "src|365scores": [2687, 1335, 1335.4]})
    assert live.calib_data() == {} and live.factor("totals", "60-75") == 1.0
    store.save(live.JOURNAL, [
        {"key": "old", "id": "lv0", "tranche": "60-75", "market": "totals", "name": "Over", "point": 2.5, "p0": 0.7, "prob": 0.7, "date": "2026-10-10",
         "result": "win"},                                            # ancienne ligne (sans « v ») déjà réglée : jamais apprise
        {"key": "old2", "id": "lv0", "tranche": "60-75", "market": "totals", "name": "Under", "point": 2.5, "p0": 0.3, "prob": 0.3, "date": str(NOW.date()),
         "result": None},                                              # ancienne ligne non réglée : plus réglée du tout
        {"key": "new", "id": "lv1", "tranche": "60-75", "market": "totals", "name": "Under", "point": 2.5, "p0": 0.7, "prob": 0.7, "date": str(NOW.date()),
         "result": None, "v": live.CALIB_VERSION, "src": "365scores"}])
    seen_rows = []
    old = S._settle_fair_row
    S._settle_fair_row = lambda r, now, fdata, stats_for=None: seen_rows.append(r["key"]) or r.update(result="loss")
    try:
        settled, learned = live.settle(NOW, fdata=object())
    finally:
        S._settle_fair_row = old
    assert (settled, learned) == (1, 1) and seen_rows == ["new"], (settled, learned, seen_rows)
    raw = store.load(live.CALIB_FILE, {})
    assert raw == {"v": live.CALIB_VERSION, "k": {"goals|60-75": [1, 0, 0.7]}}, raw          # ni ancien cumul ni « src|365scores » : une seule clé famille|tranche
    assert live.settle(NOW, fdata=object()) == (0, 0)
    data = {"goals|60-75": [100, 40, 70.0]}                                                    # 40 % gagnés pour 70 % annoncés
    assert live.factor("totals", "60-75", data) < 1 and live.factor("totals", "45-60", data) == 1.0 and live.factor("h2h", "60-75", data) == 1.0
    keep, seen, _ = live.legs(raws(1), now=NOW, data=data, model_for=KNOWN)
    raw_p = next(c for c in seen if c["market"] == "totals" and c["name"] == "Under")
    assert not any(c["market"] == "totals" for c in keep), "la correction rapproche de 50 % : le pari n'est plus assez probable"
    base = next(c for c in live.legs(raws(1), now=NOW, data={}, model_for=KNOWN)[1] if c["market"] == "totals" and c["name"] == "Under")
    k = live.factor("totals", "60-75", data)
    assert abs(raw_p["prob"] - (0.5 + (base["p0"] - 0.5) * k)) < 1e-3 and raw_p["p0"] == base["p0"], "p0 = avant correction (jamais mesurée contre elle-même), prob = une seule correction"
    print("ok  calibration live : ancien fichier ignoré puis remplacé, anciennes lignes jamais apprises, une seule correction famille x tranche")


def test_combo_limits_and_stake():
    store.save(live.JOURNAL, [])
    store.save(custom.FILE, {})
    old = xbet.save_coupon
    xbet.save_coupon = lambda legs: "LV20"
    try:
        # cote > 20 : refusé tout de suite, sans lire 1xBet
        called = []
        txt = live.combo(30.0, DAY, get=lambda u: called.append(u) or {"Value": raws(8)}, model_for=KNOWN, now=NOW)
        assert "ne dépasse pas la cote 20" in txt and not called, txt
        assert "2 à 4 paris" in live.combo(5.0, DAY, get=lambda u: {"Value": raws(8)}, model_for=KNOWN, now=NOW, nlegs=(6, True))
        assert "2 à 4 paris" in live.combo(5.0, DAY, get=lambda u: {"Value": raws(8)}, model_for=KNOWN, now=NOW, nlegs=(5, "min"))
        # 8 matchs à ~1,5 : la cote 20 demanderait 7 paris ; le live n'en prend que 4 -> pas de combiné, et le message ne promet pas plus de 4 paris
        none = live.combo(20.0, DAY, get=lambda u: {"Value": raws(8)}, model_for=KNOWN, now=NOW)
        assert none.startswith("🔴 Pas de combiné live à la cote 20") and "au plus 4,9" in none, none
        msg = live.combo(3.0, DAY, get=lambda u: {"Value": raws(8)}, model_for=KNOWN, now=NOW)
        item = store.load(custom.FILE, {})["items"]["m1"]
        assert 2 <= len(item["legs"]) <= 4 and 3.0 <= item["total"] <= 3.75, item
        for leg in item["legs"]:
            assert leg["minute"] == 63 and leg["score"] == "1-0" and 0 < leg["px"] < 1 and leg["pk"] > 0, leg      # minute, score et probas de chaque pari gardés
        assert custom._row("m1", DAY, item)["legs"][0]["minute"] == 63, "... et dans la ligne de suivi quand le combiné est joué"
        text = str(msg)
        # espérance au prix de 1xBet sans marge <= 0 (toujours le cas ici : marge de 1xBet) : aucune mise conseillée, même avec un budget
        assert "Kelly ne conseille pas de miser dessus" in text and "Mise :" not in text and "pas de mise conseillée" in text, text
        assert item["stake"] == 0
        money.set_daily(5000)
        assert "Mise :" not in str(live.combo(3.0, DAY, get=lambda u: {"Value": raws(8)}, model_for=KNOWN, now=NOW)), "budget ou non, pas de mise"
        # espérance positive au prix du marché : la mise redevient possible (cotes de 1xBet supposées en retard sur son propre prix sans marge)
        real = CB.best_combo
        CB.best_combo = lambda cands, lo, hi, max_legs=None, min_legs=None: ([dict(c, px=0.9) for c in cands[:2]], 1.4, 0.8)
        try:
            text = str(live.combo(1.4, DAY, get=lambda u: {"Value": raws(8)}, model_for=KNOWN, now=NOW))
        finally:
            CB.best_combo = real
        assert "Mise : " in text and "Kelly ne conseille pas" not in text, text
        assert live.market_ev([{"px": 0.5}, {"px": 0.5}], 4.4) > 0 and live.market_ev([{"px": 0.5}, {"px": None}], 4.4) is None and live.market_ev([{"px": 0.5}], 1.9) < 0
    finally:
        xbet.save_coupon = old
        money.set_daily(0) if hasattr(money, "set_daily") else None
    print("ok  combiné live : 2 à 4 paris, cote 20 au plus, minute / score par pari, aucune mise si l'espérance au prix de 1xBet est <= 0")


def test_watch_records_only_retained():
    from kelly import watch
    store.save(watch.FILE, {})
    store.save(live.JOURNAL, [])
    now = NOW.replace(hour=16)
    current = [{"statusGroup": 3, "gameTime": 50}] * 9
    old = live.model
    live.model = lambda m, key: PRED
    try:
        msg = watch.opportunity(now, current=current, get=lambda u: {"Value": raws(6, minute=50)})
    finally:
        live.model = old
    assert msg and "Opportunité live" in str(msg)
    rows = store.load(live.JOURNAL, [])
    assert rows and len(rows) == 12 and all(r["v"] == live.CALIB_VERSION for r in rows), len(rows)       # 6 matchs x 2 paris retenus (1N2 et plus / moins), pas 72 lignes
    print("ok  opportunité live : seuls les paris retenus sont notés")


def test_players():
    pl = {"101": {"name": "K. Mbappé", "team_id": 1, "shots": 4.0, "shots_on_target": 2.0, "fouls": 1.0},
          "202": {"name": "B. Silva", "team_id": 2, "shots": 1.0}, "203": {"name": "D. Silva", "team_id": 1, "shots": 2.0},
          "301": {"name": "Carlos Gomez", "team_id": 1, "shots": 3.0}, "302": {"name": "Carlos Gomez", "team_id": 2, "shots": 0.0}}
    fs = {"players": pl}
    assert players.stat(fs, "Kylian Mbappe", "shots") == 4.0 and players.stat(fs, "Mbappé", "shots_on_target", team=1) == 2.0
    assert players.stat(fs, "Mbappé", "shots", team=2) is None, "pas dans cette équipe"
    assert players.stat(fs, "Carlos Gomez", "shots") is None, "même nom dans les deux équipes, équipe inconnue : ambigu, jamais réglé au hasard"
    assert players.stat(fs, "Carlos Gomez", "shots", team=1) == 3.0 and players.stat(fs, "Carlos Gomez", "shots", team="2") == 0.0
    assert players.stat(fs, "Silva", "shots") is None and players.stat(fs, "Bernardo Silva", "shots", team=2) == 1.0
    assert players.stat(fs, "Mbappé", "fouls") == 1.0 and players.stat(fs, "Haaland", "shots") is None and players.stat(None, "x", "shots") is None
    legacy = {"players": {"K. Mbappé": {"shots": 4.0}, "B. Silva": {"shots": 1.0}}}                          # ancien format {nom: stats}
    assert players.stat(legacy, "Kylian Mbappe", "shots") == 4.0 and players.stat(legacy, "Mbappé", "shots", team=1) == 4.0
    print("ok  stat d'un joueur : par identifiant, filtrée par équipe, homonymes jamais confondus, ancien format lu")


def main():
    test_settings()
    test_blend()
    test_guards()
    test_one_side_per_market()
    test_record_only_retained()
    test_calibration_reset_and_single_correction()
    test_combo_limits_and_stake()
    test_watch_records_only_retained()
    test_players()
    print("tests live et joueurs : OK")


if __name__ == "__main__":
    main()
