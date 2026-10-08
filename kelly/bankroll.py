"""Gestion de mise : Kelly fractionnaire, plafonds par palier, série noire."""
from . import config as C, store


def load():
    return store.load("bankroll.json", {"bankroll": 100.0, "initial": 100.0})


def save(b):
    store.save("bankroll.json", b)


def stake(prob, odds, tier, bankroll):
    """Mise conseillée (unités). Kelly/4, plafonné selon le palier."""
    cap = next((v for k, v in C.STAKE_CAP.items() if tier.startswith(k)), 0.01)
    f = (prob * odds - 1) / (odds - 1) if odds > 1 else 0
    share = max(C.MIN_STAKE_SHARE, min(cap, f * C.KELLY_FRACTION))
    return round(bankroll * share, 2)


def apply_results(combos_settled):
    """Met à jour la bankroll avec les combinés réglés non encore comptés."""
    b = load()
    for c in combos_settled:
        if c.get("counted") or c.get("result") not in ("win", "loss"):
            continue
        c["counted"] = True
        b["bankroll"] += c["stake"] * (c.get("odds_final", c["odds"]) - 1) if c["result"] == "win" else -c["stake"]
    b["bankroll"] = round(b["bankroll"], 2)
    save(b)
    return b


def streak(combos):
    """Nombre de combinés perdus d'affilée (les plus récents)."""
    n = 0
    for c in sorted((c for c in combos if c.get("result") in ("win", "loss")), key=lambda c: c["date"], reverse=True):
        if c["result"] == "loss":
            n += 1
        else:
            break
    return n


def summary(combos):
    b = load()
    pnl = b["bankroll"] - b["initial"]
    lines = [f"💼 Bankroll : {b['bankroll']:.2f} unités ({pnl:+.2f} depuis le début)"]
    s = streak(combos)
    if s >= C.STREAK_WARNING:
        lines.append(f"⚠️ {s} combinés perdus d'affilée : lève le pied, réduis les mises ou fais une pause.")
    return "\n".join(lines)
