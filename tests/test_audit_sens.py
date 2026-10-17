"""tools/audit_sens.py (H0 du biais de sens, step1_ledger.json) sur des journaux FICTIFS : populations (référence sans sval, choisies sval 0 / 1, coupes à part, oui / non et 1N2 hors test),
famille du ledger, résidu par match, lisibilité, verdict (tient / rejetée / non lisible), biais de sélection, chargement du journal et de l'archive (pm quand p0 manque).
Lancer : python -m tests.test_audit_sens"""
import json
import os
import sqlite3
import sys
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-auditsens-")
os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-auditsens-cache-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import audit_sens as S  # noqa: E402
from kelly import ensemble  # noqa: E402


def row(i, market="team_corners_total", name="Under", p=0.65, win=True, **kw):
    return {"key": f"k{i}", "id": f"m{i}", "market": market, "name": name, "p0": p, "result": "win" if win else "loss", "ref": 1} | kw


def batch(n, win_share, **kw):
    """n lignes, win_share de gagnées, un match chacune (alternance déterministe)."""
    out, acc = [], 0.0
    for i in range(n):
        acc += win_share
        win = acc >= 1.0
        acc -= 1.0 if win else 0.0
        out.append(row(kw.pop("start", 0) + i if False else i, win=win, **kw))
    return out


def test_populations():
    rows = [row(1), row(2, sval=0), row(3, ref=None, sval=1), row(4, ref=None), row(5, cup=True), row(6, ref=None, sval=0, cup=True),
            row(7, name="Yes", market="btts"), row(8, market="h2h", name="Home"), row(9, p=None), row(10, win=None) | {"result": None}, row(11, market="player_goals"), row(12, name="Over", market="totals")]
    pops = S.populations(rows)
    ids = lambda xs: sorted(i["id"] for i in xs)  # noqa: E731
    assert ids(pops["REF"]) == ["m1", "m12"], ids(pops["REF"])                      # ref = 1 sans sval ; sval = 2 n'est pas une référence
    assert ids(pops["CHOSEN"]) == ["m2", "m3"], ids(pops["CHOSEN"])                  # sval 0 ou 1, avec ou sans ref
    assert ids(pops["cups"]["REF"]) == ["m5"] and ids(pops["cups"]["CHOSEN"]) == ["m6"]
    assert S.family_of("totals") == "totals" and S.family_of("team_totals_home") == "team_totals" and S.family_of("alternate_totals_corners") == "corners"
    assert S.family_of("team_sot_away") == "sot" and S.family_of("team_yellow_total") == "yellow" and S.family_of("team_fouls_total") == "fouls"
    assert {i["sens"] for i in pops["REF"]} == {"moins", "plus"}
    print("ok  populations : référence sans sval, choisies sval 0 / 1, coupes à part, oui / non, 1N2, joueurs, non réglées et sans p0 exclus ; familles du ledger")


def test_verdicts():
    # référence « moins » juste (65 % annoncés, 65 % réalisés) ; choisies « moins » trop confiantes (65 % annoncés, 45 % réalisés)
    ref = [row(i, win=(i % 20) < 13) for i in range(200)]
    cho = [row(1000 + i, win=(i % 20) < 9, ref=None, sval=0) for i in range(200)]
    rows = ref + cho
    pops = S.populations(rows)
    v, why = S.verdict_sens([i for i in pops["REF"] if i["sens"] == "moins"])
    assert v == "tient" and "inclut zéro" in why, (v, why)
    d = S.diff([i for i in pops["CHOSEN"]], [i for i in pops["REF"]])
    assert d[0] < -0.15 and d[0] + d[1] < 0, d                                      # biais de sélection net, négatif
    txt = S.report(rows)
    assert "« moins » : H0 TIENT" in txt and "« plus » : H0 NON LISIBLE" in txt and "NON CONCLUANT" in txt and "biais de sélection" in txt, txt
    # référence « moins » trop confiante : rejetée -> biais de modèle
    bad = [row(i, win=(i % 20) < 9) for i in range(200)]
    txt = S.report(bad)
    assert "« moins » : H0 REJETÉE" in txt and "trop confiant" in txt and "biais de MODÈLE" in txt, txt
    # « plus » sous-estimé (65 % annoncés, 85 % réalisés), plus « moins » juste : rejetée pour plus ; tient pour moins -> rejetée au global
    mix = ref + [row(2000 + i, name="Over", win=(i % 20) < 17) for i in range(200)]
    txt = S.report(mix)
    assert "« plus » : H0 REJETÉE" in txt and "sous-estimé" in txt and "H0 REJETÉE (plus)" in txt, txt
    # deux sens justes : tient pour les deux
    both = ref + [row(3000 + i, name="Over", win=(i % 20) < 13) for i in range(200)]
    assert "H0 TIENT pour les deux sens" in S.report(both)
    print("ok  verdicts : tient / rejetée (moins trop confiant, plus sous-estimé) / non lisible ; biais de sélection négatif mesuré")


def test_readability_and_clusters():
    few = [row(i, win=False) for i in range(30)]                                     # 30 lignes : sous les 100 lignes de l'agrégat
    assert S.verdict_sens(S.populations(few)["REF"])[0] == "non lisible"
    same_match = [dict(row(i, win=(i % 2 == 0)), id="m0") for i in range(150)]       # 150 lignes d'UN seul match : non lisible (30 matchs requis)
    assert S.verdict_sens(S.populations(same_match)["REF"])[0] == "non lisible"
    clustered = [dict(row(i, win=((i // 5) % 2 == 0)), id=f"m{i // 5}") for i in range(150)]          # un match = tout gagné ou tout perdu
    items = S.populations(clustered)["REF"]
    n, m, h, k = S.resid(items)
    iid = S.stats([i["y"] - i["p"] for i in items], list(range(150)))
    assert k == 30 and iid[2] is not None and h is not None and h > iid[2], "lignes d'un même match corrélées : l'intervalle par match est plus large que celui de lignes indépendantes"
    print("ok  lisibilité : 100 lignes ET 30 matchs ; grappes par match")


def test_load_journal_and_archive():
    tmp = tempfile.mkdtemp(prefix="kelly-auditsens-db-")
    con = sqlite3.connect(os.path.join(tmp, "kelly.db"))
    con.execute("create table docs (name text primary key, body text)")
    log = [row(1) | {"p0": 0.6}]
    arch_row = row(2, name="Over") | {"strategy": "saison", "pm": 0.7, "p0": None}
    packed = ensemble._pack({"market": "team_corners_total", "id": "m2", "name": "Over", "point": 4.5, "commence": "2026-10-14T16:00:00Z", "pm": 0.7, "result": "win", "ref": 1, "strategy": "saison"})
    con.execute("insert into docs values (?, ?)", ("season_log.json", json.dumps(log)))
    con.execute("insert into docs values (?, ?)", ("arch/sea/2026-10-14", json.dumps({"cols": list(ensemble.ARCH_COLS), "rows": {"k2": packed, "k1": packed}})))
    con.commit()
    con.close()
    rows = S.load(tmp)
    assert {r["key"] for r in rows} == {"k1", "k2"} and next(r for r in rows if r["key"] == "k1")["p0"] == 0.6, "le journal l'emporte sur l'archive"
    pops = S.populations(rows)
    assert [i["p"] for i in pops["REF"]] == [0.6] and pops["no_p0"] == 1, "archive : pas de p0 (son pm est CALIBRÉ par la voie) -> exclue et comptée, jamais lue comme p0"
    assert "Exclues faute de p0 brut" in S.report(rows) and ": 1 lignes" in S.report(rows)
    assert S.load(tempfile.mkdtemp()) == []
    print("ok  chargement : journal + archive compacte (le journal l'emporte), archive sans p0 exclue et comptée, copie en lecture seule")


def test_exact_numbers_and_boundaries():
    # p0 prioritaire sur pm ; sval hors 0 / 1 n'est pas une ligne choisie ; push exclu
    both = S.populations([row(1, p=0.6) | {"pm": 0.9}, row(2, ref=None, sval=2), row(3, ref=None, sval=1), row(4) | {"result": "push"}])
    assert [i["p"] for i in both["REF"]] == [0.6] and [i["id"] for i in both["CHOSEN"]] == ["m3"] and not both["REF"][1:], both
    # intervalle exact : résidus +1, -1, +1, -1 sur 4 matchs de p0 0 (bornes exclues -> p0 0,5) : moyenne 0, demi-marge 1,96 sqrt(k/(k-1) sum c^2) / n
    n, m, h, k = S.stats([0.5, -0.5, 0.5, -0.5], ["a", "b", "c", "d"])
    assert n == 4 and k == 4 and abs(m) < 1e-12 and abs(h - 1.96 * (4 / 3 * 4 * 0.25) ** 0.5 / 4) < 1e-12, (n, m, h, k)
    # deux échantillons : marge = racine de la somme des carrés des deux marges
    a = S.populations([row(i, win=(i % 2 == 0), p=0.5) for i in range(40)])["REF"]
    b = S.populations([row(100 + i, win=(i % 3 == 0), p=0.5, ref=None, sval=0) for i in range(40)])["CHOSEN"]
    (da, ha), (db, hb) = (S.resid(a)[1], S.resid(a)[2]), (S.resid(b)[1], S.resid(b)[2])
    d, hd = S.diff(b, a)
    assert abs(d - (db - da)) < 1e-12 and abs(hd - (ha ** 2 + hb ** 2) ** 0.5) < 1e-12, "choisies - référence, marge en racine de somme de carrés"
    # lisibilité d'une case : 40 lignes ET 25 matchs
    def cell(n_lines, n_matches):
        items = [dict(i, id=f"m{j % n_matches}") for j, i in enumerate(S.populations([row(j, win=(j % 2 == 0)) for j in range(n_lines)])["REF"])]
        return S.cell_line("x", items)
    assert "trop peu" in cell(39, 39) and "trop peu" in cell(40, 24) and "trop peu" not in cell(40, 25) and "trop peu" not in cell(80, 40)
    # lisibilité de l'agrégat : 100 lignes ET 30 matchs
    def agg(n_lines, n_matches):
        return S.verdict_sens([dict(i, id=f"m{j % n_matches}") for j, i in enumerate(S.populations([row(j, win=(j % 2 == 0)) for j in range(n_lines)])["REF"])])[0]
    assert agg(99, 99) == "non lisible" and agg(100, 29) == "non lisible" and agg(100, 30) != "non lisible"
    # frontière du verdict : l'IC doit EXCLURE zéro (|m| > h), pas |m| > 2h
    items = S.populations([row(i, win=(i % 20) < 9) for i in range(200)])["REF"]
    n, m, h, _ = S.resid(items)
    assert abs(m) > h
    assert S.verdict_sens(items)[0] == "rejetée"
    # signe du biais de sélection dans le rapport : choisies moins bien calibrées que la référence -> négatif ; pe lu depuis pe
    rows = [row(i, win=(i % 20) < 13) | {"pe": 0.1} for i in range(200)] + [row(1000 + i, win=(i % 20) < 9, ref=None, sval=0) for i in range(200)]
    txt = S.report(rows)
    assert "biais de sélection (choisies - référence) : -" in txt, txt
    assert "probabilité retenue pe (mélange avec le marché) : résidu +55" in txt, txt           # y moyen 0,65 - pe 0,1 : +55 points, pas le résidu de p0
    assert "tranche commune p0 0.55-0.75 : choisies n=200, référence n=200" in txt
    only_ref = [row(i, p=0.9, win=True) for i in range(5)] + [row(10 + i, p=0.6, win=True) for i in range(5)] + [row(20 + i, p=0.6, win=True, ref=None, sval=0) for i in range(5)]
    assert "choisies n=5, référence n=5" in S.report(only_ref), "tranche commune appliquée aux DEUX populations"
    print("ok  chiffres exacts : p0 prioritaire, sval, push, intervalle, écart de deux échantillons, lisibilité 39/40, 24/25, 99/100, 29/30, |m| > h, signe de la sélection, pe, tranche commune")


def main():
    test_exact_numbers_and_boundaries()
    test_populations()
    test_verdicts()
    test_readability_and_clusters()
    test_load_journal_and_archive()
    print("tests audit_sens : OK")


if __name__ == "__main__":
    main()
