"""Familles de marché (kelly/families.py, kelly/familyeval.py), hors ligne, sur journaux FICTIFS : prédicats et valeurs par défaut, calibration (dans la marge, hors marge, non mesurable), CLV fantôme,
chaque transition du lundi (hors_ticket -> fantome, fantome -> proposee, fantome -> hors_ticket sur deux lundis à 7 jours exacts, jouee -> fantome par la carte), jamais de passage automatique à
jouee, retour à la main, garde des journaux illisibles, un seul passage par lundi, constantes = pré-déclaration.
Lancer : python -m tests.test_families"""
import hashlib
import json
import math
import os
import tempfile
from datetime import date, timedelta

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-families-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-families-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY"):
    os.environ.pop(k, None)

from kelly import config as C, cupstats, ensemble, families as F, familyeval as FE, lanestats, store  # noqa: E402

LEDGER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "families_ledger.json")
MON = date(2026, 10, 19)                                    # un lundi
TH = "team_throws_total"


def fair_rows(market, n, wins, prob=0.6, matches=None, tag="f"):
    """n lignes win / loss de fair.json : `wins` gagnées, annoncé `prob`, réparties sur `matches` matchs."""
    matches = matches or n
    return [{"key": f"{tag}-{market}-{i}", "id": f"{tag}-m{i % matches}", "market": market, "name": "Under", "point": 9.5, "prob": prob,
             "result": "win" if i < wins else "loss"} for i in range(n)]


def interleaved(market, n, rate, prob, tag="f"):
    """n lignes dont `rate` gagnent, mélangées (les grappes de match ne se compensent pas par construction)."""
    wins, rows = 0, []
    for i in range(n):
        w = int((i + 1) * rate) > wins
        wins += w
        rows.append({"key": f"{tag}-{market}-{i}", "id": f"{tag}-m{i}", "market": market, "name": "Under", "point": 9.5, "prob": prob, "result": "win" if w else "loss"})
    return rows


def ghost(market, n, base, tag="g", matches=None):
    """n lignes réglées du journal d'ensemble avec un CLV autour de `base` (alternance +- 0,02 : l'intervalle n'est jamais nul)."""
    matches = matches or n
    out = []
    for i in range(n):
        c = base + (0.02 if i % 2 else -0.02)
        out.append({"key": f"{tag}-{market}-{i}", "id": f"{tag}-m{i % matches}", "sport_key": "soccer_epl", "market": market, "name": "Under", "point": 9.5, "result": "win", "xodds": 2.0,
                    "xclose": {"odds": 2.0 / math.exp(c), "read": "x", "lead_min": 30, "susp": False}, "strategy": "fill", "date": "2026-10-15"})
    return out


def reset(fair=(), journal=(), cups=(), doc=None):
    store.save(FE.FAIR_FILE, list(fair))
    store.save(ensemble.LOG_FILE, list(journal))
    store.save(cupstats.LOG, list(cups))
    store.remove(F.FILE) if store.exists(F.FILE) else None
    if doc is not None:
        store.save(F.FILE, doc)
    F.reset()
    lanestats._memo.update(key=None, rows=None)
    C.FAMILIES_AUTO = True


def test_defaults_and_predicates():
    reset()
    for f in ("goals", "h2h", "dc", "btts", "team_goals"):
        assert F.state_of(f) == "jouee" and F.lane(f) == "sharpgap" and F.ticket(f) and F.reads(f) and not F.closeread(f)
    for f in ("corners", "cards", "yellow", "shots", "sot", "fouls"):
        assert F.state_of(f) == "jouee" and F.lane(f) == "stats" and F.closeread(f)
    for f in ("throws", "offsides", "player", "inconnue"):
        assert F.state_of(f) == "hors_ticket" and F.lane(f) is None and not F.ticket(f) and not F.reads(f) and not F.closeread(f)
    assert F.state_of("stats_cup") == "fantome" and F.reads("stats_cup") and not F.ticket("stats_cup")
    assert F.voie_of("throws") == "stats" and F.voie_of("player") is None and F.voie_of("goals") == "sharpgap"
    assert F.of_market("totals") == "jouee" and F.of_market("team_throws_total") == "hors_ticket" and F.of_market("alternate_totals_corners") == "jouee"
    assert F.filter_prefix(("team_fouls_", "team_throws_", "team_offsides_", "team_shots_", "team_sot_")) == ("team_fouls_", "team_shots_", "team_sot_")
    assert F.filter_prefix("team_throws_") == F.NO_MARKET and F.filter_prefix(("player_",)) == F.NO_MARKET and F.filter_prefix(()) == ()
    assert not "team_throws_total".startswith(F.NO_MARKET) and F.filter_prefix(("team_shots_",)) == ("team_shots_",)
    assert F.lane_families("sharpgap") == ("goals", "h2h", "dc", "btts", "team_goals") and F.lane_families("stats") == ("corners", "cards", "yellow", "shots", "sot", "fouls")
    print("ok  valeurs par défaut = anciennes listes (voies, tickets, lectures, préfixes)")


def test_cup_rule_keeps_the_old_behaviour():
    reset()
    for mode in ("on", "ghost", "off"):
        C.STATS_CUPS_MODE = mode
        assert F.cup_ok() == (mode == "on"), mode
    C.STATS_CUPS_MODE = "ghost"
    F.set_state("stats_cup", "jouee", "main", MON, "moi")
    assert F.cup_ok() is True
    C.STATS_CUPS_MODE = "off"
    assert F.cup_ok() is False, "off prime"
    C.STATS_CUPS_MODE = "ghost"
    print("ok  stats de coupe : on / ghost / off redonnent le comportement d'avant tant que stats_cup est fantome")


def test_migration_is_neutral_and_idempotent():
    reset()
    before = {f: (F.state_of(f), F.voie_of(f), F.lane(f)) for f in list(F.DEFAULTS) + ["autre"]}
    assert F.ensure(MON) is True and F.ensure(MON) is False
    F.reset()
    assert {f: (F.state_of(f), F.voie_of(f), F.lane(f)) for f in before} == before, "le document migré répond comme les valeurs par défaut"
    assert F.doc()["corners"]["how"] == "migration" and F.doc()["_log"] == []
    from kelly import migrate
    reset()
    assert migrate.families_init() == 1 and migrate.families_init() == 0 and store.load("migrations.json", {}).get("families_init") is True and store.exists(F.FILE)
    assert F.state_of("throws") == "hors_ticket" and F.state_of("corners") == "jouee"
    print("ok  migration : même réponse que les défauts, une seule fois")


def test_calibration_measure():
    reset()
    ok = FE.calib_stat("throws", interleaved(TH, 40, 0.6, 0.6))
    assert ok["measurable"] and ok["in_margin"] and ok["n"] == 40
    bad = FE.calib_stat("throws", interleaved(TH, 60, 0.2, 0.6))
    assert bad["measurable"] and not bad["in_margin"], "20 % réalisé pour 60 % annoncé : hors marge"
    few = FE.calib_stat("throws", interleaved(TH, 29, 0.6, 0.6))
    assert not few["measurable"] and not few["in_margin"], "29 résultats : non mesurable (et jamais « dans la marge »)"
    one = FE.calib_stat("throws", fair_rows(TH, 40, 24, matches=1))
    assert not one["measurable"], "un seul match : pas d'intervalle"
    rows = interleaved(TH, 40, 0.6, 0.6) + [{"key": "v", "id": "v", "market": TH, "prob": 0.6, "result": "void"}, {"key": "n", "id": "n", "market": TH, "prob": 0.6, "result": None}]
    assert FE.calib_stat("throws", rows)["n"] == 40, "void et sans résultat exclus"
    assert FE.calib_stat("offsides", interleaved(TH, 40, 0.6, 0.6))["n"] == 0, "une autre famille n'entre pas"
    print("ok  calibration : dans la marge / hors marge / non mesurable (29 résultats, un match), void exclu")


def test_ghost_measure():
    reset()
    good = FE.ghost_stat("throws", ghost(TH, 200, 0.03))
    assert good["nc"] == 200 and good["passed"] and good["clv"] > 0
    assert not FE.ghost_stat("throws", ghost(TH, 199, 0.03))["passed"], "199 lignes : pas assez"
    assert not FE.ghost_stat("throws", ghost(TH, 300, -0.03))["passed"], "CLV négatif"
    assert not FE.ghost_stat("throws", ghost(TH, 300, 0.0))["passed"], "zéro dans l'intervalle"
    rows = ghost(TH, 250, 0.03)
    for r in rows[:60]:
        r["xclose"] = None
    assert FE.ghost_stat("throws", rows)["nc"] == 190 and not FE.ghost_stat("throws", rows)["passed"], "sans relevé de clôture : pas de CLV"
    assert FE.ghost_stat("throws", ghost(TH, 200, 0.03) + ghost(TH, 200, 0.03))["n"] == 200, "une clé une seule fois"
    print("ok  CLV fantôme : 200 lignes, moyenne >= 0, borne basse > 0")


def monday_state(**kw):
    return FE.apply(kw.get("day", MON))


def test_hors_ticket_to_fantome():
    reset(fair=interleaved(TH, 40, 0.6, 0.6))
    ch = FE.apply(MON)
    assert [(c[0], c[1], c[2]) for c in ch] == [("throws", "hors_ticket", "fantome")], ch
    assert F.state_of("throws") == "fantome" and F.reads("throws") and not F.ticket("throws") and F.doc()["throws"]["since"] == str(MON)
    assert F.state_of("offsides") == "hors_ticket" and F.state_of("player") == "hors_ticket", "pas de résultats : inchangés"
    assert F.state_of("corners") == "jouee"
    reset(fair=interleaved(TH, 29, 0.6, 0.6))
    assert FE.apply(MON) == [] and F.state_of("throws") == "hors_ticket", "29 résultats : non mesurable"
    reset(fair=interleaved(TH, 60, 0.2, 0.6))
    assert FE.apply(MON) == [] and F.state_of("throws") == "hors_ticket", "hors marge : reste hors_ticket"
    reset(fair=interleaved("player_shots", 80, 0.6, 0.6))
    assert FE.apply(MON) == [] and F.state_of("player") == "hors_ticket", "joueurs : aucune voie, jamais admissible"
    print("ok  hors_ticket -> fantome : >= 30 résultats dans la marge ; sinon rien ; joueurs jamais")


def test_fantome_to_proposee_and_never_jouee():
    reset(fair=interleaved(TH, 40, 0.6, 0.6), journal=ghost(TH, 200, 0.03), doc={"throws": {"state": "fantome", "voie": "stats", "demoted": False}})
    ch = FE.apply(MON)
    assert [(c[0], c[2]) for c in ch] == [("throws", "proposee")] and F.state_of("throws") == "proposee"
    assert F.reads("throws") and not F.ticket("throws") and F.lane("throws") is None, "proposee : aucun ticket"
    assert FE.apply(MON) is None, "un seul passage par lundi"
    assert FE.apply(MON + timedelta(days=7)) == [] and F.state_of("throws") == "proposee", "jamais de passage automatique à jouee"
    reset(fair=interleaved(TH, 40, 0.6, 0.6), journal=ghost(TH, 150, 0.03), doc={"throws": {"state": "fantome", "voie": "stats"}})
    assert FE.apply(MON) == [] and F.state_of("throws") == "fantome", "150 lignes : trop tôt"
    reset(fair=interleaved(TH, 40, 0.6, 0.6), journal=ghost(TH, 300, -0.04), doc={"throws": {"state": "fantome", "voie": "stats"}})
    assert FE.apply(MON) == [] and F.state_of("throws") == "fantome", "CLV négatif"
    print("ok  fantome -> proposee : 200 lignes à CLV >= 0 et borne basse > 0 ; jamais jouee seule")


def test_stats_cup_to_proposee():
    cups = [dict(r, strategy="stats") for r in ghost("alternate_totals_corners", 200, 0.03, tag="c")]
    reset(fair=[], journal=[], cups=cups)
    assert F.state_of("stats_cup") == "fantome"
    ch = FE.apply(MON)
    assert [(c[0], c[2]) for c in ch] == [("stats_cup", "proposee")] and F.state_of("stats_cup") == "proposee"
    print("ok  stats de coupe : fantome -> proposee sur les lignes du journal des coupes")


def test_fantome_to_hors_ticket_two_mondays():
    docfant = {"throws": {"state": "fantome", "voie": "stats", "demoted": False}}
    bad = interleaved(TH, 60, 0.2, 0.6)
    reset(fair=bad, doc=docfant)
    assert FE.apply(MON) == [] and F.state_of("throws") == "fantome" and F.doc()["_watch"]["throws"]["on"] == str(MON)
    assert FE.apply(MON) is None and F.doc()["_watch"]["throws"]["on"] == str(MON), "même lundi relancé : la chaîne n'avance pas"
    ch = FE.apply(MON + timedelta(days=7))
    assert [(c[0], c[2]) for c in ch] == [("throws", "hors_ticket")] and F.state_of("throws") == "hors_ticket"
    reset(fair=bad, doc=docfant)
    FE.apply(MON)
    assert FE.apply(MON + timedelta(days=14)) == [] and F.state_of("throws") == "fantome", "un lundi manqué casse la chaîne (14 jours)"
    assert F.doc()["_watch"]["throws"]["on"] == str(MON + timedelta(days=14)), "nouvelle chaîne"
    reset(fair=bad, doc=docfant)
    FE.apply(MON)
    store.save(FE.FAIR_FILE, interleaved(TH, 60, 0.6, 0.6))
    assert FE.apply(MON + timedelta(days=7)) == [] and F.state_of("throws") == "fantome" and "throws" not in F.doc()["_watch"], "de retour dans la marge : chaîne effacée"
    reset(fair=bad, doc=docfant)
    FE.apply(MON)
    store.save(FE.FAIR_FILE, interleaved(TH, 20, 0.2, 0.6))
    assert FE.apply(MON + timedelta(days=7)) == [] and F.state_of("throws") == "fantome", "non mesurable : jamais hors marge"
    print("ok  fantome -> hors_ticket : hors marge deux lundis à 7 jours exacts, chaîne cassée sinon")


def test_demoted_never_auto_back():
    doc = {"corners": {"state": "fantome", "voie": "stats", "demoted": True, "from": "jouee"}}
    reset(fair=interleaved("alternate_totals_corners", 80, 0.2, 0.6), journal=ghost("alternate_totals_corners", 300, 0.05), doc=doc)
    assert FE.apply(MON) == [] and FE.apply(MON + timedelta(days=7)) == [] and FE.apply(MON + timedelta(days=14)) == []
    assert F.state_of("corners") == "fantome", "rétrogradée : ni proposée, ni renvoyée en hors_ticket : retour à la main"
    print("ok  famille rétrogradée : aucun retour ni sortie automatique")


def test_jouee_to_fantome_by_map():
    reset(fair=[], journal=[ghost("x", 1, 0)[0]])
    real = FE.red_families
    FE.red_families = lambda: {"corners"}
    try:
        ch = FE.apply(MON)
    finally:
        FE.red_families = real
    assert [(c[0], c[1], c[2]) for c in ch] == [("corners", "jouee", "fantome")], ch
    assert F.state_of("corners") == "fantome" and F.doc()["corners"]["demoted"] is True and F.state_of("cards") == "jouee"
    assert not F.ticket("corners") and F.reads("corners") and F.closeread("corners")
    assert F.lane("corners") is None and F.lane_families("stats") == ("cards", "yellow", "shots", "sot", "fouls")
    # red_families : mécanisme éteint -> vide
    C.EDGEMAP_GATE = False
    assert FE.red_families() == set()
    print("ok  jouee -> fantome : carte rouge committée, famille rétrogradée marquée")


def test_guard_and_switch():
    reset(fair=interleaved(TH, 40, 0.6, 0.6))
    store.remove(ensemble.LOG_FILE)
    assert FE.apply(MON) is None and F.state_of("throws") == "hors_ticket" and F.doc()["skipped"] == {"week": str(MON), "docs": [ensemble.LOG_FILE]}, F.doc()
    store.save(ensemble.LOG_FILE, [])
    F.reset()
    ch = FE.apply(MON)
    assert ch and F.state_of("throws") == "fantome" and "skipped" not in F.doc(), "le journal revient : le lundi s'évalue, `skipped` effacé"
    reset(fair=interleaved(TH, 40, 0.6, 0.6))
    assert FE.apply(MON + timedelta(days=1)) is None and F.state_of("throws") == "hors_ticket", "pas un lundi"
    C.FAMILIES_AUTO = False
    assert FE.apply(MON) is None and F.state_of("throws") == "hors_ticket", "FAMILIES_AUTO coupé"
    C.FAMILIES_AUTO = True
    print("ok  garde : journal illisible = aucune écriture ; hors lundi et FAMILIES_AUTO coupé = rien")


def test_history_and_set_state():
    reset()
    assert F.set_state("throws", "fantome", "essai", MON, "moi") == ("hors_ticket", "fantome")
    assert F.set_state("throws", "fantome", "essai", MON, "moi") is None
    h = F.history()
    assert h == [{"fam": "throws", "from": "hors_ticket", "to": "fantome", "on": str(MON), "how": "essai", "by": "moi"}]
    try:
        F.set_state("throws", "inconnu", "x", MON)
        raise AssertionError("état inconnu accepté")
    except ValueError:
        pass
    store.save(F.FILE, {"throws": "abîmé", "corners": {"state": "n'importe quoi"}, "_log": "x"})
    F.reset()
    assert F.state_of("throws") == "hors_ticket" and F.state_of("corners") == "jouee" and F.history() == [], "document abîmé : valeurs par défaut"
    print("ok  historique tracé avec date et auteur ; document abîmé = défauts")


def test_declaration_matches_code():
    led = json.load(open(LEDGER, encoding="utf-8"))
    pre = led["prereg"]
    assert hashlib.sha256(json.dumps(pre, sort_keys=True, ensure_ascii=False).encode()).hexdigest() == led["prereg_sha"], "pré-déclaration inchangée depuis son commit"
    assert (C.FAM_MIN_CALIB, C.FAM_MIN_GHOST) == (pre["thresholds"]["FAM_MIN_CALIB"], pre["thresholds"]["FAM_MIN_GHOST"]) == (30, 200) and C.FAMILIES_AUTO is True
    ini = pre["initial_state"]
    assert {f for f, (s, v) in F.DEFAULTS.items() if s == "jouee" and v == "sharpgap"} == set(ini["jouee_sharpgap"])
    assert {f for f, (s, v) in F.DEFAULTS.items() if s == "jouee" and v == "stats"} == set(ini["jouee_stats"])
    assert {f for f, (s, v) in F.DEFAULTS.items() if s == "fantome"} == set(ini["fantome"]) and {f for f, (s, v) in F.DEFAULTS.items() if s == "hors_ticket"} == set(ini["hors_ticket"])
    assert set(F.STATES) == set(pre["states"]) and F.FILE == "families.json"
    print("ok  seuils et état initial du code = pré-déclaration (empreinte inchangée)")


if __name__ == "__main__":
    test_defaults_and_predicates()
    test_cup_rule_keeps_the_old_behaviour()
    test_migration_is_neutral_and_idempotent()
    test_calibration_measure()
    test_ghost_measure()
    test_hors_ticket_to_fantome()
    test_fantome_to_proposee_and_never_jouee()
    test_stats_cup_to_proposee()
    test_fantome_to_hors_ticket_two_mondays()
    test_demoted_never_auto_back()
    test_jouee_to_fantome_by_map()
    test_guard_and_switch()
    test_history_and_set_state()
    test_declaration_matches_code()
    print("tests familles de marché : OK")
