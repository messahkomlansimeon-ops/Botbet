"""Apprentissage : le bot ajuste ses poids selon ses résultats réels.

Pour chaque marché et chaque ligue : facteur = (victoires + K x attendu) /
(attendu + K x attendu). Un marché qui gagne plus que ce que les cotes
prédisent est favorisé, un marché qui déçoit est pénalisé. K lisse les
petits échantillons : avec 3 paris le facteur bouge à peine, avec 100 il
reflète vraiment la performance.
"""
from . import config as C, store


def _factor(rows):
    n = len(rows)
    if n == 0:
        return 1.0, 0, 0
    wins = sum(1 for r in rows if r["result"] == "win")
    expected = sum(r["prob"] for r in rows)
    prior = C.LEARN_SHRINK * expected / n
    f = (wins + prior) / (expected + prior)
    lo, hi = C.LEARN_CLAMP
    return max(lo, min(hi, f)), n, wins


def update(bets):
    picks = [b for b in bets["picks"] if b.get("result") in ("win", "loss")]
    model = {"market": {}, "league": {}, "n": len(picks)}
    for field, name in (("market", "market"), ("sport_key", "league")):
        groups = {}
        for b in picks:
            groups.setdefault(b[field], []).append(b)
        for k, rows in groups.items():
            f, n, w = _factor(rows)
            model[name][k] = {"factor": round(f, 3), "n": n, "wins": w}
    store.save("model.json", model)
    return model


def load():
    return store.load("model.json", {})


def describe(model):
    if not model or not model.get("n"):
        return ""
    ups = [(k, v) for k, v in model["market"].items() if v["factor"] >= 1.08 and v["n"] >= 5]
    downs = [(k, v) for k, v in model["market"].items() if v["factor"] <= 0.92 and v["n"] >= 5]
    parts = []
    if ups:
        parts.append("favorise " + ", ".join(f'{C.MARKET_LABELS.get(k, k)} ({v["wins"]}/{v["n"]})' for k, v in ups))
    if downs:
        parts.append("se méfie de " + ", ".join(f'{C.MARKET_LABELS.get(k, k)} ({v["wins"]}/{v["n"]})' for k, v in downs))
    return f"🧠 Appris sur {model['n']} paris : " + " ; ".join(parts) if parts else ""
