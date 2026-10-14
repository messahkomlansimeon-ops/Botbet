"""Décision 5 : les écarts 1xBet / sharp, par heure de relevé, en LECTURE SEULE sur une COPIE des données de Kelly.

    python tools/audit_gaps.py --data-dir /chemin/vers/un/dossier/avec/kelly.db

Lit data/gapdist.json (kelly/gapdist.py) : la distribution quotidienne des écarts de TOUTES les sélections qui portent un avis sharp, avec l'âge du prix sharp, et les fantômes
+1,5..+3 % suivis jusqu'au dernier relevé avant le match. Affiche :
  1. la distribution des écarts par tranche (toutes lectures, puis seulement sharp frais, puis sharp frais ET match dans moins de 90 min) ;
  2. la fraîcheur du sharp PAR HEURE de lecture (UTC) : c'est la limite horaire ;
  3. les fantômes +1,5..+3 % : CLV contre la clôture sharp (cote prise x dernière proba sharp - 1) et contre le dernier prix 1xBet connu, marges PAR MATCH ;
  4. ce que ce rapport peut et ne peut pas conclure.
Ne touche jamais au dossier donné (kelly.db copié dans un dossier temporaire, ouvert en lecture seule) ; aucun réseau, ni .env, ni cache des sources."""
import argparse
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kelly import config as _C  # noqa: E402

MIN_N = 30
FRESH_MIN = _C.SHARPGAP_MAX_AGE_MIN   # 120 min : au-delà, un écart sharp n'est pas annoncé (même constante que sharpgap)
LAST_LEAD_MAX = 180              # la « clôture » d'un fantôme : dernière lecture à 180 min ou moins du match, sharp d'au plus 120 min
BINS = ("<-10", "-10..-5", "-5..0", "0..1.5", "1.5..3", "3..5", "5..10", "10..25", ">25")


def load(data_dir):
    """Document gapdist.json d'une copie de kelly.db ; {} si absent."""
    src = os.path.join(data_dir, "kelly.db")
    if not os.path.exists(src):
        return {}
    tmp = tempfile.mkdtemp(prefix="kelly-gaps-")
    dst = os.path.join(tmp, "kelly.db")
    shutil.copy(src, dst)
    con = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    try:
        row = con.execute("select body from docs where name=?", ("gapdist.json",)).fetchone()
        doc = json.loads(row[0]) if row else {}
        return doc if isinstance(doc, dict) else {}
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _sum(cells, key):
    out = {}
    for c in cells:
        for k, v in (c.get(key) or {}).items():
            out[k] = out.get(k, 0) + v
    return out


def _row(d, total=None):
    total = total if total is not None else sum(d.values())
    return "  ".join(f"{b} {d.get(b, 0)}" for b in BINS) + f"   (n={total})"


def section_distribution(doc):
    days = doc.get("days") or {}
    cells = [c for hours in days.values() for c in hours.values()]
    out = ["1. DISTRIBUTION DES ÉCARTS (cote 1xBet x proba sharp - 1), sélections portant un avis sharp ; une lecture par sélection et par heure",
           f"   {len(days)} jour(s), {sum(c.get('n', 0) for c in cells)} lectures, {sum(c.get('weak', 0) for c in cells)} sélections à un seul book sharp sans Pinnacle (écartées)."]
    if not cells:
        return out + ["   Aucune donnée : le document n'existe pas encore (rien n'a été lu depuis l'installation)."]
    out.append("   Toutes les lectures      : " + _row(_sum(cells, "bins")))
    out.append(f"   Sharp frais (≤{FRESH_MIN} min) : " + _row(_sum(cells, "fresh")))
    out.append(f"   Sharp frais ET match dans ≤ 90 min : " + _row(_sum(cells, "soon")))
    fresh, soon = _sum(cells, "fresh"), _sum(cells, "soon")
    above = lambda d: sum(v for k, v in d.items() if k in ("3..5", "5..10", "10..25"))      # noqa: E731 - de +3 % à +25 % (le > 25 % est « suspect », écarté)
    out.append(f"   De +3 % à +25 % (au-dessus du seuil ; le > 25 % est « suspect », écarté) : {above(fresh)} avec sharp frais, dont {above(soon)} avec le match dans ≤ 90 min ; "
               f"entre +1,5 % et +3 % (les fantômes) : {fresh.get('1.5..3', 0)} avec sharp frais.")
    return out


def section_hours(doc):
    days = doc.get("days") or {}
    by = {}
    for hours in days.values():
        for h, c in hours.items():
            a = by.setdefault(h, {"n": 0, "age": {}, "fresh": {}, "soon": {}})
            a["n"] += c.get("n", 0)
            for k in ("age", "fresh", "soon"):
                for kk, v in (c.get(k) or {}).items():
                    a[k][kk] = a[k].get(kk, 0) + v
    out = ["2. FRAÎCHEUR DU PRIX SHARP PAR HEURE DE LECTURE (UTC = heure d'Abidjan) : la limite horaire"]
    if not by:
        return out + ["   Aucune donnée."]
    out.append("   heure   lectures   sharp ≤30   31-90   91-240   >240   inconnu   frais(≤120)   frais ET match ≤90 min   écarts +3..+25 % (sharp frais / sharp frais ET ≤90 min)")
    for h in sorted(by):
        a = by[h]
        n = a["n"] or 1
        ages = a["age"]
        fresh = sum(a["fresh"].values())
        soon = sum(a["soon"].values())
        big = lambda d: sum(v for k, v in d.items() if k in ("3..5", "5..10", "10..25"))     # noqa: E731
        out.append(f"   {h}h     {a['n']:>6}   " + "  ".join(f"{round(ages.get(k, 0) * 100 / n):>5}%" for k in ("≤30", "31-90", "91-240", ">240", "inconnu"))
                   + f"   {round(fresh * 100 / n):>5}%   {round(soon * 100 / n):>5}%   {big(a['fresh'])} / {big(a['soon'])}")
    out.append(f"   Lecture : « sharp frais » = prix sharp relevé il y a au plus {FRESH_MIN} min (la règle de sharpgap pour annoncer un écart) ; « ≤90 min » = le match commence dans moins de 90 min.")
    out.append("   Les colonnes d'âge disent, heure par heure, quelle part des lectures dispose d'un prix sharp frais : c'est la LIMITE HORAIRE mesurée. Un relevé sharp du petit matin (04:00) passe")
    out.append(f"   au-delà de {FRESH_MIN} min en fin de matinée : s'il n'y a pas de relevé sharp plus tard, les écarts des heures suivantes ne sont pas annonçables. La dernière colonne dit combien de")
    out.append("   lectures remplissent à la fois « sharp frais » et « match dans ≤ 90 min ». À lire sur les données, pas à supposer : trois jours suffisent à voir la forme horaire.")
    return out


def _stats(xs, groups):
    pairs = [(x, groups[i]) for i, x in enumerate(xs)]
    n = len(pairs)
    if not n:
        return 0, None, None
    m = sum(x for x, _ in pairs) / n
    cl = {}
    for x, g in pairs:
        cl[g] = cl.get(g, 0.0) + x - m
    k = len(cl)
    if k < 2:
        return n, m, None
    return n, m, 1.96 * math.sqrt(k / (k - 1) * sum(v * v for v in cl.values())) / n


def _fmt(xs, groups):
    n, m, h = _stats(xs, groups)
    if not n:
        return "n=0"
    t = f"n={n:<4} moy {m * 100:+.1f} %" + (f" ± {h * 100:.1f}" if h is not None else "")
    return t + ("" if n >= MIN_N else "   (trop peu pour conclure)")


def section_ghost(doc):
    ghost = list((doc.get("ghost") or {}).values())
    out = [f"3. FANTÔMES +1,5 % À +3 % (sous le seuil, jamais joués) : {len(ghost)} suivis"]
    if not ghost:
        return out + ["   Aucun encore."]
    # Une relecture APRÈS la première est exigée : sans elle la « clôture » serait la prise elle-même (CLV nul par construction).
    reread = [g for g in ghost if isinstance(g.get("last"), dict) and g["last"].get("at") != g.get("at")]
    xclosed = [g for g in reread if g["last"].get("lead") is not None and g["last"]["lead"] <= LAST_LEAD_MAX and g["last"].get("x")]
    sclosed = [g for g in ghost if isinstance(g.get("last_fresh"), dict) and g["last_fresh"].get("at") != g.get("at") and g["last_fresh"].get("lead") is not None
               and g["last_fresh"]["lead"] <= LAST_LEAD_MAX]
    out.append(f"   {len(reread)} relus au moins une fois après la prise ; {len(sclosed)} avec une dernière lecture à sharp frais (≤ {FRESH_MIN} min) à ≤ {LAST_LEAD_MAX} min du match ; "
               f"{len(xclosed)} avec un dernier prix 1xBet à ≤ {LAST_LEAD_MAX} min du match.")
    out.append(f"   Les {len(ghost) - len(sclosed)} autres n'ont pas de clôture sharp exploitable. ATTENTION biais de survie : garder seulement les fantômes dont le sharp a été relevé près du match "
               "favorise les matchs que Kelly relit tard (les mêmes qui ont un 1xBet encore ouvert) ; lire ces chiffres avec cette réserve.")
    gid = lambda gs: [g.get("id") or i for i, g in enumerate(gs)]       # noqa: E731
    sharp = [g["xodds"] * g["last_fresh"]["p"] - 1 for g in sclosed]
    out.append(f"   Écart contre la CLÔTURE SHARP approchée (cote prise x dernière proba sharp fraîche - 1) : {_fmt(sharp, gid(sclosed))}"
               + (f"   (à la prise, mêmes paris : moy {sum(g['edge'] for g in sclosed) / len(sclosed) * 100:+.1f} %)" if sclosed else ""))
    xs = [math.log(g["xodds"] / g["last"]["x"]) for g in xclosed]
    out.append(f"   CLV contre le DERNIER PRIX 1xBET connu (log cote prise / dernier prix) : {_fmt(xs, gid(xclosed))}")
    out.append("   Lecture : un écart de +1,5..3 % qui reste positif contre la clôture sharp ET qui bat le dernier prix 1xBet serait de la valeur laissée de côté par le seuil. Les deux mesures doivent être")
    out.append("   lues ensemble : battre 1xBet sans battre le sharp = 1xBet a ajusté tard, pas un pari où il restait de la valeur.")
    return out


def section_limits():
    return ["4. CE QUE CE RAPPORT PEUT ET NE PEUT PAS CONCLURE",
            "   - La distribution compte des LECTURES (une par sélection et par heure), pas des paris ; plusieurs lectures d'un même pari à des heures différentes sont normales.",
            "   - 'Dernière lecture' d'un fantôme = le dernier passage de Kelly avant le match, pas la clôture réelle des books : la clôture sharp est approchée, la marge est large.",
            "   - Seules les sélections que Kelly relit sont vues (celles qui ont un prix 1xBet et un avis sharp) : ce n'est pas l'ensemble du marché. Les fantômes sont pris dans l'ordre d'arrivée",
            "     jusqu'à un plafond de 300 par jour : un jour très chargé en écarte la fin.",
            "   - Aucun résultat de pari n'est en jeu ici (CLV seulement) ; trois jours de données suffisent à voir la forme horaire, pas à décider d'un seuil.",
            "   - Le seuil +3 % des paris réels n'est pas modifié par ce rapport (décision 5 : ne pas baisser le seuil sur les paris réels)."]


def report(doc):
    return "\n\n".join("\n".join(s) for s in (section_distribution(doc), section_hours(doc), section_ghost(doc), section_limits()))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", required=True, help="dossier qui contient kelly.db (copié, jamais modifié)")
    args = ap.parse_args(argv)
    doc = load(args.data_dir)
    if not doc:
        print("Aucune donnée (kelly.db ou gapdist.json absent).", file=sys.stderr)
        return 1
    print(report(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
