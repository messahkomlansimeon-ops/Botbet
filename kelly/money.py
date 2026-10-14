"""Budget du jour : le montant que tu es prêt à miser AUJOURD'HUI (« budget 5000 »), modifiable n'importe quel jour (il reste tel quel
jusqu'à ce que tu le changes). Kelly le répartit chaque matin entre le pari du jour, les combinés et les combinés à vérifier chez 1xBet,
selon sa confiance : ce sont des MISES CONSEILLÉES en FCFA. Seuls les paris que tu notes (« ✅ Je l'ai joué », « ✅ Je le joue », /joue :
x["played"] = {odds, stake, at}) comptent dans tes résultats, au bilan et pour les alertes « tu as joué » ; « ✏️ Autre cote » corrige la
cote obtenue et « ↩️ Annuler » retire la note. Sans budget, Kelly ne donne aucune mise conseillée (jamais d'unités).

Répartition (allocate) : chaque pari a une part maximale du budget selon son palier (SHARE), multipliée par sa confiance =
espérance (proba x cote - 1) rapportée à CONF_FULL, bornée à [CONF_MIN, 1] ; espérance nulle ou négative : AUCUNE mise (Kelly ne conseille
jamais de miser sur un pari perdant en moyenne, le plancher CONF_MIN ne vaut que pour une espérance positive). Journée riche (la somme dépasse le budget) : tout est
réduit dans la même proportion, le budget est entièrement misé. Journée pauvre : Kelly mise moins et garde le reste. Mises arrondies
(round_step) ; une mise plus petite que l'arrondi est abandonnée.

Capital (« capital 50000 », mode recommandé, à la place du budget du jour) : la somme confiée à Kelly. Il suit tes résultats (capital de départ +
gains et pertes des paris notés et réglés depuis qu'il a été fixé). Chaque jour, Kelly répartit au plus la limite du jour : la plus petite de ta perte
maximale (« perte max 5000 ») et de CAPITAL_DAY_SHARE (3 %) du capital, moins ce que tu as déjà misé (noté) aujourd'hui ; un ticket ne prend jamais plus de
TICKET_SHARE (1 %) du capital, et un ticket « loterie » (is_lottery : cote de 50 et plus, ou trop peu de chances) LOTTERY_STAKE (100 FCFA) au plus. Les mises
baissent donc après des pertes, jamais l'inverse, et une fois la limite atteinte Kelly ne propose plus de mise ce jour-là. Pourquoi si peu (audit du 13/10) :
aucun avantage n'est encore prouvé face à 1xBet ; à 10 % du capital par jour, quelques points de trop sur les chances de chaque pari suffisent à perdre la
moitié du capital en quelques mois. Objectif du mois (« objectif 18000 ») : affiché avec ce qui est déjà fait ce mois-ci, jamais utilisé pour grossir une
mise.

data/mybudget.json : {"daily": montant, "cur": "FCFA"} ou {"capital": montant, "since": "AAAA-MM-JJ", "max_day": montant, "goal": montant, "cur": "FCFA"}. « cur » sur la ligne d'un pari = devise de sa mise (stake) : la mise conseillée du
matin, ou celle de la note. Un pari ne compte dans l'argent (gain, bilan) que s'il est NOTÉ (x["played"]) et pas retiré.
"""
import math
import re
import time
from datetime import date, datetime, timedelta, timezone

from . import config as C, fmt, store

FILE = "mybudget.json"
CUR = "FCFA"
MAX_AMOUNT = 1e9                      # au-delà : montant absurde (refusé)
CUR_RE = re.compile(r"(?i)(?!(?:inf|infinity|nan)$)[^\W\d_]{1,8}|[€$£¥]")                     # un mot d'unité après un montant : « FCFA », « k », « € »
FCFA_WORDS = {"fcfa", "cfa", "xof", "f", "fr", "frs", "franc", "francs"}                   # les écritures du FCFA ; « k » = fois mille ; tout autre mot est refusé
SHARE = {"pari": 0.25, "🛡️": 0.30, "⚖️": 0.18, "🎯": 0.15, "🔥": 0.08, "💀": 0.04}   # part max du budget par palier
FAIR_SHARE = ((8, 0.12), (20, 0.08), (math.inf, 0.05))                            # combinés à vérifier, selon la cote mini
CONF_FULL, CONF_MIN = 0.10, 0.3       # espérance de +10 % ou plus : part entière ; jamais moins de 30 % de la part
CAPITAL_DAY_SHARE = 0.03              # mode capital : au plus 3 % du capital misé par jour (et jamais plus que la perte maximale choisie)
TICKET_SHARE = 0.01                   # mode capital : au plus 1 % du capital sur un même ticket
TICKET_WARN_SHARE = 0.05              # une mise NOTÉE au-delà de 5 % du capital est signalée (erreur de frappe ?), jamais refusée
LOTTERY_P = 0.05                      # ticket « loterie » : moins de 5 % de chances selon Kelly...
LOTTERY_ODDS = 50                     # ... ou une cote de 50 et plus...
LOTTERY_MKT_P = 0.02                  # ... ou moins de 2 % de chances selon 1xBet (sans sa marge)
LOTTERY_STAKE = 100                   # sa mise conseillée : 100 FCFA au plus, quel que soit le budget
TICKETS_DAY = 3                       # au-delà de 3 tickets notés dans la journée, la confirmation le signale (rien n'est bloqué)


def load():
    d = store.load(FILE, {})
    return d if isinstance(d, dict) else {}


def _amount(x):
    """Montant fini, entre 0 et MAX_AMOUNT ; 0 pour toute valeur illisible (jamais d'exception)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0
    return v if not isinstance(x, bool) and math.isfinite(v) and 0 < v <= MAX_AMOUNT else 0


def active():
    """Kelly donne des mises conseillées : capital fixé (même limite du jour atteinte : la mise dit alors pourquoi elle est nulle) ou budget du jour."""
    d = load()
    return bool(_amount(d.get("capital")) or _amount(d.get("daily")))


def capital_mode():
    return bool(_amount(load().get("capital")))


def daily():
    """Montant à répartir maintenant : en mode capital, ce qu'il reste de la limite du jour (capital_state) ; sinon le budget du jour."""
    d = load()
    if _amount(d.get("capital")):
        return capital_state()["left"]
    return _amount(d.get("daily"))


def _today():
    return datetime.now(timezone.utc).date()                 # heure d'Abidjan = UTC


def _docs():
    from . import fair, track
    return store.load("bets.json", {"picks": [], "combos": []}), track.load(), fair.load_combos()


_cap_memo = {}


def capital_state(today=None, docs=None):
    """{"capital": capital actuel, "base", "since", "limit": limite du jour, "staked": misé (noté) aujourd'hui, "left": reste à miser aujourd'hui,
    "max_day", "goal" (objectif du mois), "month": gain des paris notés de ce mois-ci, "yesterday": gain des paris notés d'hier (None s'il n'y en a pas)} ; {} hors mode capital. Gardé en mémoire tant que les
    paris et le réglage ne changent pas (relu au plus une fois par seconde)."""
    d = load()
    base = _amount(d.get("capital"))
    if not base:
        return {}
    today = today or _today()
    key = (str(today), tuple(store.version(n) for n in (FILE, "bets.json", "tracked.json", "fair_combos.json")))
    if docs is None and _cap_memo.get("key") == key and time.monotonic() - _cap_memo.get("at", 0) < 1.0:
        return _cap_memo["state"]
    bets, rows, fair_rows = docs or _docs()
    since, day, yday = str(d.get("since") or "")[:10], str(today), str(today - timedelta(days=1))
    gains = _counted(bets, rows, fair_rows)
    capital = max(0.0, base + sum(g for x, g in gains if str(x.get("date") or "")[:10] >= since))
    yest = [g for x, g in gains if str(x.get("date") or "")[:10] == yday]
    month = sum(g for x, g in gains if str(x.get("date") or "")[:10] >= since and str(x.get("date") or "")[:7] == day[:7])
    today_played = [x for x in items(bets, rows, fair_rows) if x.get("played") and not x.get("skipped") and str(x.get("date") or "")[:10] == day]
    staked = sum(_amount(x["played"].get("stake")) for x in today_played)
    max_day = _amount(d.get("max_day"))
    limit = min(max_day or math.inf, capital * CAPITAL_DAY_SHARE)
    step = round_step(limit) if limit >= 1 else 1
    limit = math.floor(limit / step) * step if limit >= 1 else 0
    state = {"capital": round(capital, 2), "base": base, "since": since, "limit": limit, "staked": staked, "left": max(0, limit - staked),
             "max_day": max_day, "goal": _amount(d.get("goal")), "month": month, "yesterday": sum(yest) if yest else None,
             "tickets": len(today_played)}
    if docs is None:
        _cap_memo.update(key=key, at=time.monotonic(), state=state)
    return state


def safe_cur(c):
    """Devise lisible (lettres ou symbole), « FCFA » sinon : une valeur étrangère ne casse ni l'affichage ni le HTML."""
    c = str(c or "").strip()
    return c if CUR_RE.fullmatch(c) else CUR


def cur():
    """La devise de Kelly : toujours le FCFA (verrouillé : « budget 5k » = 5 000 FCFA, jamais une devise « K » ; voir FCFA_WORDS)."""
    return CUR


def _save_capital(**kw):
    """Met à jour le réglage du mode capital (les autres champs restent)."""
    d = {k: v for k, v in load().items() if k in ("capital", "since", "max_day", "goal")}
    d.update(kw)
    store.save(FILE, {k: v for k, v in d.items() if v} | {"cur": CUR})
    _cap_memo.clear()


def set_daily(amount, currency=None):
    """Fixe le budget du jour en FCFA (`currency` est ignoré : tout est en FCFA)."""
    store.save(FILE, {"daily": round(float(amount), 2), "cur": CUR})


def round_step(budget):
    """Arrondi des mises : 50 pour un budget de 5 000, 500 pour 50 000, 0,5 pour 10."""
    budget = _amount(budget)
    return 10 ** max(0, len(str(int(budget))) - 2) / 2 if budget >= 1 else 0.01


def money(x, currency=None):
    """« 1 500 FCFA » ; un montant non entier garde ses décimales (« 1 500,5 FCFA »), toujours le même séparateur de milliers."""
    if float(x).is_integer():
        txt = f"{x:,.0f}".replace(",", " ")
    else:
        txt = fmt.trim(x, 2)
        whole, _, dec = txt.partition(",")
        digits = re.sub(r"\D", "", whole)
        txt = whole[:len(whole) - len(digits)] + re.sub(r"\B(?=(\d{3})+(?!\d))", " ", digits) + ("," + dec if dec else "")
    return f"{txt} {safe_cur(currency) if currency else cur()}"


def signed(x, currency=None):
    return ("+" if x >= 0 else "−") + money(abs(x), currency)


def _share(kind, tier):
    if kind == "fair":
        try:
            return next((s for limit, s in FAIR_SHARE if tier < limit), FAIR_SHARE[-1][1])
        except TypeError:
            return FAIR_SHARE[-1][1]
    return SHARE.get("pari" if kind == "pari" else next((k for k in SHARE if str(tier).startswith(k)), "🎯"), 0.15)


def edge(prob, odds):
    """Espérance (proba x cote - 1) d'un pari ; 0 si illisible."""
    try:
        v = float(prob) * float(odds) - 1
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def has_edge(prob, odds):
    """Vrai si le pari a une espérance POSITIVE : sinon Kelly ne conseille aucune mise (allocate rend 0)."""
    return edge(prob, odds) > 0


def _confidence(prob, odds):
    """Confiance dans [CONF_MIN, 1] ; 0 (aucune mise) si l'espérance est nulle ou négative."""
    ev = edge(prob, odds)
    return max(CONF_MIN, min(1.0, ev / CONF_FULL)) if ev > 0 else 0.0


def _raw(it, with_frac=True):
    """Part du budget d'un pari (palier x confiance) ; 0 si ses données sont illisibles. `with_frac` faux : la part AVANT la fraction de Kelly de la voie (allocate s'en sert pour
    la somme des parts : un jour sur-souscrit ne gonfle pas les mises sharpgap parce qu'une mise stats a été réduite)."""
    try:
        if it.get("novalue"):
            return 0.0                              # combiné sans aucune jambe à valeur : hors Kelly, hors de la somme des parts (mise fixe, voir allocate)
        r = _share(it["kind"], it["tier"]) * _confidence(float(it["prob"]), float(it["odds"]))
        if with_frac and it.get("frac") is not None:
            r *= max(0.0, float(it["frac"])) / C.KELLY_FRACTION_REF       # fraction de Kelly de la voie, relative au calibrage historique (0,25 : sharpgap inchangé, stats 0,15 = 60 %)
    except (KeyError, TypeError, ValueError, AttributeError):
        return 0.0
    return r if math.isfinite(r) and r > 0 else 0.0


def allocate(items, budget=None, detail=False):
    """items = [{kind: pari|combo|fair, tier: titre du palier (cote mini visée pour fair), prob, odds, p_mkt (facultatif : chances selon 1xBet, pour
    reconnaître un ticket « loterie »)}] -> (mises, misé, gardé), dans l'ordre des items ; une mise de 0 = pari laissé de côté (plus petit que l'arrondi). Ne lève jamais (valeur illisible : mise 0).
    `detail` : un 4e élément, « journée pauvre » = la somme des parts reste sous le budget (rien n'a été réduit), à ne pas confondre
    avec le reste d'un arrondi."""
    budget = _amount(daily() if budget is None else budget)
    items = list(items)
    if not budget or not items:
        out = ([0] * len(items), 0, budget)
        return out + (True,) if detail else out
    raw = [_raw(it) for it in items]
    total = sum(_raw(it, False) for it in items)     # somme des parts AVANT la fraction de la voie : réduire une mise stats n'augmente pas celles des autres
    scale = min(1.0, 1.0 / total) if total else 0
    step = round_step(budget)
    stakes = [math.floor(budget * r * scale / step) * step for r in raw]
    cap = ticket_cap()
    if cap is not None:                              # mode capital : jamais plus de TICKET_SHARE du capital sur un ticket (le reste est gardé)
        stakes = [min(st, cap) for st in stakes]
    stakes = [min(st, LOTTERY_STAKE) if _lottery(it) else st for st, it in zip(stakes, items)]     # ticket « loterie » : 100 FCFA au plus (gardé)
    room = budget - sum(stakes)                      # combinés SANS valeur : par défaut AUCUNE mise (suivi en fantôme) ; NOVALUE_STAKE > 0 : mise fixe optionnelle, APRÈS les autres, même plafond de ticket, prise sur le reste du jour
    for i, it in enumerate(items):
        if isinstance(it, dict) and it.get("novalue") and C.NOVALUE_STAKE > 0:
            fixed = max(C.NOVALUE_STAKE, step)       # jamais sous un pas d'arrondi
            st = min(fixed, cap if cap is not None else fixed, LOTTERY_STAKE if _lottery(it) else fixed, room)
            stakes[i] = math.floor(max(0.0, st) / step) * step
            room -= stakes[i]
    spent = round(sum(stakes), 2)
    out = (stakes, spent, round(budget - spent, 2))
    return out + (total < 1,) if detail else out


def is_lottery(prob=None, odds=None, p_mkt=None):
    """Ticket « loterie » : moins de LOTTERY_P de chances selon Kelly (`prob`), cote de LOTTERY_ODDS ou plus (`odds`), ou moins de LOTTERY_MKT_P de
    chances selon 1xBet sans sa marge (`p_mkt`, combos.market_prob) ; une valeur absente ou illisible ne compte pas."""
    def num(x):
        try:
            v = float(x)
        except (TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None
    p, o, m = num(prob), num(odds), num(p_mkt)
    return (p is not None and p < LOTTERY_P) or (o is not None and o >= LOTTERY_ODDS) or (m is not None and m < LOTTERY_MKT_P)


def _lottery(it):
    return isinstance(it, dict) and is_lottery(it.get("prob"), it.get("odds"), it.get("p_mkt"))


def ticket_cap():
    """Mode capital : mise maximale d'un ticket, TICKET_SHARE du capital arrondie au pas des mises du jour ; None hors mode capital."""
    st = capital_state()
    if not st:
        return None
    step = round_step(st["limit"]) if st["limit"] >= 1 else 1
    return math.floor(max(0.0, st["capital"]) * TICKET_SHARE / step) * step


def row_item(x):
    """Ligne de suivi d'un pari -> l'item de `allocate` (kind, tier, prob, odds) qui le décrit ; None si ses données sont illisibles.
    Combiné à vérifier / aux cotes 1xBet / sur mesure (« legs », fair_combos.json) : kind « fair » à sa cote ; combiné réel (« keys »,
    bets.json) : kind « combo » et son palier ; pari du jour (tracked.json) : kind « pari » à sa cote 1xBet quand il en a une."""
    try:
        if "legs" in x:
            from . import combos, lanes
            return {"kind": "fair", "tier": x["min_odds"], "prob": float(x["prob"]), "odds": float(x["min_odds"]), "p_mkt": combos.market_prob(x["legs"])} | lanes.item_fields(x["legs"])
        if "keys" in x:
            return ({"kind": "combo", "tier": x["tier"], "prob": float(x["prob"]), "odds": float(x["odds"])}
                    | ({"novalue": True} if x.get("has_value") is False and C.ENSEMBLE_ENABLED else {}) | ({"frac": float(x["frac"])} if x.get("frac") is not None else {}))
        return {"kind": "pari", "tier": "pari", "prob": float(x["prob"]), "odds": float(x.get("xodds") or x["odds"])}
    except (KeyError, TypeError, ValueError):
        return None


def over_limit(stake):
    """Mode capital : la mise `stake` (pas encore notée) dépasse-t-elle ce qu'il reste de la limite du jour ? -> ce qu'il reste (FCFA), sinon None.
    Une mise conseillée est calculée à l'envoi du message ; d'autres tickets notés depuis ont pu consommer la limite."""
    if not capital_mode():
        return None
    _cap_memo.clear()
    left = capital_state().get("left")
    return left if left is not None and stake > left + 0.5 else None


def suggested(x):
    """Mise conseillée (devise du budget) d'un pari : celle décidée le matin (ligne avec sa devise « cur » et une mise > 0) ; 0 si le matin la
    jugeait trop petite pour le budget (« stake » = 0) ; sinon (pas de mise du matin : budget fixé après coup, ancienne ligne) calculée sur le
    budget d'aujourd'hui (allocate, ce pari seul). 0 sans budget du jour : Kelly ne donne alors AUCUNE mise conseillée (jamais d'unités).
    Ne lève jamais."""
    if not active():
        return 0
    st = x.get("stake")
    if x.get("cur") and _amount(st) > 0:
        return st
    if st == 0 and not isinstance(st, bool):
        return 0
    item = row_item(x)
    return allocate([item])[0][0] if item else 0


NO_EDGE = fmt.NO_EDGE                      # à la place d'une mise, quand l'espérance est négative : Kelly ne conseille pas de miser
NO_BUDGET = fmt.NO_BUDGET                  # à la place d'une mise, quand il n'y a pas de budget du jour


def no_stake_txt(prob=None, odds=None):
    """Mise 0 en mots : « Kelly ne conseille pas de miser dessus (espérance négative) » si le pari n'a pas d'espérance positive (`prob` et `odds`
    connus) ; en mode capital, limite du jour atteinte ou capital épuisé ; sinon « pas de mise (trop petite pour ton budget) » (la part est plus
    petite que l'arrondi)."""
    if prob is not None and odds is not None and not has_edge(prob, odds):
        return NO_EDGE
    st = capital_state()
    if st and st["capital"] <= 0:
        return "pas de mise : capital épuisé (écris « capital 50000 » pour repartir)"
    if st and st["left"] < round_step(st["limit"] or 1):
        return f"pas de mise : limite du jour atteinte ({money(st['staked'])} misés aujourd'hui)"
    return "pas de mise (trop petite pour ton budget)"


NOVALUE_TXT = "👻 sans valeur : suivi en fantôme, pas de mise"


def stake_words(st, prob=None, odds=None, suffix="", p_mkt=None, novalue=False):
    """Mise `st` (allocate) en mots ; sans budget ni capital (None) : NO_EDGE si le pari perd en moyenne (`prob`, `odds`), None sinon. 0 : voir
    no_stake_txt. `suffix` : « (sur ton budget du jour) » après un montant (pas en mode capital, où la ligne dit ce que rapporte la mise). `p_mkt` :
    chances selon 1xBet (ticket « loterie », is_lottery)."""
    if st is None:
        return NO_EDGE if prob is not None and odds is not None and not has_edge(prob, odds) else None
    if not st:
        return NOVALUE_TXT if novalue else no_stake_txt(prob, odds)
    tag = " · sans valeur" if novalue else ""            # mise fixe optionnelle d'un combiné sans valeur : toujours dite
    if capital_mode():
        lot = " · ticket loterie" if is_lottery(prob, odds, p_mkt) else ""
        return money(st) + tag + lot + (f" → si ça passe : {signed(round(st * (odds - 1)))}" if odds and odds > 1 else "")
    return money(st) + tag + suffix


def _r(x):
    """Montant du mode capital arrondi au franc (le capital suit des gains à centimes : « −5 368,03 » s'affiche « −5 368 »)."""
    return round(x)


def _n(x):
    """« 5 000 » : montant arrondi, sans la devise."""
    return money(_r(x)).removesuffix(" " + cur())


def capital_line(html=True):
    """Une ligne : « 💼 Capital 50 000 FCFA · reste aujourd'hui 200 / 1 500 FCFA » (HTML si `html`) ;
    '' hors mode capital."""
    st = capital_state()
    if not st:
        return ""
    b = fmt.b if html else (lambda x: x)
    e = fmt.esc if html else (lambda x: x)
    return f"💼 Capital {b(e(money(_r(st['capital']))))} · reste aujourd'hui {e(_n(st['left']))} / {e(money(_r(st['limit'])))}"


def _bar(part, cells=10):
    """« ▰▰▰▰▰▰▰▰▰▱ 96 % » : part (0 à 1) de la limite du jour déjà misée."""
    return fmt.bar(part, cells) + f" {round(max(0.0, min(1.0, part)) * 100)} %"


def capital_card(st=None):
    """« 💼 Mon capital » (HTML) : le capital et son départ ; la journée (limite, déjà misé, reste, barre) ; l'objectif et la veille ; une ligne
    d'explication. Montants arrondis au franc, date en français."""
    from .telegram import Html
    st = st or capital_state()
    e, b, i = fmt.esc, fmt.b, fmt.i
    since = fmt.fr_day(st["since"]).split(" ", 1)[1] if st.get("since") else ""
    lines = [b("💼 Mon capital"), "", f"💰 Capital : {b(e(money(_r(st['capital']))))}",
             f"📅 Depuis le {e(since)} (départ {e(money(_r(st['base'])))})" if since else f"📅 Départ : {e(money(_r(st['base'])))}", ""]
    if st["capital"] <= 0:
        lines.append("⛔ " + b("Capital épuisé") + " : écris " + i("capital 50000") + " pour repartir.")
    else:
        lines += ["📆 " + b("Aujourd'hui"), f"Limite : {e(money(_r(st['limit'])))}", f"Déjà misé : {e(money(_r(st['staked'])))}"]
        step = round_step(st["limit"]) if st["limit"] >= 1 else 1
        left = st["left"] if st["left"] >= step else 0
        lines.append(f"Reste : {b(e(money(_r(left))))}" + (" · limite atteinte, plus de mise aujourd'hui" if not left else ""))
        if st["limit"]:
            lines.append(_bar(st["staked"] / st["limit"]))
        if not st["max_day"]:
            lines.append("💡 Fixe ta perte max : " + i("perte max 5000"))
    lines.append("")
    from . import lanestats                             # plus d'objectif en FCFA (décision du 14/10) : deux objectifs mesurés
    lines += [e(x) for x in lanestats.objective_lines()]
    if st["yesterday"] is not None:
        lines.append(f"📈 Hier : {e(signed(_r(st['yesterday'])))}")
    lines += ["", i(f"ℹ️ Limite = {round(CAPITAL_DAY_SHARE * 100)} % du capital (jamais plus que ta perte max), {round(TICKET_SHARE * 100)} % au plus par "
                    f"ticket, {money(LOTTERY_STAKE)} au plus sur un ticket loterie. Le capital suit les paris que tu notes.")]
    return Html("\n".join(lines))


def budget_line(spent, kept, poor=None):
    """« 💰 Budget du jour : 5 000 FCFA · 3 800 répartis, 1 200 gardés (journée pauvre) » (HTML). `poor` (allocate, detail) : Kelly conseille
    moins que le budget parce que ses paris sont peu sûrs ; sinon (journée riche, tout a été réparti) le reste n'est qu'un arrondi."""
    head = f"💰 {'À répartir aujourd’hui' if capital_mode() else 'Budget du jour'} : {fmt.b(fmt.esc(money(daily())))}"
    if not spent:
        return head + " · aucune mise conseillée aujourd'hui"
    if poor is None:
        poor = True
    return head + (f" · {fmt.esc(money(spent))} répartis, {fmt.esc(money(kept))} gardés " + fmt.i("(journée pauvre)")
                   if poor and kept >= round_step(daily()) else " · tout est réparti")


# ---------- lecture des montants ----------

_GROUP = re.compile(r"\d{3}(?:[.,]\d+)?")                  # « 000 », « 000,50 » : un groupe de milliers écrit après une espace


def split_tokens(args):
    """Mots d'une saisie : espaces insécables comprises, devise collée au montant séparée (« 5000FCFA », « 50€ »)."""
    txt = " ".join(map(str, args)).replace(" ", " ").replace(" ", " ")
    return re.sub(r"(?<=\d)(?=[^\W\d_]|[€$£¥])", " ", txt).split()


def _digits(s):
    """« 5000 », « 5,40 », « 5.000 », « 1.234.567 », « 1.234,56 » -> nombre ; None si le format n'est pas clair. Un séparateur suivi
    d'exactement 3 chiffres est un séparateur de milliers (« 5.000 », « 5,000 » = 5000), sinon c'est une virgule décimale."""
    if re.fullmatch(r"\d+", s):
        v = int(s)
    elif not re.fullmatch(r"\d+(?:[.,]\d+)+", s):
        return None
    else:
        seps = re.findall(r"[.,]", s)
        if len(set(seps)) == 1:
            sep = seps[0]
            if len(seps) > 1:                                  # 1.234.567
                if not re.fullmatch(rf"\d{{1,3}}(?:\{sep}\d{{3}})+", s):
                    return None
                v = int(s.replace(sep, ""))
            elif re.fullmatch(r"[1-9]\d{0,2}[.,]\d{3}", s):    # 5.000 / 5,000
                v = int(s.replace(sep, ""))
            else:
                v = float(s.replace(",", "."))
        else:                                                  # 1.234,56 / 1,234.56 : le dernier séparateur est la virgule
            dec = seps[-1]
            th = "," if dec == "." else "."
            if not re.fullmatch(rf"\d{{1,3}}(?:\{th}\d{{3}})+\{dec}\d+", s):
                return None
            v = float(s.replace(th, "").replace(",", "."))
    return v if math.isfinite(v) and v <= MAX_AMOUNT else None


def take_amount(tokens):
    """Montant écrit au début de `tokens` -> (valeur | None, nombre de mots lus). Les groupes de milliers séparés par des espaces sont
    réunis (« 5 », « 000 » = 5000) ; un décimal (« 5,40 ») ne se réunit pas au mot suivant. Jamais négatif, infini ni NaN."""
    if not tokens or not re.fullmatch(r"\d[\d.,]*", tokens[0]):
        return None, 0
    txt, n = tokens[0], 1
    while n < len(tokens) and _GROUP.fullmatch(tokens[n]) and re.fullmatch(r"\d{1,3}(?: \d{3})*", txt):
        txt += " " + tokens[n]
        n += 1
    v = _digits(txt.replace(" ", ""))
    return (v, n) if v is not None else (None, 0)


def strip_unit(tokens):
    """Retire l'unité écrite après un montant -> (mots sans l'unité, multiplicateur, mot étranger | None) : « k » vaut ×1000 (« 5k » = 5 000,
    « 2k » = 2 000), les écritures du FCFA (FCFA_WORDS) sont ignorées, tout autre mot (« € », « euros »...) est rendu en 3e position : Kelly
    ne compte qu'en FCFA. Un montant seul (un seul mot) n'a pas d'unité."""
    if len(tokens) > 1 and CUR_RE.fullmatch(tokens[-1]):
        word = tokens[-1].casefold()
        if word == "k":
            return tokens[:-1], 1000, None
        if word in FCFA_WORDS:
            return tokens[:-1], 1, None
        return tokens[:-1], 1, tokens[-1]
    return tokens, 1, None


def parse_amount(args):
    """« 5 000 », « 5.000 », « 5,5 », « 5k », « 50 € » -> (montant, devise | None) ; (None, None) si illisible ou absurde. L'unité est un mot (ou
    un symbole) après le montant : « k » multiplie par 1000 (pas une devise), « FCFA » est rendu en capitales, un autre mot est rendu tel quel
    (la commande « budget » le refuse : tout est en FCFA)."""
    tokens = split_tokens(args)
    v, n = take_amount(tokens)
    rest = tokens[n:]
    if v is None or len(rest) > 1 or (rest and not CUR_RE.fullmatch(rest[0])):
        return None, None
    if rest and rest[0].casefold() == "k":
        v *= 1000
        return (int(v) if float(v).is_integer() else v, None) if v <= MAX_AMOUNT else (None, None)
    return v, (rest[0].upper() if rest else None)


# ---------- commande « budget » ----------

QUICK = (2000, 5000, 10000, 20000)
EXPLAIN = ("C'est le montant que tu es prêt à miser AUJOURD'HUI : Kelly s'en sert pour calculer ses mises conseillées. Tu peux le changer "
           "n'importe quel jour, il reste tel quel jusqu'à ce que tu le changes. Seuls les paris que tu notes (« ✅ Je l'ai joué ») "
           "comptent dans tes résultats.")


def command(args):
    """« budget » : le budget actuel et des boutons pour le changer ; « budget 7500 » (ou « budget 7,5k ») : le fixe, en FCFA ;
    « budget 0 » : le supprime (plus de mise conseillée)."""
    from . import telegram
    if capital_mode():
        return ("💼 Tu utilises le mode capital : Kelly calcule elle-même ta limite du jour (écris « capital » pour la voir). Pour revenir au "
                "budget du jour, écris « capital 0 » puis « budget 5000 ».")
    if not args:
        txt = (f"💰 Ton budget du jour : {money(daily())}.\n{EXPLAIN}"
               if active() else f"💰 Pas encore de budget du jour : Kelly ne donne pas de mise conseillée.\n{EXPLAIN}")
        return telegram.with_buttons(txt + "\nChoisis un montant, ou écris par exemple : budget 7500",
                                     telegram.inline([[(f"{q:,}".replace(",", " "), f"budget|{q}") for q in QUICK]]))
    amount, currency = parse_amount(args)
    if amount is None:
        return "Montant invalide. Exemple : budget 5000"
    if currency and currency.casefold() not in FCFA_WORDS:
        return ("Kelly compte tout en FCFA : écris ton budget en FCFA, par exemple budget 5000 (ou budget 5k pour 5 000 FCFA). "
                f"La devise « {currency} » n'est pas gérée.")
    if amount <= 0:
        store.save(FILE, {})
        return "Budget du jour supprimé : Kelly ne donne plus de mise conseillée (tes paris notés restent comptés dans tes résultats)."
    set_daily(amount, currency)
    return (f"✅ Budget du jour : {money(amount)}. Kelly en tire ses mises conseillées (demain matin, et tout de suite pour les paris "
            "sans mise). Tu peux le changer quand tu veux ; seuls les paris que tu notes comptent dans tes résultats.")


# ---------- commandes « capital », « perte max », « objectif » ----------

CAPITAL_HELP = ("Écris « capital 50000 » (la somme que tu confies à Kelly, que tu acceptes de perdre), « perte max 5000 » (le plus que tu acceptes de "
                "perdre en un jour). Plus d'objectif en FCFA : « 💼 Mon capital » affiche deux objectifs mesurés.")


def _amount_arg(args, name):
    """Montant d'une commande en FCFA -> (montant, None) ou (None, message d'erreur)."""
    amount, currency = parse_amount(args)
    if amount is None:
        return None, f"Montant invalide. Exemple : {name} 5000"
    if currency and currency.casefold() not in FCFA_WORDS:
        return None, f"Kelly compte tout en FCFA : écris par exemple {name} 5000 (ou {name} 5k)."
    return amount, None


def capital_status():
    """La carte « 💼 Mon capital » (capital_card) ; sans capital, comment le fixer."""
    st = capital_state()
    if not st:
        return "💼 Pas encore de capital. " + CAPITAL_HELP
    return capital_card(st)


def _done(extra=""):
    """« ✅ » + la carte, après un changement (`extra` : phrase HTML ajoutée en dessous)."""
    from .telegram import Html
    return Html("✅ " + capital_status() + (("\n\n" + extra) if extra else ""))


def capital_command(args):
    """« capital » : l'état ; « capital 50000 » : nouveau capital de départ (compté à partir d'aujourd'hui) ; « capital 0 » : sortie du mode capital."""
    if not args:
        return capital_status()
    amount, err = _amount_arg(args, "capital")
    if err:
        return err
    if amount <= 0:
        store.save(FILE, {})
        _cap_memo.clear()
        return "💼 Mode capital arrêté : plus de mise conseillée. Écris « capital 50000 » pour le reprendre, ou « budget 5000 » pour un budget du jour."
    _save_capital(capital=amount, since=str(_today()))
    return _done()


def max_loss_command(args):
    """« perte max 5000 » (ou « limite 5000 ») : le plus que Kelly répartit en un jour ; « perte max 0 » : seulement la part du capital."""
    args = [a for a in args if played_norm(a) not in ("max", "maximum", "maxi", "du", "jour", "par")]
    if not args:
        return capital_status()
    amount, err = _amount_arg(args, "perte max")
    if err:
        return err
    if not capital_mode():
        return "💼 Fixe d'abord ton capital : « capital 50000 »."
    _save_capital(max_day=amount if amount > 0 else None)
    return _done()


def goal_command(args):
    """« objectif » : l'objectif en FCFA n'existe plus (décision du 14/10) ; la carte du capital affiche deux objectifs mesurés (CLV 1xBet positif sur au moins 300
    lignes de valeur réglées ; capital au moins égal au capital de départ). Ne change rien."""
    return _done(fmt.i("L'objectif en FCFA est remplacé par deux objectifs affichés dans « 💼 Mon capital » : un CLV contre le dernier prix 1xBet positif "
                       "(intervalle à 95 % au-dessus de zéro, 300 lignes réglées au moins) et un capital au moins égal à celui de départ."))


def played_norm(w):
    from .played import norm
    return norm(w)


# ---------- « ❌ Pas joué » ----------

def skip(what, day, undo=False):
    """Ancien bouton « ❌ Pas joué » (messages d'avant le mode « je note ce que je joue ») : retire (ou remet, `undo`) un pari du jour du bilan
    en argent. Plus aucun message neuf n'en propose ; le bouton d'un ancien message marche toujours. -> réponse."""
    from . import played
    t = played.find(what, day)
    if t is None or isinstance(t, str):
        return t or "Ce pari n'existe plus."
    if t.item.get("result") is not None:             # réglé : le retirer (ou le remettre) changerait un bilan déjà annoncé
        return f"Le {t.label} est déjà réglé : trop tard pour le {'remettre dans' if undo else 'retirer du'} bilan."
    if undo:
        t.item.pop("skipped", None)
    else:
        t.item["skipped"] = True
    t.save()
    return f"↩️ {t.label} : compté comme joué." if undo else f"❌ {t.label} : retiré de ton bilan (pas joué)."


# ---------- bilan en argent ----------

def gain(x):
    """Gain (devise) d'un pari NOTÉ (x["played"]) et réglé (gagné / perdu), à la cote et à la mise notées ; None s'il ne compte pas (pas
    noté, retiré par l'ancien « ❌ Pas joué », pas réglé, non vérifiable). Une mise conseillée seule (« cur » + « stake » sans note) ne
    compte jamais."""
    if not x.get("played") or x.get("skipped") or x.get("result") not in ("win", "loss"):
        return None
    from .bankroll import played_gain
    return played_gain(x)


def items(bets, rows, fair_rows):
    from .played import gap_rows, single_rows
    return list(bets.get("combos", [])) + single_rows(rows, bets.get("combos", [])) + gap_rows(rows) + list(fair_rows)


def _counted(bets, rows, fair_rows, days=None, today=None):
    """[(pari, gain)] des paris NOTÉS et réglés, sur `days` jours (tout sinon)."""
    cut = str((date.fromisoformat(str(today)[:10]) if today else date.today()) - timedelta(days=days)) if days else ""
    out = []
    for x in items(bets, rows, fair_rows):
        g = gain(x)
        if g is not None and (x.get("date") or "") >= cut:
            out.append((x, g))
    return out


def result(bets, rows, fair_rows, days=None, today=None):
    """(nombre de paris comptés, gain total, total misé) des paris notés et réglés, sur `days` jours (tout sinon)."""
    done = _counted(bets, rows, fair_rows, days, today)
    return len(done), sum(g for _, g in done), sum(x["played"]["stake"] for x, _ in done)


def summary_lines(bets, rows, fair_rows, today=None):
    """Lignes du bilan en argent (texte brut) : « 7 derniers jours : −11 608 FCFA · 2 gagnés sur 90 », la jauge de la mise récupérée de la
    première période affichée (« ▰▱▱▱▱▱▱▱▱▱ 12 % de la mise récupérée »), puis « Depuis le début : … » seulement s'il y a plus de paris que sur
    7 jours. Montants arrondis au franc. [] si aucun pari noté n'est réglé."""
    out = []
    week = _counted(bets, rows, fair_rows, 7, today)
    all_ = _counted(bets, rows, fair_rows, None, today)
    periods = [(week, "7 derniers jours")] + ([(all_, "Depuis le début")] if len(all_) > len(week) else [])
    for done, label in periods:
        if not done:
            continue
        n, total, staked = len(done), sum(g for _, g in done), sum(x["played"]["stake"] for x, _ in done)
        won = sum(1 for x, _ in done if x["result"] == "win")
        out.append(f"{label} : {signed(round(total))} · {won} gagné{'s' if won > 1 else ''} sur {n}")
        if staked and len(out) == 1:                # en perte : la part de la mise revenue (jauge) ; en gain : ce que la mise a rapporté
            back = max(0.0, (staked + total) / staked)
            out.append(f"{fmt.bar(back)} {round(back * 100)} % de la mise récupérée" if back < 1 else
                       f"📈 +{round((back - 1) * 100)} % sur ce qui a été misé")
    return out
