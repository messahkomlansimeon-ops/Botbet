"""Mise en forme des messages Telegram : français lisible, virgule décimale, HTML échappé, pas de jargon.

Les messages sont du HTML Telegram (balises <b>, <i>) : tout texte venu de l'extérieur (équipes, joueurs, ligues) passe par
esc(). Les fonctions renvoient un `telegram.Html` (envoyé en parse_mode HTML, repli en texte brut si Telegram refuse).
"""
import html
import re
from datetime import date, datetime

from . import config as C
from .telegram import Html, plain as telegram_plain

DAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre",
          "décembre")
# Mots internes qui ne doivent jamais apparaître dans un message du jour (testé).
JARGON = ("consensus", "désaccord", "edge", "steam", "clv", "brier", "score", "xg", "crédits", "kelly fraction")


def esc(s):
    return html.escape(str(s), quote=False)


def b(s):
    return f"<b>{s}</b>"


def i(s):
    return f"<i>{s}</i>"


def num(x, d=2):
    """1.55 -> « 1,55 »."""
    return f"{x:.{d}f}".replace(".", ",")


def trim(x, d=2):
    """Décimales inutiles retirées : 2.50 -> « 2,5 », 3.0 -> « 3 »."""
    s = f"{x:.{d}f}"
    s = s.rstrip("0").rstrip(".") if "." in s else s
    return s.replace(".", ",")


def odds_txt(x):
    """Cote : 2 décimales, 1 au-delà de 100 (« 156,7 »)."""
    return num(x, 2) if x < 100 else num(x, 1)


def pct(p):
    """0.314 -> « 31 % » ; sous 1 % une décimale (« 0,7 % »)."""
    v = p * 100
    return f"{v:.1f} %".replace(".", ",") if v < 1 else f"{round(v)} %"


def units(x):
    """Montant en unités : « 2 u », « 0,2 u ». Plus utilisé dans les messages (tout est en FCFA) : gardé comme outil de format."""
    return f"{trim(x, 1 if abs(x) >= 1 else 2)} u"


NO_BUDGET = "règle ton budget du jour avec « 💰 Mon budget »"      # à la place d'une mise, quand il n'y a pas de budget du jour (jamais d'unités)


def no_stake_line():
    """Une ligne (une fois par message) à la place de la mise conseillée quand il n'y a pas de budget du jour."""
    return i("💡 Mise conseillée : " + esc(NO_BUDGET))


def signed(x, suffix=" u"):
    """+10,2 u / −0,2 u (vrai signe moins)."""
    return ("+" if x >= 0 else "−") + trim(abs(x), 1 if abs(x) >= 1 else 2) + suffix


def hhmm(t):
    """« 21:00 » (UTC = Abidjan) -> « 21h00 »."""
    t = str(t or "")
    return t.replace(":", "h", 1) if re.fullmatch(r"\d\d:\d\d", t) else t


def fr_day(d):
    """« jeudi 8 octobre »."""
    d = d if isinstance(d, date) else (d.date() if isinstance(d, datetime) else date.fromisoformat(str(d)[:10]))
    return f"{DAYS[d.weekday()]} {d.day}{'er' if d.day == 1 else ''} {MONTHS[d.month - 1]}"


def tier_name(title):
    """« 🛡️ SÛR (cote 5 à 7) » -> « 🛡️ SÛR »."""
    return title.split(" (")[0]


def tier_word(title):
    """« 🛡️ SÛR (cote 5 à 7) » -> « Sûr » ; « 🎯 SPÉCIAL CORNERS/CARTONS… » -> « Spécial »."""
    words = tier_name(title).split()
    return words[1].capitalize() if len(words) > 1 else words[0].capitalize()


plain = telegram_plain


# ---------- libellés de marché, comme on les lit chez le bookmaker ----------

SINGULAR = {"tirs cadrés": "tir cadré", "tirs": "tir", "fautes": "faute", "points": "point", "rebonds": "rebond",
            "passes": "passe", "paniers à 3 pts": "panier à 3 pts"}


TEAM_MARKET = re.compile(r"^team_(shots|sot|fouls|throws|offsides)_(total|home|away)$")
# mots de 1xBet (« Tirs vers le but », « Tirs cadrés ») : le pari se retrouve directement chez le bookmaker
TEAM_UNITS = {"shots": "tirs vers le but", "sot": "tirs cadrés", "fouls": "fautes", "throws": "touches", "offsides": "hors-jeu"}


def label(market, name, point, who, group):
    """« Plus de 2,5 buts », « Moins de 9,5 corners », « Total cartons : plus de 3,5 », « Mbappé : 2 tirs cadrés ou plus »,
    « Les deux équipes marquent : oui », « Double chance : Nul ou Flamengo », « Flamengo gagne »."""
    tm = TEAM_MARKET.match(market)
    if tm:                                                         # tirs, tirs cadrés, fautes, touches, hors-jeu : total ou par équipe
        u = TEAM_UNITS[tm[1]]
        if market.endswith("_total"):
            return f"{'Plus' if name == 'Over' else 'Moins'} de {trim(point)} {u} au total"
        return f"{who or ('Équipe à domicile' if market.endswith('_home') else 'Équipe extérieure')} : " \
               f"{'plus' if name == 'Over' else 'moins'} de {trim(point)} {u}"
    unit = C.UNITS.get(market)
    if isinstance(unit, dict):
        unit = unit.get(group, "")
    over = name == "Over"
    if market.startswith("player_") and unit and point is not None:
        n = int(point) + 1 if float(point) % 1 else int(point)
        u = SINGULAR.get(unit, unit) if n == 1 else unit
        who_txt = f"{who} : " if who else ""
        return f"{who_txt}{n} {u} ou plus" if over else f"{who_txt}moins de {n} {unit}"
    if market == "alternate_totals_cards":
        return f"Total cartons : {'plus' if over else 'moins'} de {trim(point)}"
    if unit:
        return f"{'Plus' if over else 'Moins'} de {trim(point)} {unit}"
    if market == "btts":
        return "Les deux équipes marquent : " + ("oui" if name == "Yes" else "non")
    if market == "spreads":
        return f"{name} {trim(point, 2) if point < 0 else '+' + trim(point, 2)} (handicap)"
    if market == "double_chance":
        return "Double chance : " + name.replace(" or ", " ou ").replace("Draw", "Nul")
    return "Match nul" if name == "Draw" else f"{name} gagne"


# ---------- « pourquoi » en mots simples ----------

_STATS = [
    (re.compile(r"stats réelles : ([\d.]+) (corners|cartons) attendus vs ligne ([\d.]+)"),
     lambda m: f"Kelly s'attend à {m[1].replace('.', ',')} {m[2]} ; la ligne est à {m[3].replace('.', ',')}"),
    (re.compile(r"(?:xG|moyennes de buts) : ([\d.]+) buts attendus vs ligne ([\d.]+)"),
     lambda m: f"Kelly s'attend à {m[1].replace('.', ',')} buts ; la ligne est à {m[2].replace('.', ',')}"),
    (re.compile(r"(?:xG|moyennes de buts) : ([\d.]+)-([\d.]+) buts attendus"),
     lambda m: f"Kelly s'attend à {m[1].replace('.', ',')} buts pour l'un et {m[2].replace('.', ',')} pour l'autre"),
    (re.compile(r"stats réelles : ([\d.]+) (tirs cadrés|tirs|fautes)/90( \(estimé\))? vs ligne ([\d.]+)"),
     lambda m: f"Il fait {m[1].replace('.', ',')} {m[2]} par match en moyenne ; la ligne est à {m[4].replace('.', ',')}"),
]


def _kind(piece):
    if piece.startswith(("cote plus haute", "valeur +")):
        return 1
    if piece.startswith("la cote baisse"):
        return 2
    if piece.startswith("les bookmakers"):
        return 3
    return 0                                   # texte du modèle d'avant-match


def why_line(c, reason=None):
    """Une phrase courte et claire sur la raison du pari (priorité : stats, valeur, mouvement de cote, bookmakers)."""
    pieces = [p.strip() for p in str(c.get("why") or "").split(", ") if p.strip()]
    pieces.sort(key=_kind)
    for p in pieces:
        for rx, fn in _STATS:
            m = rx.search(p)
            if m:
                return fn(m)
        if _kind(p):
            return p[0].upper() + p[1:]
    pm = c.get("pm") or {}
    if reason == "stats":
        if (pm.get("pgap") or 0) >= C.LEG_PM_GAP:
            return f"Kelly le juge plus probable que les bookmakers ({round(pm['pgap'] * 100)} points de plus)"
        if (pm.get("xgd") or 0) >= C.LEG_XGD:
            return f"D'après les stats, cette équipe devrait marquer {num(pm['xgd'], 1)} but de plus que l'autre"
    if reason == "safe" or (c.get("prob") or 0) >= C.LEG_SAFE_PROB:
        return f"Très probable : {pct(c['prob'])} de chances"
    return ""


def best_note(c):
    """« jusqu'à 4,60 chez Coolbet » quand la meilleure cote dépasse nettement la cote typique, sinon ''."""
    best = c.get("best_odds")
    if best and best >= c["odds"] * (1 + C.BEST_NOTE_GAP):
        return f"jusqu'à {num(best)} chez {esc(c.get('book') or '?')}"
    return ""


def event_name(c):
    return f"{esc(c['home'])} – {esc(c['away'])}" if c.get("home") and c.get("away") else esc(c.get("event", ""))


def _match_line(n, c):
    """« <b>1. PSG – Lyon</b>  <i>14h00 · Ligue 1</i> » : le match d'une jambe, son heure et sa ligue."""
    when = " · ".join(x for x in (hhmm(c.get("start")), esc(c.get("league") or "")) if x)
    return b(f"{'' if n is None else f'{n}. '}{event_name(c)}") + (f"  {i(when)}" if when else "")


def price_txt(c, real=False, when=None):
    """Cote d'un pari réel, honnête sur son origine : « cote 1xBet 1,50 (09:30) » quand Kelly l'a lue chez 1xBet (c["xodds"], `when` :
    heure « HH:MM » du relevé) ; « pas proposé chez 1xBet » pour un pari absent de 1xBet (xnone, xmiss, ou `real` : 1xBet a chiffré
    d'autres paris du jour) ; sinon « cote de référence … (pas 1xBet) » : la cote typique des autres bookmakers."""
    if c.get("xodds"):
        return f"cote 1xBet {b(odds_txt(c['xodds']))}" + (f" ({esc(when)})" if when else "")
    if real or c.get("xnone") or c.get("xmiss"):
        return i("pas proposé chez 1xBet")
    return f"cote de référence {b(odds_txt(c['odds']))} " + i("(pas 1xBet)")


def opinion_line(c):
    """Ligne d'avis sous un pari (pronostic 💎 ou jambe d'un combiné) : « 📊 1xBet : 49 % · Kelly : 71 % » (+ « · retenu : 61 % » là où l'ensemble
    décide) ; '' sans avis de 1xBet. Italique, HTML échappé."""
    from . import ensemble                           # import tardif : ensemble importe odds, qui importe fmt
    try:
        txt = ensemble.explain_c(c)
    except (KeyError, TypeError, ValueError):
        txt = ""
    return i(f"📊 {esc(txt)}") if txt else ""


def leg(n, c, reason=None, price=None):
    """Bloc d'une jambe : le match, le pari et sa cote, la raison (une information par ligne). `price` : libellé de la cote (price_txt)
    à la place de la cote en gras."""
    note = best_note(c)
    lines = [_match_line(n, c), f"👉 {esc(c['pick'])} — {price or b(odds_txt(c['odds']))}" + (f" {i('(' + note + ')')}" if note else "")]
    ens = opinion_line(c)
    if ens:
        lines.append(ens)
    why = why_line(c, reason)
    if why:
        lines.append(f"💡 {i(esc(why))}")
    return "\n".join(lines)


def expect(prob, odds_total):
    """« espérance +6 % » : ce que rend la mise en moyenne à cette cote, d'après les probabilités de Kelly."""
    return f"espérance {signed((prob * odds_total - 1) * 100, ' %')}"


def code(c):
    """Un code coupon en <code> : un appui dessus le copie dans toutes les applications Telegram."""
    return f"<code>{esc(c)}</code>"


def coupon_block(coupon, retry=None):
    """Code coupon 1xBet d'un combiné ou d'un pari (xbet.save_coupon), à charger dans l'appli (en <code>, à copier d'un appui) ; '' sans code. `retry` : pari qui devait
    avoir son code (toutes les jambes lues chez 1xBet) mais dont l'enregistrement a échoué -> une ligne dit quoi faire (« touche … »)."""
    if coupon:
        return f"\n\n🎟️ Code coupon 1xBet : {code(coupon)}\n" + i("Dans l'appli 1xBet : ticket, « Charger le coupon »")
    return "\n\n" + i(f"Code coupon indisponible pour l'instant : {esc(retry)}") if retry else ""


def xbet_note(legs, when=None):
    """Pied d'un pari réel dont toutes les cotes sont celles de 1xBet (xprice.price_picks) ; '' sinon. `when` : heure « HH:MM » du relevé."""
    if not legs or not all(c.get("xodds") for c in legs):
        return ""
    return "\n\n" + i(f"💎 Cotes 1xBet de {esc(when)} : vérifie qu'elles n'ont pas baissé avant de valider." if when else
                       "💎 Cotes relevées chez 1xBet par Kelly : vérifie qu'elles n'ont pas baissé avant de valider.")


def combo(title, picks, odds_total, prob, stake=None, reasons=None, stake_txt=None, coupon=None, when=None, retry=None):
    """Message d'un combiné : titre et cote ; chances, espérance et mise sur deux lignes ; puis les jambes, séparées par une ligne
    vide. `stake_txt` : la mise conseillée sur le budget du jour (« 1 500 FCFA ») ; sans elle (pas de budget du jour), une ligne dit de régler
    le budget (jamais de mise en unités ; `stake`, ancienne mise en unités, est ignoré). `coupon` : code coupon 1xBet (toutes les jambes
    aux cotes 1xBet), `when` : heure « HH:MM » de ces cotes, `retry` : voir coupon_block."""
    reasons = reasons or {}
    head = [b(f"{esc(tier_name(title))} — cote {odds_txt(odds_total)}"),
            f"🎯 {len(picks)} paris · {pct(prob)} de chances",
            f"📈 {expect(prob, odds_total).capitalize()}" + (f" · 💰 mise : {b(esc(stake_txt))}" if stake_txt else "")]
    if not stake_txt:
        head.append(no_stake_line())
    legs = [leg(k, c, reasons.get(c["key"])) for k, c in enumerate(picks, 1)]
    return Html("\n".join(head) + "\n\n" + "\n\n".join(legs) + coupon_block(coupon, retry) + xbet_note(picks, when))


def single(c, reason=None, title="⭐ PARI DU JOUR", stake_txt=None, coupon=None, when=None, retry=None):
    """Pari du jour : une seule jambe (et sa mise conseillée sur le budget du jour, `stake_txt`, sinon la ligne « règle ton budget »).
    `coupon` / `when` / `retry` : voir combo."""
    head = [b(f"{title} — cote {odds_txt(c['odds'])}"), f"🎯 {pct(c['prob'])} de chances" + (f" · 💰 mise : {b(esc(stake_txt))}" if stake_txt else "")]
    if not stake_txt:
        head.append(no_stake_line())
    return Html("\n".join(head) + "\n\n" + leg(None, c, reason) + coupon_block(coupon, retry) + xbet_note([c], when))


def _legend():
    """Fin de la légende 💎 : ce que Kelly suit (cote 1xBet sur les buts et le résultat, ses statistiques ailleurs) quand l'ensemble décide ; '' sinon."""
    from . import ensemble
    t = ensemble.legend()
    return esc(t) + " " if t else ""


def pronos(items, limit=10, title=None, intro=None, when=None):
    """Pronostics sans cote de bookmaker, regroupés par match : le match, puis un pari par ligne avec sa cote minimale (ou sa cote
    1xBet, 💎). `title` / `intro` : autre titre et autre phrase d'introduction (relevé 1xBet de l'après-midi). `when` : heure « HH:MM »
    du relevé des cotes 1xBet, rappelée dans l'introduction (sinon « aujourd'hui »). Chaque pronostic peut porter l'heure de SA lecture (c["xat"]) :
    quand elles diffèrent (relevé partiel : seuls certains matchs ont été relus), l'introduction le dit et chaque ligne donne son heure."""
    shown = items[:limit]
    if not shown:
        return Html(b("🎲 Pronostics Kelly") + "\n" + i("Aucun pronostic assez solide : Kelly ne montre que des pronostics "
                                                                 "viables (assez de chances et de données)."))
    priced = any(c.get("xodds") for c in shown)
    unpriced = not all(c.get("xodds") for c in shown)
    stamps = {c.get("xat") or when for c in shown if c.get("xodds")} - {None}
    partial = len(stamps) > 1                          # des cotes lues à des heures différentes : l'heure du dernier relevé ne vaut pas pour toutes
    if len(stamps) == 1:
        when = next(iter(stamps))                      # toutes lues à la même heure : c'est elle, même si un relevé plus récent a eu lieu ailleurs
    stamp = f" Cotes 1xBet de {when}." if when and priced and not partial else ""
    lines = [b(title or "🎲 Pronostics Kelly"),
             i(esc(intro) + esc(stamp) if intro else "Le meilleur pronostic de chaque match, s'il est solide. "
               + ("💎 = cote 1xBet relevée par Kelly " + ("(relevé partiel : l'heure de lecture est indiquée pour chaque pari)" if partial else
                                                          f"à {esc(when)}" if when else "aujourd'hui")
                  + " et assez haute pour jouer (vérifie qu'elle n'a pas baissé). " + _legend() if priced else "")
               + ("« mini » : à comparer chez 1xBet, joue seulement si la cote est au moins celle indiquée." if unpriced else ""))]
    by = {}
    for c in shown:
        by.setdefault(c["id"], []).append(c)
    for cs in by.values():
        lines.append("")
        lines.append(_match_line(None, cs[0]))
        for c in cs:
            q = c.get("conf")
            data = "" if q is None else (" · données solides" if q >= 0.4 else " · données correctes")
            if c.get("xodds"):
                at = f" (lue à {esc(c.get('xat') or when)})" if partial and (c.get("xat") or when) else ""
                lines.append(f"💎 {esc(c['pick'])} — cote 1xBet {b(num(c['xodds']))}{at}")
                lines.append(i(f"   {pct(c['prob'])} de chances{data} · valeur {c['xev'] * 100:+.0f} %"))
            else:
                lines.append(f"👉 {esc(c['pick'])} — mini {b(num(c['min_odds']))}")
                lines.append(i(f"   {pct(c['prob'])} de chances{data}"))
            ens = opinion_line(c)
            if ens:
                lines.append("   " + ens)
    if len(items) > len(shown):
        lines += ["", i(f"+ {len(items) - len(shown)} autres : écris un nom d'équipe pour voir un match")]
    return Html("\n".join(lines))


# ---------- « 💰 1xBet paie plus que le marché » (kelly/sharpgap.py) ----------

GAP_TITLE = "💰 1xBet paie plus que le marché"
GAP_INTRO = ("La cote 1xBet est au-dessus de la cote juste des books de référence (Pinnacle, marge retirée) : ce n'est pas un avis de Kelly sur le match, "
             "c'est un prix en retard chez 1xBet. Vérifie la cote avant de valider.")


def gap_block(n, x, d, coupon=None, stake_txt=None):
    """Un pari « 1xBet paie plus que le marché » : le match, le pari avec la cote 1xBet et la cote juste, l'âge du relevé, la mise conseillée (`stake_txt`, FCFA)
    et son code coupon en <code>. `x` : candidat de sharpgap, `d` : sharpgap.describe(x), `n` : numéro affiché dans le message (None : sans)."""
    lines = [_match_line(n, x), f"👉 {esc(d['title'])} — {esc(d['line'])}", i(f"   {esc(d['age'])}")]
    if stake_txt:
        lines.append(f"💰 Mise conseillée : {b(esc(stake_txt))}")
    if coupon:
        lines.append(f"🎟️ Code coupon 1xBet : {code(coupon)}")
    return "\n".join(lines)


def gap_message(blocks, when=None, budget=True, intro=True):
    """Le message « 💰 1xBet paie plus que le marché » : titre (heure `when` du relevé), phrase d'explication, un bloc par pari (gap_block), et, sans budget du
    jour (`budget` faux), une ligne pour le régler."""
    head = [b(GAP_TITLE + (f" · {esc(when)}" if when else ""))] + ([i(esc(GAP_INTRO))] if intro else [])
    return Html("\n".join(head) + "\n\n" + "\n\n".join(blocks) + ("" if budget else "\n\n" + no_stake_line()))


def combo_changes(removed, added, changed=()):
    """Paris retirés d'un combiné remis aux cotes du moment (retirés par 1xBet ou devenus mal payés), leurs remplaçants, et les paris
    dont la ligne ou le côté a changé (`changed` : [(ancien, nouveau)])."""
    lines = [f"❌ Retiré : {esc(c.get('home', '?'))} – {esc(c.get('away', '?'))} ({esc(c.get('pick', ''))})" for c in removed]
    lines += [f"🔁 Changé : {esc(n.get('home', '?'))} – {esc(n.get('away', '?'))} : {esc(o.get('pick', ''))} → {esc(n.get('pick', ''))}"
              for o, n in changed]
    lines += [f"➕ Ajouté : {esc(c['home'])} – {esc(c['away'])} ({esc(c['pick'])})" for c in added]
    return "\n".join(lines)


def fair_leg(n, c):
    """Jambe d'un combiné à cote juste : match, pari, cote minimale à exiger chez 1xBet et chances selon Kelly."""
    chances = i(f"({pct(c['prob'])} de chances)")
    ens = opinion_line(c)
    if c.get("xodds"):
        return "\n".join([_match_line(n, c), f"💎 {esc(c['pick'])}", f"✔️ Cote 1xBet {b(num(c['xodds']))}  {chances}"] + ([ens] if ens else []))
    return "\n".join([_match_line(n, c), f"👉 {esc(c['pick'])}", f"✔️ Joue si 1xBet paie au moins {b(num(c['min_odds']))}  {chances}"]
                      + ([ens] if ens else []))


def fair_combo(min_total, picks, prob, stake=None, stake_txt=None, title=None, coupon=None, when=None, retry=None, stale=False):
    """Message d'un combiné à cote juste : toujours clairement annoncé « à vérifier chez 1xBet », chaque jambe avec sa cote minimale ;
    la mise conseillée vient du budget du jour (`stake_txt`), sinon une ligne dit de régler le budget (jamais d'unités ; `stake`, ancienne
    mise indicative en unités, est ignoré). `title` : combinés « spécial tirs » (« 🎯 COMBINÉ TIRS CADRÉS »).
    `when` : heure « HH:MM » du relevé des cotes 1xBet (combiné réel), rappelée en bas du message ; `retry` : voir coupon_block ;
    `stale` : cotes du DERNIER relevé (1xBet injoignable à l'instant) : le titre et le pied le disent, avec l'heure `when` du relevé si on
    la connaît."""
    real = all(c.get("xodds") for c in picks)          # toutes les cotes relevées chez 1xBet par Kelly : la cote totale est réelle
    tail = "COTES 1XBET" if real else "À VÉRIFIER CHEZ 1XBET"
    old = real and stale
    if old:                                          # jamais le titre « aux cotes 1xBet » sans dire de quand elles datent
        tail = "COTES 1XBET DU DERNIER RELEVÉ" + (f" ({when})" if when else "")
    head = [b(f"{title} — {tail}" if title else (f"💎 COMBINÉ — {tail}" if old else "💎 COMBINÉ AUX COTES 1XBET" if real else
                                                    "🎲 COMBINÉ À VÉRIFIER CHEZ 1XBET")),
            f"🎯 {'Cote' if real else 'Cote mini'} {b(odds_txt(min_total))} · {len(picks)} paris · {pct(prob)} de chances",
            (f"💰 Mise : {b(esc(stake_txt))}" + ("" if real else f" {i('(seulement si 1xBet paie la cote mini)')}") if stake_txt else
             no_stake_line())]
    foot = i(f"💎 Cotes 1xBet du dernier relevé ({esc(when)}) : elles ont pu baisser depuis, vérifie-les avant de valider." if old and when else
             "💎 Cotes 1xBet du dernier relevé : elles ont pu baisser depuis, vérifie-les avant de valider." if old else
             f"💎 Cotes 1xBet de {esc(when)} : vérifie qu'elles n'ont pas baissé avant de valider." if real and when else
             "💎 Cotes relevées chez 1xBet par Kelly : vérifie qu'elles n'ont pas baissé avant de valider." if real else
             ("⚠️ Kelly n'a pas vu toutes les cotes de 1xBet : " if any(c.get("xodds") for c in picks) else
              "⚠️ Kelly n'a pas vu les cotes de 1xBet : ") + "joue seulement si CHAQUE pari atteint sa cote mini "
             f"(la cote totale sera alors d'au moins {odds_txt(min_total)}), sinon passe ton chemin.")
    c_line = coupon_block(coupon, retry)
    return Html("\n".join(head) + "\n\n" + "\n\n".join(fair_leg(k, c) for k, c in enumerate(picks, 1))
                + c_line + "\n\n" + foot)
