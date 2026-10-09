"""Paris réellement joués chez 1xBet (/joue) : la cote obtenue et la mise. Le bilan et la bankroll suivent alors ce que tu
joues vraiment, et Kelly mesure l'écart entre la cote de 1xBet et sa cote de référence (cote typique du marché pour les paris
à cotes réelles, cote mini pour les combinés à vérifier).

/joue <quoi> <cote> [mise]   quoi : sur, costaud, special, mechant, monstre (combinés du jour), pari (pari du jour),
                              v5, v12, v25 (combinés à vérifier chez 1xBet, par cote mini visée). Cote 0 : annule la note.
Le pari noté est celui du jour, sinon le plus récent pas encore réglé. Stockage dans le pari lui-même : x["played"] =
{odds, stake, at} (combiné de bets.json, ligne envoyée de tracked.json, combiné de fair_combos.json). Le premier /joue active
le mode réel de la bankroll (bankroll.py).
"""
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from . import bankroll, combos as CB, config as C, fair, fmt, money as M, store, telegram, track

MAX_ODDS = 10000.0


def norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return s.lower().strip()


def tier_words():
    """« sur » -> « 🛡️ SÛR (cote 5 à 7) »..."""
    return {norm(fmt.tier_word(t)): t for t, _, _ in C.COMBOS}


def _num(s):
    """Une cote : « 5,40 » ou « 5.40 » (ni signe, ni exposant, ni inf / nan) ; None sinon."""
    if not re.fullmatch(r"\d+(?:[.,]\d+)?", str(s).strip()):
        return None
    x = float(str(s).strip().replace(",", "."))
    return x if math.isfinite(x) else None


def parse_odds_stake(args, strict=False):
    """« 5,40 », « 5,40 2 000 », « 5.40 2.000 FCFA » -> (cote, mise | None, erreur | None). La cote est le premier mot ; la mise lit les
    milliers (voir money.take_amount). `strict` (message libre sans commande) : « 2 000 » ou « 2 500 » seuls peuvent être une cote
    entière et une mise, ou une mise sans cote : erreur « ambigu », jamais de devinette."""
    tokens = M.split_tokens(args)
    if tokens and M.CUR_RE.fullmatch(tokens[-1]) and len(tokens) > 1:       # devise en fin de message : ignorée
        tokens = tokens[:-1]
    odds = _num(tokens[0]) if tokens else None
    if odds is None:
        return None, None, "invalide"
    rest = tokens[1:]
    if strict and len(tokens) == 2 and re.fullmatch(r"\d{1,3}", tokens[0]) and re.fullmatch(r"\d{3}(?:[.,]\d+)?", rest[0]):
        return odds, None, "ambigu"
    if not rest:
        return odds, None, None
    stake, n = M.take_amount(rest)
    if stake is None or n != len(rest):
        return odds, None, "invalide"
    return odds, stake, None


def unfunded(t):
    """Pari sans mise sur le budget du jour (trop petite pour l'arrondi) : ni « Autre cote » ni note."""
    return not t.item.get("cur") and t.stake == 0


def _pick(items, day):
    """Le pari du jour `day`, sinon le plus récent pas encore réglé."""
    items = sorted(items, key=lambda x: x["date"], reverse=True)
    return next((x for x in items if x["date"] == day), None) or next((x for x in items if x.get("result") is None), None)


def single_rows(rows, combos):
    """Paris du jour envoyés seuls (le pari du jour) : lignes envoyées de tracked.json marquées r["single"] (track.record : le
    pari du jour peut aussi être une jambe d'un combiné). Anciennes lignes sans marque : celles qui ne sont dans aucun combiné
    de leur date."""
    in_combo = {(c["date"], k) for c in combos for k in c["keys"]}
    return [r for r in rows if r.get("sent")
            and (r["single"] if "single" in r else (r.get("date"), r["key"]) not in in_combo)]


class Target:
    """Ce que désigne un mot de /joue : le pari, sa cote de référence, sa mise conseillée, et comment le sauver."""

    def __init__(self, item, ref, stake, label, save, ref_word):
        self.item, self.ref, self.stake, self.label, self.save, self.ref_word = item, ref, stake, label, save, ref_word


def find(word, day):
    """Target du mot `word` pour le jour `day` (AAAA-MM-JJ), ou un message d'erreur (str)."""
    w = norm(word)
    tiers = tier_words()
    if w in tiers:
        bets = store.load("bets.json", {"picks": [], "combos": []})
        c = _pick([c for c in bets["combos"] if c["tier"] == tiers[w]], day)
        if not c:
            return f"Pas de combiné {fmt.tier_word(tiers[w])} à noter (ni aujourd'hui ni en attente de résultat)."
        return Target(c, c["odds"], c["stake"], f"{fmt.tier_word(c['tier'])} du {fmt.fr_day(c['date'])}",
                      lambda: store.save("bets.json", bets), "cote typique")
    if w in ("pari", "jour", "single"):
        rows = track.load()
        r = _pick(single_rows(rows, store.load("bets.json", {"combos": []}).get("combos", [])), day)
        if not r:
            return "Pas de pari du jour à noter (ni aujourd'hui ni en attente de résultat)."
        st = r["stake"] if r.get("cur") or r.get("stake") == 0 else bankroll.stake(r["prob"], r["odds"], "⭐", bankroll.load()["bankroll"])
        return Target(r, r["odds"], st, f"pari du jour du {fmt.fr_day(r['date'])} ({r['pick']})", lambda: track.save(rows),
                      "cote typique")
    if w.startswith("v") and len(w) > 1:              # v5, v12, v25 ; vtc5 / vtv5 : combinés « spécial tirs »
        rows = fair.load_combos()
        c = _pick([c for c in rows if str(c["tier"]) == w[1:]], day)
        if not c:
            return None if not (w[1:].isdigit() or CB.shots_title(w[1:])) else \
                f"Pas de combiné à vérifier « {_fair_name(w[1:])} » à noter (ni aujourd'hui ni en attente de résultat)."
        return Target(c, c["min_odds"], c["stake"] if c.get("cur") or c.get("stake") == 0 else CB.fair_stake(c["min_odds"]), f"combiné à vérifier (cote mini {fmt.odds_txt(c['min_odds'])}) "
                      f"du {fmt.fr_day(c['date'])}", lambda: store.save(fair.COMBOS_FILE, rows), "cote mini")
    return None


def _fair_name(tier):
    """« Tirs cadrés », « Tirs vers le but » pour les combinés « spécial tirs », sinon « À vérifier »."""
    t = CB.shots_title(str(tier))
    return t.replace("🎯 COMBINÉ ", "").capitalize() if t else "À vérifier"


def _gap(odds, ref):
    return odds / ref - 1


def command(args, day, now=None):
    """/joue <quoi> <cote> [mise] ; sans argument : ce qui est à noter aujourd'hui."""
    day = str(day)[:10]
    if not args:
        return todo(day)
    if len(args) < 2:
        return "Exemple : /joue sur 5,40 (combiné Sûr joué à 5,40 chez 1xBet). /joue seul : ce qui reste à noter."
    t = find(args[0], day)
    if t is None:
        return (f"« {args[0]} » inconnu. Choix : {', '.join(tier_words())}, pari (pari du jour), v5 / v12 / v25 (combinés à vérifier). "
                "Exemple : /joue sur 5,40")
    if isinstance(t, str):
        return t
    odds, given, err = parse_odds_stake(args[1:])
    if odds is None or err or (given is not None and given < 0):
        return "Cote ou mise invalide. Exemple : /joue sur 5,40 2 (cote 5,40, mise 2 u) ; /joue sur 5,40 2 000 (mise 2 000 FCFA)."
    stake = t.stake if given is None else given
    if t.item.get("result") is not None:
        return f"Le {t.label} est déjà réglé : trop tard pour le noter."
    if odds == 0:
        if not t.item.pop("played", None):
            return f"Le {t.label} n'était pas noté."
        t.save()
        return f"Note annulée : {t.label}."
    if not 1.0 < odds <= MAX_ODDS:
        return "La cote doit être supérieure à 1. Exemple : /joue sur 5,40"
    on_budget = bool(t.item.get("cur"))              # pari misé sur le budget du jour : pas de bankroll, la cote corrige le bilan
    if not on_budget and t.stake == 0 and (M.active() or given is None):
        return f"Le {t.label} n'a pas de mise sur ton budget du jour (trop petite) : rien à noter."
    if not stake > 0:
        return "La mise doit être supérieure à 0. Exemple : /joue sur 5,40 2 (annuler : /joue sur 0)."
    first = not on_budget and not bankroll.load().get("real")
    t.item["played"] = {"odds": round(odds, 2), "stake": round(stake, 2),
                        "at": (now or datetime.now(timezone.utc)).isoformat(timespec="minutes")}
    t.item.pop("skipped", None)
    t.save()
    if first:
        b = bankroll.load()
        b["real"] = True
        bankroll.save(b)
    gap = _gap(odds, t.ref)
    from . import money
    mise = money.money(stake, t.item["cur"]) if on_budget else fmt.units(stake)
    lines = [f"✅ Noté : {t.label}, joué à {fmt.odds_txt(odds)} chez 1xBet, mise {mise} "
             f"({t.ref_word} {fmt.odds_txt(t.ref)} : {fmt.signed(gap * 100, ' %')})."]
    if t.ref_word == "cote mini" and odds < t.ref:
        lines.append("⚠️ 1xBet paie moins que la cote mini : Kelly conseillait de passer ton chemin.")
    elif gap < -0.15:
        lines.append("⚠️ Cote bien plus basse que prévu : vérifie la cote saisie, ou les cotes de 1xBet ont bougé.")
    if first:
        lines.append("Ta bankroll ne suit plus que les paris notés avec /joue, à la cote et à la mise de 1xBet. "
                     "/bank <montant> pour la régler sur ta vraie bankroll.")
    return "\n".join(lines)


def todo(day):
    """/joue sans argument : les paris du jour, notés ou non, avec la commande à taper."""
    bets = store.load("bets.json", {"picks": [], "combos": []})
    lines, rows = [], []
    items_ = [(fmt.tier_word(c["tier"]), c, c["odds"], norm(fmt.tier_word(c["tier"]))) for c in bets["combos"] if c["date"] == day]
    items_ += [("Pari du jour", r, r["odds"], "pari") for r in single_rows(track.load(), bets["combos"]) if r["date"] == day]
    items_ += [(f"{_fair_name(c['tier'])} (cote mini {fmt.odds_txt(c['min_odds'])})", c, c["min_odds"], f"v{c['tier']}")
               for c in fair.load_combos() if c["date"] == day]
    if not items_:
        return "Rien à noter aujourd'hui : les paris du jour arrivent à 08:00."
    if any(x.get("cur") for _, x, _, _ in items_):
        return _budget_list(items_, day)
    for name, x, ref, what in items_:
        if not x.get("cur") and x.get("stake") == 0 and x.get("result") is None:     # budget du jour trop petit pour ce pari
            lines.append(f"▫️ {name} : pas de mise (trop petite pour ton budget)")
            continue
        lines.append(_todo_line(name, x, ref, f"/joue {what} <cote>"))
        if not x.get("played") and x.get("result") is None:
            rows.append([(f"✅ {name} — {fmt.odds_txt(ref)}", f"joue|{what}|{day}")])
    text = "\n".join(["🧾 Tes paris du jour", ""] + lines + ["", "Appuie sur le pari que tu as joué, puis envoie la cote obtenue chez 1xBet."
                                                         if rows else ("Rien à jouer avec ton budget d'aujourd'hui."
                                                                       if any("pas de mise" in x for x in lines) else "Tout est noté 👍")])
    return telegram.with_buttons(text, telegram.inline(rows)) if rows else text


def _budget_list(items_, day):
    """Paris du jour misés sur le budget : comptés comme joués sauf « ❌ Pas joué » ; un bouton par pari pour changer d'avis."""
    from . import money
    lines, rows = ["🧾 Tes paris du jour", "Comptés comme joués, à la mise affichée. Touche un pari que tu n'as pas joué.", ""], []
    for name, x, ref, what in items_:
        if not x.get("cur"):
            continue
        p = x.get("played") or {}
        odds = p.get("odds", ref)
        if x.get("skipped"):
            lines.append(f"❌ {name} : pas joué")
            rows.append([(f"↩️ {name} : finalement joué", f"unskip|{what}|{day}|l")])
        else:
            lines.append(f"✅ {name} : {money.money(p.get('stake', x['stake']), x['cur'])} à {fmt.odds_txt(odds)}")
            if x.get("result") is None:
                rows.append([(f"❌ {name} pas joué", f"skip|{what}|{day}|l"), ("✏️ Autre cote", f"joue|{what}|{day}")])
    text = "\n".join(lines)
    return telegram.with_buttons(text, telegram.inline(rows)) if rows else text


def _todo_line(name, x, ref, cmd):
    p = x.get("played")
    if p:
        return f"✅ {name} : joué à {fmt.odds_txt(p['odds'])}, mise {fmt.units(p['stake'])}"
    if x.get("result") is not None:
        return f"⏹ {name} (cote {fmt.odds_txt(ref)}) : déjà réglé, pas noté"
    return f"▫️ {name} (cote {fmt.odds_txt(ref)}) : pas noté — {cmd}"


# ---------- bilan ----------

def items(bets, rows, fair_rows, days=None, today=None):
    """Paris notés : [(genre, nom, élément, cote de référence)], genre = « réel » (cotes réelles) ou « vérif »."""
    out = [("réel", fmt.tier_word(c["tier"]), c, c["odds"]) for c in bets["combos"] if c.get("played")]
    out += [("réel", "Pari du jour", r, r["odds"]) for r in single_rows(rows, bets["combos"]) if r.get("played")]
    out += [("vérif", "À vérifier", c, c["min_odds"]) for c in fair_rows if c.get("played")]
    out = [x for x in out if not x[2].get("skipped")]             # « ❌ Pas joué » : sorti du bilan
    if days:
        cut = str((datetime.fromisoformat(str(today)) if today else datetime.now(timezone.utc)).date() - timedelta(days=days))
        out = [x for x in out if x[2]["date"] >= cut]
    return out


def _gain_txt(xs):
    """Résultat des paris joués et réglés : en unités pour ceux de la bankroll, en devise pour ceux misés sur le budget (jamais
    mélangés) : « +12 u », « −1 000 FCFA » ou « +12 u et −1 000 FCFA »."""
    parts = []
    units = [x for x in xs if not x.get("cur")]
    if units or not xs:
        parts.append(fmt.signed(sum(bankroll.played_gain(x) for x in units)))
    for c in dict.fromkeys(x["cur"] for x in xs if x.get("cur")):
        parts.append(M.signed(sum(bankroll.played_gain(x) for x in xs if x.get("cur") == c), c))
    return " et ".join(parts)


def summary_lines(bets, rows, fair_rows, days=None, detail=False, today=None):
    """Lignes du bilan sur les paris notés (texte brut) : résultat réel, puis écart de cote 1xBet. [] si rien de noté."""
    its = items(bets, rows, fair_rows, days, today)
    if not its:
        return []
    done = [x for x in its if x[2].get("result") in ("win", "loss")]
    unk = sum(1 for x in its if x[2].get("result") == "unknown")                # non vérifiables : ni gagnés ni perdus
    unk_txt = f" ({unk} non vérifiable{'s' if unk > 1 else ''}, vérifie chez 1xBet)" if unk else ""
    lines = []
    if done:
        w = sum(1 for x in done if x[2]["result"] == "win")
        gain = _gain_txt([x[2] for x in its if x[2].get("result") in bankroll.DONE])
        lines.append(f"Joués chez 1xBet : {w} gagné{'s' if w > 1 else ''} sur {len(done)} · résultat {gain}{unk_txt}")
    else:
        lines.append(f"Joués chez 1xBet : {len(its)} noté{'s' if len(its) > 1 else ''}, aucun réglé pour l'instant{unk_txt}")
    real = [x for x in its if x[0] == "réel"]
    if real:
        g = sum(_gap(x[2]["played"]["odds"], x[3]) for x in real) / len(real)
        lines.append(f"Cotes de 1xBet : {fmt.signed(g * 100, ' %')} en moyenne face à la cote typique du marché "
                     f"({len(real)} pari{'s' if len(real) > 1 else ''} noté{'s' if len(real) > 1 else ''})")
    ver = [x for x in its if x[0] == "vérif"]
    if ver:
        g = sum(_gap(x[2]["played"]["odds"], x[3]) for x in ver) / len(ver)
        lines.append(f"Combinés à vérifier joués : 1xBet payait {fmt.signed(g * 100, ' %')} face à la cote mini ({len(ver)})")
    if detail and real:
        by = {}
        for x in real:
            by.setdefault(x[1], []).append(_gap(x[2]["played"]["odds"], x[3]))
        lines.append("Écart de cote 1xBet par palier : " + ", ".join(
            f"{k} {fmt.signed(sum(v) / len(v) * 100, ' %')} ({len(v)})" for k, v in by.items()))
    return lines
