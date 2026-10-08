"""Gestion de mise : Kelly fractionnaire, plafonds par palier, série noire.

Mode réel (bankroll.json « real », activé par le premier /joue) : la bankroll ne suit plus que les paris notés avec /joue, à la
cote et à la mise obtenues chez 1xBet (x["played"] = {odds, stake}). Sinon elle suit les combinés envoyés, à la mise conseillée."""
import math

from . import config as C, store

DONE = ("win", "loss", "push", "void")


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

def played_gain(x):
    """Gain réel (u) d'un pari noté avec /joue et réglé. Jambes annulées : la cote 1xBet baisse dans la même proportion que la
    cote de référence (cote typique ; cote mini des jambes gagnées pour un combiné à vérifier)."""
    p = x["played"]
    if x["result"] == "loss":
        return -p["stake"]
    if x["result"] != "win":
        return 0.0
    if "legs" in x:                                 # combiné à vérifier chez 1xBet (data/fair_combos.json)
        won = [leg["min_odds"] for leg in x["legs"] if leg.get("result") == "win"]
        ratio = math.prod(won) / x["min_odds"] if won else 1.0
    else:
        ratio = x.get("odds_final", x["odds"]) / x["odds"]
    return p["stake"] * (p["odds"] * ratio - 1)

def apply_results(items, played_only=False):
    """Met à jour la bankroll avec les paris réglés non encore comptés (marqués « counted »). Mode réel : seuls les paris notés
    comptent (played_gain). Sinon : les combinés gagnés ou perdus, à la mise conseillée. `played_only` : éléments qui ne
    comptent qu'en mode réel (pari du jour, combinés à vérifier chez 1xBet)."""
    b = load()
    real = b.get("real")
    if played_only and not real:
        return b
    for c in items:
        if c.get("counted") or c.get("result") not in DONE:
            continue
        if real:
            c["counted"] = True
            if c.get("played"):
                b["bankroll"] += played_gain(c)
        elif c["result"] in ("win", "loss"):
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
    lines = [f"💼 Bankroll : {b['bankroll']:.2f} unités ({pnl:+.2f} depuis le début)"
             + (" — paris notés avec /joue seulement" if b.get("real") else "")]
    s = streak(combos)
    if s >= C.STREAK_WARNING:
        lines.append(f"⚠️ {s} combinés perdus d'affilée : lève le pied, réduis les mises ou fais une pause.")
    return "\n".join(lines)
