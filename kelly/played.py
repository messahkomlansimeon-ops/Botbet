"""Paris que tu NOTES : « ✅ Je l'ai joué » (ou /joue) donne la cote obtenue chez 1xBet et la mise, en FCFA. Seuls ces paris comptent dans tes
résultats, le bilan et les alertes « tu as joué » : un pari que Kelly a seulement conseillé (mise conseillée du budget du jour) ne compte
jamais. Kelly mesure aussi l'écart entre la cote de 1xBet et sa cote de référence (cote typique du marché pour les paris à cotes réelles,
cote mini pour les combinés à vérifier).

/joue <quoi> <cote> [mise]   quoi : sur, costaud, special, mechant, monstre (combinés du jour), pari (pari du jour),
                              v5, v12, v25 (combinés à vérifier chez 1xBet, par cote mini visée), vm1, vm2... (combis sur mesure suivis par
                              « ✅ Je le joue », voir kelly/custom.py). Cote 0 : annule la note.
Mise par défaut : la mise conseillée du budget du jour (money.suggested) ; sans mise conseillée, Kelly la demande (jamais d'unités).
Le pari noté est celui du jour, sinon le plus récent pas encore réglé. Stockage dans le pari lui-même : x["played"] =
{odds, stake, at} et x["cur"] = la devise (FCFA) de la mise (combiné de bets.json, ligne envoyée de tracked.json, combiné de
fair_combos.json).
"""
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from . import bankroll, combos as CB, config as C, fair, fmt, money as M, store, telegram, track
from .telegram import Html

MAX_ODDS = 1000000.0           # « combi 10000 » peut monter à 12 500 (fourchette de /combi)
NEED_STAKE = "Avec quelle mise en FCFA ?"          # début de la réponse quand la mise manque (chat._answer garde alors la question ouverte)


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
    """Ce que désigne un mot de /joue : le pari, sa cote de référence, sa mise conseillée (FCFA, 0 si Kelly n'en donne pas), et comment le
    sauver. `known` : la cote 1xBet du pari est CONNUE (price_info : cote 1xBet de la version en cours, pas une cote mini ni une cote de
    référence d'un autre bookmaker) : Kelly ne la demande alors jamais, `ref` est la cote jouée."""

    def __init__(self, item, ref, stake, label, save, ref_word, known=False):
        self.item, self.ref, self.stake, self.label, self.save, self.ref_word, self.known = item, ref, stake, label, save, ref_word, known


def find(word, day):
    """Target du mot `word` pour le jour `day` (AAAA-MM-JJ), ou un message d'erreur (str)."""
    w = norm(word)
    tiers = tier_words()
    if w in tiers:
        bets = store.load("bets.json", {"picks": [], "combos": []})
        c = _pick([c for c in bets["combos"] if c["tier"] == tiers[w]], day)
        if not c:
            return f"Pas de combiné {fmt.tier_word(tiers[w])} à noter (ni aujourd'hui ni en attente de résultat)."
        px = price_info(w, c)
        return Target(c, px["odds"], M.suggested(c), f"{fmt.tier_word(c['tier'])} du {fmt.fr_day(c['date'])}",
                      lambda: store.save("bets.json", bets), "cote 1xBet" if px["real"] else "cote typique", _known(px))
    if w in ("pari", "jour", "single"):
        rows = track.load()
        r = _pick(single_rows(rows, store.load("bets.json", {"combos": []}).get("combos", [])), day)
        if not r:
            return "Pas de pari du jour à noter (ni aujourd'hui ni en attente de résultat)."
        px = price_info("pari", r)
        return Target(r, px["odds"], M.suggested(r), f"pari du jour du {fmt.fr_day(r['date'])} ({r['pick']})", lambda: track.save(rows),
                      "cote 1xBet" if px["real"] else "cote typique", _known(px))
    if w.startswith("v") and len(w) > 1:              # v5, v12, v25 ; vtc5 / vtv5 : combinés « spécial tirs » ; vm1, vm2... : combis sur mesure
        rows = fair.load_combos()
        c = _pick([c for c in rows if str(c["tier"]) == w[1:]], day)
        if not c:
            if re.fullmatch(r"vm\d+", w):
                return f"Pas de combi sur mesure « {w} » à noter (ni aujourd'hui ni en attente de résultat)."
            return None if not (w[1:].isdigit() or CB.shots_title(w[1:])) else \
                f"Pas de combiné « {_fair_name(w[1:])} » à noter (ni aujourd'hui ni en attente de résultat)."
        what = (custom_name(c).lower() if is_custom(c) else
                f"combiné aux cotes 1xBet (cote {fmt.odds_txt(c['min_odds'])})" if is_real(c)
                else f"combiné à vérifier (cote mini {fmt.odds_txt(c['min_odds'])})")
        return Target(c, c["min_odds"], M.suggested(c),
                      f"{what} du {fmt.fr_day(c['date'])}", lambda: store.save(fair.COMBOS_FILE, rows),
                      "cote 1xBet" if is_real(c) else "cote mini", is_real(c))
    return None


def _known(px):
    """La cote 1xBet du pari est-elle connue (price_info) ? Oui s'il est chiffré par 1xBet et pas déclaré injouable."""
    return bool(px.get("real")) and not px.get("dead")


def is_custom(c):
    """Ligne de fair_combos.json d'un combiné sur mesure (« combi 20 ») que l'utilisateur joue : « ✅ Je le joue », voir kelly/custom.py."""
    return bool(c.get("custom"))


def custom_name(c):
    """« Combi sur mesure (cote 20) » : le nom d'un combiné sur mesure suivi (la cote demandée avec « combi 20 », pas la cote totale)."""
    return f"Combi sur mesure (cote {fmt.trim(c.get('target') or c['min_odds'], 1)})"


def is_real(c):
    """Combiné de fair_combos.json bâti avec les cotes 1xBet relevées (toutes les jambes ont leur cote) : « cote », pas « cote mini »."""
    return bool(c.get("legs")) and all(leg.get("xodds") for leg in c["legs"])


def price_info(what, x):
    """Prix de la VERSION EN COURS d'un pari du jour pour « Mes paris » et « Autre cote » : {odds, real, label, coupon}.
    - combiné à vérifier « v<palier> » (fair_combos.json) : sa ligne porte déjà la dernière version envoyée (fair.update_combo) ; aux
      cotes 1xBet « cote 1xBet 5,60 (HH:MM) » et son code coupon, sinon « cote mini 5,00 » ;
    - combiné réel (bets.json, « xlegs ») ou pari du jour (« xodds ») aux cotes 1xBet : la dernière vérification (x["xnow"], voir
      app.refresh_real) ou celle du matin ; un combiné devenu injouable est signalé ;
    - sinon « cote de référence 1,60 (pas 1xBet) » : la cote typique des autres bookmakers."""
    if "legs" in x:
        real = is_real(x)
        odds = x["min_odds"]
        if real:
            at = x.get("updated") or x.get("at")
            return {"odds": odds, "real": True, "coupon": x.get("coupon"),
                    "label": f"cote 1xBet {fmt.odds_txt(odds)}" + (f" ({at})" if at else "")}
        return {"odds": odds, "real": False, "coupon": None, "label": f"cote mini {fmt.odds_txt(odds)}"}
    now = x.get("xnow") or {}
    if x.get("xlegs") or x.get("xodds"):
        odds = now.get("odds") or (x["odds"] if x.get("xlegs") else x["xodds"])
        at = now.get("at") or x.get("at") or x.get("xat")
        label = f"cote 1xBet {fmt.odds_txt(odds)}" + (f" ({at})" if at else "")
        if now.get("dead"):
            label = f"plus jouable chez 1xBet (vérifié à {now['at']})"
        return {"odds": odds, "real": True, "dead": bool(now.get("dead")), "label": label,
                "coupon": None if now.get("dead") else (now.get("coupon") if now else x.get("coupon"))}
    return {"odds": x["odds"], "real": False, "coupon": None, "label": f"cote de référence {fmt.odds_txt(x['odds'])} (pas 1xBet)"}


def _fair_name(tier, real=False, row=None):
    """« Tirs cadrés », « Tirs vers le but » pour les combinés « spécial tirs », sinon « À vérifier (cote mini 25) » ou, `real`, « Aux cotes 1xBet
    (cote 25,1) » : le niveau de cote dit lequel c'est (`row` : la ligne de suivi, pour la cote d'un combiné aux cotes 1xBet)."""
    t = CB.shots_title(str(tier))
    if t:
        return t.replace("🎯 COMBINÉ ", "").capitalize()
    if real:
        return "Aux cotes 1xBet" + (f" (cote {fmt.trim(row['min_odds'], 1)})" if row and row.get("min_odds") else "")
    return f"À vérifier (cote mini {tier})" if str(tier) else "À vérifier"


def _gap(odds, ref):
    return odds / ref - 1


def command(args, day, now=None):
    """/joue <quoi> <cote> [mise] ; sans argument : ce qui est à noter aujourd'hui. La mise est en FCFA ; sans mise, celle conseillée par le
    budget du jour, et sans mise conseillée Kelly la demande (NEED_STAKE : jamais une mise en unités inventée)."""
    day = str(day)[:10]
    if not args:
        return todo(day)
    if len(args) < 2:
        return "Exemple : /joue sur 5,40 1000 (combiné Sûr joué à 5,40 chez 1xBet, mise 1 000 FCFA). /joue seul : ce qui reste à noter."
    t = find(args[0], day)
    if t is None:
        return (f"« {args[0]} » inconnu. Choix : {', '.join(tier_words())}, pari (pari du jour), v5 / v12 / v25 (combinés à vérifier), "
                "vm1 (combi sur mesure). Exemple : /joue sur 5,40 1000")
    if isinstance(t, str):
        return t
    odds, given, err = parse_odds_stake(args[1:])
    if odds is None or err or (given is not None and given < 0):
        return "Cote ou mise invalide. Exemple : /joue sur 5,40 1000 (cote 5,40, mise 1 000 FCFA)."
    if t.item.get("result") is not None:
        return f"Le {t.label} est déjà réglé : trop tard pour le noter."
    if odds == 0:
        if not t.item.pop("played", None):
            return f"Le {t.label} n'était pas noté."
        t.save()
        return f"Note annulée : {t.label}."
    if not 1.0 < odds <= MAX_ODDS:
        return "La cote doit être supérieure à 1. Exemple : /joue sur 5,40 1000"
    stake = given if given is not None else ((t.item.get("played") or {}).get("stake") or t.stake)      # nouvelle cote d'un pari noté : même mise
    if given is None and not stake > 0:             # pas de mise conseillée (pas de budget du jour, ou trop petite) : jamais d'unités
        return f"{NEED_STAKE} Réponds ta mise, par exemple 1 000 (cote {fmt.num(odds)} retenue)."
    if not stake > 0:
        return "La mise doit être supérieure à 0. Exemple : /joue sur 5,40 1000 (annuler : /joue sur 0)."
    return note(t, odds, stake, now)


def note(t, odds, stake, now=None):
    """Note le pari `t` (find) joué à `odds` pour `stake` FCFA : x["played"] = {odds, stake, at} et la devise de la mise (x["cur"]). Une ligne
    sans devise (ancienne mise en unités, jamais affichée) prend la mise conseillée du moment si Kelly en donne une (money.suggested) : le « stake »
    d'une ligne avec devise est toujours une mise conseillée en FCFA. Sauvegarde la ligne. -> « ✅ Noté : … »."""
    x = t.item
    x["played"] = {"odds": round(odds, 2), "stake": round(stake, 2), "at": (now or datetime.now(timezone.utc)).isoformat(timespec="minutes")}
    x.pop("skipped", None)
    if x.get("cur") != M.cur():                     # ligne sans devise (ancienne mise en unités) ou d'une autre devise : elle passe en FCFA
        x["cur"] = M.cur()
        if t.stake > 0:
            x["stake"] = t.stake
        elif x.get("stake") != 0:
            x["stake"] = None
    t.save()
    gap = _gap(odds, t.ref)
    lines = [f"✅ Noté : {t.label}, joué à {fmt.odds_txt(odds)} chez 1xBet, mise {M.money(stake, x['cur'])} "
             f"({t.ref_word} {fmt.odds_txt(t.ref)} : {fmt.signed(gap * 100, ' %')})."]
    if t.ref_word == "cote mini" and odds < t.ref:
        lines.append("⚠️ 1xBet paie moins que la cote mini : Kelly conseillait de passer ton chemin.")
    elif gap < -0.15:
        lines.append("⚠️ Cote bien plus basse que prévu : vérifie la cote saisie, ou les cotes de 1xBet ont bougé.")
    return "\n".join(lines)


def todo(day):
    """« 🧾 Mes paris » : les paris du jour, notés ou non, avec leur mise conseillée, leur code coupon (en <code>, un appui le copie) et un
    bouton par pari — « ✅ Je l'ai joué » (avec la mise conseillée) pour un pari pas noté, « ✏️ Autre cote » et « ↩️ Annuler » pour un pari noté.
    Message HTML : tout le texte est échappé (fmt.esc)."""
    bets = store.load("bets.json", {"picks": [], "combos": []})
    items_ = [(fmt.tier_word(c["tier"]), c, c["odds"], norm(fmt.tier_word(c["tier"]))) for c in bets["combos"] if c["date"] == day]
    items_ += [("Pari du jour", r, r["odds"], "pari") for r in single_rows(track.load(), bets["combos"]) if r["date"] == day]
    items_ += [(custom_name(c) if is_custom(c) else _fair_name(c["tier"], is_real(c), c), c, c["min_odds"], f"v{c['tier']}")
               for c in fair.load_combos() if c["date"] == day]
    if not items_:
        return f"Rien à noter aujourd'hui : les paris du jour arrivent à {C.DAY_END_HOUR:02d}h00."
    lines, rows, left = [], [], False
    for name, x, ref, what in items_:
        px = price_info(what, x)
        if x.get("skipped") and x.get("result") is None:         # retiré avec l'ancien « ❌ Pas joué » (ancien message) : on peut le remettre
            lines.append(fmt.esc(f"❌ {name} : pas joué"))
            rows.append([(f"↩️ {name} : finalement joué", f"unskip|{what}|{day}|l")])
            continue
        stake = M.suggested(x) if not x.get("played") and x.get("result") is None else 0
        lines.append(fmt.esc(_todo_line(name, x, px["label"], f"/joue {what} <cote>", stake)) + _coupon_line(px))
        if x.get("result") is None:
            if x.get("played"):
                rows.append([(f"✏️ Autre cote : {name}", f"joue|{what}|{day}"), (f"↩️ Annuler : {name}", f"annule|{what}|{day}")])
            else:
                left = True
                rows.append([(f"✅ {name} · Je l'ai joué" + (f" — {M.money(stake)}" if stake else ""), f"joue|{what}|{day}")])
            rows += _verify_row(what, x, day, name)
    foot = "Appuie sur le pari que tu as joué, puis envoie la cote obtenue chez 1xBet." if left else "Tout est noté 👍"
    head = ["🧾 Tes paris du jour", "Seuls les paris que tu notes comptent dans tes résultats.", ""]
    hint = ["", fmt.esc("💡 Mise conseillée : " + M.NO_BUDGET)] if left and not M.active() else []
    text = Html("\n".join([fmt.esc(h) for h in head] + lines + hint + ["", fmt.esc(foot)]))
    return telegram.with_buttons(text, telegram.inline(rows)) if rows else text


def _todo_line(name, x, label, cmd, stake=0):
    """Ligne d'un pari de « Mes paris » ; `label` : son prix honnête (price_info : « cote 1xBet 5,60 (09:30) », « cote mini 5,00 »...) ;
    `stake` : sa mise conseillée en FCFA (0 : aucune)."""
    p = x.get("played")
    if x.get("dead"):                                # déclaré injouable par Kelly (paris retirés de 1xBet, pas de remplaçant)
        return f"🚫 {name} : plus jouable chez 1xBet (Kelly a conseillé de ne pas le jouer)"
    if p:
        return f"✅ {name} : joué à {fmt.odds_txt(p['odds'])}, mise {M.money(p['stake'], x.get('cur'))}"
    if x.get("result") is not None:
        return f"⏹ {name} — {label} : déjà réglé, pas noté"
    advice = (f" — mise conseillée {M.money(stake)}" if stake else " — pas de mise conseillée (trop petite pour ton budget)" if M.active() else "")
    return f"▫️ {name} — {label} : pas noté{advice} — {cmd}"


def _coupon_line(px):
    """Code coupon 1xBet de la version en cours, sous la ligne du pari, en <code> (un appui le copie ; rien sans code). HTML."""
    return f"\n      🎟️ Code coupon 1xBet : {fmt.code(px['coupon'])}" if px.get("coupon") else ""


def _verify_row(what, x, day, name):
    """Ligne « 🎟️ Vérifier … » d'un combiné ou pari aux cotes 1xBet pas encore réglé (voir chat.refresh_button) ; [] sinon."""
    if x.get("result") is not None or x.get("skipped"):
        return []
    from . import chat
    b = chat.refresh_button(what, day, f"🎟️ Vérifier ce combiné : {name}")
    return [[b]] if b else []


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
    """Résultat en FCFA des paris notés et réglés : « +3 200 FCFA » (une ligne sans devise est en FCFA, voir kelly/migrate.py) ; une devise
    différente est comptée à part (« +3 200 FCFA et −10 € »)."""
    by = {}
    for x in xs:
        by.setdefault(x.get("cur") or M.cur(), []).append(x)
    return " et ".join(M.signed(sum(bankroll.played_gain(x) for x in g), c) for c, g in by.items()) or M.signed(0)


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
    for group, tmpl in (([x for x in ver if is_real(x[2])], "Combinés aux cotes 1xBet joués : cote obtenue {g} face à la cote lue par Kelly ({n})"),
                        ([x for x in ver if not is_real(x[2])], "Combinés à vérifier joués : 1xBet payait {g} face à la cote mini ({n})")):
        if group:
            g = sum(_gap(x[2]["played"]["odds"], x[3]) for x in group) / len(group)
            lines.append(tmpl.format(g=fmt.signed(g * 100, ' %'), n=len(group)))
    if detail and real:
        by = {}
        for x in real:
            by.setdefault(x[1], []).append(_gap(x[2]["played"]["odds"], x[3]))
        lines.append("Écart de cote 1xBet par palier : " + ", ".join(
            f"{k} {fmt.signed(sum(v) / len(v) * 100, ' %')} ({len(v)})" for k, v in by.items()))
    return lines
