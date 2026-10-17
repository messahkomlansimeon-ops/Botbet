"""Étape 1, biais de sens : H0 « sur les lignes de référence (choisies sans regarder le prix), la calibration par sens est juste » (step1_ledger.json, extension du 17/10, écrite AVANT la
première lecture). LECTURE SEULE sur une COPIE de kelly.db, aucun réseau, aucun réglage modifié : la règle de verdict est CALCULÉE et rapportée, jamais appliquée.

    python tools/audit_sens.py --data-dir DOSSIER   (DOSSIER contient kelly.db : la sauvegarde)

Populations (ledger) : REF = journal de la voie `saison` (season_log.json + arch/sea/), ref = 1 SANS sval, réglées win / loss, hors coupes ; CHOSEN = sval dans (0, 1), hors coupes ;
les coupes à part, jamais dans le verdict. Résidu = (1 si gagné) - p0, p0 = probabilité brute du modèle (`p0` du journal, `pm` dans l'archive compacte : ce sont les mêmes valeurs
de la voie), moyenne et demi-intervalle à 95 % PAR MATCH. Familles : totals, team_totals, corners, cards, yellow, shots, sot, fouls ; sens : plus (Over) / moins (Under)."""
import argparse
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

FAMILIES = ("totals", "team_totals", "corners", "cards", "yellow", "shots", "sot", "fouls")
SENS = (("Over", "plus"), ("Under", "moins"))
CELL_MIN, CELL_MATCHES = 40, 25                 # lisibilité d'une case (ledger)
AGG_MIN, AGG_MATCHES = 100, 30                  # lisibilité d'un agrégat par sens
TRANCHE = (0.55, 0.75)                          # tranche commune de p0 (REF et CHOSEN)


def isolate_env():
    for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
        os.environ.pop(k, None)
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-audit-sens-")


def family_of(market):
    """Famille du ledger : « totals », « team_totals », puis calib.family (corners, cards, yellow, shots, sot, fouls)."""
    from kelly import calib
    m = str(market or "")
    if m == "totals":
        return "totals"
    if m.startswith("team_totals_"):
        return "team_totals"
    return calib.family(m)


def load(data_dir):
    """Lignes du journal de la voie `saison` + archive compacte (le journal l'emporte), d'une copie de kelly.db ; [] si absent."""
    from kelly import ensemble
    src = os.path.join(data_dir, "kelly.db")
    if not os.path.exists(src):
        return []
    tmp = tempfile.mkdtemp(prefix="kelly-audit-")
    dst = os.path.join(tmp, "kelly.db")
    shutil.copy(src, dst)
    con = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    try:
        row = con.execute("select body from docs where name=?", ("season_log.json",)).fetchone()
        log = [r for r in (json.loads(row[0]) if row else []) if isinstance(r, dict)]
        seen = {r.get("key") for r in log}
        for nm, body in con.execute("select name, body from docs where name like 'arch/sea/%' order by name"):
            day = nm[len("arch/sea/"):]
            for k, v in (json.loads(body).get("rows") or {}).items():
                if k not in seen:
                    log.append(ensemble.unpack_archive_row(k, day, v))
        return log
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)


def prob(r):
    """p0 brut du journal ; None (ligne exclue) sans p0, notamment toute ligne de l'archive compacte (son `pm` est calibré)."""
    p = r.get("p0")
    return float(p) if isinstance(p, (int, float)) and not isinstance(p, bool) and math.isfinite(p) and 0.0 < p < 1.0 else None


def populations(rows):
    """{"REF": [...], "CHOSEN": [...], "cups": {"REF": [...], "CHOSEN": [...]}} : lignes réglées win / loss avec p0, une famille du ledger et un sens plus / moins."""
    out = {"REF": [], "CHOSEN": [], "cups": {"REF": [], "CHOSEN": []}, "no_p0": 0}
    for r in rows:
        if r.get("result") not in ("win", "loss") or r.get("name") not in ("Over", "Under"):
            continue
        if prob(r) is None:
            if (r.get("ref") == 1 and r.get("sval") is None) or r.get("sval") in (0, 1):
                out["no_p0"] += 1                                  # ligne de l'archive (sans p0) : exclue, comptée
            continue
        fam = family_of(r.get("market"))
        if fam not in FAMILIES:
            continue
        if r.get("ref") == 1 and r.get("sval") is None:
            pop = "REF"
        elif r.get("sval") in (0, 1):
            pop = "CHOSEN"
        else:
            continue
        item = {"fam": fam, "sens": "plus" if r["name"] == "Over" else "moins", "p": prob(r), "y": 1.0 if r["result"] == "win" else 0.0, "id": r.get("id") or r.get("key"),
                "pe": r.get("pe") if isinstance(r.get("pe"), (int, float)) else None}
        (out["cups"][pop] if r.get("cup") else out[pop]).append(item)
    return out


def stats(xs, groups):
    """(n, moyenne, demi-marge à 95 % par grappes, grappes) ; demi-marge None sous deux grappes."""
    n = len(xs)
    if not n:
        return 0, None, None, 0
    m = sum(xs) / n
    cl = defaultdict(float)
    for x, g in zip(xs, groups):
        cl[g] += x - m
    k = len(cl)
    if k < 2:
        return n, m, None, k
    se = math.sqrt(k / (k - 1) * sum(v * v for v in cl.values())) / n
    return n, m, 1.96 * se, k


def resid(items):
    return stats([i["y"] - i["p"] for i in items], [i["id"] for i in items])


def diff(a, b):
    """Écart de deux échantillons indépendants : (a - b, demi-marge) ; None si une marge manque."""
    na, ma, ha, _ = resid(a)
    nb, mb, hb, _ = resid(b)
    if ha is None or hb is None:
        return None
    return ma - mb, math.sqrt(ha * ha + hb * hb)


def pct(x, signed=True):
    return "n/a" if x is None else f"{x * 100:+.1f}" if signed else f"{x * 100:.1f}"


def cell_line(label, items):
    n, m, h, k = resid(items)
    if not n:
        return f"     {label:<26} n=0"
    p = sum(i["p"] for i in items) / n
    y = sum(i["y"] for i in items) / n
    read = "" if (n >= CELL_MIN and k >= CELL_MATCHES) else "   (trop peu pour conclure)"
    return f"     {label:<26} n={n:<4} matchs={k:<4} annoncé {p * 100:.0f} % réalisé {y * 100:.0f} %  résidu {pct(m)} ± {pct(h, False)} pts{read}"


def verdict_sens(items):
    """(verdict, texte) d'un sens : « rejetée » / « tient » / « non lisible »."""
    n, m, h, k = resid(items)
    if n < AGG_MIN or k < AGG_MATCHES or h is None:
        return "non lisible", f"n={n}, matchs={k} (il faut {AGG_MIN} lignes et {AGG_MATCHES} matchs)"
    if abs(m) > h:
        return "rejetée", f"résidu {pct(m)} ± {pct(h, False)} pts : l'IC exclut zéro ({'trop confiant' if m < 0 else 'sous-estimé'})"
    return "tient", f"résidu {pct(m)} ± {pct(h, False)} pts : l'IC inclut zéro"


def quart(xs):
    s = sorted(xs)
    return "n/a" if not s else " / ".join(f"{s[int(q * (len(s) - 1))] * 100:.0f}" for q in (0.1, 0.5, 0.9))


def report(rows):
    pops = populations(rows)
    out = ["H0 (step1_ledger.json, extension du 17/10) : sur les lignes de référence, la calibration par sens est juste.",
           f"   Voie `saison`, hors coupes : {len(pops['REF'])} lignes de référence réglées, {len(pops['CHOSEN'])} lignes choisies réglées ; coupes à part : "
           f"{len(pops['cups']['REF'])} et {len(pops['cups']['CHOSEN'])}.",
           f"   Exclues faute de p0 brut (lignes de l'archive compacte, dont le pm est calibré) : {pops['no_p0']} lignes de référence ou choisies, coupes comprises.",
           "   Résidu = réalisé - p0 (p0 = probabilité brute du modèle, avant mélange avec le marché), en points, ± demi-intervalle à 95 % PAR MATCH.", ""]
    for sens_en, sens in SENS:
        out.append(f"   SENS « {sens} »")
        for fam in FAMILIES:
            for pop in ("REF", "CHOSEN"):
                items = [i for i in pops[pop] if i["fam"] == fam and i["sens"] == sens]
                if items:
                    out.append(cell_line(f"{fam} {pop}", items))
        for pop in ("REF", "CHOSEN"):
            out.append(cell_line(f"AGRÉGAT {pop}", [i for i in pops[pop] if i["sens"] == sens]))
        out.append("")
    out.append("   VERDICT (calculé, jamais appliqué)")
    verdicts = {}
    for sens_en, sens in SENS:
        ref = [i for i in pops["REF"] if i["sens"] == sens]
        v, why = verdict_sens(ref)
        verdicts[sens] = v
        out.append(f"     « {sens} » : H0 {v.upper()} — {why}")
        cho = [i for i in pops["CHOSEN"] if i["sens"] == sens]
        d = diff(cho, ref)
        out.append(f"        biais de sélection (choisies - référence) : " + ("non mesurable (une marge manque)" if d is None else f"{pct(d[0])} ± {pct(d[1], False)} pts"))
        common = [[i for i in grp if TRANCHE[0] <= i["p"] <= TRANCHE[1]] for grp in (cho, ref)]
        dc = diff(*common)
        out.append(f"        tranche commune p0 {TRANCHE[0]:.2f}-{TRANCHE[1]:.2f} : choisies n={len(common[0])}, référence n={len(common[1])}, écart "
                   + ("non mesurable" if dc is None else f"{pct(dc[0])} ± {pct(dc[1], False)} pts"))
        out.append(f"        p0 (10 % / médiane / 90 %) : référence {quart([i['p'] for i in ref])} ; choisies {quart([i['p'] for i in cho])}")
        pe = [i for i in ref if i["pe"] is not None]
        if pe:
            n, m, h, _ = stats([i["y"] - i["pe"] for i in pe], [i["id"] for i in pe])
            out.append(f"        en information, probabilité retenue pe (mélange avec le marché) : résidu {pct(m)} ± {pct(h, False)} pts (n={n})")
    rejected = [s for s, v in verdicts.items() if v == "rejetée"]
    hold = [s for s, v in verdicts.items() if v == "tient"]
    if rejected:
        overall = "H0 REJETÉE (" + ", ".join(rejected) + ") : biais de MODÈLE, la correction va dans `means`, par sens"
    elif len(hold) == len(verdicts):
        overall = "H0 TIENT pour les deux sens : biais de SÉLECTION, la correction va au moment du choix (retrait vers le marché proportionnel à l'écart)"
    else:
        overall = "NON CONCLUANT (" + ", ".join(f"{s} : {v}" for s, v in verdicts.items()) + ")"
    out += ["", f"   => {overall}", ""]
    out.append("   Coupes (information, hors verdict) :")
    for sens_en, sens in SENS:
        for pop in ("REF", "CHOSEN"):
            out.append(cell_line(f"{pop} « {sens} »", [i for i in pops["cups"][pop] if i["sens"] == sens]))
    out += ["", "   Limites (ledger) : la voie `saison` n'est pas la voie qui a produit les chiffres du bilan (calib.json, pronostics de l'ensemble) : le verdict parle du modèle de la voie `saison` ;",
            "   la ligne de référence vise p0 ≈ 0,65 (composition différente des lignes choisies : voir la tranche commune) ; trois jours de journal à la première lecture : intervalles larges ;",
            "   test BILATÉRAL (un « moins de » trop peu confiant ou un « plus de » trop confiant rejetterait aussi H0) ; REF = référence qui n'a PAS été prise comme jambe ; l'écart choisies - référence suppose deux",
            "   échantillons indépendants (un même match peut avoir les deux : intervalle conservateur)."]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", required=True, help="dossier qui contient kelly.db (copié, jamais modifié)")
    args = ap.parse_args(argv)
    isolate_env()
    rows = load(args.data_dir)
    if not rows:
        print("Aucune donnée (kelly.db absent ou journal de la voie `saison` vide).", file=sys.stderr)
        return 1
    print(report(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
