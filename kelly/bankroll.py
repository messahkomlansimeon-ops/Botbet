"""Bankroll en unités (interne) : Kelly fractionnaire, plafonds par palier, série noire. Kelly ne l'AFFICHE plus : les mises conseillées et
les résultats sont en FCFA (kelly/money.py, budget du jour) et seuls les paris notés comptent ; ce module garde le calcul des gains d'un
pari noté (played_gain), la série de combinés perdus et l'ancien fichier bankroll.json (jamais effacé). Un pari qui porte une devise
(« cur ») n'entre jamais dans la bankroll en unités."""
import math

from . import config as C, store

DONE = ("win", "loss", "push", "void")       # « unknown » (non vérifiable) n'en fait pas partie : jamais compté, ni en mode réel


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

def real_legs(x):
    """Combiné de fair_combos.json bâti aux cotes 1xBet relevées (toutes les jambes ont leur cote) : son total (« min_odds ») est le
    produit de ces cotes, pas celui des cotes mini de Kelly."""
    return bool(x.get("legs")) and all(leg.get("xodds") for leg in x["legs"])


def won_odds(x):
    """Cotes des jambes gagnées d'un combiné de fair_combos.json : la cote 1xBet de chacune pour un combiné réel (real_legs), sinon sa
    cote mini (le total « min_odds » est alors le produit des cotes mini)."""
    real = real_legs(x)
    return [(leg.get("xodds") or leg.get("odds")) if real else leg["min_odds"] for leg in x["legs"] if leg.get("result") == "win"]


def played_gain(x):
    """Gain réel (FCFA : la mise notée) d'un pari noté et réglé. Jambes annulées : la cote 1xBet baisse dans la même proportion que la
    cote de référence (cote typique ; cote 1xBet des jambes gagnées pour un combiné aux cotes 1xBet, cote mini pour un combiné à
    vérifier)."""
    p = x["played"]
    if x["result"] == "loss":
        return -p["stake"]
    if x["result"] != "win":                        # push, void, unknown : mise rendue ou inconnue, rien ne bouge
        return 0.0
    if "legs" in x:                                 # combiné à vérifier chez 1xBet (data/fair_combos.json)
        won = won_odds(x)
        if not won:                                 # aucune jambe gagnée (anciennes lignes « win » à tort) : rien à payer
            return 0.0
        ratio = math.prod(won) / x["min_odds"]
    else:
        ratio = x.get("odds_final", x["odds"]) / x["odds"]
    return p["stake"] * (p["odds"] * ratio - 1)

def apply_results(items, played_only=False):
    """Met à jour l'ancienne bankroll en unités (interne, jamais affichée) avec les paris réglés non encore comptés (marqués « counted »). Un
    pari qui porte une devise (« cur » : mise conseillée en FCFA ou pari noté) est ignoré et marqué « counted » : l'argent n'entre jamais dans
    la bankroll en unités. Mode réel : seuls les paris notés comptent (played_gain). Sinon : les combinés gagnés ou perdus, à la mise des
    anciennes lignes. Un résultat « unknown » (non vérifiable) ne change rien. `played_only` : éléments qui ne comptent qu'en mode réel
    (pari du jour, combinés à vérifier chez 1xBet)."""
    for c in items:
        if c.get("cur"):                            # misé sur le budget du jour (devise) : jamais dans la bankroll en unités
            c["counted"] = True
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
            st = c.get("stake") or 0                  # plus de mise en unités depuis le budget en FCFA : 0, la bankroll en unités ne bouge plus
            b["bankroll"] += st * (c.get("odds_final", c.get("xodds") or c["odds"]) - 1) if c["result"] == "win" else -st
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

def streak_line(combos):
    """Alerte après une série de combinés perdus d'affilée (C.STREAK_WARNING) ; '' sinon."""
    s = streak(combos)
    return f"⚠️ {s} combinés perdus d'affilée : lève le pied, réduis les mises ou fais une pause." if s >= C.STREAK_WARNING else ""
