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

Point de départ (rejeu de la saison passée, kelly/replay.py, data/calib_boot.json « means ») : avant tes résultats réels, la correction part de ce que le
rejeu a mesuré sur des milliers de matchs (18 championnats européens aux stats complètes), au lieu de 1 ; un championnat rejoué part de son propre écart
au rejeu (relatif à sa famille). Les résultats réels prennent le dessus à mesure qu'ils arrivent (mêmes n / (n + PRIOR)). Le rejeu mesure aussi la
DISPERSION (dispersion(fam)) : si les matchs varient plus que le modèle ne le croit, ses lignes « sûres » des deux sens sont trop confiantes ; la
dispersion du modèle est alors multipliée d'autant (bornée à DISP_BOUNDS). Limites : le rejeu compte les stats comme football-data ; les autres
sources et 1xBet peuvent compter autrement, ce que seuls les vrais résultats révèlent.

data/means.json : {"fam": {famille: [n, Σ réel, Σ attendu]}, "lg": {famille: {championnat: [n, Σ réel, Σ attendu]}}, "t": jour du dernier
vieillissement, "done": {jour|match|marché: jour}}."""
import contextlib
import copy
from datetime import date, timedelta

from . import calib, config as C, store

FILE = "means.json"
FAMILIES = ("corners", "cards", "yellow", "shots", "sot", "fouls", "throws", "offsides")
PRIOR_N = 40                    # matchs : la correction mesurée ne pèse qu'à proportion de n / (n + 40)
PRIOR_LG = 30                   # matchs d'un championnat : sa propre correction ne pèse qu'à proportion de n / (n + 30) face à celle de la famille
LG_SHOW = 15                    # matchs d'un championnat avant de l'afficher (📊 Marchés)
BOOT_MIN = 100                  # matchs rejoués d'une famille (ou d'un championnat) pour servir de point de départ
BOOT_LG_PRIOR = 100             # écart d'un championnat au rejeu, cru à proportion de n / (n + 100)
DISP_MIN = 200                  # matchs rejoués d'une famille pour mesurer sa dispersion
DISP_BOUNDS = (0.3, 4.0)        # dispersion du modèle multipliée au plus par 4 (fautes au rejeu du 13/10 : 3,5), divisée au plus par 3
BOUNDS = (0.80, 1.25)           # jamais plus de 20 % de baisse ni 25 % de hausse
HALF_LIFE = 90                  # jours : un match vieux de 90 jours compte moitié moins
DONE_DAYS = 12                  # jours de mémoire des matchs déjà comptés
SHOW_MIN = 15                   # matchs avant d'afficher la correction (📊 Marchés)
_memo = {}
_OFF = [False]                  # vrai pendant le rejeu : il applique ses propres corrections (replay.replay_div), jamais celles du direct


@contextlib.contextmanager
def off():
    """Corrections du direct coupées (factor et dispersion rendent 1) le temps d'un bloc : le rejeu de la saison passée."""
    old, _OFF[0] = _OFF[0], True
    try:
        yield
    finally:
        _OFF[0] = old


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


def _boot(boot=None):
    """Moyennes du rejeu ({"fam": {famille: [n, Σ réel, Σ attendu, …]}, "lg": {famille: {championnat: […]}}}) ; {} sans rejeu ou C.REPLAY_PRIOR à 0."""
    if boot is not None:
        return boot
    if not C.REPLAY_PRIOR:
        return {}
    return calib.boot().get("means") or {}


def _ratio(a, min_n):
    return a[1] / a[2] if a and len(a) >= 3 and a[0] >= min_n and a[2] > 0 else None


def boot_factor(fam, sport_key=None, boot=None):
    """Point de départ de la correction : ce que le rejeu a mesuré (réel / attendu) pour la famille, puis l'écart du championnat à sa famille (relatif,
    rapproché de 0 selon ses matchs rejoués) ; 1 sans rejeu suffisant."""
    b = _boot(boot)
    kf = _ratio((b.get("fam") or {}).get(fam), BOOT_MIN) or 1.0
    if not sport_key:
        return kf
    lg = ((b.get("lg") or {}).get(fam) or {}).get(sport_key)
    rl = _ratio(lg, BOOT_MIN)
    if rl is None or not _ratio((b.get("fam") or {}).get(fam), BOOT_MIN):
        return kf
    return kf * (1 + (rl / kf - 1) * lg[0] / (lg[0] + BOOT_LG_PRIOR))


def dispersion(fam, boot=None):
    """Multiplicateur de la dispersion du modèle pour la famille : dispersion mesurée sur le rejeu (méthode des moments, autour de la moyenne
    corrigée) / dispersion moyenne du modèle, borné à DISP_BOUNDS ; 1 sans rejeu suffisant (DISP_MIN matchs) ou C.MEANS_CORRECT éteint."""
    if not C.MEANS_CORRECT or fam not in FAMILIES or _OFF[0]:
        return 1.0
    return disp_of((_boot(boot).get("fam") or {}).get(fam))


def disp_of(a, min_n=DISP_MIN):
    """Multiplicateur de dispersion d'un cumul du rejeu [n, Σ réel, Σ attendu, Σ réel², Σ réel x attendu, Σ attendu², Σ dispersion x attendu²] ; 1 sous
    `min_n` matchs."""
    if not a or len(a) < 7 or a[0] < min_n or a[2] <= 0 or a[5] <= 0 or a[6] <= 0:
        return 1.0
    n, sv, sm, svv, svm, smm, sam = a
    k = sv / sm
    resid = svv - 2 * k * svm + k * k * smm                      # Σ (réel - k x attendu)²
    alpha = (resid - k * sm) / (k * k * smm)                     # binomiale négative : variance = moyenne + alpha x moyenne²
    model = sam / smm                                            # dispersion moyenne du modèle (pesée comme les moyennes²)
    if model <= 0:
        return 1.0
    return max(DISP_BOUNDS[0], min(DISP_BOUNDS[1], alpha / model)) if alpha > 0 else DISP_BOUNDS[0]


def factor(fam, sport_key=None, data=None, boot=None):
    """Facteur appliqué aux moyennes de la famille `fam` : réel / attendu, rapproché du point de départ (le rejeu, sinon 1) selon le nombre de matchs ;
    puis, pour le championnat `sport_key`, son propre réel / attendu rapproché de celui de la famille (au départ : son écart au rejeu) ; borné ; 1 sans
    données ou C.MEANS_CORRECT éteint."""
    if not C.MEANS_CORRECT or fam not in FAMILIES or _OFF[0]:
        return 1.0
    data = data if data is not None else load()
    k0 = boot_factor(fam, None, boot)
    k = k0
    acc = (data.get("fam") or {}).get(fam)
    if acc and acc[0] > 0 and acc[2] > 0:
        n, sv, sm = acc
        k = k0 + (sv / sm - k0) * n / (n + PRIOR_N)
    if sport_key:
        bf = boot_factor(fam, sport_key, boot)
        k = k * (bf / k0 if k0 > 0 else 1.0)                     # écart du championnat au rejeu, avant ses vrais résultats
    lg = ((data.get("lg") or {}).get(fam) or {}).get(sport_key) if sport_key else None
    if lg and lg[0] > 0 and lg[2] > 0:
        n, sv, sm = lg
        k = k + (sv / sm - k) * n / (n + PRIOR_LG)
    return max(BOUNDS[0], min(BOUNDS[1], k))


def scaled(pred, sport_key=None):
    """Prévision du modèle approfondi (strength.predict) avec les moyennes de stats corrigées pour le championnat `sport_key` (et celles du « match
    moyen » de référence, pour que la comparaison au match moyen reste juste) et la dispersion corrigée (dispersion : « al », « at ») ; la même si aucune
    correction ne s'applique."""
    if not pred or not pred.get("stats"):
        return pred
    ks = {s: factor(s, sport_key) for s in pred["stats"] if s in FAMILIES}
    ds = {s: dispersion(s) for s in ks}
    if all(abs(k - 1) < 1e-9 for k in ks.values()) and all(abs(x - 1) < 1e-9 for x in ds.values()):
        return pred
    return apply(pred, ks, ds)


def apply(pred, ks, ds=None):
    """Copie de la prévision `pred` aux moyennes multipliées par `ks` ({famille: facteur}) et dispersions par `ds` (celles du « match moyen » aussi)."""
    ds = ds or {}
    out = copy.copy(pred)
    out["stats"] = {s: (dict(v, h=v["h"] * ks[s], a=v["a"] * ks[s], al=(v.get("al") or 0) * ds.get(s, 1.0), at=(v.get("at") or 0) * ds.get(s, 1.0))
                        if s in ks else v) for s, v in pred["stats"].items()}
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


def var_ratio(a, min_n=DISP_MIN):
    """Variance réelle autour de la moyenne corrigée / variance que le modèle prévoit (moyenne + dispersion x moyenne²) : ce que la correction de
    dispersion rattrape, en clair (×1,10 : des écarts 10 % plus grands que prévu) ; None sous `min_n` matchs."""
    if not a or len(a) < 7 or a[0] < min_n or a[2] <= 0 or a[5] <= 0:
        return None
    n, sv, sm, svv, svm, smm, sam = a
    k = sv / sm
    model = k * sm + (sam / smm) * k * k * smm
    return (svv - 2 * k * svm + k * k * smm) / model if model > 0 else None


def boot_note(fam, boot=None):
    """« moyenne ×1,02 · variance ×1,15 » : ce que le rejeu a mesuré (affiché avec « 📚 rejeu » dans 📊 Marchés) ; '' sans rejeu suffisant."""
    b = (_boot(boot).get("fam") or {}).get(fam)
    if not b or b[0] < BOOT_MIN:
        return ""
    parts = []
    k = boot_factor(fam, None, boot)
    if abs(k - 1) >= 0.005:
        parts.append("moyenne ×" + f"{k:.2f}".replace(".", ","))
    v = var_ratio(b) if C.MEANS_CORRECT else None
    if v is not None and abs(v - 1) >= 0.02:
        parts.append("variance ×" + f"{v:.2f}".replace(".", ","))
    return " · ".join(parts)


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
