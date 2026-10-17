"""Analyse d'un match, fiche d'équipe, prochains matchs : issus du découpage d'app.py (le code lit app via A.nom)."""

import re
import sys
from . import config as C, combos as CB, fmt, played, sources, stats, statswindow, strength, telegram, xprice
from .telegram import Html
from datetime import datetime, timedelta
from ._appref import A


def _pair(v, k, label, fmt="{:.1f}"):
    f, a = v.get(k + "_for"), v.get(k + "_against")
    return f"{label} {fmt.format(f)} pour / {fmt.format(a)} contre" if f is not None and a is not None else None


def _venues(src):
    """Lignes « À domicile / À l'extérieur » d'un profil (football-data ou API-Football)."""
    out = []
    for venue, label in (("home", "À domicile"), ("away", "À l'extérieur")):
        v = src.get(venue) or {}
        bits = [x for x in (A._pair(v, "corners", "corners", "{:.1f}"), A._pair(v, "cards", "cartons")) if x]
        if v.get("n") and bits:
            out.append(f"{label} ({v['n']} matchs) : " + " ; ".join(b.replace(" pour / ", "/").replace(" contre", "") for b in bits))
    return out


def _stat_block(allv, skip=()):
    return [x for k, x in (("goals", A._pair(allv, "goals", "• Buts :")), ("shots", A._pair(allv, "shots", "• Tirs :")),
                           ("sot", A._pair(allv, "sot", "• Tirs cadrés :")), ("corners", A._pair(allv, "corners", "• Corners :")),
                           ("cards", A._pair(allv, "cards", "• Cartons (jaunes + rouges) :")),
                           ("fouls", A._pair(allv, "fouls", "• Fautes :")), ("throws", A._pair(allv, "throws", "• Touches :")),
                           ("offsides", A._pair(allv, "offsides", "• Hors-jeu :")))
            if x and k not in skip]


def _xg_team_line(p):
    """Ligne « buts attendus » d'une équipe d'après les xG par match de ses profils (FotMob, puis API-Football : même priorité que
    features.match_features), avec sa source ; None sous XG_MIN_MATCHES matchs portant un xG. Sans le mot « xG »."""
    from .sources import features
    for src, label in ((p.get("fm"), "FotMob"), (p.get("aps"), "API-Football")):
        v = features._xg_view(src or {}, "all")
        if v:
            txt = (f"Buts attendus d'après les occasions ({label}, {v['xg_n']} derniers matchs) : "
                   f"{v['xg_for']:.2f} pour / {v['xg_against']:.2f} contre")
            for venue, name in (("home", "domicile"), ("away", "extérieur")):
                w = (src or {}).get(venue) or {}
                if w.get("xg_n", 0) >= C.XG_MIN_MATCHES and w.get("xg_for") is not None and w.get("xg_against") is not None:
                    txt += f" ; {name} {w['xg_for']:.2f}/{w['xg_against']:.2f}"
            return txt
    return None


def fmt_team(p, query):
    """Texte de /stats : profil d'une équipe (derniers matchs : football-data, Understat, et API-Football pour les championnats
    que football-data ne détaille pas)."""
    if not query:
        return "Exemple : /stats Arsenal"
    if not p:
        return (f"Aucune stat pour « {query} » (équipe inconnue ou sources pas encore actualisées : "
                f"elles le sont chaque matin à {C.DAY_END_HOUR:02d}h00).")
    if "many" in p:
        return f"Plusieurs équipes correspondent à « {query} » : {', '.join(p['many'])}. Précise le nom."
    league = A.LEAGUES.get(p["div"], p["div"]) if p.get("div") else None
    lines = [f"📈 {p['team']}" + (f" ({league})" if league else "")]
    fd = p.get("fd") or {}
    allv = fd.get("all") or {}
    shown = set()
    if allv.get("n"):
        lines += ["", f"📋 {allv['n']} derniers matchs (football-data), pour / contre :"]
        lines += A._stat_block(allv)
        lines += A._venues(fd)
        shown |= {k for k in ("corners", "cards", "shots", "sot", "fouls", "goals") if allv.get(k + "_for") is not None}
    # FotMob (stats de match) puis API-Football complètent sans répéter ce qui figure déjà
    aps = p.get("aps") or {}
    for src, label in ((p.get("fm") or {}, "FotMob"), (aps, "API-Football" + (f", {aps['lg']}" if aps.get("lg") else ""))):
        av = src.get("all") or {}
        block = A._stat_block(av, shown) if av.get("n") else []
        if block:
            lines += ["", f"📋 {av['n']} derniers matchs ({label}), pour / contre :"] + block
            shown |= {k for k in ("corners", "cards", "shots", "sot", "fouls", "goals", "throws", "offsides")
                      if av.get(k + "_for") is not None}
    us = p.get("us") or {}
    if (us.get("all") or {}).get("xg") is not None:
        u = us["all"]
        txt = f"xG (Understat, {u['n']} derniers) : {u['xg']:.2f} pour / {u.get('xga', 0):.2f} contre"
        for venue, label in (("home", "domicile"), ("away", "extérieur")):
            v = us.get(venue) or {}
            if v.get("xg") is not None and v.get("xga") is not None:
                txt += f" ; {label} {v['xg']:.2f}/{v['xga']:.2f}"
        lines += ["", txt]
    else:                                                       # pas d'Understat : buts attendus par match des profils FotMob / API-Football
        xl = A._xg_team_line(p)
        if xl:
            lines += ["", xl]
    if len(lines) == 1:
        lines.append("Pas assez de matchs récents dans les sources.")
    if p.get("updated"):
        lines += ["", f"(sources actualisées le {p['updated'][8:10]}/{p['updated'][5:7]})"]
    return "\n".join(lines)


def comma(text):
    """Décimales à la française : 2.5 -> 2,5 (hors identifiants et dates)."""
    return re.sub(r"(?<=\d)\.(?=\d)", ",", str(text))


def pm_note(c):
    pm = c.get("pm") or {}
    if pm.get("exp") is None or c.get("point") is None:
        return ""
    return f" (Kelly prévoit {fmt.num(pm['exp'], 1)} ; ligne à {fmt.trim(c['point'])})"


def referee_of(c):
    """Arbitre désigné du match du pronostic ou pari `c` (liste du jour d'API-Football déjà en cache : aucune requête) ; None s'il n'est pas (encore) connu."""
    if not (c.get("sport_key") or "").startswith("soccer") or not c.get("commence"):
        return None
    try:
        return stats.referee_for({"home_team": c["home"], "away_team": c["away"], "commence_time": c["commence"]})
    except Exception:                               # noqa: BLE001 - l'arbitre est facultatif
        return None


def referee_line(ref, *texts):
    """« 👨‍⚖️ Arbitre désigné : M. Oliver » quand l'arbitre est connu et qu'aucun des textes déjà affichés (analyse du modèle, chiffres d'avant-match) n'en parle ; '' sinon."""
    if not ref or any("Arbitre" in str(t or "") for t in texts):
        return ""
    return f"👨‍⚖️ Arbitre désigné : {fmt.esc(ref)}"


def deep_analysis(first, referee=None):
    """Analyse approfondie (modèle de force, contexte, grille de marchés) ou None si indisponible. `referee` : arbitre désigné (referee_of), dont l'effet sur les cartons est chiffré."""
    if not first.get("sport_key", "").startswith("soccer"):
        return None
    try:
        txt = strength.analysis_text(first["home"], first["away"], first["sport_key"], referee=referee)
    except Exception as e:                       # noqa: BLE001 - l'analyse ne doit jamais casser la commande
        print(f"analyse approfondie : {e}", file=sys.stderr)
        return None
    if not txt or "Pas assez de données" in txt[:300]:
        return None
    lines = txt.split("\n")
    return "\n".join(lines[1:] if lines[0].startswith("🔬") else lines)   # l'en-tête est déjà affiché


def features_lines(f, only=None, head="Chiffres d'avant-match"):
    """Lignes « corners, cartons, tirs, buts attendus » d'après sources.match_features ; `only` : clés exp_* à montrer."""
    n = lambda k: f.get(k)                                  # noqa: E731
    ok = lambda k: only is None or k in only                # noqa: E731

    def two(k, label, unit=""):
        t, h, a = n("exp_" + k), n(f"exp_{k}_home"), n(f"exp_{k}_away")
        if (t is None and h is None) or not (ok("exp_" + k) or ok(f"exp_{k}_home")):
            return None
        split = f"  {fmt.i(f'({h:.1f} + {a:.1f})')}" if h is not None and a is not None else ""
        return f"• {label} : {fmt.b(f'{t if t is not None else h + a:.1f}{unit}')}{split}"
    lines = ["", fmt.b(f"📐 {head}"), fmt.i(f"{fmt.esc(', '.join(f['sources']))} · {f.get('n_home', '?')} et "
                                          f"{f.get('n_away', '?')} derniers matchs"), ""]
    lines += [x for x in (two("goals", "Buts"), two("shots", "Tirs"), two("sot", "Tirs cadrés"), two("corners", "Corners"),
                          two("cards", "Cartons"), two("fouls", "Fautes"), two("throws", "Touches"),
                          two("offsides", "Hors-jeu")) if x]
    if only is None:
        if f.get("exp_xg_total") is not None:
            xg = f"{f['exp_xg_total']:.1f}"
            src = A.XG_SOURCES.get(f.get("xg_src"))
            lines.append(f"• Buts attendus d'après les occasions (xG) : {fmt.b(xg)}" + (f" {fmt.i('(' + src + ')')}" if src else ""))
        if f.get("ref_cards") is not None:
            lines.append(f"• Arbitre {fmt.esc(f['referee'])} : {f['ref_cards']:.1f} cartons par match")
    return lines if len(lines) > 4 else []


def event_matches(items, query):
    """Paris dont le match contient `query` : sans casse ni accents (« Atletico » trouve « Atlético Madrid »), puis, faute de mieux,
    par ressemblance avec le nom d'une équipe (comme la détection des équipes du texte libre, chat._team_command)."""
    q = played.norm(query)
    if not q:
        return []
    found = [c for c in items if q in played.norm(c["event"])]
    if found:
        return found
    from .sources import names
    return [c for c in items if any(t and names.team_sim(query, t) >= 0.8 for t in (c.get("home"), c.get("away")))]


def analyse(picks, query, pronos=(), when=None, picks_when=None):
    """Texte de /analyse : analyse approfondie d'un match du jour + ses meilleurs paris avec leur raison. `when` / `picks_when` : heures
    « HH:MM » des derniers relevés 1xBet des pronostics et des paris réels (affichées avec leurs cotes)."""
    if not query:
        return "Exemple : /analyse Arsenal"
    found = A.event_matches(picks, query)
    if not found:                               # match sans cotes réelles : analyse seule (+ pronostics)
        free = A.event_matches(pronos, query)
        events = list(dict.fromkeys(c["event"] for c in free))
        if not free:
            return f"Aucun match du jour ne contient « {query} »."
        if len(events) > 1:
            return "Plusieurs matchs correspondent, précise : " + " ; ".join(events[:8])
        ref = A.referee_of(free[0])
        deep = A.deep_analysis(free[0], ref)
        head = f"🔬 {fmt.b(fmt.esc(free[0]['event']))}\n{fmt.i(fmt.hhmm(free[0]['start']) + ' · ' + fmt.esc(free[0]['league']))}"
        fl = [] if deep else A.features_lines(sources.match_features(free[0]["home"], free[0]["away"], free[0]["sport_key"], **({"referee": ref} if ref else {})))
        body = ("\n" + fmt.esc(deep)) if deep else ("\n".join(fl) if fl else "\nPas assez de données pour une analyse approfondie de ce match.")
        rl = A.referee_line(ref, deep, "\n".join(fl))
        if rl:
            body += "\n" + rl
        shown, wait = CB.shown_pronos(free), "\n\n".join(x for x in (A.pending_lines(free), A.underpaid_lines(free)) if x)
        return Html(A.comma(head + "\n" + body) + "\n\n" + ("\n\n".join(([str(fmt.pronos(shown, 6, when=when))] if shown or not wait else []) + [wait])
                                                         if wait else str(fmt.pronos(shown, 6, when=when))))
    events = list(dict.fromkeys(c["event"] for c in found))
    if len(events) > 1:
        return "Plusieurs matchs correspondent, précise : " + " ; ".join(events[:8])
    first = found[0]
    lines = [f"🔬 {fmt.b(fmt.event_name(first))}", fmt.i(f"{fmt.hhmm(first['start'])} · {fmt.esc(first['league'])}")]
    ref = A.referee_of(first)
    deep = A.deep_analysis(first, ref)
    f = {"sources": []} if first["group"] != "soccer" else sources.match_features(first["home"], first["away"], first["sport_key"], **({"referee": ref} if ref else {}))
    shown_ = []
    if deep:
        lines += ["", fmt.esc(deep)]
        shown_.append(deep)
        if f.get("aps_keys"):                                   # le modèle approfondi n'a que les buts : API-Football complète
            lines += A.features_lines(f, only=set(f["aps_keys"]), head="Chiffres complémentaires")
    elif f["sources"]:
        lines += A.features_lines(f)
        shown_.append("\n".join(lines))
    else:
        lines.append("Pas de stats réelles pour ce match (équipes inconnues des sources ou sources indisponibles).")
    rl = A.referee_line(ref, *shown_)
    if rl:
        lines.append(rl)
    real = any(c.get("xodds") for c in picks)               # 1xBet a chiffré des paris du jour : les autres ne sont pas jouables
    best = sorted(found, key=lambda c: (bool(c.get("xodds")), c["score"]), reverse=True)[:6] if real else \
        sorted(found, key=lambda c: c["score"], reverse=True)[:6]
    lines += ["", fmt.b("🎯 Meilleurs paris")]
    for c in best:
        why = "" if real and not c.get("xodds") else fmt.why_line(c, CB.leg_reason(c))      # pas de « cote plus haute… » d'un autre book
        lines.append(f"\n👉 {fmt.esc(c['pick'])} — {fmt.price_txt(c, real, picks_when)} ({fmt.pct(c['prob'])} de chances)"
                     f"{'' if why else A.pm_note(c)}"
                     + (f"\n💡 {fmt.i(fmt.esc(why))}" if why else ""))
    shown = A._event_pronos(pronos, found)
    if shown:
        lines += ["", str(fmt.pronos(shown, 6, when=when))]
    mine_ = [c for c in pronos if c["id"] in {x["id"] for x in found}]
    wait = "\n\n".join(x for x in (A.pending_lines(mine_), A.underpaid_lines(mine_)) if x)
    if wait:
        lines += ["", wait]
    return Html(A.comma("\n".join(lines)))


def _pending(rows, now=None):
    """Pronostics stats (tirs, tirs cadrés, fautes, touches, hors-jeu) SOLIDES que 1xBet n'a pas encore chiffrés : il ouvre ces paris quelques heures
    avant le match, le relevé du matin les a vus fermés (c["xnone"] : « absent », qui veut dire ici « pas encore ouvert »). Mêmes règles que les
    pronostics montrés (combos.viable, xnone mis à part, et combos.fair_eligible) ; matchs qui ne sont pas sur le point de commencer. Le plus probable
    par marché, les plus probables d'abord."""
    now = now or A.now_utc()
    limit = now + timedelta(minutes=C.STATS_WINDOW_MIN_MIN - 15)          # 1xBet ferme ces paris ~30 min avant : plus la peine d'attendre
    best = {}
    for c in rows:
        if not statswindow.is_stat(c) or c.get("xodds") or not CB.fair_eligible(c):
            continue
        if not CB.viable({k: v for k, v in c.items() if k != "xnone"}):
            continue
        try:
            if datetime.fromisoformat(c["commence"].replace("Z", "+00:00")) <= limit:
                continue
        except (KeyError, ValueError):
            continue
        k = (c["id"], c["market"])
        if k not in best or c["prob"] > best[k]["prob"]:
            best[k] = c
    return sorted(best.values(), key=lambda c: -c["prob"])


def pending_lines(rows, now=None, limit=5):
    """Bloc « ⏳ Pas encore ouverts chez 1xBet » de /analyse et /match : les pronostics stats solides d'un match en attente de leur cote
    (_pending), avec la cote minimale à exiger et l'heure à partir de laquelle Kelly les relit (fenêtre des stats) ; '' s'il n'y en a pas."""
    now = now or A.now_utc()
    wait = A._pending(rows, now)[:limit]
    if not wait:
        return ""
    lines = [fmt.b("⏳ Pas encore ouverts chez 1xBet"),
             fmt.i(f"Kelly les surveille (toutes les {C.STATS_WINDOW_FAR_RETRY_MIN // 60} h, puis chaque heure dans les "
                   f"{fmt.trim(C.STATS_WINDOW_NEAR_H, 1)} dernières heures) et te prévient.")]
    lines += [f"• {fmt.esc(c['pick'])} · Kelly {fmt.pct(c['prob'])} · joue dès {fmt.odds_txt(c['min_odds'])}" for c in wait]
    return "\n".join(lines)


def reread_match(pronos, query, now=None):
    """/analyse et /match : si le match demandé est dans la fenêtre des stats et qu'un de ses pronostics stats solides attend sa cote (_pending), ses
    paris sont relus EN DIRECT chez 1xBet (xprice.live_check, 2 matchs au plus) : un pari qui vient d'ouvrir est montré à sa cote. -> les pronostics
    du jour (relus ou non). Ne lève jamais."""
    now = now or A.now_utc()
    if not query or not C.XBET_ENABLED:
        return pronos
    ids = []
    for c in A._pending(A.event_matches(pronos, query), now):
        if statswindow.in_window(c, now) and c["id"] not in ids:
            ids.append(c["id"])
    if not ids or len(ids) > 2:
        return pronos
    try:
        if not statswindow.month_ok():
            return pronos
        telegram.send("⏳ Je regarde chez 1xBet si les paris tirs / fautes de ce match viennent d'ouvrir…", queue=False, silent=True)
        return xprice.live_check(set(ids), now, fresh_min=C.STATS_WINDOW_FRESH_MIN)[0]
    except Exception as e:                          # noqa: BLE001 - la réponse part quand même, avec les cotes déjà connues
        print(f"[1xbet] relecture du match impossible : {type(e).__name__}: {e}", file=sys.stderr)
        return pronos


def _event_pronos(pronos, found):
    """Pronostics 💎 (cote 1xBet relevée) des matchs de `found` : de quoi montrer leurs prix 1xBet à côté des paris réels."""
    ids = {c["id"] for c in found}
    return CB.shown_pronos([c for c in pronos if c["id"] in ids and c.get("xodds")])


def top_reply(live, live_pronos, when=None, picks_when=None):
    """/top : les meilleurs paris réels encore jouables (cotes 1xBet de `picks_when` quand 1xBet les a chiffrés ; sinon cotes de référence
    des autres bookmakers, dites comme telles) ET les pronostics 💎 aux cotes 1xBet (`when`) des autres matchs. Un pari absent de
    1xBet n'est pas montré comme jouable (CB.make_pool)."""
    pool = CB.make_pool(live) if live else []
    keep = {c["key"] for c in CB.per_event(pool, k=1)} | {c["key"] for c in pool if CB.value_leg(c)}      # le meilleur pari de chaque match + TOUTES les jambes à valeur
    best = sorted((c for c in pool if c["key"] in keep), key=lambda c: c["score"], reverse=True)[:10]
    shown = CB.shown_pronos(live_pronos)
    if best:                                        # avec des paris réels : seulement les 💎 d'un autre marché ou d'un autre match (jamais deux fois le même pari)
        slots = {A._slot(c) for c in best}
        shown = [c for c in shown if c.get("xodds") and A._slot(c) not in slots]
    parts = []
    if best:
        priced = all(c.get("xodds") for c in best)
        note = (fmt.i(f"🕒 Cotes 1xBet de {fmt.esc(picks_when)} : vérifie-les avant de valider.") if priced and picks_when
                else fmt.i("🕒 Cotes 1xBet : vérifie-les avant de valider.") if priced
                else fmt.i("⚠️ Cotes de référence d'autres bookmakers, pas celles de 1xBet : vérifie chaque cote chez 1xBet."))
        parts.append(Html(fmt.b("🔝 Les meilleurs paris du jour") + "\n" + note + "\n\n" + "\n".join(
            fmt.leg(k, c, CB.leg_reason(c), price=fmt.price_txt(c)) for k, c in enumerate(best, 1))))
    if shown:
        parts.append(fmt.pronos(shown, 10 if not best else 6, when=when))
    if not parts:
        return ("Aucun pari de qualité aujourd'hui : rien qui ait un avantage clair, une vraie raison de statistiques "
                "ou une forte probabilité. /match <équipe> montre tout ce qui existe pour un match.")
    out = parts[0] if len(parts) == 1 else Html("\n\n".join(parts))
    return telegram.with_buttons(out, telegram.inline([[("🔄 Mettre à jour les cotes", "maj")]]))       # l'ancien bouton du menu, là où il sert


def upcoming(items, now=None):
    """Paris dont le match n'a pas encore commencé (on ne peut plus jouer les autres)."""
    t = (now or A.now_utc()).isoformat()[:16]
    return [c for c in items if (c.get("commence") or "9")[:16] > t]


def waiting_note(today, now=None):
    """Entre minuit et le passage du matin, l'analyse en mémoire est celle de la veille (matchs commencés ou finis) : le dire, avec
    l'heure de la prochaine analyse, au lieu de « rien aujourd'hui ». '' si l'analyse est celle du jour."""
    now = now or A.now_utc()
    if not today.get("day") or today["day"] >= str(now.date()):
        return ""
    return (f"⏳ Les matchs d'aujourd'hui ({fmt.fr_day(now.date())}) seront analysés au passage de "
            f"{C.DAY_END_HOUR:02d}h00 (heure d'Abidjan). Pour l'instant Kelly n'a que l'analyse d'hier, dont les matchs ont commencé.")
