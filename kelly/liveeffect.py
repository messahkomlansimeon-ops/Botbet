"""Journal de l'effet du score en direct (live_ledger.json, commit A du 17/10) : pour chaque pari de statistiques suivi, la chance en direct AVEC et SANS l'effet du score et des cartons rouges
(inplay.team_mults), prise à la PREMIÈRE lecture en direct de la jambe à partir de la LIVE_MIN_MINUTE-ième minute (un échantillon par jambe : pas de lectures tardives, qui dépendent déjà du résultat),
puis le résultat quand la jambe est tranchée à la fin du match. Sert à une seule question pré-déclarée : l'effet améliore-t-il la calibration, par famille, sur 200 jambes réglées ? Lecture seule du
reste : rien ici ne change un pari, une mise ni un message.

data/live_effect_log.json : [{key, id, fam, side, name, point, minute, score, reds, p, p_raw, p0, result}] ; au plus MAX_ROWS lignes (les plus anciennes réglées partent d'abord)."""
import math
import sys
from collections import defaultdict

from . import config as C, store

FILE = "live_effect_log.json"
MAX_ROWS = 2000
FAMILIES = ("corners", "shots", "sot", "fouls")           # familles dont l'effet change la chance (inplay.SCORE_STATS + fautes des cartons rouges)
MIN_LEGS = 200                                            # jambes réglées avant de juger (ledger)
EPS = 1e-4


def record(leg, key, side, score, minute, reds, p, p_raw):
    """Note la lecture (une fois par jambe) ; ne lève jamais. -> True si une ligne a été ajoutée."""
    try:
        if key not in FAMILIES or p is None or p_raw is None or minute < C.LIVE_MIN_MINUTE or not leg.get("key"):
            return False

        def add(rows):
            rows = rows if isinstance(rows, list) else []
            if any(r.get("key") == leg["key"] for r in rows):
                return None, False
            rows.append({"key": leg["key"], "id": leg.get("id"), "fam": key, "side": side, "name": leg.get("name"), "point": leg.get("point"), "minute": int(minute),
                         "score": list(score), "reds": list(reds or (0, 0)), "p": round(p, 4), "p_raw": round(p_raw, 4), "p0": leg.get("prob"), "result": None})
            if len(rows) > MAX_ROWS:
                drop = len(rows) - MAX_ROWS
                keep = [r for r in rows if r.get("result") is None]
                done = [r for r in rows if r.get("result") is not None][drop:]
                rows = done + keep if len(keep) < MAX_ROWS else keep[-MAX_ROWS:]
            return rows, True
        return bool(store.update(FILE, add, []))
    except Exception as e:                          # noqa: BLE001 - facultatif : ne gêne jamais le suivi
        print(f"[live] journal de l'effet du score ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        return False


def settle(key, result):
    """Pose le résultat (« win » / « loss ») de la jambe `key` si elle a une ligne sans résultat ; ne lève jamais."""
    try:
        if result not in ("win", "loss"):
            return False

        def put(rows):
            rows = rows if isinstance(rows, list) else []
            hit = [r for r in rows if r.get("key") == key and r.get("result") is None]
            if not hit:
                return None, False
            hit[0]["result"] = result
            return rows, True
        return bool(store.update(FILE, put, []))
    except Exception as e:                          # noqa: BLE001
        print(f"[live] résultat de l'effet du score ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        return False


def rows():
    r = store.load(FILE, [])
    return [x for x in r if isinstance(x, dict)] if isinstance(r, list) else []


def logloss(p, y):
    p = min(1 - EPS, max(EPS, p))
    return -(math.log(p) if y else math.log(1 - p))


def compare(data=None):
    """{famille: {n, matchs, p, p_raw, réalisé, ll_avec, ll_sans, écart (avec - sans), demi-marge}} sur les jambes réglées ; écart négatif = l'effet améliore. Demi-marge à 95 % PAR MATCH (un point par jambe,
    grappes par match). Pure. 'all' regroupe les familles."""
    data = rows() if data is None else data
    out = {}
    for fam in FAMILIES + ("all",):
        rs = [r for r in data if r.get("result") in ("win", "loss") and (fam == "all" or r.get("fam") == fam) and r.get("p") is not None and r.get("p_raw") is not None]
        if not rs:
            continue
        ys = [1 if r["result"] == "win" else 0 for r in rs]
        d = [logloss(r["p"], y) - logloss(r["p_raw"], y) for r, y in zip(rs, ys)]
        n, m = len(d), sum(d) / len(d)
        cl = defaultdict(float)
        for x, r in zip(d, rs):
            cl[r.get("id") or r["key"]] += x - m
        k = len(cl)
        half = 1.96 * math.sqrt(k / (k - 1) * sum(v * v for v in cl.values())) / n if k > 1 else None
        out[fam] = {"n": n, "matchs": k, "p": sum(r["p"] for r in rs) / n, "p_raw": sum(r["p_raw"] for r in rs) / n, "realise": sum(ys) / n,
                    "ll_avec": sum(logloss(r["p"], y) for r, y in zip(rs, ys)) / n, "ll_sans": sum(logloss(r["p_raw"], y) for r, y in zip(rs, ys)) / n, "ecart": m, "half": half}
    return out


def verdict(c):
    """« mieux » / « pire » / « indécis » / « pas assez de jambes » (MIN_LEGS) pour un résultat de compare() : calculé, jamais appliqué."""
    if not c or c["n"] < MIN_LEGS or c["half"] is None:
        return "pas assez de jambes"
    return "mieux" if c["ecart"] + c["half"] < 0 else "pire" if c["ecart"] - c["half"] > 0 else "indécis"
