"""Toutes les jambes à valeur d'un match (pool fictif, hors ligne) : un match qui a trois familles à valeur les montre toutes (💎 du matin, /top, ligne 📊), le pool des combinés les
garde toutes, un combiné n'en prend qu'une par match, deux combinés « saison auto » peuvent prendre deux marchés différents du même match. Les favoris « probables » 🎯 restent UN par match.
Lancer : python -m tests.test_all_value_legs"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-allvalue-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-allvalue-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY"):
    os.environ.pop(k, None)

from kelly import app, combos as CB, config as C  # noqa: E402


def prono(mid, market, strategy, xev, prob=0.6, odds=2.0, **kw):
    c = {"key": f"{mid}|{market}||2.5|Over", "id": mid, "market": market, "name": "Over", "point": 2.5, "prob": prob, "odds": odds, "xodds": odds, "xev": xev,
         "score": xev, "strategy": strategy, "commence": "2026-10-15T18:00:00Z", "lane": None}
    return c | kw


def fixture():
    return [
        prono("A", "totals", "sharpgap", 0.10), prono("A", "team_corners", "stats", 0.08), prono("A", "cards", "stats", 0.06),      # trois familles à valeur sur le match A
        prono("A", "btts", "fill", 0.02, xkind="probable"),                                                                         # un 🎯 du même match : pas une valeur
        prono("B", "totals", "sharpgap", 0.07), prono("B", "h2h", "fill", 0.01, xkind="probable"),
        prono("C", "h2h", "fill", 0.03, xkind="probable"),                                                                          # match sans valeur : un seul 🎯
        prono("C", "totals", "fill", 0.01, xkind="probable"),
    ]


def with_viable(fn):
    old = CB.viable
    CB.viable = lambda c: True
    try:
        return fn()
    finally:
        CB.viable = old


def test_shown_pronos_keeps_every_value_leg():
    pronos = fixture()
    best = with_viable(lambda: CB.best_per_match(pronos))
    shown = with_viable(lambda: CB.shown_pronos(pronos))
    assert len(best) == 3, "l'ancienne règle : un par match"
    got = {c["key"] for c in shown}
    assert {f"A|{m}||2.5|Over" for m in ("totals", "team_corners", "cards")} <= got, "les trois familles à valeur du match A"
    assert "B|totals||2.5|Over" in got and {c["key"] for c in best} <= got, "sur-ensemble de l'ancienne règle"
    probables = [c for c in shown if c.get("xkind") == "probable"]
    assert len({c["id"] for c in probables}) == len(probables), "les 🎯 restent un par match"
    assert sum(1 for c in shown if c["id"] == "C") == 1, "match sans valeur : un seul pari"
    assert [c["xev"] * c["prob"] for c in shown] == sorted((c["xev"] * c["prob"] for c in shown), reverse=True), "ordre du classement"
    print("ok  pronostics montrés : les trois familles à valeur d'un match sortent, les 🎯 restent un par match, sur-ensemble de l'ancienne règle")


def test_value_line_and_counts():
    legs = [c for c in fixture() if CB.value_leg(c)]
    assert len(legs) == 4 and CB.value_counts(legs) == (4, 2, 1)
    assert CB.value_line(legs) == "4 jambes à valeur sur 2 matchs (1 match avec plusieurs)"
    assert CB.value_line(legs + [prono("D", "totals", "stats", 0.1), prono("D", "cards", "stats", 0.1), prono("E", "totals", "stats", 0.1)]) == "7 jambes à valeur sur 4 matchs (2 matchs avec plusieurs)"
    assert CB.value_line([prono("A", "totals", "sharpgap", 0.1)]) == "1 jambe à valeur sur 1 match (aucun match avec plusieurs)"
    assert CB.value_line([c for c in fixture() if not CB.value_leg(c)]) == "aucune jambe à valeur"
    print("ok  ligne 📊 : « n jambes à valeur sur n matchs (n matchs avec plusieurs) », comptes exacts")


def test_combo_takes_one_leg_per_match_and_two_combos_two_markets():
    # le pool des combinés garde les trois marchés du match A ; un combiné n'en prend qu'un
    legs = [prono("A", m, "stats", 0.1, prob=0.62 - 0.01 * i, odds=1.8 + 0.05 * i, key=f"A|{m}||2.5|Over") for i, m in enumerate(("totals", "team_corners", "cards"))]
    legs += [prono(f"M{i}", "totals", "stats", 0.1, prob=0.6, odds=1.8, key=f"M{i}|totals||2.5|Over") for i in range(4)]
    combo = CB.best_combo(legs, 5.0, 12.0)
    assert combo and sum(1 for c in combo[0] if c["id"] == "A") <= 1 and len({c["id"] for c in combo[0]}) == len(combo[0])
    # saison auto : deux combinés différents prennent deux marchés différents du même match
    old = (C.SEASON_AUTO, C.SEASON_AUTO_MIN_EV, C.SEASON_AUTO_MAX_ODDS, C.SEASON_AUTO_MAX_COMBOS, C.SEASON_AUTO_MIN_P)
    C.SEASON_AUTO, C.SEASON_AUTO_MIN_EV, C.SEASON_AUTO_MAX_ODDS, C.SEASON_AUTO_MAX_COMBOS, C.SEASON_AUTO_MIN_P = True, -0.15, 6.0, 4, 0.0
    try:
        def s(i, m, p, o):
            return {"id": f"m{i}", "key": f"m{i}|{m}||2.5|Over|saison", "market": m, "lane": "saison", "strategy": "saison", "sval": 1, "prob": p, "odds": o, "xodds": o,
                    "commence": f"2026-10-15T{10 + i:02d}:00:00Z"}
        pool = [s(0, "totals", 0.62, 1.8), s(0, "team_corners", 0.60, 1.8), s(0, "cards", 0.58, 1.8),      # match m0 : trois marchés à valeur
                s(1, "totals", 0.62, 1.8), s(1, "team_corners", 0.60, 1.8), s(2, "totals", 0.62, 1.8), s(3, "totals", 0.62, 1.8), s(4, "totals", 0.62, 1.8)]
        plan = CB.season_auto_plan(pool, CB.Usage())
        assert len(plan) >= 2, plan
        seen_legs = set()
        for _, (lg, odds, prob) in plan:
            assert len({c["id"] for c in lg}) == len(lg) >= 2, "un seul pari par match DANS un combiné"
            keys = {c["key"] for c in lg}
            assert not (keys & seen_legs), "aucune jambe partagée entre deux combinés"
            seen_legs |= keys
        in_m0 = [[c["market"] for c in lg if c["id"] == "m0"] for _, (lg, _, _) in plan]
        assert sum(1 for x in in_m0 if x) >= 2 and len({x[0] for x in in_m0 if x}) >= 2, ("m0 dans deux combinés, avec deux marchés différents", in_m0)
        # un match déjà dans un combiné JOUÉ (exclude) reste interdit
        plan2 = CB.season_auto_plan(pool, CB.Usage(), exclude=("m0",))
        assert all(c["id"] != "m0" for _, (lg, _, _) in plan2 for c in lg)
    finally:
        C.SEASON_AUTO, C.SEASON_AUTO_MIN_EV, C.SEASON_AUTO_MAX_ODDS, C.SEASON_AUTO_MAX_COMBOS, C.SEASON_AUTO_MIN_P = old
    print("ok  combinés : une jambe par match dans un combiné ; saison auto : deux combinés, deux marchés différents du même match, aucune jambe partagée, match joué toujours exclu")


def test_top_reply_lists_every_leg_and_dedups_by_slot():
    old = (CB.make_pool, CB.leg_reason, app.fmt.leg, app.fmt.pronos)
    CB.make_pool = lambda picks: list(picks)
    CB.leg_reason = lambda c: "value"
    shown_seen = {}
    app.fmt.leg = lambda k, c, reason, price="": f"LEG {c['key']}"
    app.fmt.pronos = lambda items, limit=10, **kw: shown_seen.setdefault("keys", [c["key"] for c in items]) and "PRONOS"
    try:
        live = [prono("A", "totals", "sharpgap", 0.1, prob=0.7), prono("A", "team_corners", "stats", 0.1, prob=0.6)]
        live_pronos = [prono("A", "totals", "sharpgap", 0.1), prono("A", "cards", "stats", 0.08), prono("Z", "totals", "sharpgap", 0.07)]
        out = str(with_viable(lambda: app.top_reply(live, live_pronos)))
        assert out.count("LEG A|") == 2, "les deux jambes du match A, pas la seule meilleure"
        assert shown_seen["keys"] == ["A|cards||2.5|Over", "Z|totals||2.5|Over"], shown_seen      # le 💎 du même pari (A / totals) n'est pas répété, l'autre marché de A l'est
        plain = [prono("A", "h2h", "fill", 0.0, prob=0.7), prono("A", "btts", "fill", 0.0, prob=0.65), prono("B", "h2h", "fill", 0.0, prob=0.6)]
        out2 = str(with_viable(lambda: app.top_reply(plain, [])))
        assert out2.count("LEG A|") == 1 and out2.count("LEG B|") == 1, "paris réels sans valeur : toujours le meilleur de chaque match"
    finally:
        CB.make_pool, CB.leg_reason, app.fmt.leg, app.fmt.pronos = old
    print("ok  /top : toutes les jambes de qualité (plusieurs par match), un 💎 déjà dans les paris réels n'est pas répété, un autre marché du même match l'est")


def test_sweep_line_and_stats_window_announce():
    from kelly import statswindow
    old = CB.viable
    CB.viable = lambda c: True
    old_fe = CB.fair_eligible
    CB.fair_eligible = lambda c: True
    try:
        pronos = [dict(c, min_odds=1.1, conf=0.9, home="h", away="a") for c in fixture()]
        line = app.sweep_line([], pronos)
        assert "4 jambes à valeur sur 2 matchs (1 match avec plusieurs)" in line and "jusqu'à" not in line, line
        # 💎 stats de la fenêtre : toutes les jambes à valeur d'un match, un seul 🎯 par match, jamais deux fois le même pari
        old_w = (statswindow.is_stat, statswindow.in_window, statswindow.fresh, C.STATS_WINDOW_ANNOUNCE, C.STATS_WINDOW_DAY_ANNOUNCE)
        statswindow.is_stat, statswindow.in_window, statswindow.fresh = (lambda c: True), (lambda c, now: True), (lambda c, now: True)
        C.STATS_WINDOW_ANNOUNCE, C.STATS_WINDOW_DAY_ANNOUNCE = 10, 10
        try:
            today = {"pronos": pronos, "announced": []}
            new = statswindow.announce_list(today, {"announced": []}, None)
            keys = {c["key"] for c in new}
            assert {f"A|{m}||2.5|Over" for m in ("totals", "team_corners", "cards")} <= keys, keys
            assert sum(1 for c in new if c["id"] == "C") == 1 and sum(1 for c in new if c["id"] == "A" and c.get("xkind") == "probable") <= 1
            again = statswindow.announce_list({"pronos": pronos, "announced": [app._slot(c) for c in new]}, {"announced": []}, None)
            assert not any(CB.value_leg(c) for c in again) and not ({c["key"] for c in again} & {c["key"] for c in new}), "déjà annoncés : jamais deux fois le même pari"
        finally:
            statswindow.is_stat, statswindow.in_window, statswindow.fresh, C.STATS_WINDOW_ANNOUNCE, C.STATS_WINDOW_DAY_ANNOUNCE = old_w
    finally:
        CB.viable, CB.fair_eligible = old, old_fe
    print("ok  ligne 📊 de « combi N » et 💎 stats de la fenêtre : toutes les jambes à valeur, une seule fois chacune")


if __name__ == "__main__":
    test_shown_pronos_keeps_every_value_leg()
    test_value_line_and_counts()
    test_combo_takes_one_leg_per_match_and_two_combos_two_markets()
    test_top_reply_lists_every_leg_and_dedups_by_slot()
    test_sweep_line_and_stats_window_announce()
    print("tests toutes les jambes à valeur : OK")
