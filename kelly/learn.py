"""Apprentissage de Kelly : par la cote de clôture d'abord, les résultats en appoint.

1. Modèle CLV : régression ridge qui prédit, à partir des signaux d'un pari
   (valeur, désaccord, mouvement, marché, bookmaker...), de combien il battra
   la clôture. Il n'est adopté que s'il fait au moins aussi bien que le score
   heuristique sur des jours qu'il n'a pas vus (split temporel).
2. Calibration : une fois assez de résultats réglés (>= CALIB_MIN_ROWS par
   famille de marchés), Platt scaling pour que « 60 % » passe bien 60 % du temps.
3. Bookmakers lents : CLV moyen quand un book porte la meilleure cote.

4. Amorçage : lignes historiques football-data (data/bootstrap.json, cotes d'ouverture vs clôture, voir
   kelly/bootstrap.py) pour entraîner le modèle dès le premier jour. Elles sont marquées (`boot`), pèsent
   moins qu'un pari réel et un indicateur « bootstrap » absorbe l'écart entre les deux populations. Le test
   hors échantillon se fait sur les paris réels dès qu'il y en a assez, sinon sur une tranche historique
   mise de côté.

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


def predict(model, feats):
    if not model or not model.get("w"):
        return None
    w, names = model["w"], model["names"]
    # écart-type quasi nul (variable constante à l'entraînement) : poids ~ 0, on évite la division explosive
    return sum(w[j] * (feats.get(n, 0.0) - model["mean"][j]) / (model["std"][j] if model["std"][j] > 1e-6 else 1.0)
               for j, n in enumerate(names)) + w[-1]


def _feature_names(rows):
    books = {}
    for r in rows:
        for n in r["features"]:
            if n.startswith("book="):
                books[n] = books.get(n, 0) + 1
    top_books = [b for b, _ in sorted(books.items(), key=lambda kv: -kv[1])[:C.TOP_BOOKS]]
    base = ["edge", "spread", "steam", "log_odds", "hours_to_ko", "heuristic", "n_books", "is_player"]
    base += [n for n in PM_NAMES if any(r["features"].get(n) for r in rows)]
    if any(r.get("boot") for r in rows):
        base.append("bootstrap")
    markets = sorted({n for r in rows for n in r["features"] if n.startswith("market=")})
    groups = sorted({n for r in rows for n in r["features"] if n.startswith("group=")})
    return base + markets + groups + top_books


def _top_mean(rows, key, k=15):
    """CLV moyen des meilleurs paris (au plus k, au plus le tiers) de chaque jour (ou groupe `grp`, une semaine
    pour l'historique), selon `key`, moyenné sur les jours."""
    by_day = {}
    for r in rows:
        by_day.setdefault(r.get("grp", r["date"]), []).append(r)
    vals = []
    for day_rows in by_day.values():
        best = sorted(day_rows, key=key, reverse=True)[:max(1, min(k, len(day_rows) // 3))]
        vals.append(sum(r["clv"] for r in best) / len(best))
    return sum(vals) / len(vals) if vals else 0.0


def _split(rows):
    """Découpe temporelle 80/20 par date : (apprentissage, test). Test vide = en échantillon (derniers 20 %)."""
    days = sorted({r["date"] for r in rows})
    cut = days[max(1, int(len(days) * 0.8)) - 1]
    train = [r for r in rows if r["date"] <= cut]
    return train, [r for r in rows if r["date"] > cut] or train[-max(5, len(train) // 5):]


def train_clv(rows, previous=None, boot=None):
    """Entraîne, valide hors échantillon, décide de l'adoption. `boot` : lignes historiques (boot_rows).
    Test : sur les paris réels dès qu'il y en a assez (>= LEARN_MIN_ROWS et 2 jours), sinon sur la dernière
    tranche de l'historique, mise de côté ; le modèle final apprend sur tout (historique + réel)."""
    real = [r for r in rows if r.get("clv") is not None and not r.get("boot")]
    boot = [r for r in (boot or []) if r.get("clv") is not None]
    out = {"n": len(real), "adopted": False, "trained_at": datetime.now(timezone.utc).isoformat(timespec="minutes")}
    real_ok = len(real) >= C.LEARN_MIN_ROWS and (len({r["date"] for r in real}) >= 2 or not boot)
    if not real_ok and len(boot) < C.BOOT_MIN_ROWS:
        out["reason"] = f"pas assez de clôtures ({len(real)}/{C.LEARN_MIN_ROWS})"
        return out
    names = _feature_names(real + boot)
    if real_ok:
        train, test = _split(real)
        fit, final_rows, src = boot + train, boot + real, "réel"
    else:
        train, test = _split(boot)
        fit, final_rows, src = train, boot + real, "historique"
    m = _ridge(fit, names, C.RIDGE_LAMBDA)
    oos_model = _top_mean(test, lambda r: predict(m, r["features"]))
    oos_heur = _top_mean(test, lambda r: r["features"]["heuristic"])
    out.update({"oos_model": round(oos_model, 4), "oos_heuristic": round(oos_heur, 4), "test_rows": len(test),
                "oos_source": src, "boot_n": len(boot)})
    if oos_model >= oos_heur:
        final = _ridge(final_rows, names, C.RIDGE_LAMBDA)        # ré-entraîné sur tout
        out.update(final | {"adopted": True})
    elif previous and previous.get("adopted") and not (src == "réel" and previous.get("oos_source") == "historique"):
        keep = {k: previous[k] for k in ("names", "mean", "std", "w")}
        out.update(keep | {"adopted": True, "reason": "nouveau modèle moins bon hors échantillon : ancien conservé"})
    else:
        out["reason"] = "le modèle ne bat pas l'heuristique hors échantillon : score heuristique conservé"
    return out


# ---------- calibration par résultats ----------

def _logit(p):
    p = min(1 - 1e-6, max(1e-6, p))
    return math.log(p / (1 - p))


def _sigmoid(x):
    return 1 / (1 + math.exp(-x))


def _platt(rows, iters=500, lr=0.05):
    a, b = 0.0, 1.0
    xs = [_logit(r["prob"]) for r in rows]
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
    return sum((_sigmoid(a + b * _logit(r["prob"])) - (1.0 if r["result"] == "win" else 0.0)) ** 2 for r in rows) / len(rows)


def calibrate(rows):
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
        out[fam] = {"n": len(frows), "active": after <= before, "a": round(a, 4), "b": round(b, 4),
                    "brier_before": round(before, 4), "brier_after": round(after, 4)}
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
        if r.get("clv") is None:
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
    if model.get("boot_n"):
        lines.append(f"📚 Historique football-data : {model['boot_n']} lignes (cotes d'ouverture vs clôture) "
                     f"en appoint de l'apprentissage")
    slow = sorted(model.get("books", {}).items(), key=lambda kv: -kv[1]["clv"])[:3]
    if slow:
        lines.append("Books les plus lents : " + ", ".join(f'{k} ({v["clv"]*100:+.1f}%, n={v["n"]})' for k, v in slow))
    cal = model.get("calibration", {})
    active = [f"{k} ({v['n']}, Brier {v['brier_before']:.3f}→{v['brier_after']:.3f})" for k, v in cal.items() if v.get("active")]
    waiting = [f"{k} {v['n']}/{C.CALIB_MIN_ROWS}" for k, v in cal.items() if not v.get("active")]
    if active:
        lines.append("Calibration active : " + ", ".join(active))
    if waiting:
        lines.append("Calibration en attente : " + ", ".join(waiting))
    return "\n".join(lines)
