"""Diagnostic des coupes d'Europe (LDC, Europa, Conférence) : les moyennes du modèle sont-elles trop basses ? LECTURE SEULE sur une COPIE des données de Kelly.

    python tools/audit_cups.py --data-dir /dossier/avec/kelly.db      (ou --zip kelly_backup_2026-10-15.zip)  [--witness soccer_epl]

Aucun réseau, aucune écriture dans data/, ni dans .env, ni dans le cache des sources : `kelly.db` est copié dans un dossier temporaire et ouvert en lecture seule
(l'empreinte du fichier source est vérifiée avant et après). Lit fair.json, ensemble_log.json, cup_log.json, season_log.json et leurs archives (arch/ens, arch/cup, arch/sea).

Ce que les lignes stockées permettent, et ne permettent pas :
  - le nombre RÉEL se lit dans `final` (« 9 corners », « 2 tirs », « 2-0 ») des lignes de fair.json et des journaux, pas des lignes d'archive (sans `final`) ;
  - la moyenne PRÉVUE (`mu`) n'est stockée pour les coupes que depuis la collecte du 15/10 (avant, cupstats.make posait mu = None, donc jamais de `val`) ; l'outil la RECONSTRUIT toujours, faute de mu sur les lignes plus anciennes en inversant la proba brute du modèle
    (p0 : brute, avant la correction de calib ; rapprochée de 50 %, donc le biais est plutôt sous-estimé) d'une ligne dans une binomiale négative (strength.nb_pmf), pour deux dispersions (0 et 0,15) ; colonne « moyenne reconstruite ». Une ligne de
    CONTRÔLE : plusieurs lignes d'un même match et d'un même marché doivent donner presque la même moyenne ; sinon l'inversion n'est pas valable et le rapport ne conclut pas ;
  - le même calcul avec la proba du marché (px : 1xBet sans marge) dit ce que le MARCHÉ attendait : si lui est juste et pas le modèle, le biais est celui du modèle ;
  - lots : « journal de coupe » (cup_log.json / arch/cup, drapeau cup : calculé par stats de coupe) contre « autres lignes UEFA » (fair.json, ensemble_log.json : buts d'une équipe
    et lignes d'avant les stats de coupe). Le lot ne vient pas de la date mais du journal. Les deux lots ne viennent pas du même code : le rapport le dit ;
  - niveau des ligues (coefficients UEFA, cotes de championnat) : hors de la sauvegarde, jamais saisi de mémoire : non calculé, voir la dernière section.
Aucune correction, aucun pari touché. Chaque ratio est donné avec n et une marge à 95 % (rééchantillonnage des matchs) ; sous 30 matchs : « indication »."""
import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MIN_MATCHES = 30
ALPHAS = (0.0, 0.15)                 # dispersions testées pour l'inversion (Poisson, puis une dispersion typique des stats)
UEFA = ("soccer_uefa_champs_league", "soccer_uefa_europa_league", "soccer_uefa_europa_conference_league")
TOL = 0.15                           # écart relatif maximal entre les moyennes reconstruites d'un même match et marché pour que l'inversion soit « valable »
DOCS = ("fair.json", "ensemble_log.json", "cup_log.json", "season_log.json")
ARCHS = ("arch/ens/", "arch/cup/", "arch/sea/")
FAMILIES = {"totals": "buts", "team_totals_home": "buts d'une équipe", "team_totals_away": "buts d'une équipe", "alternate_totals_corners": "corners",
            "alternate_totals_cards": "cartons"}


def family_of(market):
    if market in FAMILIES:
        return FAMILIES[market]
    m = re.match(r"^team_(shots|sot|fouls|throws|offsides|corners|yellow)_(total|home|away)$", market or "")
    if m:
        base = {"shots": "tirs", "sot": "tirs cadrés", "fouls": "fautes", "throws": "touches", "offsides": "hors-jeu", "corners": "corners", "yellow": "cartons jaunes"}[m.group(1)]
        return f"{base} ({'total' if m.group(2) == 'total' else 'une équipe'})"          # total = domicile + extérieur : corrélés, jamais mêlés à une équipe seule
    return None


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(db_path):
    """{"rows": [lignes], "counts": {document: n}} d'une copie en lecture seule de kelly.db. Chaque ligne porte `src` (journal d'origine) ; les lignes d'archive n'ont pas de `final`."""
    tmp = tempfile.mkdtemp(prefix="kelly-audit-cups-")
    dst = os.path.join(tmp, "kelly.db")
    srcdb = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)           # copie cohérente par l'API de sauvegarde (une base vivante peut avoir un journal WAL)
    cp = sqlite3.connect(dst)
    try:
        srcdb.backup(cp)
    finally:
        cp.close()
        srcdb.close()
    con = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    rows, counts = [], {}
    try:
        for name in DOCS:
            row = con.execute("select body from docs where name=?", (name,)).fetchone()
            doc = json.loads(row[0]) if row else []
            doc = [r for r in doc if isinstance(r, dict)] if isinstance(doc, list) else []
            counts[name] = len(doc)
            rows += [dict(r, src=name) for r in doc]
        from kelly import ensemble                       # fonction pure de dépliage des lignes d'archive
        for pref in ARCHS:
            n = 0
            for nm, body in con.execute("select name, body from docs where name like ? order by name", (pref + "%",)):
                doc = json.loads(body)
                day = nm[len(pref):]
                for k, v in (doc.get("rows") or {}).items():
                    r = ensemble.unpack_archive_row(k, day, v)
                    rows.append(dict(r, src=pref, cup=(pref == "arch/cup/") or r.get("cup")))
                    n += 1
            counts[pref] = n
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return {"rows": rows, "counts": counts}


def actual_value(r):
    """Nombre réel du chiffre de la ligne d'après `final` ; None si absent. Buts : « 2-0 » -> total, ou le côté de la ligne d'équipe ; stats : le premier nombre."""
    fin = r.get("final")
    if not fin or r.get("result") not in ("win", "loss", "push"):
        return None
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", str(fin))
    mk = r.get("market") or ""
    if m:
        h, a = int(m.group(1)), int(m.group(2))
        return {"totals": h + a, "team_totals_home": h, "team_totals_away": a}.get(mk)
    m = re.match(r"^\s*(\d+(?:[.,]\d+)?)\b", str(fin))
    return float(m.group(1).replace(",", ".")) if m and mk not in ("totals", "team_totals_home", "team_totals_away") else None


def reconstruct_mean(p_over, point, alpha):
    """Moyenne `mu` d'une binomiale négative (variance mu + alpha mu^2) telle que P(X > point) = p_over (dichotomie) ; None si la proba est hors de (0,01 ; 0,99)."""
    from kelly import strength
    if p_over is None or not 0.01 < p_over < 0.99 or point is None:
        return None
    lo, hi = 0.05, 80.0
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        if strength.p_over(strength.nb_pmf(mid, alpha), point) < p_over:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def model_prob(r):
    """(proba du modèle, nom du champ) : p0 d'abord (brute, rapprochée de 50 % mais AVANT la correction de calib : fair.make_pick), sinon pm / prob (déjà corrigées par calib). Seules
    les lignes en p0 entrent dans les mesures « brutes » ; les autres sont comptées à part."""
    for k in ("p0", "pm", "prob"):
        if r.get(k) is not None:
            return float(r[k]), k
    return None, None


def over_prob(p, name):
    return p if name == "Over" else (1.0 - p if name == "Under" else None)


MERGE = ("p0", "pm", "prob", "px", "final", "result", "point", "name", "market", "id", "sport_key", "mu", "val")


def prepare(rows, sports):
    """Lignes Over/Under (lignes entières écartées : le push rend P(moins) != 1 - P(plus)) de stats ou de buts des `sports`, fusionnées par clé champ par champ (premier non vide dans l'ordre
    des documents : fair.json, ensemble_log, cup_log, saison, archives ; `final` vient de celle qui l'a). Lot : fixé par (match, marché), journal de coupe s'il y en a une ligne. `shown` :
    la clé est dans fair.json, même si la ligne retenue vient d'un journal."""
    merged, shown, cup_units = {}, set(), set()
    for r in rows:
        if r.get("sport_key") not in sports or r.get("name") not in ("Over", "Under") or r.get("point") is None or not r.get("id") or not r.get("key"):
            continue
        if not family_of(r.get("market")) or float(r["point"]) == int(float(r["point"])):
            continue
        m = merged.setdefault(r["key"], {"key": r["key"], "srcs": set()})
        m["srcs"].add(r.get("src"))
        for f in MERGE:
            if m.get(f) is None and r.get(f) is not None:
                m[f] = r[f]
        if r.get("src") == "fair.json":
            shown.add(r["key"])
        if r.get("cup") or r.get("src") in ("cup_log.json", "arch/cup/"):
            cup_units.add((r["id"], r["market"]))
    out = []
    for k, r in merged.items():
        p, field = model_prob(r)
        out.append({"key": k, "id": r["id"], "market": r["market"], "fam": family_of(r["market"]), "name": r["name"], "point": float(r["point"]), "sport": r["sport_key"],
                    "side": "moins" if r["name"] == "Under" else "plus", "lot": "journal de coupe" if (r["id"], r["market"]) in cup_units else "autres lignes",
                    "p": p, "pfield": field, "raw": field == "p0", "px": r.get("px"), "mu": r.get("mu"), "val": actual_value(r) if actual_value(r) is not None else r.get("val"), "result": r.get("result"), "shown": k in shown,
                    "src": ",".join(sorted(x for x in r["srcs"] if x))})
    return out


def units(lines, p_key, alpha):
    """Un point par (match, marché) : moyenne reconstruite (médiane des lignes valables), contrôle de cohérence, nombre réel. -> liste de dicts."""
    by = defaultdict(list)
    for l in lines:
        by[(l["id"], l["market"])].append(l)
    out = []
    for (mid, mk), ls in by.items():
        mus, val = [], None
        stored = next((l["mu"] for l in ls if l.get("mu") is not None), None) if p_key == "p" else None
        if stored is not None:                           # moyenne ENREGISTRÉE par Kelly (collecte du 15/10) : plus de reconstruction, ni du biais de p0
            mus = [float(stored)]
        for l in ls:
            if stored is not None:
                if l["val"] is not None:
                    val = l["val"]
                continue
            if p_key == "p" and not l["raw"]:
                continue                                  # proba déjà corrigée par calib : pas une mesure du modèle brut
            p = l["p"] if p_key == "p" else (float(l["px"]) if l.get("px") is not None else None)
            po = over_prob(p, l["name"]) if p is not None else None
            mu = reconstruct_mean(po, l["point"], alpha)
            if mu is not None:
                mus.append(mu)
            if l["val"] is not None:
                val = l["val"]
        if not mus or val is None:
            continue
        mus.sort()
        med = mus[len(mus) // 2] if len(mus) % 2 else 0.5 * (mus[len(mus) // 2 - 1] + mus[len(mus) // 2])
        spread = (mus[-1] - mus[0]) / med if len(mus) > 1 and med else None
        out.append({"id": mid, "market": mk, "fam": ls[0]["fam"], "lot": ls[0]["lot"], "mu": med, "val": val, "n_lines": len(mus), "spread": spread, "stored": stored is not None})
    return out


def ratio_ci(us, seed=1, boot=1000):
    """(n matchs, rapport réel / attendu, borne basse, borne haute, part des matchs où réel > attendu) ; rééchantillonnage des matchs (une grappe par match)."""
    if not us:
        return None
    r = sum(u["val"] for u in us) / sum(u["mu"] for u in us)
    share = sum(1 for u in us if u["val"] > u["mu"]) / len(us)
    clusters = defaultdict(list)                          # une grappe par match : domicile et extérieur d'un même match ne comptent pas comme deux preuves
    for u in us:
        clusters[u["id"]].append(u)
    groups = list(clusters.values())
    n = len(groups)
    if n < 3:
        return n, r, None, None, share
    rng = random.Random(seed)
    xs = []
    for _ in range(boot):
        s = [u for g in (groups[rng.randrange(n)] for _ in range(n)) for u in g]
        xs.append(sum(u["val"] for u in s) / sum(u["mu"] for u in s))
    xs.sort()
    return n, r, xs[int(0.025 * boot)], xs[int(0.975 * boot) - 1], share


def valid(us):
    """Contrôle de l'inversion : part des points à plusieurs lignes dont l'écart relatif des moyennes reconstruites est sous TOL. -> (points testés, part valable)."""
    if us and all(u.get("stored") for u in us):          # moyenne enregistrée : rien à contrôler
        return len(us), 1.0
    t = [u for u in us if u["spread"] is not None]
    return len(t), (sum(1 for u in t if u["spread"] <= TOL) / len(t) if t else None)


def verdict(res, n_ok):
    if res is None:
        return "pas de données"
    n, r, lo, hi, _ = res
    if n < MIN_MATCHES:
        return "indication (moins de 30 matchs)"
    if n_ok is None:
        return "inversion non contrôlée (aucun match à plusieurs lignes) : pas de conclusion"
    if n_ok < 0.7:
        return "inversion non valable : pas de conclusion"
    if lo is not None and lo > 1.0:
        return "biais à la baisse confirmé (réel > attendu)"
    if hi is not None and hi < 1.0:
        return "biais à la hausse confirmé (réel < attendu)"
    return "pas de biais mesurable"


def f_ratio(res):
    if res is None:
        return "—"
    n, r, lo, hi, share = res
    ci = f" [{lo:.2f} ; {hi:.2f}]" if lo is not None else ""
    return f"{r:.2f}{ci} · {n} matchs · réel > attendu dans {share:.0%}"


def section_ratio(lines, title, lots=("journal de coupe", "autres lignes")):
    out = [title]
    fams = sorted({l["fam"] for l in lines})
    for fam in fams:
        for lot in lots:
            ls = [l for l in lines if l["fam"] == fam and l["lot"] == lot]
            if not ls:
                continue
            for a in ALPHAS:
                mu_m = units(ls, "p", a)
                mu_x = units(ls, "px", a)
                nt, ok = valid(mu_m)
                res = ratio_ci(mu_m)
                resx = ratio_ci(mu_x)
                chk = f"contrôle {nt} points → {ok:.0%} cohérents" if ok is not None else "contrôle : aucun match à plusieurs lignes"
                out.append(f"  {fam} · {lot} · dispersion {a:g}\n      modèle : {f_ratio(res)} — {verdict(res, ok)}\n      marché : {f_ratio(resx)}\n      {chk}")
    return out if len(out) > 1 else out + ["  aucune ligne réglée avec un nombre réel"]


def section_selection(lines):
    """Point 3 : réalisé / annoncé par sens, sur toutes les lignes générées (a) et sur celles montrées dans fair.json (b) ; (b) − (a) = biais de sélection."""
    out = ["3. Modèle ou sélection ? Réalisé / annoncé (proba brute) par famille et par sens",
           "  (a) toutes les lignes réglées avec leur proba brute (p0) ; (b) celles montrées (fair.json) ; sélection = (b) − (a), en points. La proba brute est déjà rapprochée de 50 %",
           "  (le biais est donc plutôt SOUS-estimé) ; les lignes sans p0 (archives) sont exclues."]
    for fam in sorted({l["fam"] for l in lines}):
        for side in ("moins", "plus"):
            def calc(ls):
                ls = [l for l in ls if l["result"] in ("win", "loss") and l["raw"]]
                if not ls:
                    return None
                p = sum((l["p"]) for l in ls) / len(ls)
                w = sum(1 for l in ls if l["result"] == "win") / len(ls)
                return len(ls), w, p
            base = [l for l in lines if l["fam"] == fam and l["side"] == side]
            a, b = calc(base), calc([l for l in base if l["shown"]])
            if not a:
                continue
            ta = f"(a) {a[1]:.0%} / {a[2]:.0%} ({a[0]})"
            tb = f"(b) {b[1]:.0%} / {b[2]:.0%} ({b[0]})" if b else "(b) —"
            sel = f" · sélection {100 * ((b[1] - b[2]) - (a[1] - a[2])):+.0f} pts" if b else ""
            out.append(f"  {fam} · {side} : {ta} · {tb}{sel}")
    return out


def report(db_path, witness):
    d = load(db_path)
    rows = d["rows"]
    out = ["DIAGNOSTIC DES COUPES D'EUROPE — lecture seule", "Documents lus : " + ", ".join(f"{k} {v}" for k, v in d["counts"].items())]
    cups = prepare(rows, UEFA)
    out += [f"1. Données : {len(cups)} lignes Over/Under UEFA, dont {sum(1 for l in cups if l['lot'] == 'journal de coupe')} du journal de coupe et "
            f"{sum(1 for l in cups if l['lot'] == 'autres lignes')} d'autres sources ; réglées : {sum(1 for l in cups if l['result'] in ('win', 'loss'))} ; avec nombre réel : {sum(1 for l in cups if l['val'] is not None)}.",
            "   Les deux lots ne viennent pas du même code (journal de coupe : profils de championnat, stats de coupe ; autres lignes : modèle principal, dont les buts d'une équipe).", ""]
    if not any(l["lot"] == "journal de coupe" for l in cups):
        out += ["   ⚠️ Aucune ligne du journal de coupe : cette sauvegarde est antérieure aux stats de coupe (commit 2700df7, 14/10). Envoie une sauvegarde plus récente.", ""]
    out += ["2. Moyenne prévue (RECONSTRUITE) contre nombre réel, par famille — un point par match et par marché"] + section_ratio(cups, "") [1:] + [""]
    out += section_selection(cups) + [""]
    out += ["4. Niveau des ligues : NON CALCULÉ ici. Il faut les cotes de clôture des championnats (strength.implied_goals) et les coefficients UEFA, absents de la sauvegarde ; les coefficients",
            "   ne sont jamais saisis de mémoire. À faire à part, sur un fichier de coefficients donné par toi, seulement si les points 2 et 3 montrent un biais sur au moins deux familles.", ""]
    w = prepare(rows, {witness}) if witness else []
    out += [f"5. Ligue témoin ({witness})"] + (section_ratio(w, "", lots=("autres lignes", "journal de coupe"))[1:] if w else ["  aucune ligne pour cette ligue"]) + [""]
    fams = defaultdict(list)
    for l in cups:
        fams[l["fam"]].append(l)
    out.append("6. Résumé par famille (dispersion 0,15, lot le plus fourni)")
    for fam, ls in sorted(fams.items()):
        lot = max(("journal de coupe", "autres lignes"), key=lambda x: sum(1 for l in ls if l["lot"] == x))
        us = units([l for l in ls if l["lot"] == lot], "p", ALPHAS[1])
        nt, ok = valid(us)
        out.append(f"  {fam} ({lot}) : {verdict(ratio_ci(us), ok)}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", help="dossier qui contient kelly.db (copié, jamais modifié)")
    ap.add_argument("--zip", help="sauvegarde envoyée par Kelly (kelly_backup_AAAA-MM-JJ.zip)")
    ap.add_argument("--witness", default="soccer_epl", help="ligue témoin (sport_key)")
    args = ap.parse_args(argv)
    import audit_clv as A
    A.isolate_env()
    tmp = None
    if args.zip:
        tmp = tempfile.mkdtemp(prefix="kelly-audit-zip-")
        with zipfile.ZipFile(args.zip) as z:
            z.extract("kelly.db", tmp)
        db = os.path.join(tmp, "kelly.db")
    elif args.data_dir:
        db = os.path.join(args.data_dir, "kelly.db")
    else:
        ap.error("--data-dir ou --zip requis")
    if not os.path.exists(db):
        print("kelly.db introuvable.", file=sys.stderr)
        return 1
    before = sha(db)
    try:
        print(report(db, args.witness))
        print(f"\nSource intacte : {'oui' if sha(db) == before else 'NON'} · cache réel écrit : 0")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
