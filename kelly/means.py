"""Correction des moyennes de Kelly sur les marchés de stats (corners, cartons, jaunes, tirs, tirs cadrés, fautes, touches, hors-jeu).

Pourquoi (13/10) : sur les tirs, Kelly annonçait 64 % de réussite des deux côtés, mais les « moins » passaient 39 % du temps et les « plus » 73 % : sa
moyenne de tirs par match était trop basse. Une moyenne biaisée donne des erreurs OPPOSÉES selon le sens du pari, que la fiabilité par famille (calib)
mélange et cache. On corrige donc à la source : pour chaque match réglé, le nombre que Kelly attendait (« mu », posé sur le pronostic à sa création :
fair.make) contre le nombre réel (« val », posé au règlement : settle._settle_fair_row). Toutes les lignes suivies comptent (pronostics montrés ou
suivis en silence, journal de l'ensemble), pas seulement tes paris : un match = un point par marché (les lignes 9,5 / 10,5 du même match ne comptent
qu'une fois).

Facteur d'une famille = réel / attendu (somme sur les matchs), rapproché de 1 tant qu'il y a peu de matchs (n / (n + PRIOR_N)), borné à BOUNDS ; oubli
de moitié en HALF_LIFE jours. Puis, en deux étages, celui d'un CHAMPIONNAT : son propre réel / attendu, rapproché du facteur de la famille tant qu'il a
peu de matchs (n / (n + PRIOR_LG)). Les sources de stats diffèrent selon le championnat (football-data pour les grands championnats européens,
365scores, FotMob ou API-Football ailleurs) et ne comptent pas toutes un « tir » de la même façon : l'écart de Kelly peut donc varier d'un
championnat à l'autre. Jamais par équipe : une équipe joue un match par semaine, ce serait du bruit (ses tendances sont déjà dans le modèle). Appliqué aux moyennes de la famille AVANT le calcul des chances (fair.deep_options : modèle approfondi et sa référence
« match moyen » ; fair._stat_options : profils). « mu » reste la moyenne BRUTE (avant correction) : le facteur mesure le biais du modèle, pas le sien.

data/means.json : {"fam": {famille: [n, Σ réel, Σ attendu]}, "lg": {famille: {championnat: [n, Σ réel, Σ attendu]}}, "t": jour du dernier
vieillissement, "done": {jour|match|marché: jour}}."""
import copy
from datetime import date, timedelta

from . import calib, config as C, store

FILE = "means.json"
FAMILIES = ("corners", "cards", "yellow", "shots", "sot", "fouls", "throws", "offsides")
PRIOR_N = 40                    # matchs : la correction mesurée ne pèse qu'à proportion de n / (n + 40)
PRIOR_LG = 30                   # matchs d'un championnat : sa propre correction ne pèse qu'à proportion de n / (n + 30) face à celle de la famille
LG_SHOW = 15                    # matchs d'un championnat avant de l'afficher (📊 Marchés)
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
        d.setdefault("lg", {})
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
            d["lg"] = {f: {sk: [round(a[0] * k, 4), round(a[1] * k, 4), round(a[2] * k, 4)] for sk, a in lgs.items() if a[0] * k >= 0.2}
                       for f, lgs in d.get("lg", {}).items()}
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
            accs = [d["fam"].setdefault(fam, [0.0, 0.0, 0.0])]
            if r.get("sport_key"):
                accs.append(d.setdefault("lg", {}).setdefault(fam, {}).setdefault(r["sport_key"], [0.0, 0.0, 0.0]))
            for acc in accs:
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


def factor(fam, sport_key=None, data=None):
    """Facteur appliqué aux moyennes de la famille `fam` : réel / attendu, rapproché de 1 selon le nombre de matchs ; puis, pour le championnat
    `sport_key`, son propre réel / attendu rapproché de celui de la famille ; borné ; 1 sans données ou C.MEANS_CORRECT éteint."""
    if not C.MEANS_CORRECT or fam not in FAMILIES:
        return 1.0
    data = data if data is not None else load()
    k = 1.0
    acc = (data.get("fam") or {}).get(fam)
    if acc and acc[0] > 0 and acc[2] > 0:
        n, sv, sm = acc
        k = 1 + (sv / sm - 1) * n / (n + PRIOR_N)
    lg = ((data.get("lg") or {}).get(fam) or {}).get(sport_key) if sport_key else None
    if lg and lg[0] > 0 and lg[2] > 0:
        n, sv, sm = lg
        k = k + (sv / sm - k) * n / (n + PRIOR_LG)
    return max(BOUNDS[0], min(BOUNDS[1], k))


def scaled(pred, sport_key=None):
    """Prévision du modèle approfondi (strength.predict) avec les moyennes de stats corrigées pour le championnat `sport_key` (et celles du « match
    moyen » de référence, pour que la comparaison au match moyen reste juste) ; la même si aucune correction ne s'applique."""
    if not pred or not pred.get("stats"):
        return pred
    ks = {s: factor(s, sport_key) for s in pred["stats"] if s in FAMILIES}
    if all(abs(k - 1) < 1e-9 for k in ks.values()):
        return pred
    out = copy.copy(pred)
    out["stats"] = {s: (dict(v, h=v["h"] * ks[s], a=v["a"] * ks[s]) if s in ks else v) for s, v in pred["stats"].items()}
    base = pred.get("base")
    if base and base.get("stats"):
        out["base"] = dict(base, stats={s: (dict(v, h=v["h"] * ks[s], a=v["a"] * ks[s]) if s in ks else v) for s, v in base["stats"].items()})
    return out


def league_lines(fam, top=2, data=None):
    """Les `top` championnats dont la correction s'écarte le plus de celle de la famille (3 points au moins, LG_SHOW matchs au moins) : « Primera
    Nacional argentine : 27,1 réels pour 23,4 attendus (38 matchs) → ×1,12 » ; [] sinon."""
    from . import catalogue
    data = data if data is not None else load()
    kf, rows = factor(fam, None, data), []
    for sk, (n, sv, sm) in (((data.get("lg") or {}).get(fam)) or {}).items():
        if n >= LG_SHOW and sm > 0:
            k = factor(fam, sk, data)
            if abs(k - kf) >= 0.03:
                rows.append((abs(k - kf), sk, n, sv, sm, k))
    num = lambda x: f"{x:.1f}".replace(".", ",")      # noqa: E731
    try:
        name = catalogue.name
    except AttributeError:
        name = str
    return [f"{name(sk)} : {num(sv / n)} réels pour {num(sm / n)} attendus ({int(round(n))} matchs) → ×" + f"{k:.2f}".replace(".", ",")
            for _, sk, n, sv, sm, k in sorted(rows, reverse=True)[:top]]


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
