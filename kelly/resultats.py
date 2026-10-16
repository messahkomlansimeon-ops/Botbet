"""Résultats des combinés, en deux vues séparées : `combis` (les combinés de Kelly : du matin, à cote juste, sur mesure ; la voie saison à part) et `saison` (la voie « saison en cours » : ses combinés
ss3..ss100, ses jambes et son CLV, championnats et coupes). Lecture seule : tout vient de bets.json, fair_combos.json et des lignes de bilan par voie (lanestats). Ne lève jamais."""
import sys

from . import combos as CB, fair, fmt, lanestats, money, store

LAST = 12                                        # combinés listés au plus (les plus récents)


def _cote(x):
    o = x.get("odds_final") or x.get("min_odds") or x.get("xodds") or x.get("odds")
    return fmt.trim(o, 1) if isinstance(o, (int, float)) and not isinstance(o, bool) else "?"


def _line(x):
    icon = "✅" if x["result"] == "win" else "❌"
    g = money.gain(x)
    name = lanestats._tier_name(x)
    return f"{icon} {x.get('date', '?')} · {name} · cote {_cote(x)}" + (f" · {money.signed(round(g))}" if g is not None else "")


def _rows(season, days):
    """Combinés RÉGLÉS (gagnés / perdus), de la voie saison (`season`) ou des autres, sur `days` jours (tout sinon), du plus récent au plus ancien."""
    bets = store.load_doc("bets.json", {"combos": []})
    cut = lanestats._since(days, None)
    out = [x for x in list(bets.get("combos") or []) + list(fair.load_combos())
           if isinstance(x, dict) and x.get("result") in ("win", "loss") and not x.get("skipped") and (x.get("date") or "") >= cut
           and (CB.season_no(x.get("tier")) is not None) == season]
    return sorted(out, key=lambda x: x.get("date") or "", reverse=True)


def _block(rows, empty):
    if not rows:
        return [empty]
    w = sum(1 for x in rows if x["result"] == "win")
    done = [(x, money.gain(x)) for x in rows]
    noted = [g for _, g in done if g is not None]
    out = [f"{len(rows)} réglé{'s' if len(rows) > 1 else ''} : {w} gagné{'s' if w > 1 else ''} ({round(w / len(rows) * 100)} %)"
           + (f" · tes paris notés ({len(noted)}) : {money.signed(round(sum(noted)))}" if noted else " · aucun noté : pas de résultat en argent")]
    out += [_line(x) for x in rows[:LAST]]
    if len(rows) > LAST:
        out.append(f"… et {len(rows) - LAST} plus ancien{'s' if len(rows) - LAST > 1 else ''}")
    return out


def _safe(fn, label):
    try:
        return fn()
    except Exception as e:                       # noqa: BLE001
        print(f"[résultats] {label} illisible : {type(e).__name__}: {e}", file=sys.stderr)
        return None


def combis(days=None):
    """Vue « combis » : tes combinés (hors voie saison) réglés, avec le résultat en argent de ceux que tu as notés."""
    rows = _safe(lambda: _rows(False, days), "combinés")
    head = f"🎯 Résultats des combinés{f' · {days} derniers jours' if days else ''}"
    body = ["Lecture impossible pour l'instant (voir /erreurs)."] if rows is None else _block(rows, "Aucun combiné réglé pour l'instant : ils sont réglés le lendemain matin.")
    return fmt.Html(fmt.b(fmt.esc(head)) + "\n\n" + "\n".join(fmt.esc(x) for x in body) + "\n\n" + fmt.i("Voie saison : écris « saison résultats ». Détail technique : « bilan détail »."))


def saison(days=None):
    """Vue « saison » : combinés ss.. réglés, jambes (valeur / remplissage), CLV 1xBet, argent par tranche de cote, championnats puis coupes."""
    rows = _safe(lambda: _rows(True, days), "combinés saison")
    head = f"🌱 Résultats de la voie saison{f' · {days} derniers jours' if days else ''}"
    blk = None if rows is None else _block(rows, "aucun combiné réglé pour l'instant.")
    body = ["Combinés saison : lecture impossible pour l'instant (voir /erreurs)."] if blk is None else ["Combinés saison — " + blk[0]] + blk[1:]
    lines = []
    for cup in (False, True):
        d = _safe(lambda: lanestats.season_detail(days, None, plain=True, cup=cup), "détail saison")
        if d:
            lines.append(("Coupes" if cup else "Championnats") + " :\n• " + d.removeprefix(" · ").replace(" · ", "\n• "))
    if not lines:
        lines = ["Jambes de la voie : rien de réglé pour l'instant."]
    return fmt.Html(fmt.b(fmt.esc(head)) + "\n\n" + "\n".join(fmt.esc(x) for x in body) + "\n\n" + "\n\n".join(fmt.esc(x) for x in lines)
                    + "\n\n" + fmt.i("Où elle bat le dernier prix 1xBet, case par case : « carte saison »."))
