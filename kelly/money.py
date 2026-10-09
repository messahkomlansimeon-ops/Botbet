"""Budget du jour : un montant fixé une fois (« budget 5000 »), modifiable quand tu veux, réparti chaque matin entre le pari du jour,
les combinés et les combinés à vérifier chez 1xBet, selon la confiance de Kelly. Il remplace la bankroll et la saisie des paris :
Kelly suppose que tu joues ce qu'elle propose, à la mise affichée ; « ❌ Pas joué » retire un pari du bilan, « ✏️ Autre cote »
corrige la cote obtenue.

Répartition (allocate) : chaque pari a une part maximale du budget selon son palier (SHARE), multipliée par sa confiance =
espérance (proba x cote - 1) rapportée à CONF_FULL, bornée à [CONF_MIN, 1]. Journée riche (la somme dépasse le budget) : tout est
réduit dans la même proportion, le budget est entièrement misé. Journée pauvre : Kelly mise moins et garde le reste. Mises arrondies
(round_step) ; une mise plus petite que l'arrondi est abandonnée.

data/mybudget.json : {"daily": montant, "cur": "FCFA"}. Les paris misés sur le budget portent « cur » (devise) : seuls eux comptent
dans le bilan en argent (gains).
"""
import math
from datetime import date, timedelta

from . import fmt, store

FILE = "mybudget.json"
CUR = "FCFA"
SHARE = {"pari": 0.25, "🛡️": 0.30, "⚖️": 0.18, "🎯": 0.15, "🔥": 0.08, "💀": 0.04}   # part max du budget par palier
FAIR_SHARE = ((8, 0.12), (20, 0.08), (math.inf, 0.05))                            # combinés à vérifier, selon la cote mini
CONF_FULL, CONF_MIN = 0.10, 0.3       # espérance de +10 % ou plus : part entière ; jamais moins de 30 % de la part


def load():
    d = store.load(FILE, {})
    return d if isinstance(d, dict) else {}


def active():
    return bool(load().get("daily"))


def daily():
    return load().get("daily") or 0


def cur():
    return load().get("cur") or CUR


def set_daily(amount, currency=None):
    store.save(FILE, {"daily": round(float(amount), 2), "cur": currency or cur()})


def round_step(budget):
    """Arrondi des mises : 50 pour un budget de 5 000, 500 pour 50 000, 0,5 pour 10."""
    return 10 ** max(0, len(str(int(budget))) - 2) / 2 if budget >= 1 else 0.01


def money(x, currency=None):
    """« 1 500 FCFA »."""
    txt = f"{x:,.0f}".replace(",", " ") if float(x).is_integer() or abs(x) >= 100 else fmt.trim(x, 2)
    return f"{txt} {currency or cur()}"


def signed(x, currency=None):
    return ("+" if x >= 0 else "−") + money(abs(x), currency)


def _share(kind, tier):
    if kind == "fair":
        return next(s for limit, s in FAIR_SHARE if tier < limit)
    return SHARE.get("pari" if kind == "pari" else next((k for k in SHARE if str(tier).startswith(k)), "🎯"), 0.15)


def _confidence(prob, odds):
    return max(CONF_MIN, min(1.0, (prob * odds - 1) / CONF_FULL))


def allocate(items, budget=None):
    """items = [{kind: pari|combo|fair, tier: titre du palier (cote mini visée pour fair), prob, odds}] -> (mises, misé, gardé), dans
    l'ordre des items ; une mise de 0 = pari laissé de côté (plus petit que l'arrondi)."""
    budget = daily() if budget is None else budget
    if not budget or not items:
        return [0] * len(items), 0, budget
    raw = [_share(it["kind"], it["tier"]) * _confidence(it["prob"], it["odds"]) for it in items]
    scale = min(1.0, 1.0 / sum(raw)) if sum(raw) else 0
    step = round_step(budget)
    stakes = [math.floor(budget * r * scale / step) * step for r in raw]
    spent = round(sum(stakes), 2)
    return stakes, spent, round(budget - spent, 2)


def budget_line(spent, kept):
    """« 💰 Budget du jour : 5 000 FCFA · 3 800 misés, 1 200 gardés (journée pauvre) »."""
    head = f"💰 Budget du jour : {fmt.b(money(daily()))}"
    if not spent:
        return head + " · rien de misé aujourd'hui"
    return head + (f" · {money(spent)} misés, {money(kept)} gardés " + fmt.i("(journée pauvre)") if kept >= round_step(daily())
                   else " · tout est misé")


# ---------- commande « budget » ----------

QUICK = (2000, 5000, 10000, 20000)


def command(args):
    """« budget » : le budget actuel et des boutons pour le changer ; « budget 7500 » (ou « budget 50 € ») : le fixe ;
    « budget 0 » : revient aux mises en unités sur la bankroll."""
    from . import telegram
    if not args:
        txt = (f"💰 Ton budget du jour : {money(daily())}.\nKelly le répartit chaque matin entre ses paris, selon sa confiance."
               if active() else "💰 Pas encore de budget du jour : Kelly donne ses mises en unités.")
        return telegram.with_buttons(txt + "\nChoisis un montant, ou écris par exemple : budget 7500",
                                     telegram.inline([[(f"{q:,}".replace(",", " "), f"budget|{q}") for q in QUICK]]))
    try:
        amount = float(args[0].replace(",", ".").replace(" ", ""))
    except ValueError:
        return "Montant invalide. Exemple : budget 5000"
    if amount <= 0:
        store.save(FILE, {})
        return "Budget du jour supprimé : Kelly redonne ses mises en unités sur ta bankroll."
    set_daily(amount, args[1] if len(args) > 1 else None)
    return (f"✅ Budget du jour : {money(amount)}. Les mises de demain matin seront calculées dessus "
            "(tu peux le changer quand tu veux).")


# ---------- « ❌ Pas joué » ----------

def skip(what, day, undo=False):
    """Retire (ou remet, `undo`) un pari du jour du bilan en argent. -> réponse."""
    from . import played
    t = played.find(what, day)
    if t is None or isinstance(t, str):
        return t or "Ce pari n'existe plus."
    if undo:
        t.item.pop("skipped", None)
    else:
        t.item["skipped"] = True
    t.save()
    return f"↩️ {t.label} : compté comme joué." if undo else f"❌ {t.label} : retiré de ton bilan (pas joué)."


# ---------- bilan en argent ----------

def _won_odds(legs, key="min_odds"):
    won = [leg[key] for leg in legs if leg.get("result") == "win"]
    return math.prod(won) if won else 1.0


def gain(x):
    """Gain (devise) d'un pari misé sur le budget et réglé (gagné / perdu) ; None s'il ne compte pas (pas sur le budget, pas joué,
    pas réglé, non vérifiable)."""
    if not x.get("cur") or x.get("skipped") or x.get("result") not in ("win", "loss"):
        return None
    if x.get("played"):
        from .bankroll import played_gain
        return played_gain(x)
    if x["result"] == "loss":
        return -x["stake"]
    if "legs" in x:                                   # combiné à vérifier : compté à la cote mini (1xBet a payé au moins ça)
        return x["stake"] * (_won_odds(x["legs"]) - 1)
    return x["stake"] * (x.get("odds_final", x["odds"]) - 1)


def items(bets, rows, fair_rows):
    from .played import single_rows
    return list(bets.get("combos", [])) + single_rows(rows, bets.get("combos", [])) + list(fair_rows)


def result(bets, rows, fair_rows, days=None, today=None):
    """(nombre de paris comptés, gain total, total misé) des paris misés sur le budget, sur `days` jours (tout sinon)."""
    cut = str((date.fromisoformat(str(today)[:10]) if today else date.today()) - timedelta(days=days)) if days else ""
    n = total = staked = 0
    for x in items(bets, rows, fair_rows):
        g = gain(x)
        if g is None or (x.get("date") or "") < cut:
            continue
        n, total, staked = n + 1, total + g, staked + (x.get("played") or {}).get("stake", x["stake"])
    return n, total, staked


def summary_lines(bets, rows, fair_rows, today=None):
    """Lignes du bilan en argent (texte brut) : 7 derniers jours et depuis le début. [] si rien de réglé sur le budget."""
    out = []
    for days, label in ((7, "7 derniers jours"), (None, "depuis le début")):
        n, total, staked = result(bets, rows, fair_rows, days, today)
        if n:
            roi = f", {round(total / staked * 100):+d} % de ce qui a été misé" if staked else ""
            out.append(f"{label} : {signed(total)} sur {n} pari{'s' if n > 1 else ''}{roi}")
    return out
