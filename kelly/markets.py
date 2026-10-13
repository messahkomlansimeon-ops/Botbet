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
    blocks.append([fmt.i("📚 rejeu : la saison passée rejouée (point de départ des corrections). Détail chiffré : « bilan détail ». Par championnat "
                         "et par sens (plus / moins) : « marchés détail », « marchés détail tirs », « marchés détail ldc ».")])
    return Html("\n\n".join("\n".join(b) for b in blocks))


# ---------- « marchés détail » : par championnat (coupes comprises) et par sens ----------

FAM_WORDS = {"buts": "goals", "but": "goals", "equipe": "team_goals", "1n2": "h2h", "dc": "dc", "double": "dc", "btts": "btts", "marquent": "btts",
             "corners": "corners", "corner": "corners", "cartons": "cards", "carton": "cards", "jaunes": "yellow", "tirs": "shots", "tir": "shots",
             "cadres": "sot", "fautes": "fouls", "faute": "fouls", "touches": "throws", "horsjeu": "offsides", "hors-jeu": "offsides"}
LEAGUE_ALIAS = {"ldc": "champions", "c1": "champions", "c3": "europa", "europa": "europa", "c4": "conference", "cdm": "coupe du monde"}
CELL_MIN = 3                    # résultats d'un sens dans un championnat pour l'afficher
SMALL = 30                      # sous ce nombre : simple indication (marge d'erreur de ±18 points ou plus)


def _settled(days_rows=None):
    """Pronostics réglés (gagnés / perdus) des 10 derniers jours : data/fair.json et le journal de l'ensemble (beaucoup plus de lignes), sans doublon
    (jour + clé) -> [{sport_key, market, side, won, p}] ; p = la proba annoncée par Kelly (p0, sinon pm, sinon prob)."""
    from . import fair, store
    rows = days_rows if days_rows is not None else (fair.load() + store.load(ensemble.LOG_FILE, []))
    seen, out = set(), []
    for r in rows:
        if r.get("result") not in ("win", "loss"):
            continue
        k = (str(r.get("date") or r.get("commence") or "")[:10], r.get("key"))
        if k in seen:
            continue
        seen.add(k)
        p = r.get("p0") if r.get("p0") is not None else r.get("pm") if r.get("pm") is not None else r.get("prob")
        sd = calib.side(r)
        if p is None or not sd:
            continue
        out.append({"sport_key": r.get("sport_key") or "?", "market": r.get("market") or "", "side": sd, "won": r["result"] == "win", "p": float(p)})
    return out


def by_league(words=(), rows=None):
    """« marchés détail [tirs | corners | ldc | Premier…] » (HTML) : par championnat (coupes comprises), chaque famille avec son détail par sens :
    « ⚽ Buts : plus 72 % / 65 % (18) · moins ⚠️ 55 % / 68 % (11) » (réalisé / annoncé, nombre). `words` : une famille (buts, corners, tirs…) et / ou un
    bout de nom de championnat (ldc = Ligue des champions). Petits nombres : une indication, pas une preuve."""
    from . import catalogue, played
    from .telegram import Html
    fam_f, lg_f = None, []
    for w in words or ():
        n = played.norm(w)
        if n in FAM_WORDS:
            fam_f = FAM_WORDS[n]
        elif n not in ("detail", "details"):
            lg_f.append(LEAGUE_ALIAS.get(n, n))
    groups = {}
    for r in _settled() if rows is None else rows:
        fam = calib.family(r["market"])
        if fam_f and fam != fam_f:
            continue
        name = catalogue.name(r["sport_key"]) if r["sport_key"] != "?" else "Championnat inconnu"
        if lg_f and not all(x in played.norm(name) or x in r["sport_key"] for x in lg_f):
            continue
        cell = groups.setdefault(name, {}).setdefault(fam, {}).setdefault(r["side"], [0, 0, 0.0])
        cell[0] += 1
        cell[1] += r["won"]
        cell[2] += r["p"]
    lines = ["📊 " + fmt.b("Marchés par championnat") + fmt.esc(" (10 derniers jours)"),
             fmt.i(fmt.esc(f"réalisé / annoncé (nombre) · ⚠️ 10 points sous l'annoncé · moins de {SMALL} résultats : simple indication"))]
    order = sorted(groups, key=lambda g: -sum(c[0] for f in groups[g].values() for c in f.values()))
    for name in order:
        fams = groups[name]
        block = []
        for fam in sorted(fams, key=lambda f: ORDER.index(f) if f in ORDER else 99):
            cells = [(sd, c) for sd, c in sorted(fams[fam].items(), key=lambda kv: -kv[1][0]) if c[0] >= CELL_MIN]
            if not cells:
                continue
            parts = [f"{sd} {'⚠️ ' if c[0] >= 10 and c[1] / c[0] <= c[2] / c[0] - 0.10 else ''}{fmt.pct(c[1] / c[0])} / {fmt.pct(c[2] / c[0])} ({c[0]})"
                     for sd, c in cells]
            icon = fmt.market_icon({"market": calib.rep_market(fam)})
            block.append(fmt.esc(f"  {icon} {calib.LABELS.get(fam, fam).capitalize()} : " + " · ".join(parts)))
        if block:
            total = sum(c[0] for f in fams.values() for c in f.values())
            lines += ["", fmt.b(fmt.esc(name)) + fmt.esc(f" · {total}")] + block
    if len(lines) == 2:
        lines.append(fmt.esc("Aucun pronostic réglé ne correspond (10 derniers jours)."))
    return Html("\n".join(lines))
