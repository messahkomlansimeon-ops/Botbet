"""Apprentissage de Kelly : par la cote de clôture d'abord, les résultats en appoint.

1. Modèle CLV : régression ridge qui prédit, à partir des signaux d'un pari
   (valeur, désaccord, mouvement, marché, bookmaker...), de combien il battra
   la clôture. Il n'est adopté que s'il fait au moins aussi bien que le score
   heuristique sur des jours qu'il n'a pas vus (split temporel).
2. Calibration : une fois assez de résultats réglés (>= CALIB_MIN_ROWS par
   famille de marchés), Platt scaling pour que « 60 % » passe bien 60 % du temps.
3. Bookmakers lents : CLV moyen quand un book porte la meilleure cote.

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


def features(c, now=None):
    """Signaux figés au moment du choix (dict nom -> valeur)."""
    now = now or datetime.now(timezone.utc)
    start = datetime.fromisoformat(c["commence"].replace("Z", "+00:00"))
    f = {
        "edge": c["edge"], "spread": c.get("spread", 0.0), "steam": c.get("steam", 0.0),
        "log_odds": math.log(c["odds"]), "hours_to_ko": max(0.0, (start - now).total_seconds() / 3600),
        "heuristic": c["score"], "n_books": c.get("n_books", 0), "is_player": float(c["market"].startswith("player_")),
        f"market={c['market']}": 1.0, f"group={c['group']}": 1.0, f"book={c.get('book', '')}": 1.0,
    }
    return f


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
    """Ridge : (XᵀX + λI) w = Xᵀy, avec biais non pénalisé et variables centrées-réduites."""
    X = [[r["features"].get(n, 0.0) for n in names] for r in rows]
    y = [r["clv"] for r in rows]
    m = len(names)
    mean = [sum(col) / len(X) for col in zip(*X)]
    std = [max(1e-9, math.sqrt(sum((v - mu) ** 2 for v in col) / len(X))) for col, mu in zip(zip(*X), mean)]
    Z = [[(x[j] - mean[j]) / std[j] for j in range(m)] + [1.0] for x in X]
    A = [[sum(z[i] * z[j] for z in Z) + (lam if i == j and i < m else 0.0) for j in range(m + 1)] for i in range(m + 1)]
    b = [sum(z[i] * yi for z, yi in zip(Z, y)) for i in range(m + 1)]
    w = _solve(A, b)
    return {"names": names, "mean": mean, "std": std, "w": w}


def predict(model, feats):
    if not model or not model.get("w"):
        return None
    w, names = model["w"], model["names"]
    return sum(w[j] * (feats.get(n, 0.0) - model["mean"][j]) / model["std"][j] for j, n in enumerate(names)) + w[-1]


def _feature_names(rows):
    books = {}
    for r in rows:
        for n in r["features"]:
            if n.startswith("book="):
                books[n] = books.get(n, 0) + 1
    top_books = [b for b, _ in sorted(books.items(), key=lambda kv: -kv[1])[:C.TOP_BOOKS]]
    base = ["edge", "spread", "steam", "log_odds", "hours_to_ko", "heuristic", "n_books", "is_player"]
    markets = sorted({n for r in rows for n in r["features"] if n.startswith("market=")})
    groups = sorted({n for r in rows for n in r["features"] if n.startswith("group=")})
    return base + markets + groups + top_books


def _top_mean(rows, key, k=15):
    """CLV moyen des k meilleurs paris du jour selon `key`, moyenné sur les jours."""
    by_day = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)
    vals = []
    for day_rows in by_day.values():
        best = sorted(day_rows, key=key, reverse=True)[:k]
        vals.append(sum(r["clv"] for r in best) / len(best))
    return sum(vals) / len(vals) if vals else 0.0


def train_clv(rows, previous=None):
    """Entraîne, valide hors échantillon, décide de l'adoption."""
    rows = [r for r in rows if r.get("clv") is not None]
    out = {"n": len(rows), "adopted": False, "trained_at": datetime.now(timezone.utc).isoformat(timespec="minutes")}
    if len(rows) < C.LEARN_MIN_ROWS:
        out["reason"] = f"pas assez de clôtures ({len(rows)}/{C.LEARN_MIN_ROWS})"
        return out
    names = _feature_names(rows)
    days = sorted({r["date"] for r in rows})
    cut = days[max(1, int(len(days) * 0.8)) - 1]
    train = [r for r in rows if r["date"] <= cut]
    test = [r for r in rows if r["date"] > cut] or train[-max(5, len(train) // 5):]
    m = _ridge(train, names, C.RIDGE_LAMBDA)
    oos_model = _top_mean(test, lambda r: predict(m, r["features"]))
    oos_heur = _top_mean(test, lambda r: r["features"]["heuristic"])
    out.update({"oos_model": round(oos_model, 4), "oos_heuristic": round(oos_heur, 4), "test_rows": len(test)})
    if oos_model >= oos_heur:
        final = _ridge(rows, names, C.RIDGE_LAMBDA)        # ré-entraîné sur tout
        out.update(final | {"adopted": True})
    elif previous and previous.get("adopted"):
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
    model = train_clv(rows, previous)
    model["calibration"] = calibrate(rows)
    model["books"] = book_stats(rows)
    store.save("model.json", model)
    return model


def load():
    return store.load("model.json", {})


def describe(model, rows, days=7):
    if not model or not rows:
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
        lines.append(f"Test hors échantillon ({model['test_rows']} paris) : modèle {model['oos_model']*100:+.1f}% "
                     f"vs heuristique {model['oos_heuristic']*100:+.1f}%")
    else:
        lines.append("Modèle : " + model.get("reason", "pas encore"))
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
