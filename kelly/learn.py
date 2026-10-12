"""Apprentissage de Kelly : par la cote de clôture d'abord, les résultats en appoint.

1. Modèle CLV : régression ridge qui prédit, à partir des signaux d'un pari
   (valeur, désaccord, mouvement, marché, bookmaker...), de combien il battra
   la clôture. Il n'est adopté que s'il bat le score heuristique, d'une marge (LEARN_OOS_MARGIN), sur des
   jours qu'il n'a pas vus (split temporel, au moins LEARN_MIN_TEST_ROWS lignes sur LEARN_MIN_TEST_DAYS jours,
   jamais sur les lignes d'apprentissage). Marchés / books / groupes rares (< LEARN_RARE_MIN lignes) : une seule
   variable « other » ; pénalité ridge proportionnelle au nombre de variables.
2. Calibration : une fois assez de résultats réglés (>= CALIB_MIN_ROWS par
   famille de marchés), Platt scaling pour que « 60 % » passe bien 60 % du temps ; activée seulement si le Brier
   s'améliore HORS échantillon (validation croisée par blocs de jours, CALIB_FOLDS).
3. Bookmakers lents : CLV moyen quand un book porte la meilleure cote.

4. Amorçage : lignes historiques football-data (data/bootstrap.json, cotes d'ouverture vs clôture, voir
   kelly/bootstrap.py) pour entraîner le modèle dès le premier jour. Elles sont marquées (`boot`), pèsent
   moins qu'un pari réel et un indicateur « bootstrap » absorbe l'écart entre les deux populations. Le test
   hors échantillon se fait sur les paris réels dès qu'il y en a assez (test valable), sinon sur une tranche
   historique mise de côté ; sans test valable, pas d'adoption.

Pur Python : quelques centaines à milliers de lignes, ~30 variables.
"""
import math
from datetime import datetime, timezone

from . import config as C, store

GROUPS = ("soccer", "basketball", "tennis")
FAMILIES = {"h2h": "resultat", "double_chance": "resultat", "totals": "resultat", "spreads": "resultat",
            "btts": "resultat", "alternate_totals_corners": "corners_cartons",
            "alternate_totals_cards": "corners_cartons"}


def family(market):
    return "joueurs" if market.startswith("player_") else FAMILIES.get(market, "autres")


# Chiffres d'avant-match (prematch.py) : écart modèle/ligne par famille de marché, buts attendus. Absents = 0.
PM_NAMES = ("pm_gap", "has_pm", "gap_corners", "gap_cards", "gap_shots", "gap_goals", "xg_diff")
PM_GAP = {"corners": "gap_corners", "cards": "gap_cards", "shots": "gap_shots", "sot": "gap_shots", "goals": "gap_goals"}


def features(c, now=None):
    """Signaux figés au moment du choix (dict nom -> valeur). `heuristic` : score heuristique (pas le CLV prédit
    quand un modèle est adopté). Chiffres d'avant-match (c["pm"]) : écart modèle-ligne et buts attendus, seulement
    s'ils existent (le reste vaut 0)."""
    now = now or datetime.now(timezone.utc)
    start = datetime.fromisoformat(c["commence"].replace("Z", "+00:00"))
    f = {
        "edge": c["edge"], "spread": c.get("spread", 0.0), "steam": c.get("steam", 0.0),
        "log_odds": math.log(c["odds"]), "hours_to_ko": max(0.0, (start - now).total_seconds() / 3600),
        "heuristic": c.get("heuristic", c["score"]), "n_books": c.get("n_books", 0),
        "is_player": float(c["market"].startswith("player_")),
        f"market={c['market']}": 1.0, f"group={c['group']}": 1.0, f"book={c.get('book', '')}": 1.0,
    }
    pm = c.get("pm") or {}
    if "pgap" in pm:
        f["pm_gap"], f["has_pm"] = pm["pgap"], 1.0
    if pm.get("gap") is not None and pm.get("kind") in PM_GAP:
        f[PM_GAP[pm["kind"]]] = pm["gap"]
    if pm.get("xgd") is not None:
        f["xg_diff"] = pm["xgd"]
    return f


# ---------- amorçage historique (data/bootstrap.json) ----------

BOOT_COLS = ("edge", "spread", "log_odds", "heuristic", "xg_diff", "gap_goals", "pm_gap")   # après [date, marché, clv]
BOOT_MARKETS = ("h2h", "totals")


def boot_rows(obj):
    """Contenu de bootstrap.json -> lignes d'apprentissage {date, clv, boot, w, features, grp} (illisibles ignorées)."""
    out = []
    for r in (obj or {}).get("rows", []) if isinstance(obj, dict) else []:
        try:
            day, mk, clv, *vals = r
            f = {"bootstrap": 1.0, "hours_to_ko": 24.0, "n_books": 10.0, "is_player": 0.0, "group=soccer": 1.0,
                 f"market={BOOT_MARKETS[mk]}": 1.0}
            f.update({n: float(v) for n, v in zip(BOOT_COLS, vals) if v or n in BOOT_COLS[:4]})
            if BOOT_MARKETS[mk] == "totals":
                f["has_pm"] = 1.0
            wk = datetime.fromisoformat(day).isocalendar()
            out.append({"date": day, "clv": float(clv), "boot": True, "w": C.BOOT_WEIGHT, "features": f,
                        "market": BOOT_MARKETS[mk], "grp": f"{wk[0]}-{wk[1]:02d}"})
        except (ValueError, TypeError, IndexError):
            continue
    return out


def load_boot():
    return boot_rows(store.load("bootstrap.json", {}))


# ---------- algèbre ----------

def _solve(A, b):
    """Résout A x = b (Gauss avec pivot partiel)."""
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        if abs(M[col][col]) < 1e-12:
            continue
        for r in range(n):
            if r != col and M[r][col]:
                k = M[r][col] / M[col][col]
                M[r] = [a - k * b_ for a, b_ in zip(M[r], M[col])]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def _ridge(rows, names, lam):
    """Ridge pondéré : (XᵀWX + λI) w = XᵀWy, avec biais non pénalisé et variables centrées-réduites.
    Poids d'une ligne : r["w"] (1 par défaut ; les lignes historiques comptent moins)."""
    X = [[r["features"].get(n, 0.0) for n in names] for r in rows]
    y = [r["clv"] for r in rows]
    wt = [r.get("w", 1.0) for r in rows]
    m = len(names)
    mean = [sum(col) / len(X) for col in zip(*X)]
    std = [max(1e-9, math.sqrt(sum((v - mu) ** 2 for v in col) / len(X))) for col, mu in zip(zip(*X), mean)]
    Z = [[(x[j] - mean[j]) / std[j] for j in range(m)] + [1.0] for x in X]
    A = [[0.0] * (m + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        for j in range(i, m + 1):
            A[i][j] = A[j][i] = sum(w_ * z[i] * z[j] for z, w_ in zip(Z, wt)) + (lam if i == j and i < m else 0.0)
    b = [sum(w_ * z[i] * yi for z, w_, yi in zip(Z, wt, y)) for i in range(m + 1)]
    w = _solve(A, b)
    return {"names": names, "mean": mean, "std": std, "w": w}


DUMMIES = ("market=", "group=", "book=")


def _keep_dummies(rows):
    """Variables muettes (marché, groupe, book) assez fréquentes pour avoir leur propre poids : vues dans >= C.LEARN_RARE_MIN lignes (books :
    les C.TOP_BOOKS plus fréquents). Les autres sont regroupées dans « <préfixe>other » (une variable à écart-type minuscule, vue 1 ou 2 fois,
    est standardisée en un pic énorme que le ridge interpole)."""
    cnt = {}
    for r in rows:
        for n in r["features"]:
            if n.startswith(DUMMIES):
                cnt[n] = cnt.get(n, 0) + 1
    keep = {n for n, c in cnt.items() if c >= C.LEARN_RARE_MIN}
    books = sorted((n for n in keep if n.startswith("book=")), key=lambda n: (-cnt[n], n))[:C.TOP_BOOKS]
    return {n for n in keep if not n.startswith("book=")} | set(books)


def _bucket(row, keep):
    """Ligne dont les variables muettes rares sont fondues dans « other » (la ligne d'origine est inchangée)."""
    f = row["features"]
    if all(n in keep for n in f if n.startswith(DUMMIES)):
        return row
    g = {n: v for n, v in f.items() if not n.startswith(DUMMIES) or n in keep}
    for n, v in f.items():
        if n.startswith(DUMMIES) and n not in keep and v:
            g[n.partition("=")[0] + "=other"] = 1.0
    return dict(row, features=g)


def predict(model, feats):
    if not model or not model.get("w"):
        return None
    w, names = model["w"], model["names"]
    for p in DUMMIES:                       # marché / book jamais vu à l'entraînement (ou rare) : la variable « other » du modèle, si elle existe
        o = p + "other"
        if o in names and o not in feats and any(k.startswith(p) and k not in names and v for k, v in feats.items()):
            feats = dict(feats, **{o: 1.0})
    # écart-type quasi nul (variable constante à l'entraînement) : poids ~ 0, on évite la division explosive
    return sum(w[j] * (feats.get(n, 0.0) - model["mean"][j]) / (model["std"][j] if model["std"][j] > 1e-6 else 1.0)
               for j, n in enumerate(names)) + w[-1]


def _feature_names(rows):
    """Variables du modèle ; `rows` déjà regroupées (_bucket) : au plus C.TOP_BOOKS books plus « other »."""
    books = {}
    for r in rows:
        for n in r["features"]:
            if n.startswith("book="):
                books[n] = books.get(n, 0) + 1
    top_books = [b for b, _ in sorted(books.items(), key=lambda kv: (-kv[1], kv[0]))[:C.TOP_BOOKS + 1]]
    base = ["edge", "spread", "steam", "log_odds", "hours_to_ko", "heuristic", "n_books", "is_player"]
    base += [n for n in PM_NAMES if any(r["features"].get(n) for r in rows)]
    if any(r.get("boot") for r in rows):
        base.append("bootstrap")
    markets = sorted({n for r in rows for n in r["features"] if n.startswith("market=")})
    groups = sorted({n for r in rows for n in r["features"] if n.startswith("group=")})
    return base + markets + groups + top_books


def _top_by_day(rows, key, k=15):
    """CLV moyen des meilleurs paris (au plus k, au plus le tiers) de chaque jour (ou groupe `grp`, une semaine pour l'historique), selon `key` :
    {jour: CLV moyen}."""
    by_day = {}
    for r in rows:
        by_day.setdefault(r.get("grp", r["date"]), []).append(r)
    out = {}
    for day, day_rows in by_day.items():
        best = sorted(day_rows, key=key, reverse=True)[:max(1, min(k, len(day_rows) // 3))]
        out[day] = sum(r["clv"] for r in best) / len(best)
    return out


def _top_mean(rows, key, k=15):
    """CLV moyen des meilleurs paris de chaque jour (voir _top_by_day), moyenné sur les jours."""
    vals = list(_top_by_day(rows, key, k).values())
    return sum(vals) / len(vals) if vals else 0.0


def _judge(a, b):
    """(a bat b ?, moyenne a, moyenne b, t) sur des CLV moyens par jour {jour: CLV} : écart moyen > C.LEARN_OOS_MARGIN ET > C.LEARN_OOS_Z erreurs-types
    (écart-type des écarts quotidiens / racine du nombre de jours)."""
    diffs = [a[d] - b[d] for d in a]
    n = len(diffs)
    mean = sum(diffs) / n if n else 0.0
    sd = math.sqrt(sum((x - mean) ** 2 for x in diffs) / (n - 1)) if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd > 0 else (math.inf if mean > 0 else 0.0)
    return mean > C.LEARN_OOS_MARGIN and t >= C.LEARN_OOS_Z, sum(a.values()) / n if n else 0.0, sum(b.values()) / n if n else 0.0, t


def beats(test, key_model, key_heur):
    """(modèle bat l'heuristique ?, oos modèle, oos heuristique, t) sur le test : écart moyen des CLV des meilleurs paris, jour par jour. Il faut un écart > C.LEARN_OOS_MARGIN ET
    un écart > C.LEARN_OOS_Z erreurs-types (écart-type des écarts quotidiens / racine du nombre de jours) : avec 5 jours de test, un écart de quelques millièmes
    est du bruit."""
    return _judge(_top_by_day(test, key_model), _top_by_day(test, key_heur))


def _folds(rows, k, min_days=0, min_train_days=1):
    """k coupes successives dans le temps, à la fin de la période, sans chevauchement : [(apprentissage, test)], chaque test sur des jours qui suivent
    TOUT son apprentissage (jamais le futur). Chaque test a au moins `min_days` jours (et environ 20 % des jours / k) ; [] s'il n'y a pas assez de jours
    pour k tests et `min_train_days` jours d'apprentissage avant le premier."""
    days = sorted({r["date"] for r in rows})
    w = max(1, min_days, int(len(days) * 0.2 / k)) if k else 0
    if k < 2 or len(days) - k * w < max(1, min_train_days):
        return []
    out = []
    for i in range(k):
        lo, hi = len(days) - (k - i) * w, len(days) - (k - 1 - i) * w
        first, last = days[lo], days[hi - 1]
        out.append(([r for r in rows if r["date"] < first], [r for r in rows if first <= r["date"] <= last]))
    return out


def _split(rows, min_days=0, min_train_days=1):
    """Découpe temporelle par date : (apprentissage, test), 80/20 des jours (le test a au moins `min_days` jours, l'apprentissage au moins
    `min_train_days`). Jamais de test sur les lignes d'apprentissage : trop peu de jours = test vide."""
    days = sorted({r["date"] for r in rows})
    n_train = min(max(1, int(len(days) * 0.8)), len(days) - min_days)
    if n_train < max(1, min_train_days) or len(days) < 2:
        return rows, []
    cut = days[n_train - 1]
    return [r for r in rows if r["date"] <= cut], [r for r in rows if r["date"] > cut]


def oos_valid(test):
    """Test hors échantillon valable : au moins C.LEARN_MIN_TEST_ROWS lignes sur C.LEARN_MIN_TEST_DAYS jours."""
    return len(test) >= C.LEARN_MIN_TEST_ROWS and len({r["date"] for r in test}) >= C.LEARN_MIN_TEST_DAYS


def train_clv(rows, previous=None, boot=None):
    """Entraîne, valide hors échantillon, décide de l'adoption. `boot` : lignes historiques (boot_rows).
    Test : sur les paris réels dès que le test est valable (voir oos_valid : >= LEARN_MIN_TEST_ROWS lignes sur >= LEARN_MIN_TEST_DAYS jours), sinon sur la
    dernière tranche de l'historique, mise de côté ; le modèle final apprend sur tout (historique + réel). Jamais de test sur les lignes
    d'apprentissage : sans test valable, rien n'est adopté (un ancien modèle adopté est conservé). Adoption : oos_modèle > oos_heuristique + LEARN_OOS_MARGIN et écart d'au moins LEARN_OOS_Z erreurs-types (beats) ;
    avec assez de jours, sur C.LEARN_FOLDS coupes successives (_folds) : mieux que l'heuristique sur la majorité ET le même critère sur l'ensemble de leurs jours de test.
    Pénalité ridge = C.RIDGE_LAMBDA x nombre de variables."""
    real = [r for r in rows if r.get("clv") is not None and not r.get("boot")]
    boot = [r for r in (boot or []) if r.get("clv") is not None]
    out = {"n": len(real), "adopted": False, "trained_at": datetime.now(timezone.utc).isoformat(timespec="minutes")}
    if len(real) < C.LEARN_MIN_ROWS and len(boot) < C.BOOT_MIN_ROWS:
        out["reason"] = f"pas assez de clôtures ({len(real)}/{C.LEARN_MIN_ROWS})"
        return out
    keep = _keep_dummies(real + boot)
    real, boot = [_bucket(r, keep) for r in real], [_bucket(r, keep) for r in boot]
    names = _feature_names(real + boot)
    src = None
    if len(real) >= C.LEARN_MIN_ROWS:
        train, test = _split(real, C.LEARN_MIN_TEST_DAYS, C.LEARN_MIN_TRAIN_DAYS)
        if oos_valid(test):
            fit, final_rows, src = boot + train, boot + real, "réel"
    if src is None and len(boot) >= C.BOOT_MIN_ROWS:
        train, test = _split(boot, C.LEARN_MIN_TEST_DAYS, C.LEARN_MIN_TRAIN_DAYS)
        if oos_valid(test):
            fit, final_rows, src = train, boot + real, "historique"
    keep_old = None
    if previous and previous.get("adopted"):
        keep_old = {k: previous[k] for k in ("names", "mean", "std", "w")} | {"adopted": True}
    if src is None:
        out["reason"] = (f"pas encore de test hors échantillon valable (il faut {C.LEARN_MIN_TEST_ROWS} lignes sur {C.LEARN_MIN_TEST_DAYS} jours "
                         f"au moins, jamais sur les lignes d'apprentissage) : score heuristique conservé")
        if keep_old:
            out.update(keep_old | {"reason": "pas de test hors échantillon valable : ancien modèle conservé"})
        return out
    lam = C.RIDGE_LAMBDA * len(names)
    heur = lambda r: r["features"]["heuristic"]           # noqa: E731
    # plusieurs coupes successives quand il y a assez de jours (C.LEARN_FOLDS, toutes valables) : le modèle doit faire mieux que l'heuristique sur la
    # majorité d'entre elles (écart moyen > 0 : les premières apprennent sur peu de jours) ET la battre sur l'ensemble de leurs jours de test (marge
    # C.LEARN_OOS_MARGIN et seuil d'erreurs-types C.LEARN_OOS_Z, comme une coupe unique) ; sinon, la coupe unique ci-dessus
    base_rows, extra = (real, boot) if src == "réel" else (boot, [])
    folds = _folds(base_rows, C.LEARN_FOLDS, C.LEARN_MIN_TEST_DAYS, C.LEARN_MIN_TRAIN_DAYS)
    if len(folds) < C.LEARN_FOLDS or not all(oos_valid(te) for _, te in folds):
        folds = []
    report = []
    if folds:
        a_all, b_all = {}, {}
        for i, (tr, te) in enumerate(folds):
            m = _ridge(extra + tr, names, lam)
            a, b = _top_by_day(te, lambda r, m=m: predict(m, r["features"])), _top_by_day(te, heur)
            gap = sum(a[d] - b[d] for d in a) / len(a)
            report.append({"days": len({r["date"] for r in te}), "rows": len(te), "gap": round(gap, 4), "win": gap > 0})
            a_all.update({(i, d): v for d, v in a.items()})
            b_all.update({(i, d): v for d, v in b.items()})
        pooled, oos_model, oos_heur, t = _judge(a_all, b_all)
        better = pooled and sum(f["win"] for f in report) * 2 > len(report)
        test = [r for _, te in folds for r in te]
    else:
        m = _ridge(fit, names, lam)
        better, oos_model, oos_heur, t = beats(test, lambda r: predict(m, r["features"]), heur)
    out.update({"oos_model": round(oos_model, 4), "oos_heuristic": round(oos_heur, 4), "test_rows": len(test),
                "test_days": len({r["date"] for r in test}), "oos_source": src, "boot_n": len(boot), "ridge_lambda": round(lam, 2),
                "oos_t": None if t == math.inf else round(t, 2)} | ({"folds": report} if report else {}))
    if better:
        final = _ridge(final_rows, names, lam)                   # ré-entraîné sur tout
        out.update(final | {"adopted": True})
    elif keep_old and not (src == "réel" and previous.get("oos_source") == "historique"):
        out.update(keep_old | {"reason": "nouveau modèle moins bon hors échantillon : ancien conservé"})
    else:
        out["reason"] = "le modèle ne bat pas l'heuristique hors échantillon (marge exigée) : score heuristique conservé"
    return out


# ---------- calibration par résultats ----------

def _logit(p):
    p = min(1 - 1e-6, max(1e-6, p))
    return math.log(p / (1 - p))


def _sigmoid(x):
    return 1 / (1 + math.exp(-x))


def _p_fit(r):
    """Proba sur laquelle la calibration s'applique : la proba brute des bookmakers (`prob_book`), pas r["prob"] qui est déjà
    calibrée puis mélangée au modèle d'avant-match (ajuster dessus ferait annuler chaque réentraînement par le précédent).
    Anciennes lignes sans prob_book : r["prob"]."""
    p = r.get("prob_book")
    return r["prob"] if p is None else p


def _platt(rows, iters=500, lr=0.05):
    a, b = 0.0, 1.0
    xs = [_logit(_p_fit(r)) for r in rows]
    ys = [1.0 if r["result"] == "win" else 0.0 for r in rows]
    n = len(rows)
    for _ in range(iters):
        ga = gb = 0.0
        for x, y in zip(xs, ys):
            e = _sigmoid(a + b * x) - y
            ga += e; gb += e * x
        a -= lr * ga / n; b -= lr * gb / n
    return a, b


def _brier(rows, a=0.0, b=1.0):
    return sum((_sigmoid(a + b * _logit(_p_fit(r))) -(1.0 if r["result"] == "win" else 0.0)) ** 2 for r in rows) / len(rows)


def _blocks(rows, k):
    """`rows` (ordre chronologique) en k blocs contigus de taille voisine ; les lignes d'un même jour (champ « date », s'il existe) restent dans le même bloc."""
    n = len(rows)
    cuts, prev = [], 0
    for j in range(1, k):
        i = max(prev, round(j * n / k))
        while 0 < i < n and rows[i].get("date") is not None and rows[i].get("date") == rows[i - 1].get("date"):
            i += 1
        cuts.append(min(i, n))
        prev = cuts[-1]
    edges = [0] + cuts + [n]
    return [rows[a:b] for a, b in zip(edges, edges[1:]) if b > a]


def _oos_brier(rows, k=None):
    """(Brier brut, Brier recalibré) HORS échantillon : validation croisée par blocs de jours (chaque bloc est évalué avec un Platt ajusté sur les autres),
    ou None si moins de 3 blocs. Les lignes sans date gardent leur ordre (tracked.json est chronologique)."""
    k = k or C.CALIB_FOLDS
    dated = all(r.get("date") for r in rows)
    ordered = sorted(rows, key=lambda r: r["date"]) if dated else list(rows)
    blocks = _blocks(ordered, k)
    if len(blocks) < 3:
        return None
    raw = cal = 0.0
    for i, test in enumerate(blocks):
        a, b = _platt([r for j, blk in enumerate(blocks) if j != i for r in blk])
        raw += _brier(test) * len(test)
        cal += _brier(test, a, b) * len(test)
    return raw / len(ordered), cal / len(ordered)


def calibrate(rows):
    """Platt par famille. `active` seulement si le Brier s'améliore HORS échantillon d'au moins C.CALIB_OOS_MARGIN (oos_before -> oos_after) : sur ses
    propres lignes le Platt gagne presque toujours. brier_before / brier_after restent les valeurs sur les lignes d'ajustement (affichage)."""
    settled = [r for r in rows if r.get("result") in ("win", "loss")]
    out = {}
    fams = {}
    for r in settled:
        fams.setdefault(family(r["market"]), []).append(r)
    for fam, frows in fams.items():
        if len(frows) < C.CALIB_MIN_ROWS:
            out[fam] = {"n": len(frows), "active": False}
            continue
        a, b = _platt(frows)
        before, after = _brier(frows), _brier(frows, a, b)
        oos = _oos_brier(frows)
        out[fam] = {"n": len(frows), "active": bool(oos and oos[1] < oos[0] - C.CALIB_OOS_MARGIN), "a": round(a, 4), "b": round(b, 4),
                    "brier_before": round(before, 4), "brier_after": round(after, 4)}
        if oos:
            out[fam] |= {"oos_before": round(oos[0], 4), "oos_after": round(oos[1], 4)}
    return out


def calibrated(model, market, p):
    cal = (model or {}).get("calibration", {}).get(family(market))
    if not cal or not cal.get("active"):
        return p
    return _sigmoid(cal["a"] + cal["b"] * _logit(p))


# ---------- books, résumé ----------

def book_stats(rows):
    books = {}
    for r in rows:
        if r.get("clv") is None or r.get("close_src"):     # cote finale gratuite : mouvement du marché, pas le prix d'un book
            continue
        b = books.setdefault(r.get("book") or "?", {"n": 0, "clv": 0.0})
        b["n"] += 1; b["clv"] += r["clv"]
    return {k: {"n": v["n"], "clv": round(v["clv"] / v["n"], 4)} for k, v in books.items() if v["n"] >= 5}


def update(rows, combos=None):
    previous = load()
    model = train_clv(rows, previous, load_boot())
    model["calibration"] = calibrate(rows)
    model["books"] = book_stats(rows)
    store.save("model.json", model)
    return model


def load():
    return store.load("model.json", {})


def describe(model, rows, days=7):
    if not model or (not rows and not model.get("boot_n")):
        return ""
    closed = [r for r in rows if r.get("clv") is not None]
    sent = [r for r in closed if r.get("sent")]
    lines = [f"🧠 KELLY APPREND — {len(rows)} paris suivis, {len(closed)} avec clôture"]
    if sent:
        cutoff = str((datetime.now(timezone.utc).date()).toordinal() - days)
        recent = [r for r in sent if datetime.fromisoformat(r["date"]).toordinal() > int(cutoff)]
        beat = sum(1 for r in sent if r["clv"] > 0) / len(sent) * 100
        mean_all = sum(r["clv"] for r in sent) / len(sent) * 100
        txt = f"Envoyés : CLV moyen {mean_all:+.1f}% (bat la clôture {beat:.0f}% du temps)"
        if recent:
            txt += f", {days} derniers jours {sum(r['clv'] for r in recent)/len(recent)*100:+.1f}%"
        lines.append(txt)
    if model.get("adopted"):
        w = sorted(zip(model["names"], model["w"]), key=lambda kv: -abs(kv[1]))[:3]
        lines.append("Modèle adopté. Signaux qui pèsent : " + ", ".join(
            f"{n.replace('market=', '').replace('book=', 'book ')} ({v:+.3f})" for n, v in w))
        what = "matchs historiques" if model.get("oos_source") == "historique" else "paris"
        lines.append(f"Test hors échantillon ({model['test_rows']} {what}) : modèle {model['oos_model']*100:+.1f}% "
                     f"vs heuristique {model['oos_heuristic']*100:+.1f}%")
    else:
        lines.append("Modèle : " + model.get("reason", "pas encore"))
    if model.get("folds"):
        f = model["folds"]
        lines.append(f"Coupes successives : mieux que l'heuristique sur {sum(x['win'] for x in f)} sur {len(f)} ("
                     + ", ".join(f"{x['gap'] * 100:+.1f}%" for x in f) + ")")
    if model.get("boot_n"):
        lines.append(f"📚 Historique football-data : {model['boot_n']} lignes (cotes d'ouverture vs clôture) "
                     f"en appoint de l'apprentissage")
    slow = sorted(model.get("books", {}).items(), key=lambda kv: -kv[1]["clv"])[:3]
    if slow:
        lines.append("Books les plus lents : " + ", ".join(f'{k} ({v["clv"]*100:+.1f}%, n={v["n"]})' for k, v in slow))
    cal = model.get("calibration", {})
    active = [f"{k} ({v['n']}, Brier hors échantillon {v.get('oos_before', v['brier_before']):.3f}→{v.get('oos_after', v['brier_after']):.3f})"
              for k, v in cal.items() if v.get("active")]
    waiting = [f"{k} {v['n']}/{C.CALIB_MIN_ROWS}" if v["n"] < C.CALIB_MIN_ROWS else f"{k} (pas d'amélioration hors échantillon)"
               for k, v in cal.items() if not v.get("active")]
    if active:
        lines.append("Calibration active : " + ", ".join(active))
    if waiting:
        lines.append("Calibration en attente : " + ", ".join(waiting))
    return "\n".join(lines)


def describe_simple(model, rows):
    """🧠 en 2 ou 3 lignes simples, sans chiffre technique (CLV, Brier, coefficients : voir /bilan détail et --check).
    '' s'il n'y a rien à dire."""
    from . import fmt
    sent = [r for r in rows if r.get("sent") and r.get("clv") is not None][-10:]
    lines = []
    if len(sent) >= 3:
        beat = sum(1 for r in sent if r["clv"] > 0)
        lines.append(f"Kelly a battu la cote finale sur {beat} de ses {len(sent)} derniers paris "
                     "(c'est le meilleur signe qu'une méthode est bonne).")
    elif rows:
        lines.append(f"Kelly suit {len(rows)} paris pour apprendre, mais ne peut pas encore se juger sur la cote finale.")
    if model and model.get("adopted"):
        mk = [(n[7:], w) for n, w in zip(model.get("names", []), model.get("w", [])) if n.startswith("market=") and w > 0]
        if mk:
            best = max(mk, key=lambda x: x[1])[0]
            lines.append(f"Elle fait davantage confiance au marché « {fmt.esc(C.MARKET_LABELS.get(best, best))} » en ce moment.")
    elif model and rows:
        lines.append("Elle apprend encore : pas assez de paris suivis pour changer sa façon de choisir.")
    return fmt.Html("🧠 <b>Ce que Kelly apprend</b>\n" + "\n".join(f"• {x}" for x in lines)) if lines else ""
