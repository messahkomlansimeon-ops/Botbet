"""Bouton « 📊 Marchés » : le bilan de chaque marché en une carte (HTML), rangé en 🟢 solides, 🔴 à problème, ⏳ en attente.

Pour chaque famille (buts, double chance, corners, tirs...) :
  - fiabilité de Kelly sur tes vrais résultats : réalisé / annoncé (calib.load, cumul vieilli), ou l'avancée vers le seuil (calib.min_need) ;
  - dans les combinés ou écartés (calib.family_k real_only : même règle que combos.data_ok), et le compteur « nouvelle règle » (calib.new_rule_note) ;
  - qui a raison quand Kelly et 1xBet (le marché pour les buts) ne sont pas d'accord, d'après tes résultats seuls (ensemble.json : log-vraisemblance
    par poids, avance du meilleur poids sur moitié-moitié) ;
  - ce qu'a mesuré le rejeu de la saison passée (calib.boot_acc), et sa correction quand les vrais résultats manquent ;
  - le détail par sens (« ↕️ plus 73 % / 64 % · moins ⚠️ 39 % / 64 % ») : une moyenne biaisée trompe les deux sens en sens contraires ;
  - la correction des moyennes (« moyenne : 23,1 attendus, 25,0 réels → corrigée ×1,08 », kelly/means.py).
"""
from . import calib, config as C, ensemble, fmt, means, status

ORDER = ("goals", "team_goals", "dc", "h2h", "btts", "corners", "cards", "yellow", "shots", "sot", "fouls", "throws", "offsides")
MIN_ENS = 30              # résultats minimum pour dire qui de Kelly ou du marché a raison
STRONG, MEDIUM = 3.0, 1.0  # avance (log-vraisemblance atténuée) du meilleur poids sur moitié-moitié : nette, petite ; en dessous, égalité


def _acc(v):
    return v if calib._acc_list(v) and v[0] > 0 else None


def who_is_right(fam, data=None):
    """« Kelly nettement devant », « 1xBet un peu devant », « à égalité » ou « trop peu de résultats » (d'après tes résultats seuls), '' sans donnée."""
    st = (((data if data is not None else ensemble.load()).get("fam") or {}).get(fam) or {}).get("m") or {}
    ll, n = st.get("ll"), st.get("n", 0)
    if not ll or not n:
        return ""
    if n < MIN_ENS:
        return f"trop peu de résultats ({calib.count(n)})"
    g = ensemble.GRID
    best = max(range(len(ll)), key=lambda k: ll[k])
    half = min(range(len(g)), key=lambda k: abs(g[k] - 0.5))
    adv = ll[best] - ll[half]
    market = "le marché" if fam in ensemble.PROBABLE_FAMILIES else "1xBet"
    if adv < MEDIUM:
        return f"Kelly et {market} à égalité ({calib.count(n)})"
    who = "Kelly" if g[best] > 0.5 else market.capitalize() if market == "le marché" else market
    return f"{who} {'nettement' if adv >= STRONG else 'un peu'} devant ({calib.count(n)})"


def _rows(real=None, boot=None, ens=None):
    """[(groupe, famille, lignes)] : groupe « good » / « bad » / « wait »."""
    real = real if real is not None else calib.load()
    boot = boot if boot is not None else calib.boot_acc()
    ens = ens if ens is not None else ensemble.load()
    fams = [f for f in ORDER] + sorted(f for f in set(real) | set(boot) | set(ens.get("fam") or {}) if not f.startswith("_") and f not in ORDER
                                       and f != "player")
    out = []
    for f in fams:
        r, b, verdict = _acc(real.get(f)), _acc(boot.get(f)), who_is_right(f, ens)
        if not (r or b or verdict):
            continue
        icon = fmt.market_icon({"market": calib.rep_market(f)})
        label = calib.LABELS.get(f, f).capitalize()
        need = calib.min_need(f)
        note = calib.new_rule_note(f, real)
        if r and r[0] >= need:
            n, w, s = r
            mood = status._mood(w / n, s / n)
            out_k = not calib.in_combos(f, real)
            group = "bad" if (out_k or mood == "trop confiante") else "good"
            head = f"{icon} <b>{fmt.esc(label)}</b> · {fmt.pct(w / n)} / {fmt.pct(s / n)} ({calib.count(n)}) · {mood}"
            if out_k:
                head += " · écartés des combinés"
        else:
            group = "wait"
            head = f"{icon} <b>{fmt.esc(label)}</b> · {calib.count(r[0]) if r else 0}/{need} résultats"
            k = calib.factor(calib.rep_market(f), real)
            if b and abs(k - 1) >= 0.005:
                head += f" · correction du rejeu ×{fmt.num(k)}"
        head += fmt.esc(note)
        sub = []
        if verdict:
            sub.append(f"⚖️ {verdict}")
        if b:
            sub.append(f"📚 rejeu {fmt.pct(b[1] / b[0])} / {fmt.pct(b[2] / b[0])}")
        extra = [x for x in (sides_line(f, real), means.line(f)) if x] + [f"   🏟 {x}" for x in means.league_lines(f)]
        out.append((group, f, [head] + (["     " + fmt.i(fmt.esc(" · ".join(sub)))] if sub else []) + ["     " + fmt.i(fmt.esc(x)) for x in extra]))
    return out


SIDE_MIN = 10                   # résultats d'un sens (plus, moins…) pour l'afficher
SIDE_WARN = 0.10                # sens à 10 points ou plus sous l'annoncé (20 résultats au moins) : ⚠️


def sides_line(fam, real=None):
    """« ↕️ plus 73 % / 64 % (22) · moins ⚠️ 39 % / 64 % (23) » : réalisé / annoncé de chaque sens du pari (plus / moins, oui / non, 1X / X2…), car
    une moyenne biaisée trompe les deux sens en sens contraires et le chiffre de la famille les mélange ; '' sans sens assez suivi."""
    cells = ((real if real is not None else calib.load()).get("_cell") or {})
    parts = []
    for key, acc in sorted(cells.items(), key=lambda kv: -(kv[1][0] if isinstance(kv[1], list) else 0)):
        f, _, sd = key.partition("|")
        if f != fam or "|" in sd or not isinstance(acc, list) or len(acc) != 3 or acc[0] < SIDE_MIN:
            continue
        n, w, sp = acc
        warn = "⚠️ " if n >= 20 and w / n <= sp / n - SIDE_WARN else ""
        parts.append(f"{sd} {warn}{fmt.pct(w / n)} / {fmt.pct(sp / n)} ({calib.count(n)})")
    return ("↕️ " + " · ".join(parts)) if parts else ""


def card():
    """Le message « 📊 Marchés » (HTML)."""
    from .telegram import Html
    rows = _rows()
    real = calib.load()
    fams = [_acc(v) for f, v in real.items() if not f.startswith("_")]
    tot = [sum(a[i] for a in fams if a) for i in range(3)]
    blocks = [["📊 <b>Bilan des marchés</b>",
               fmt.i("réalisé / annoncé (résultats) · ⚖️ qui a raison quand Kelly et 1xBet ne sont pas d'accord, d'après tes résultats")]]
    if tot[0]:
        blocks[0].append(f"🎯 En tout : {fmt.pct(tot[1] / tot[0])} réalisés pour {fmt.pct(tot[2] / tot[0])} annoncés ({calib.count(tot[0])} pronostics)")
    for group, title in (("good", "🟢 <b>Solides</b>"), ("bad", "🔴 <b>À problème</b>"), ("wait", "⏳ <b>Pas encore assez de résultats</b>")):
        lines = [x for g, _, ls in rows if g == group for x in ls]
        if lines:
            blocks.append([title] + lines)
    if len(blocks) == 1:
        blocks.append(["Aucun résultat pour l'instant : les marchés apparaîtront dès les premiers pronostics réglés."])
    blocks.append([fmt.i("📚 rejeu : la saison passée rejouée (point de départ des corrections). Détail chiffré : « bilan détail ».")])
    return Html("\n\n".join("\n".join(b) for b in blocks))
