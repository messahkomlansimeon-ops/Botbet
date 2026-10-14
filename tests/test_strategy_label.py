"""Étiquette `strategy` (sharpgap / stats / fill) : calculée UNE fois quand le pronostic ou le pari réel est mis au prix 1xBet (xprice), recopiée sans être recalculée (suivi, journal
d'ensemble, archive, fair.json, jambes de combinés), jamais écrasée sur une ligne déjà étiquetée, absente sans cote 1xBet ; une ligne d'avant les étiquettes se lit « legacy »
(jamais ré-étiquetée) ; un combiné porte celles de ses jambes et un `has_value`. Hors ligne. Lancer : python -m tests.test_strategy_label"""
import copy
import os
import tempfile
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-strategy-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-strategy-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY", "ENSEMBLE_FROZEN_MODEL"):
    os.environ.pop(k, None)
os.environ["QUIET_HOURS"] = "off"

from kelly import config as C, custom, ensemble as E, fair, store, track, xprice  # noqa: E402

NOW = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)


def fresh():
    for f in (E.FILE, E.LOG_FILE):
        store.save(f, {} if f == E.FILE else [])
    E._memo.clear()


def line(market="totals", name="Over", point=2.5, **kw):
    return {"key": f"m|{market}||{point}|{name}", "id": "m", "sport_key": "soccer_epl", "home": "A", "away": "B", "commence": "2026-10-14T19:00:00Z", "market": market,
            "name": name, "point": point, "prob": 0.7, "p0": 0.7, "xodds": 1.55, "xref": [9, 9, 2.5], "p_model": 0.75, "p_x": 0.62, "p_sharp": 0.70,
            "sharp_at": datetime.now(timezone.utc).isoformat()} | kw


def test_strategy_of():
    fresh()
    assert E.strategy_of(line(xkind="value")) == "sharpgap" and E.strategy_of(line("btts", "Yes", None, xkind="value")) == "sharpgap"
    assert E.strategy_of(line("double_chance", "A or Draw", None, xkind="value")) == "sharpgap" and E.strategy_of(line("team_totals_home", xkind="value")) == "sharpgap"
    assert E.strategy_of(line(xkind="probable")) == "fill", "un favori sharp sans valeur est « fill »"
    assert E.strategy_of(line("alternate_totals_corners", "Under", 9.5, xkind="value")) == "stats"
    assert E.strategy_of(line("team_shots_home", "Over", 5.5, xkind="value")) == "stats" and E.strategy_of(line("team_sot_total", "Over", 8.5, xkind="value")) == "stats"
    assert E.strategy_of(line("alternate_totals_corners", "Under", 9.5, xodds=1.30)) == "fill", "stat sans valeur (xkind absent, ensemble ne valide pas) : « fill »"
    assert E.strategy_of(line("team_throws_total", "Over", 30.5, xkind="value")) == "fill" and E.strategy_of(line("player_shots", "Over", 1.5)) == "fill"
    assert E.strategy_of(line(xkind="value", xodds=None)) is None, "sans cote 1xBet : pas de voie"
    # pari réel (The Odds API) : pas d'xkind, la valeur est rejouée avec ses avis (sharp 0,70 contre cote 1,55 = +8,5 %)
    assert E.strategy_of(line()) == "sharpgap"
    assert E.strategy_of(line(xodds=1.35)) == "fill", "prix qui ne dépasse pas le sharp : « fill »"
    assert E.strategy_label({"strategy": "stats"}) == "stats" and E.strategy_label({}) == "legacy" and E.strategy_label(None) == "legacy"
    print("ok  strategy_of : cinq familles + valeur = sharpgap ; stats + valeur = stats ; le reste = fill ; sans cote = rien ; ancien = legacy")


def test_xprice_sets_once():
    fresh()
    px = {("totals", "Over", 1.5): 1.45, ("totals", "Under", 1.5): 2.9}
    refs = {("totals", "Over", 1.5): [9, 9, 1.5]}
    base = {"key": "m|totals||1.5|Over", "id": "m", "sport_key": "soccer_epl", "home": "A", "away": "B", "commence": "2026-10-14T19:00:00Z", "market": "totals",
            "name": "Over", "point": 1.5, "who": None, "prob": 0.76, "p0": 0.76, "min_odds": 1.4, "conf": 0.6, "n_min": 8, "score": 0.2, "pick": "Plus de 1,5 buts",
            "alts": [["Over", 1.5, 0.76, None], ["Under", 1.5, 0.24, None]]}
    sharp = {"key": base["key"], "id": "m", "market": "totals", "name": "Over", "point": 1.5, "prob": 0.74, "prob_book": 0.74, "prob_src": "pinnacle", "n_sharp": 1,
             "sharp_at": datetime.now(timezone.utc).isoformat()}
    c = copy.deepcopy(base)
    xprice.apply([c], fetch=lambda *a, **k: {"m": {"gid": 900, "px": px, "refs": refs}}, picks=[copy.deepcopy(sharp)])
    assert c["xkind"] == "value" and c["strategy"] == "sharpgap", c
    low = copy.deepcopy(base)                                  # sans sharp : jamais « value » (no_sharp), mais il porte un prix 1xBet = jouable comme complément : « fill »
    assert xprice.price(low, px, refs) == "low" and low["xodds"] and low["strategy"] == "fill", low
    c["strategy"] = "stats"                                     # une relecture remplace la voie par celle du nouveau prix, elle ne s'accumule pas
    xprice.apply([c], fetch=lambda *a, **k: {"m": {"gid": 900, "px": {}, "refs": refs}})
    assert "strategy" not in c, "match relu mais pari absent de 1xBet : plus de voie"
    # pari réel remis au prix 1xBet
    pick = {"key": "p|totals||2.5|Over", "id": "p", "sport_key": "soccer_epl", "home": "A", "away": "B", "commence": "2026-10-14T19:00:00Z", "market": "totals", "name": "Over",
            "point": 2.5, "prob": 0.70, "prob_book": 0.70, "prob_src": "pinnacle", "n_sharp": 1, "odds": 1.50, "best_odds": 1.50, "edge": 0.05, "score": 0.1, "weight": 1.0,
            "why": "", "book": "Pinnacle", "sharp_at": datetime.now(timezone.utc).isoformat()}
    pxp = {("totals", "Over", 2.5): 1.60, ("totals", "Under", 2.5): 2.4}
    assert xprice._pick_price(pick, pxp, {("totals", "Over", 2.5): [1, 2, 2.5]}) is True
    assert pick["strategy"] == "sharpgap" and pick["xodds"] == 1.60
    print("ok  xprice : voie posée à la mise au prix (pronostic « value » et pari réel), retirée si le pari n'est plus jouable, jamais sans valeur sharp pour les cinq familles")


def test_copies_and_legacy():
    fresh()
    c = line(xkind="value", sharp_at=NOW.isoformat()) | {"strategy": "sharpgap", "prob_book": 0.7, "odds": 1.55, "edge": 0.08, "spread": 0.02, "steam": 0.0, "weight": 1.0, "score": 0.1,
                                 "best_odds": 1.55, "book": "Pinnacle", "n_books": 12, "league": "L", "event": "A - B", "pick": "Plus de 2,5 buts", "why": "", "group": "soccer"}
    track.record([c], {c["key"]}, "2026-10-14", NOW)
    r = {x["key"]: x for x in track.load()}[c["key"]]
    assert r["strategy"] == "sharpgap", "le suivi recopie la voie"
    old = dict(r)
    track.record([c | {"strategy": "fill"}], set(), "2026-10-14", NOW)           # une ligne déjà suivie n'est jamais ré-étiquetée
    assert {x["key"]: x for x in track.load()}[c["key"]]["strategy"] == "sharpgap" == old["strategy"]
    # record_gap : voie par défaut sharpgap, mais jamais écrasée sur une ligne déjà étiquetée
    cand = {"key": c["key"], "n": 1, "batch": "b1", "edge": 0.08, "p_sharp": 0.7, "src": "pinnacle", "n_sharp": 1, "fair_odds": 1.43, "xodds": 1.55, "xref": [9, 9, 2.5]}
    rows = track.load()
    rows[0]["strategy"] = "stats"
    track.save(rows)
    track.record_gap([(c, cand, 500, "ABC")], "2026-10-14", NOW)
    assert track.load()[0]["sharpgap"] and track.load()[0]["strategy"] == "stats", "déjà étiquetée : gardée"
    new = line("totals", "Under", 2.5, xkind="value") | {"strategy": None, "prob_book": 0.3, "odds": 2.4, "edge": 0.05, "spread": 0.02, "steam": 0.0, "weight": 1.0, "score": 0.1,
                                                         "best_odds": 2.4, "book": "Pinnacle", "n_books": 12, "league": "L", "event": "A - B", "pick": "Moins de 2,5", "why": "", "group": "soccer"}
    cand2 = cand | {"key": new["key"], "n": 2}
    track.record_gap([(new, cand2, 0, None)], "2026-10-14", NOW)
    assert {x["key"]: x for x in track.load()}[new["key"]]["strategy"] == "sharpgap", "ligne neuve d'un écart annoncé"
    # journal d'ensemble, archive, fair.json
    E.record([c | {"p_model": 0.75, "xp": 0.62}], "2026-10-14", NOW)
    log = store.load(E.LOG_FILE, [])
    assert log[0]["strategy"] == "sharpgap"
    log[0]["result"] = "win"
    E.archive(log, today=NOW.date())
    assert [r["strategy"] for r in E.archive_rows()] == ["sharpgap"]
    fair.record([c], "2026-10-14", shown=[c])
    assert any(r.get("strategy") == "sharpgap" for r in store.load(fair.FILE, []))
    # une ligne d'avant les étiquettes se lit legacy et n'est jamais ré-étiquetée
    assert E.strategy_label({"key": "x", "market": "totals"}) == "legacy"
    print("ok  recopie : suivi, écart annoncé (jamais écrasée), journal, archive, fair.json ; ancienne ligne = legacy")


def test_combos():
    fresh()
    sg, st, fl = line(xkind="value") | {"strategy": "sharpgap"}, line("alternate_totals_corners", "Under", 9.5) | {"strategy": "stats", "key": "k2"}, \
        line(xkind="probable") | {"strategy": "fill", "key": "k3"}
    assert E.has_value([sg, fl]) is True and E.has_value([st]) is True and E.has_value([fl, fl | {"key": "k4"}]) is False
    assert E.has_value([sg, {"key": "old"}]) is True and E.has_value([fl, {"key": "old"}]) is None and E.has_value([]) is None, "voies inconnues : None"
    row = fair.combo_row("5", ([sg, fl], 6.0, 0.2), "2026-10-14")
    assert row["has_value"] is True and [l["strategy"] for l in row["legs"]] == ["sharpgap", "fill"]
    row2 = fair.combo_row("5", ([fl, fl | {"key": "k4"}], 6.0, 0.2), "2026-10-14")
    assert row2["has_value"] is False
    row3 = fair.combo_row("5", ([{"key": "o1"}, {"key": "o2"}], 6.0, 0.2), "2026-10-14")
    assert "has_value" not in row3, "d'avant les étiquettes : rien d'inventé"
    fair.update_combo(row2, ([sg, fl], 6.0, 0.2), "10:00")
    assert row2["has_value"] is True, "un combiné mis à jour recalcule has_value sur ses nouvelles jambes"
    slim = fair._keep([row | {"played": {"odds": 6.0, "stake": 100}, "result": "win", "date": "2020-01-01"}], "2026-10-14")[0]
    assert slim["slim"] is True and [l.get("strategy") for l in slim["legs"]] == ["sharpgap", "fill"], slim        # élagué (ancien, noté, réglé) : les voies des jambes restent
    # fair.json : une ligne qui change de pari prend la voie de sa nouvelle ligne
    fair.record([sg | {"strategy": "sharpgap"}], "2026-10-14", shown=[sg])
    moved = sg | {"key": "m|totals||3.5|Over", "point": 3.5, "strategy": "fill"}
    fair.record([moved], "2026-10-14", shown=[moved])
    rows_ = [r for r in store.load(fair.FILE, []) if r["id"] == "m"]
    assert len(rows_) == 1 and rows_[0]["key"] == moved["key"] and rows_[0]["strategy"] == "fill", rows_
    cid = custom.remember("2026-10-14", 6, 5, 8, ([sg, fl], 6.0, 0.2), stake=100, cur="FCFA")
    assert custom._load("2026-10-14")["items"][cid]["has_value"] is True
    print("ok  combinés : has_value (au moins une jambe à valeur ; tout fill = False ; inconnu = absent), recalculé à la mise à jour, voies gardées dans les jambes")


def main():
    test_strategy_of()
    test_xprice_sets_once()
    test_copies_and_legacy()
    test_combos()
    print("tests étiquette strategy : OK")


if __name__ == "__main__":
    main()
