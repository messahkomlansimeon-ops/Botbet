"""Équivalence du câblage des familles (families.py) avec les ANCIENNES listes codées en dur, copiées ici telles quelles : avec l'état initial (valeurs par défaut ET document migré), toute décision
qui lisait une liste de familles donne la même réponse (voie, ticket, étiquette, usable / priced, sous-matchs 1xBet lus, préfixes de la fenêtre des stats et de ts5, marchés de la voie saison et des
coupes, relevé de clôture), puis changements d'état : chaque état déplace exactement les décisions qu'il doit déplacer. Et la sortie de `python bot.py --sample` est identique à celle d'AVANT le
câblage (tests/golden/sample_daily.txt). Seule différence déclarée : le type « Joueurs » disparaît du sélecteur (famille hors_ticket, jamais un ticket).
Lancer : python -m tests.test_families_equiv"""
import os
import subprocess
import sys
import tempfile
from datetime import date

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-famequiv-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-famequiv-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY"):
    os.environ.pop(k, None)

from kelly import closeread, combos as CB, config as C, cupstats, ensemble, families as F, lanes, seasonlane, statswindow, store  # noqa: E402
from kelly.sources import xbet  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- ANCIENNES constantes (copiées d'avant le câblage : ensemble.py, xbet.py, config.py, cmd_custom.py, seasonlane.py)
OLD_SHARPGAP = frozenset(("goals", "btts", "dc", "h2h", "team_goals"))
OLD_STATS_LANE = frozenset(("corners", "cards", "yellow", "shots", "sot", "fouls"))
OLD_SUBGAMES = {2: ("alternate_totals_corners", "team_corners_home", "team_corners_away"), "yellow": ("team_yellow_total", None, None), 12: ("team_sot_total", "team_sot_home", "team_sot_away"),
                95: ("team_shots_total", "team_shots_home", "team_shots_away"), 14: ("team_fouls_total", "team_fouls_home", "team_fouls_away"),
                55: ("team_throws_total", "team_throws_home", "team_throws_away"), 13: ("team_offsides_total", None, None)}
OLD_OFF_LANE_TYPES = frozenset((55, 13))
OLD_MARKET_SUB = {m: t for t, ms in OLD_SUBGAMES.items() if t not in OLD_OFF_LANE_TYPES for m in ms if m}
OLD_WINDOW = ("team_fouls_", "team_shots_", "team_sot_")
OLD_EXTRA = ("team_corners_", "team_yellow_")
OLD_HIDDEN_KINDS = ("to", "hj")

MARKETS = ("totals", "team_totals_home", "team_totals_away", "btts", "double_chance", "h2h", "alternate_totals_corners", "alternate_totals_cards", "team_corners_total", "team_corners_home",
           "team_yellow_total", "team_shots_total", "team_shots_home", "team_sot_total", "team_sot_away", "team_fouls_total", "team_fouls_home", "team_throws_total", "team_throws_home",
           "team_offsides_total", "player_shots", "player_to_score", "spreads", "truc_inconnu")


def old_lane(market):
    fam = ensemble.family(market)
    return "sharpgap" if fam in OLD_SHARPGAP else "stats" if fam in OLD_STATS_LANE else None


def pick(market, **kw):
    return {"market": market, "name": "Under", "point": 9.5, "xodds": 1.9, "sport_key": "soccer_epl", "strategy": "stats", "prob": 0.6} | kw


def check_initial(label):
    C.ENSEMBLE_ENABLED = True
    for m in MARKETS:
        ol = old_lane(m)
        assert lanes.lane_of_market(m) == ol, (label, m)
        assert lanes.ticket_ok({"market": m}) == (ol is not None), (label, m)
        for kind, want in (("value", ol or "fill"), ("autre", "fill")):
            assert ensemble.strategy_of(pick(m, xkind=kind)) == want, (label, m, kind)
        assert ensemble.strategy_of(pick(m, xodds=None, xkind="value")) is None
        c = pick(m)
        old_usable = (not ol or lanes.mode(ol, c["sport_key"]) == "on") and not (c["strategy"] == "fill" and not C.FILL_LEGS)
        assert lanes.usable(c) == (old_usable and lanes._edge_ok(c)), (label, m)
        old_priced = not (ol and lanes.mode(ol, c["sport_key"]) == "off")
        assert lanes.priced(c) == old_priced, (label, m)
        assert closeread.families.closeread(F.family_of(m)) == (ol == "stats"), (label, m)
    assert xbet.market_sub() == OLD_MARKET_SUB, label
    assert F.window_prefix() == OLD_WINDOW and statswindow.prefix() == OLD_WINDOW + OLD_EXTRA, label
    ts5 = next(t for t in C.FAIR_SHOTS_TIERS if t[0] == "ts5")
    assert F.filter_prefix(ts5[1]) == OLD_WINDOW and F.filter_prefix(next(t for t in C.FAIR_SHOTS_TIERS if t[0] == "tc5")[1]) == ("team_sot_",), label
    old_season = frozenset(("totals", "btts", "double_chance", "h2h", "team_totals_home", "team_totals_away") + tuple(m for m in seasonlane.MARKETS if not m.startswith(("team_throws", "team_offsides"))
                           and m not in ("totals", "btts", "double_chance", "h2h", "team_totals_home", "team_totals_away")))
    assert seasonlane.markets() == old_season, label
    assert not any(m.startswith(("team_throws", "team_offsides")) for m in cupstats.stat_markets()), label
    assert set(CB.hidden_kinds()) == set(OLD_HIDDEN_KINDS) | {"j"} and CB.hidden_kinds()[:2] == OLD_HIDDEN_KINDS, label     # seul écart déclaré : « Joueurs »
    for mode in ("on", "ghost", "off"):                                                                # stats de coupe : STATS_CUPS_MODE = comportement d'avant
        C.STATS_CUPS_MODE = mode
        cup = pick("team_fouls_total", cup=True)
        old_cup_usable = mode == "on" and C.STRATEGY_STATS == "on" and (lanes.state().get("stats") or {}).get("mode") != "ghost" and lanes._edge_ok(cup)
        assert lanes.usable(cup) == old_cup_usable, (label, mode)
        assert lanes.priced(cup) == (mode != "off" and C.STRATEGY_STATS != "off"), (label, mode)
        assert cupstats.mode() == mode, (label, mode)
    C.STATS_CUPS_MODE = "ghost"


def test_defaults_equal_old_lists():
    store.remove(F.FILE) if store.exists(F.FILE) else None
    F.reset()
    check_initial("défauts")
    print("ok  état initial (valeurs par défaut) = anciennes listes : voie, ticket, étiquette, usable / priced, sous-matchs, préfixes, saison, coupes, clôture")


def test_migrated_document_equal_old_lists():
    from kelly import migrate
    store.remove(F.FILE) if store.exists(F.FILE) else None
    store.save("migrations.json", {})
    migrate.families_init()
    F.reset()
    assert F.doc()["throws"]["state"] == "hors_ticket"
    check_initial("document migré")
    print("ok  état initial (document migré) = anciennes listes")


def test_state_changes_move_exactly_their_decisions():
    day = date(2026, 10, 19)
    store.remove(F.FILE) if store.exists(F.FILE) else None
    F.reset()
    F.set_state("throws", "fantome", "essai", day)                      # fantome : lu, jamais un ticket
    assert "team_throws_total" in xbet.market_sub() and "team_throws_home" in xbet.market_sub() and "team_offsides_total" not in xbet.market_sub()
    assert "team_throws_" in statswindow.prefix() and "team_throws_" in F.window_prefix() and not lanes.ticket_ok({"market": "team_throws_total"})
    assert lanes.lane_of_market("team_throws_total") is None and ensemble.strategy_of(pick("team_throws_total", xkind="value")) == "fill"
    assert closeread.families.closeread("throws") and "team_throws_total" in seasonlane.markets() and "team_throws_total" in cupstats.stat_markets()
    assert "to" in CB.hidden_kinds(), "fantome : toujours pas de case Touches"
    F.set_state("throws", "jouee", "main", day)                          # jouee : voie stats, ticket, case Touches
    assert lanes.lane_of_market("team_throws_total") == "stats" and lanes.ticket_ok({"market": "team_throws_total"}) and "to" not in CB.hidden_kinds()
    assert ensemble.strategy_of(pick("team_throws_total", xkind="value")) == "stats"
    F.set_state("corners", "fantome", "carte", day, demoted=True)        # une famille de voie rétrogradée : plus de ticket, toujours lue
    assert not lanes.ticket_ok({"market": "alternate_totals_corners"}) and "alternate_totals_corners" in xbet.market_sub() and closeread.families.closeread("corners")
    assert ensemble.strategy_of(pick("alternate_totals_corners", xkind="value")) == "fill" and "co" in CB.hidden_kinds()
    F.set_state("corners", "hors_ticket", "essai", day)
    assert "alternate_totals_corners" not in xbet.market_sub() and not closeread.families.closeread("corners")
    F.set_state("stats_cup", "jouee", "main", day)
    C.STATS_CUPS_MODE = "ghost"
    assert cupstats.mode() == "on" and lanes.usable(pick("team_fouls_total", cup=True))
    C.STATS_CUPS_MODE = "ghost"
    assert F.window_prefix() != ()
    for f in ("fouls", "shots", "sot"):
        F.set_state(f, "hors_ticket", "essai", day)
    assert F.window_prefix() == ("team_throws_",) and F.filter_prefix(("team_shots_",)) == F.NO_MARKET, "plus aucun marché : jamais un filtre vide qui voudrait dire « tout »"
    store.remove(F.FILE)
    F.reset()
    print("ok  chaque état déplace exactement ses décisions (lecture, ticket, voie, étiquette, sélecteur, coupes)")


def test_fantome_family_never_reaches_a_builder():
    """Une famille FANTÔME est lue et chiffrée (CLV), mais aucun constructeur de ticket ne la prend : jambes du matin, voie saison (pool, auto, remplaçants), « usable » de la saison. La fenêtre des stats
    (relue, jamais annoncée ni combinée) est testée dans test_stats_window."""
    day = date(2026, 10, 19)
    store.remove(F.FILE) if store.exists(F.FILE) else None
    F.reset()

    def leg(market, **kw):
        return {"id": "m1", "key": f"m1|{market}||9.5|Under", "market": market, "name": "Under", "point": 9.5, "sport_key": "soccer_epl", "prob": 0.9, "conf": 0.9, "min_odds": 1.1, "fair": 1.1,
                "xodds": 1.9, "xev": 0.5, "xref": [1, 2, 9.5], "commence": "2099-01-01T15:00:00Z", "strategy": "stats", "odds": 1.9, "who": None, "group": "soccer", "why": "x"} | kw
    ctrl, thr = leg("team_shots_total"), leg("team_throws_total")
    for fam_state in ("hors_ticket", "fantome", "proposee"):
        F.set_state("throws", fam_state, "essai", day)
        assert lanes.ticket_ok(thr) is False
        assert [c["market"] for c in CB.fair_legs([ctrl, thr])] == ["team_shots_total"], fam_state
        assert not [c for c in CB.quality_legs([thr]) if c["market"] == "team_throws_total"], fam_state
        sea = lambda c: c | {"lane": "saison", "strategy": "saison", "sval": 1, "id": c["market"]}      # noqa: E731
        assert CB.season_pool([sea(ctrl), sea(thr)]) and all(c["market"] != "team_throws_total" for c in CB.season_pool([sea(ctrl), sea(thr)])), fam_state
        plan = CB.season_auto_plan([sea(ctrl), sea(leg("team_fouls_total", id="m2", key="m2|f")) | {"id": "m2"}, sea(leg("team_sot_total", key="m4|s")) | {"id": "m4"}, sea(thr) | {"id": "m3"}])
        assert plan and all(l["market"] != "team_throws_total" for _, combo in plan for l in (combo[0] if combo else [])), fam_state
        assert lanes.usable(sea(thr)) is False and lanes.usable(sea(ctrl)) is True, fam_state
    F.set_state("throws", "jouee", "main", day)
    assert [c["market"] for c in CB.fair_legs([ctrl, thr])] == ["team_shots_total", "team_throws_total"], "jouee : sur les tickets"
    store.remove(F.FILE)
    F.reset()
    print("ok  famille hors_ticket / fantôme / proposée : aucun constructeur ne la prend (matin, saison) ; jouee : oui")


def test_sample_output_identical():
    """`python bot.py --sample` : sortie standard identique à celle d'avant le câblage (fichier d'or, journée fictive déterministe)."""
    env = {k: v for k, v in os.environ.items() if k not in ("DATA_DIR", "TELEGRAM_TOKEN", "XBET_PROXY")}
    env["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-famequiv-sample-")
    out = subprocess.run([sys.executable, "bot.py", "--sample"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    gold = open(os.path.join(ROOT, "tests", "golden", "sample_daily.txt"), encoding="utf-8").read()
    assert out.returncode == 0 and out.stdout == gold, (out.returncode, out.stdout[:300], out.stderr[-300:])
    print("ok  bot.py --sample : sortie identique au fichier d'or d'avant le câblage")


if __name__ == "__main__":
    test_defaults_equal_old_lists()
    test_migrated_document_equal_old_lists()
    test_state_changes_move_exactly_their_decisions()
    test_fantome_family_never_reaches_a_builder()
    test_sample_output_identical()
    print("tests équivalence des familles : OK")
