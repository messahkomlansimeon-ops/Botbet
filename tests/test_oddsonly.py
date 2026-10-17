"""Voie cotes, mécanismes purs (kelly/oddsonly.py) sur des COTES FICTIVES : cohérence 1N2 <-> double chance (1a, deux sens), totaux d'équipe -> total / les 2 marquent (1b), lignes successives en
leave-one-out (1c), retard sur les autres books (2), mouvement (3, observation), rejets (overround, ligne entière, familles hors voies), statut valeur / suspect. Hors ligne.
Lancer : python -m tests.test_oddsonly"""
import math
import os
import tempfile
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-oddsonly-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-oddsonly-cache-")
for k in ("TELEGRAM_TOKEN", "XBET_PROXY"):
    os.environ.pop(k, None)

from kelly import oddsonly as O, strength  # noqa: E402

H, A = "Home", "Away"
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
MARGIN = 1.05


def odds(p, m=MARGIN):
    return round(1 / (p * m), 3)


def book_h2h(p1=0.5, px_=0.27, p2=0.23, m=MARGIN):
    return {("h2h", H, None): odds(p1, m), ("h2h", "Draw", None): odds(px_, m), ("h2h", A, None): odds(p2, m)}


def book_dc(p1=0.5, px_=0.27, p2=0.23, m=1.05):
    return {("double_chance", f"{H} or Draw", None): odds(p1 + px_, m), ("double_chance", f"{H} or {A}", None): odds(p1 + p2, m),
            ("double_chance", f"Draw or {A}", None): odds(px_ + p2, m)}


def pois_over(mu, L):
    k = int(math.floor(L))
    return 1 - sum(strength.pois_pmf(mu, k + 6)[: k + 1])


def nb_over(mu, r, L):
    k = int(math.floor(L))
    return 1 - sum(strength.nb_pmf(mu, 1 / r, cap=k + 60)[: k + 1])


def ladder(market, fn, lines, m=MARGIN):
    out = {}
    for L in lines:
        p = fn(L)
        out[(market, "Over", L)] = odds(p, m)
        out[(market, "Under", L)] = odds(1 - p, m)
    return out


def by(rows, **kw):
    return [r for r in rows if all(r.get(k) == v for k, v in kw.items())]


def test_1a():
    px = book_h2h() | book_dc()
    rows = O.mech_1a(px, H, A)
    assert len(rows) == 6 and {r["sens"] for r in rows} == {"1N2->DC", "DC->1N2"}
    assert all(-0.09 < r["edge"] < 0 for r in rows), [(r["name"], r["edge"]) for r in rows]       # cohérent : l'écart est la marge du bookmaker (≈ −5 %), jamais une valeur
    # double chance 1X payée trop cher (prix relevé trop haut) : écart >= 3 % dans le sens 1N2 -> DC
    px2 = dict(px)
    px2[("double_chance", f"{H} or Draw", None)] = round(px[("double_chance", f"{H} or Draw", None)] * 1.10, 3)
    bad = by(O.mech_1a(px2, H, A), name=f"{H} or Draw", sens="1N2->DC")
    assert bad and bad[0]["edge"] >= O.EDGE and O.status_of(bad[0]["edge"]) == "value", bad
    # 1N2 incomplet (cellule manquante) ou overround hors plage : rien dans ce sens
    px3 = dict(px)
    px3.pop(("h2h", "Draw", None))
    assert not by(O.mech_1a(px3, H, A), sens="1N2->DC")
    px4 = book_h2h(m=1.6) | book_dc()
    assert not by(O.mech_1a(px4, H, A), sens="1N2->DC") and by(O.mech_1a(px4, H, A), sens="DC->1N2")
    print("ok  1a : 1N2 <-> double chance dans les deux sens, incohérence détectée, rejets")


def test_1b():
    lh, la = 1.5, 1.1
    px = ladder("team_totals_home", lambda L: pois_over(lh, L), (0.5, 1.5, 2.5, 3.5)) | ladder("team_totals_away", lambda L: pois_over(la, L), (0.5, 1.5, 2.5))
    px |= ladder("totals", lambda L: pois_over(lh + la, L), (1.5, 2.5, 3.5))
    px |= {("btts", "Yes", None): odds((1 - math.exp(-lh)) * (1 - math.exp(-la))), ("btts", "No", None): odds(1 - (1 - math.exp(-lh)) * (1 - math.exp(-la)))}
    rows = O.mech_1b(px)
    assert len(rows) == 8 and all(-0.09 < r["edge"] < -0.01 for r in rows), [(r["market"], r["name"], r["point"], r["edge"]) for r in rows]
    px2 = dict(px)
    px2[("totals", "Over", 2.5)] = round(px[("totals", "Over", 2.5)] * 1.12, 3)
    hit = by(O.mech_1b(px2), market="totals", name="Over", point=2.5)
    assert hit and hit[0]["edge"] >= O.EDGE, hit
    assert O.mech_1b({k: v for k, v in px.items() if k[0] != "team_totals_away"}) == [], "sans les totaux de l'autre équipe : rien"
    print("ok  1b : totaux d'équipe -> total du match et les 2 marquent (Poisson)")


def test_1c():
    fn = lambda L: nb_over(10.0, 60.0, L)       # noqa: E731
    lines = (7.5, 8.5, 9.5, 10.5, 11.5, 12.5)
    px = ladder("alternate_totals_corners", fn, lines)
    rows = O.mech_1c(px)
    assert len(rows) == 12 and all(-0.12 < r["edge"] < 0.02 for r in rows), [(r["name"], r["point"], r["edge"]) for r in rows]
    px2 = dict(px)
    px2[("alternate_totals_corners", "Over", 10.5)] = round(px[("alternate_totals_corners", "Over", 10.5)] * 1.15, 3)
    hit = by(O.mech_1c(px2), name="Over", point=10.5)
    assert hit and hit[0]["edge"] >= 0.06 and O.status_of(hit[0]["edge"]) == "value", hit
    assert all(r["n_lines"] == 5 for r in rows), "leave-one-out : la ligne testée n'entre pas dans son ajustement"
    # sans la ligne testée dans l'ajustement, une ligne fausse ne se « cache » pas : même ligne fausse, ajustement avec elle -> écart plus petit
    pairs = O._team_pairs(px2, "alternate_totals_corners")
    mu_all, r_all = O.fit_nb(pairs, "corners")
    with_self = O._over_nb(mu_all, r_all, 10.5)
    mu_loo, r_loo = O.fit_nb([q for q in pairs if q[0] != 10.5], "corners")
    loo = O._over_nb(mu_loo, r_loo, 10.5)
    assert abs(loo - fn(10.5)) < abs(with_self - fn(10.5)) + 1e-9, (with_self, loo, fn(10.5))
    # moins de 4 paires : pas de test ; ligne entière : ignorée ; touches et hors-jeu : jamais
    assert O.mech_1c(ladder("alternate_totals_corners", fn, (8.5, 9.5, 10.5))) == []
    assert O.mech_1c(ladder("team_throws_total", fn, lines)) == [] and O.mech_1c(ladder("team_offsides_total", fn, lines)) == []
    integer = ladder("alternate_totals_corners", fn, lines) | {("alternate_totals_corners", "Over", 10.0): 1.9, ("alternate_totals_corners", "Under", 10.0): 1.9}
    assert all(r["point"] != 10.0 for r in O.mech_1c(integer))
    print("ok  1c : lignes successives en leave-one-out, rejets (< 4 paires, ligne entière, touches, hors-jeu)")


def test_1c_leave_one_out_spy():
    """Le leave-one-out est vérifié DANS mech_1c : l'ajustement d'une ligne ne reçoit jamais cette ligne."""
    seen = []
    real = O.fit_nb

    def spy(pairs, fam):
        seen.append(sorted(L for L, _ in pairs))
        return real(pairs, fam)
    px = ladder("alternate_totals_corners", lambda L: nb_over(10.0, 60.0, L), (7.5, 8.5, 9.5, 10.5, 11.5, 12.5))
    O.fit_nb = spy
    try:
        rows = O.mech_1c(px)
    finally:
        O.fit_nb = real
    tested = sorted({r["point"] for r in rows})
    assert tested == [7.5, 8.5, 9.5, 10.5, 11.5, 12.5] and len(seen) == 6
    for L, used in zip(tested, seen):
        assert L not in used and len(used) == 5, (L, used)
    print("ok  1c : l'ajustement de chaque ligne exclut cette ligne (leave-one-out sous test direct)")


def pick(market, name, point, p, n_books=3, src="consensus", age_min=10, **kw):
    t = NOW.timestamp() - age_min * 60
    return {"key": f"m1|{market}|{point}|{name}", "id": "m1", "market": market, "name": name, "point": point, "prob_book": p, "prob_src": src, "n_books": n_books,
            "n_sharp": 1 if src in ("pinnacle", "sharp") else 0, "sharp_at": datetime.fromtimestamp(t, timezone.utc).isoformat()} | kw


def test_2():
    px = {("totals", "Over", 2.5): 2.00, ("totals", "Under", 2.5): 1.85, ("alternate_totals_corners", "Over", 9.5): 2.0, ("team_shots_total", "Over", 24.5): 1.9}
    sharp = pick("totals", "Over", 2.5, 0.53, src="pinnacle")
    rows = O.mech_2(px, [sharp], NOW)
    assert len(rows) == 1 and rows[0]["overlap_sharpgap"] is True and abs(rows[0]["edge"] - (0.53 * 2.0 - 1)) < 1e-4 and O.status_of(rows[0]["edge"]) == "value"
    assert O.mech_2(px, [pick("totals", "Over", 2.5, 0.53, src="consensus")], NOW) == [], "buts : un book de référence est exigé"
    cor = pick("alternate_totals_corners", "Over", 9.5, 0.55, n_books=3)
    r = O.mech_2(px, [cor], NOW)
    assert len(r) == 1 and r[0]["overlap_sharpgap"] is False
    assert r[0]["ref"] == "consensus" and "ref" not in O.mech_2(px, [sharp], NOW)[0], "buts : pas d'étiquette ref"
    assert O.mech_2(px, [pick("alternate_totals_corners", "Over", 9.5, 0.55, n_books=1)], NOW) == [], "corners : 1 seul book ordinaire refusé"
    assert O.mech_2(px, [pick("alternate_totals_corners", "Over", 9.5, 0.55, src="sharp", n_books=1)], NOW) == [], "corners : un autre book de référence seul ne suffit pas"
    solo = O.mech_2(px, [pick("alternate_totals_corners", "Over", 9.5, 0.55, src="pinnacle", n_books=1)], NOW)
    assert len(solo) == 1 and solo[0]["ref"] == "pinnacle_seul" and solo[0]["overlap_sharpgap"] is False, "corners : Pinnacle seul accepté, étiqueté"
    assert O.mech_2(px, [pick("alternate_totals_corners", "Over", 9.5, 0.55, src="pinnacle", n_books=3)], NOW)[0]["ref"] == "consensus"
    assert O._new_row(solo[0], "m1", {"home_team": "a", "away_team": "b", "commence_time": "2026-10-18T15:00:00Z"}, "soccer", "2026-10-18", "t")["ref"] == "pinnacle_seul"
    assert O.mech_2(px, [pick("team_shots_total", "Over", 24.5, 0.6)], NOW) == [], "tirs : mécanisme inactif"
    assert O.mech_2(px, [pick("totals", "Over", 2.5, 0.53, src="pinnacle", age_min=400)], NOW) == [], "relevé des books trop vieux"
    assert O.mech_2(px, [pick("team_throws_total", "Over", 10.5, 0.6)], NOW) == []
    assert O.mech_2(px, [pick("totals", "Over", 2.5, 0.53, src="pinnacle", age_min=-30)], NOW) == [], "books plus récents que la lecture 1xBet de plus de 10 min : retard déjà comblé"
    assert O.mech_2(px, [pick("totals", "Over", 2.5, 0.53, src="pinnacle", age_min=-5)], NOW), "tolérance de synchronisation"
    print("ok  2 : retard sur les books (buts via Pinnacle avec recouvrement sharpgap, corners à 2 books, tirs inactif, âge)")


def test_status_and_movement():
    assert O.status_of(0.029) is None and O.status_of(0.03) == "value" and O.status_of(0.15) == "value" and O.status_of(0.151) == "suspect" and O.status_of(None) is None
    now, prev = {("totals", "Over", 2.5): 2.0, ("totals", "Under", 2.5): 1.8}, {("totals", "Over", 2.5): 1.9, ("totals", "Under", 2.5): 1.79}
    mv = O.movement(now, prev, {("totals", "Over", 2.5): 0.5}, {("totals", "Over", 2.5): 0.52})
    assert list(mv) == [("totals", "Over", 2.5)] and mv[("totals", "Over", 2.5)]["mv"] > 0.05 and mv[("totals", "Over", 2.5)]["mv_ref"] < 0
    assert O.mode() == "ghost"
    print("ok  statut valeur / suspect, mouvement (observation), voie fantôme")


def test_evaluate_filters_off_lane():
    px = ladder("team_throws_total", lambda L: nb_over(10, 40, L), (8.5, 9.5, 10.5, 11.5, 12.5)) | book_h2h() | book_dc()
    rows = O.evaluate(px, H, A, [], NOW)
    assert rows and all(r["market"] in ("h2h", "double_chance") for r in rows), "touches : jamais détectées"
    print("ok  evaluate : familles hors voies jamais journalisées")


def test_declaration_matches_code():
    import hashlib
    import json
    led = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "oddsonly_ledger.json")))
    pre = led["prereg"]
    assert hashlib.sha256(json.dumps(pre, sort_keys=True, ensure_ascii=False).encode()).hexdigest() == led["prereg_sha"], "pré-déclaration inchangée depuis son commit"
    from kelly import config as C, ensemble
    assert O.FALLBACK_R == pre["mechanisms"]["1c"]["fallback_r"] and O.MULTS == (0.5, 1.0, 2.0) and "{0,5 ; 1 ; 2}" in pre["mechanisms"]["1c"]["rule"]
    assert (C.ODDSONLY_EDGE, C.ODDSONLY_MAX_EDGE, O.MOVE_MIN) == (0.03, 0.15, 0.03) and "(0,03)" in pre["decision"]["value_leg"] and "(0,15)" in pre["decision"]["value_leg"]
    assert (O.MIN_PAIRS, O.MIN_TEAM_PAIRS) == (4, 2) and "au moins 4 paires" in pre["mechanisms"]["1c"]["rule"] and "au moins 2 paires" in pre["mechanisms"]["1b"]["rule"]
    assert list(ensemble.OVERROUND) == pre["demargination"]["overround"]["ordinary"] and list(ensemble.DC_OVERROUND) == pre["demargination"]["overround"]["double_chance"]
    assert set(pre["families"]["goal"]) == set(O.GOAL_FAMILIES) and set(pre["families"]["stats"]) == {"corners", "cards", "yellow", "shots", "sot", "fouls"}
    am = json.dumps(led["amendments"], ensure_ascii=False)
    assert (O.MV_TOP, O.MV_MAX, O.SYNC_MIN, O.ARCH_DAYS, O.STATE_DAYS) == (5, 20, 10, 90, 2) and "au plus 5 mouvements" in am and "20 lignes de mouvement" in am and "plus de 10 minutes" in am
    assert "pinnacle_seul" in am and "xbet.OFF_LANE_TYPES" in am and "alternate_totals_cards" in am
    assert "plus de 90 jours" in am and "2 jours après" in am and "40 points" in am and "14 sections" in am and "xbet.odds_time None" in am
    assert O.MECHS == ("1a", "1b", "1c", "2", "3") and pre["voie"]["default"] == "ghost" and C.STRATEGY_ODDSONLY == "ghost"
    print("ok  seuils, dispersions, plages d'overround et familles du code = pré-déclaration (empreinte inchangée)")


if __name__ == "__main__":
    test_1a()
    test_1b()
    test_1c()
    test_1c_leave_one_out_spy()
    test_2()
    test_status_and_movement()
    test_evaluate_filters_off_lane()
    test_declaration_matches_code()
    print("tests voie cotes (mécanismes) : OK")
