"""Correction des moyennes de Kelly sur les marchés de stats (corners, cartons, jaunes, tirs, tirs cadrés, fautes, touches, hors-jeu).

Pourquoi (13/10) : sur les tirs, Kelly annonçait 64 % de réussite des deux côtés, mais les « moins » passaient 39 % du temps et les « plus » 73 % : sa
moyenne de tirs par match était trop basse. Une moyenne biaisée donne des erreurs OPPOSÉES selon le sens du pari, que la fiabilité par famille (calib)
mélange et cache. On corrige donc à la source : pour chaque match réglé, le nombre que Kelly attendait (« mu », posé sur le pronostic à sa création :
fair.make) contre le nombre réel (« val », posé au règlement : settle._settle_fair_row). Toutes les lignes suivies comptent (pronostics montrés ou
suivis en silence, journal de l'ensemble), pas seulement tes paris : un match = un point par marché (les lignes 9,5 / 10,5 du même match ne comptent
qu'une fois).

Facteur d'une famille = réel / attendu (somme sur les matchs), rapproché de 1 tant qu'il y a peu de matchs (n / (n + PRIOR_N)), borné à BOUNDS ; oubli
de moitié en HALF_LIFE jours. Appliqué aux moyennes de la famille AVANT le calcul des chances (fair.deep_options : modèle approfondi et sa référence
« match moyen » ; fair._stat_options : profils). « mu » reste la moyenne BRUTE (avant correction) : le facteur mesure le biais du modèle, pas le sien.

data/means.json : {"fam": {famille: [n, Σ réel, Σ attendu]}, "t": jour du dernier vieillissement, "done": {jour|match|marché: jour}}."""
import copy
from datetime import date, timedelta

from . import calib, config as C, store

FILE = "means.json"
FAMILIES = ("corners", "cards", "yellow", "shots", "sot", "fouls", "throws", "offsides")
PRIOR_N = 40                    # matchs : la correction mesurée ne pèse qu'à proportion de n / (n + 40)
BOUNDS = (0.80, 1.25)           # jamais plus de 20 % de baisse ni 25 % de hausse
HALF_LIFE = 90                  # jours : un match vieux de 90 jours compte moitié moins
DONE_DAYS = 12                  # jours de mémoire des matchs déjà comptés
SHOW_MIN = 15                   # matchs avant d'afficher la correction (📊 Marchés)
_memo = {}


def load():
    """Cumuls (data/means.json), gardés en mémoire tant que le document ne change pas (store.version)."""
    ver = store.version(FILE)
    if _memo.get("ver") != ver or "data" not in _memo:
        d = store.load(FILE, {})
        d = d if isinstance(d, dict) else {}
        d.setdefault("fam", {})
        d.setdefault("done", {})
        _memo.clear()
        _memo.update(data=d, ver=ver)
    return _memo["data"]


def reset():
    _memo.clear()


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v >= 0 else None


def _age(d, today):
    """Vieillit les cumuls jusqu'à `today` (oubli de moitié en HALF_LIFE jours)."""
    t = d.get("t")
    if t and t != str(today):
        try:
            days = (date.fromisoformat(str(today)) - date.fromisoformat(t)).days
        except ValueError:
            days = 0
        if days > 0:
            k = 0.5 ** (days / HALF_LIFE)
            d["fam"] = {f: [round(a[0] * k, 4), round(a[1] * k, 4), round(a[2] * k, 4)] for f, a in d["fam"].items()}
    d["t"] = str(today)


def learn(rows, today=None):
    """Compte les lignes réglées (gagnées, perdues, remboursées) qui portent « mu » (attendu) et « val » (réel), une fois par jour, match et marché.
    -> nombre de matchs ajoutés. Ne lève jamais."""
    try:
        today = today or date.today()
        d = load()
        new = 0
        for r in rows:
            fam = calib.family(r.get("market") or "")
            mu, val = _num(r.get("mu")), _num(r.get("val"))
            if fam not in FAMILIES or mu is None or mu <= 0 or val is None or r.get("result") not in ("win", "loss", "push"):
                continue
            key = f"{str(r.get('date') or r.get('commence') or '')[:10]}|{r.get('id')}|{r.get('market')}"
            if key in d["done"]:
                continue
            if not new:
                _age(d, today)
            acc = d["fam"].setdefault(fam, [0.0, 0.0, 0.0])
            acc[0], acc[1], acc[2] = round(acc[0] + 1, 4), round(acc[1] + val, 4), round(acc[2] + mu, 4)
            d["done"][key] = str(today)
            new += 1
        if new:
            cut = str(date.fromisoformat(str(today)) - timedelta(days=DONE_DAYS))
            d["done"] = {k: v for k, v in d["done"].items() if v >= cut}
            store.save(FILE, d, compact=True)
            reset()
        return new
    except Exception:                               # noqa: BLE001 - facultatif : sans lui, les moyennes restent brutes
        return 0


def factor(fam, data=None):
    """Facteur appliqué aux moyennes de la famille `fam` : réel / attendu, rapproché de 1 selon le nombre de matchs, borné ; 1 sans données ou
    C.MEANS_CORRECT éteint."""
    if not C.MEANS_CORRECT or fam not in FAMILIES:
        return 1.0
    acc = ((data if data is not None else load()).get("fam") or {}).get(fam)
    if not acc or acc[0] <= 0 or acc[2] <= 0:
        return 1.0
    n, sv, sm = acc
    k = 1 + (sv / sm - 1) * n / (n + PRIOR_N)
    return max(BOUNDS[0], min(BOUNDS[1], k))


def scaled(pred):
    """Prévision du modèle approfondi (strength.predict) avec les moyennes de stats corrigées (et celles du « match moyen » de référence, pour
    que la comparaison au match moyen reste juste) ; la même si aucune correction ne s'applique."""
    if not pred or not pred.get("stats"):
        return pred
    ks = {s: factor(s) for s in pred["stats"] if s in FAMILIES}
    if all(abs(k - 1) < 1e-9 for k in ks.values()):
        return pred
    out = copy.copy(pred)
    out["stats"] = {s: (dict(v, h=v["h"] * ks[s], a=v["a"] * ks[s]) if s in ks else v) for s, v in pred["stats"].items()}
    base = pred.get("base")
    if base and base.get("stats"):
        out["base"] = dict(base, stats={s: (dict(v, h=v["h"] * ks[s], a=v["a"] * ks[s]) if s in ks else v) for s, v in base["stats"].items()})
    return out


def line(fam, data=None):
    """« moyenne : 23,1 attendus, 25,0 réels (140 matchs) → corrigée ×1,08 » pour 📊 Marchés ; '' sous SHOW_MIN matchs."""
    acc = ((data if data is not None else load()).get("fam") or {}).get(fam)
    if not acc or acc[0] < SHOW_MIN or acc[2] <= 0:
        return ""
    n, sv, sm = acc
    k = factor(fam, data)
    num = lambda x: f"{x:.1f}".replace(".", ",")      # noqa: E731
    return (f"moyenne : {num(sm / n)} attendus, {num(sv / n)} réels ({int(round(n))} matchs)"
            + (f" → corrigée ×{k:.2f}".replace(".", ",") if abs(k - 1) >= 0.005 else ""))
