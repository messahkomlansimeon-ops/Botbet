"""Étape 0a de l'audit : ce que valent le modèle et le CLV, en LECTURE SEULE sur une COPIE des données de Kelly.

    python tools/audit_clv.py --data-dir /chemin/vers/un/dossier/avec/kelly.db

Ne touche jamais au dossier donné : seul `kelly.db` en est copié dans un dossier temporaire, puis ouvert en lecture seule (aucun réseau, aucune
écriture dans data/, aucun accès à .env ni au cache des sources). Lit tracked.json (paris suivis, envoyés et fantômes) et ensemble_log.json
(les avis du modèle, de 1xBet et du sharp pour chaque pronostic). Affiche :
  A. ce que mesure le CLV d'aujourd'hui (quel prix contre quelle clôture) ;
  B. le CLV, avec sa marge d'erreur, par origine, par clôture, par marché et par heure ;
  C. le retard de 1xBet : les paris « sharpgap » (1xBet paie plus que la cote juste des books de référence) à part des autres ;
  D. le modèle contre le marché seul : log-loss du modèle, de 1xBet, du sharp et de l'ensemble, avec le poids du modèle comme variable
     (0 %, 12 %, 25 %), par famille de marché ; la ligne de base est le MARCHÉ SEUL, pas « contexte éteint » ;
  E. le rythme réel de paris réglés et le temps qu'il faut pour conclure ;
  G. les marchés de stats : CLV contre le dernier prix 1xBet et couverture de ce prix (règle des 70 %) ;
  F. ce que ce rapport peut et ne peut pas conclure.
Chaque moyenne est donnée avec n et une marge d'erreur à 95 % : en dessous de MIN_N lignes elle est marquée « trop peu pour conclure »."""
import argparse
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def isolate_env():
    """À appeler (par main) AVANT le premier import de kelly : jamais d'envoi ni de proxy, kelly.store ne touche jamais au vrai data/ ni au vrai cache.
    Pas à l'import du module : d'autres outils (audit_replay) l'importent et lisent, eux, le cache des sources."""
    for k in ("TELEGRAM_TOKEN", "XBET_PROXY", "S365_PROXY"):
        os.environ.pop(k, None)
    os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-audit-data-")
    os.environ["SOURCES_CACHE_DIR"] = tempfile.mkdtemp(prefix="kelly-audit-cache-")

MIN_N = 30                    # lignes au minimum pour qu'une moyenne soit lue comme une mesure
EPS = 1e-4
WEIGHTS = (0.0, 0.12, 0.25)   # poids du modèle testés (0 = le marché seul)
TARGET_N = 1300               # paris de valeur réglés qu'il faut au minimum (audit du 13/10, bas de la fourchette 1 300 à 2 600)


def load(data_dir):
    """(paris suivis, journal d'ensemble) d'une copie de kelly.db ; ([], []) si absent."""
    src = os.path.join(data_dir, "kelly.db")
    if not os.path.exists(src):
        return [], []
    tmp = tempfile.mkdtemp(prefix="kelly-audit-")
    dst = os.path.join(tmp, "kelly.db")
    shutil.copy(src, dst)
    con = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    try:
        out = []
        for name in ("tracked.json", "ensemble_log.json"):
            row = con.execute("select body from docs where name=?", (name,)).fetchone()
            doc = json.loads(row[0]) if row else []
            out.append(doc if isinstance(doc, list) else [])
        seen = {r.get("key") for r in out[1]}
        try:
            from kelly import ensemble                       # archive compacte (90 jours) : les lignes réglées que le journal a déjà oubliées
            for nm, body in con.execute("select name, body from docs where name like 'arch/ens/%' order by name"):
                doc = json.loads(body)
                day = nm[len("arch/ens/"):]
                for k, v in (doc.get("rows") or {}).items():
                    if k not in seen:                          # le journal, plus complet, l'emporte
                        out[1].append(ensemble.unpack_archive_row(k, day, v))
        except Exception as e:                                 # noqa: BLE001 - l'audit marche aussi sans archive
            print(f"[audit] archive non lue : {type(e).__name__}: {e}", file=sys.stderr)
        return out[0], out[1]
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)


def links(data_dir):
    """{"played": clés des paris de combinés JOUÉS ou de lignes suivies jouées, "sent": clés de TOUT combiné envoyé (fair_combos, bets)} d'une copie de kelly.db.
    Sert à séparer, au journal d'ensemble, les lignes réelles des fantômes."""
    out = {"played": set(), "sent": set()}
    src = os.path.join(data_dir, "kelly.db")
    if not os.path.exists(src):
        return out
    tmp = tempfile.mkdtemp(prefix="kelly-audit-")
    dst = os.path.join(tmp, "kelly.db")
    shutil.copy(src, dst)
    con = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    try:
        def doc(name):
            row = con.execute("select body from docs where name=?", (name,)).fetchone()
            return json.loads(row[0]) if row else None
        for c in (doc("fair_combos.json") or []):
            if isinstance(c, dict):
                out["sent"].update(c.get("keys") or [])
                if (c.get("played") or {}).get("odds"):
                    out["played"].update(c.get("keys") or [])
        for c in ((doc("bets.json") or {}).get("combos") or []):
            if isinstance(c, dict):
                out["sent"].update(c.get("keys") or [])
        for r in (doc("tracked.json") or []):
            if isinstance(r, dict) and r.get("played"):
                out["played"].add(r.get("key"))
        return out
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)


def stats(xs, groups=None):
    """(n, moyenne, demi-marge à 95 %) ; (0, None, None) sans valeur. Avec `groups` (un identifiant de match par valeur), la marge tient compte de la
    corrélation entre lignes d'un même match (erreur-type par grappes) : sans cela, 7 lignes d'un match compteraient comme 7 preuves."""
    pairs = [(x, (groups[i] if groups is not None else i)) for i, x in enumerate(xs) if x is not None]
    n = len(pairs)
    if not n:
        return 0, None, None
    m = sum(x for x, _ in pairs) / n
    clusters = defaultdict(float)
    for x, g in pairs:
        clusters[g] += x - m
    k = len(clusters)
    if k < 2:
        return n, m, None
    se = math.sqrt(k / (k - 1) * sum(v * v for v in clusters.values())) / n
    return n, m, 1.96 * se


def fmt_stats(xs, groups=None, pct=True, digits=1):
    n, m, h = stats(xs, groups)
    if n == 0:
        return "n=0"
    k = 100 if pct else 1
    txt = f"n={n:<4} moy {m * k:+.{digits}f}{' %' if pct else ''}"
    if h is not None:
        txt += f" ± {h * k:.{digits}f}"
    return txt + ("" if n >= MIN_N else "   (trop peu pour conclure)")


def clamp(p):
    return min(1 - EPS, max(EPS, p))


def logit(p):
    p = clamp(p)
    return math.log(p / (1 - p))


def expit(x):
    return 1 / (1 + math.exp(-x))


def logloss(p, y):
    p = clamp(p)
    return -(math.log(p) if y else math.log(1 - p))


def close_kind(r):
    c = str(r.get("closed_at") or "")
    return "non clôturé" if not c else "ratée" if c.startswith("miss") else "gratuite (football-data/BetExplorer)" if c.startswith("free") else "relevée (The Odds API)"


def hour_of(r):
    try:
        return datetime.fromisoformat(str(r["taken_at"]).replace("Z", "+00:00")).strftime("%Hh")
    except (KeyError, ValueError):
        return "?"


def section_a(rows):
    n = len(rows)
    out = ["A. CE QUE MESURE LE CLV AUJOURD'HUI",
           f"   {n} lignes suivies ({sum(1 for r in rows if r.get('sent'))} envoyées, {sum(1 for r in rows if not r.get('sent'))} fantômes), "
           f"{sum(1 for r in rows if r.get('clv') is not None)} avec un CLV, {sum(1 for r in rows if r.get('result') in ('win', 'loss'))} réglées.",
           "   clv = log(cote typique prise / cote typique de clôture), les deux lues chez The Odds API (région eu) : la « cote typique » est le 75e",
           "   centile des books (odds.typical_price), ni la meilleure cote ni Pinnacle seul. Ce n'est PAS ton prix 1xBet (r['xodds'], à part).",
           "   La clôture « gratuite » est, elle, une proba démarginée (Shin) de football-data / BetExplorer (Pinnacle sinon moyenne) : autre mesure.",
           f"   Lignes avec une cote 1xBet : {sum(1 for r in rows if r.get('xodds'))} ; avec la proba des books de référence : "
           f"{sum(1 for r in rows if r.get('prob_book'))} ; avec une clôture sharp explicite (gap_edge_close) : "
           f"{sum(1 for r in rows if r.get('gap_edge_close') is not None)}. Aucune clôture 1xBet n'est enregistrée.",
           "   Ligne par type de clôture : " + ", ".join(f"{k} {v}" for k, v in sorted(_count(close_kind(r) for r in rows).items()))]
    return out


def _count(it):
    d = defaultdict(int)
    for x in it:
        d[x] += 1
    return d


def _group(rows, key):
    g = defaultdict(list)
    for r in rows:
        g[key(r)].append(r)
    return g


def section_b(rows):
    cl = [r for r in rows if r.get("clv") is not None]
    out = ["B. CLV (log du prix pris sur la clôture ; positif = on a pris mieux que la clôture)", f"   Toutes lignes : {fmt_stats([r['clv'] for r in cl], [r.get('id') for r in cl])}   (marge par match, pas par ligne)"]
    for title, key in (("origine", lambda r: "envoyé" if r.get("sent") else "fantôme"), ("clôture", close_kind),
                       ("marché", lambda r: r.get("market") or "?"), ("heure où Kelly a pris la ligne (UTC, champ taken_at)", hour_of),
                       ("écart 1xBet/sharp", lambda r: "sharpgap" if r.get("sharpgap") else "sans sharpgap")):
        out.append(f"   Par {title} :")
        for k, g in sorted(_group(cl, key).items()):
            out.append(f"     {k:<38} {fmt_stats([r['clv'] for r in g], [r.get('id') for r in g])}")
    out.append("   Note : taken_at est l'heure où Kelly a relevé le prix ; l'heure où tu as réellement placé le pari n'existe que pour les paris notés "
               "(played.at).")
    return out


def section_c(rows):
    gap = [r for r in rows if r.get("sharpgap")]
    rest = [r for r in rows if not r.get("sharpgap") and r.get("clv") is not None]
    out = ["C. LE RETARD DE 1xBET SUR LES BOOKS DE RÉFÉRENCE (paris « sharpgap »)",
           f"   {len(gap)} paris sharpgap, dont {sum(1 for r in gap if r.get('result') in ('win', 'loss'))} réglés ; {len(rest)} autres lignes avec CLV."]
    out.append(f"   CLV marché des paris sharpgap          : {fmt_stats([r['clv'] for r in gap if r.get('clv') is not None])}")
    out.append(f"   Écart 1xBet vs clôture sharp (edge)    : {fmt_stats([r['gap_edge_close'] for r in gap if r.get('gap_edge_close') is not None])}")
    out.append(f"   CLV marché des autres lignes (modèle)  : {fmt_stats([r['clv'] for r in rest])}")
    for r in gap:
        g = r.get("gap") or {}
        out.append(f"     - {r.get('event')} / {r.get('pick')} : prix marché {r.get('odds')}, 1xBet {r.get('xodds')}, edge à la prise {g.get('edge')}, "
                   f"edge contre la clôture {r.get('gap_edge_close')} ({r.get('gap_close_src')}), résultat {r.get('result')}")
    xc = [r for r in rows if isinstance(r.get("xclose"), dict) and r["xclose"].get("odds") and r.get("xodds")]
    fresh = [r for r in xc if (r["xclose"].get("lead_min") is not None and r["xclose"]["lead_min"] <= LEAD_OK_MIN and not r.get("xgone"))]    # ≤ 90 min AVANT LE MATCH (comme la section G), pas l'âge du relevé
    out.append(f"   Prix 1xBet de clôture (champ xclose, lu dans le cache, collecté seulement depuis l'Étape 0b) : {len(xc)} lignes, dont {len(fresh)} avec un relevé "
               "de moins de 90 min avant la clôture.")
    if xc:
        out.append(f"   CLV 1xBet (prix 1xBet pris / prix 1xBet de clôture), relevés ≤ 90 min : {fmt_stats([math.log(r['xodds'] / r['xclose']['odds']) for r in fresh], [r.get('id') for r in fresh])}")
    src = _count(str(r.get("prob_src")) for r in rows)
    out.append("   Avis des books à la prise (champ prob_src) : " + ", ".join(f"{k} {v}" for k, v in sorted(src.items())) +
               "   (« None » = ligne écrite avant que ce champ existe, ou sans avis)")
    out.append("   Lecture : si le gain vient de ces lignes, il vient du retard de 1xBet, pas du modèle. Battre la clôture 1xBet sans battre Pinnacle = 1xBet a")
    out.append("   ajusté tard, pas un pari où le modèle avait raison.")
    return out


def family_of(market):
    try:
        from kelly import ensemble
        return ensemble.family(market)
    except Exception as e:                                                # noqa: BLE001 - le rapport ne doit pas tomber pour un import
        print(f"[audit] ensemble.family indisponible ({type(e).__name__}) : familles regroupées en gros (buts / stats)", file=sys.stderr)
        return "stats" if any(s in str(market) for s in ("corner", "card", "shot", "sot", "foul", "throw", "offside", "yellow")) else "buts"


def pool(pm, mk, wm):
    """Mise en commun en logit : wm × modèle + (1 - wm) × marché (comme l'ensemble)."""
    return expit(wm * logit(pm) + (1 - wm) * logit(mk))


MIN_MATCHES = 30              # MATCHS distincts au minimum pour qu'un verdict soit rendu (les lignes d'un match sont corrélées)


def compare(g, mk, out, indent="     "):
    """Écart de log-loss de « marché + wm × modèle » au marché seul, pour les lignes `g` (marché `mk`) ; renvoie le nombre de comparaisons faites."""
    ys = [r["result"] == "win" for r in g]
    ll_mkt = [logloss(m, y) for m, y in zip(mk, ys)]
    matches = len({r.get("id") for r in g})
    out.append(f"{indent}marché seul : {sum(ll_mkt) / len(g):.4f}   modèle seul : {sum(logloss(r['pm'], y) for r, y in zip(g, ys)) / len(g):.4f}   "
               f"({len(g)} lignes, {matches} matchs)")
    done = 0
    for wm in WEIGHTS:
        if wm == 0:
            continue
        d = [logloss(pool(r['pm'], m, wm), y) - lm for r, m, y, lm in zip(g, mk, ys, ll_mkt)]
        n, mean, h = stats(d, [r.get("id") for r in g])
        done += 1
        verdict = ("   (moins de %d matchs : pas de verdict)" % MIN_MATCHES if matches < MIN_MATCHES else "" if h is None else
                   "   <- piste : le modèle AIDE" if mean + h < 0 else "   <- piste : le modèle NUIT" if mean - h > 0 else "")
        out.append(f"{indent}marché + {int(wm * 100):>2} % modèle : écart {mean:+.4f}" + (f" ± {h:.4f}" if h is not None else "") + verdict)
    return done


def section_d(log):
    lines = [r for r in log if r.get("result") in ("win", "loss") and r.get("pm") is not None and (r.get("ps") is not None or r.get("px") is not None)]
    out = ["D. LE MODÈLE CONTRE LE MARCHÉ SEUL (journal d'ensemble ; log-loss, plus bas = mieux)",
           f"   {len(lines)} lignes réglées ({len({r.get('id') for r in lines})} matchs) avec le modèle et un avis de marché.",
           "   Le « marché » est le sharp (Pinnacle…) quand la ligne le porte, SINON le prix 1xBet démarginé : battre 1xBet démarginé est une preuve bien",
           "   plus faible que battre Pinnacle. Chaque famille donne la part de chaque référence, puis le sous-ensemble sharp seul.",
           "   Marges d'erreur PAR MATCH ; verdict seulement à partir de %d matchs ; le poids du modèle est simulé par mise en commun en logit, comme" % MIN_MATCHES,
           "   l'ensemble (les poids réellement appris par famille ne sont pas rejoués ici)."]
    total = 0
    for fam, g in sorted(_group(lines, lambda r: family_of(r.get("market"))).items(), key=lambda kv: -len(kv[1])):
        mk = [r["ps"] if r.get("ps") is not None else r["px"] for r in g]
        sharp = [r for r in g if r.get("ps") is not None]
        out.append(f"   Famille {fam} : {len(g)} lignes ; marché = sharp pour {len(sharp)}, 1xBet démarginé pour {len(g) - len(sharp)}")
        total += compare(g, mk, out)
        if len(sharp) >= 10 and len(sharp) < len(g):
            out.append("     sous-ensemble sharp seul :")
            total += compare(sharp, [r["ps"] for r in sharp], out, "       ")
    out.append(f"   {total} comparaisons faites : à 95 %, ≈ {total * 0.05:.1f} verdict(s) « piste » peuvent sortir du seul hasard. Une piste n'est pas un résultat :")
    out.append("   elle doit se retrouver sur une saison réservée avant toute décision (règle fixée dans le plan). Ces lignes sont celles que Kelly a")
    out.append("   retenues ou regardées : biais de sélection possible, et la fenêtre est très courte.")
    return out


def _pace(label, rs):
    days = sorted({r.get("date") for r in rs if r.get("date")})
    if not rs:
        return f"   {label} : aucune ligne réglée."
    per = len(rs) / max(1, len(days))
    return (f"   {label} : {len(rs)} réglées sur {len(days)} jour(s) = {per:.1f} par jour ; {TARGET_N} demanderaient ≈ {TARGET_N / per:.0f} jours "
            f"({TARGET_N / per / 30:.1f} mois) à ce rythme.")


def section_e(rows, log):
    settled = [r for r in rows if r.get("result") in ("win", "loss")]
    out = ["E. RYTHME RÉEL (ce qui compte pour la significativité, ce sont les PARIS DE VALEUR, pas les candidats suivis)",
           _pace("Candidats suivis (envoyés + fantômes)", settled),
           _pace("Paris de valeur envoyés (sent)", [r for r in settled if r.get("sent")]),
           _pace("Paris réellement notés par toi (played)", [r for r in settled if r.get("played")]),
           _pace("Paris sharpgap (1xBet en retard)", [r for r in settled if r.get("sharpgap")])]
    ldays = sorted({r.get("date") for r in log if r.get("result") in ("win", "loss") and r.get("date")})
    lset = [r for r in log if r.get("result") in ("win", "loss")]
    if ldays:
        out.append(f"   Journal d'ensemble (pas des paris : tous les pronostics regardés) : {len(lset)} lignes réglées sur {len(ldays)} jours, mais "
                   f"{len({r.get('id') for r in lset})} MATCHS distincts seulement (les lignes d'un match sont corrélées).")
    out.append(f"   Seuil de référence : {TARGET_N} paris de valeur réglés au minimum (audit du 13/10, bas de la fourchette 1 300 à 2 600).")
    return out


STATS_PREFIXES = ("team_shots", "team_sot", "team_fouls", "team_throws", "team_offsides", "team_corners", "alternate_totals_corners", "alternate_totals_cards",
                  "team_yellow", "team_cards")
LEAD_OK_MIN = 90                  # un relevé 1xBet ≤ 90 min avant le coup d'envoi compte comme « clôture » (décision 3 : seuil de couverture 70 %)
COVERAGE_MIN = 0.70


def is_stat(market):
    return str(market or "").startswith(STATS_PREFIXES)


def _kind(r, lk):
    k = r.get("key")
    return "joué" if k in lk["played"] else "dans un combiné envoyé (non joué)" if k in lk["sent"] else "fantôme"


def _ko_hour(r):
    try:
        return datetime.fromisoformat(str(r["commence"]).replace("Z", "+00:00")).strftime("%Hh")
    except (KeyError, ValueError):
        return "?"


def _cover(rs):
    ok = [r for r in rs if isinstance(r.get("xclose"), dict) and r["xclose"].get("odds") and r["xclose"].get("lead_min") is not None
          and r["xclose"]["lead_min"] <= LEAD_OK_MIN]
    return f"{len(ok)}/{len(rs)} = {len(ok) / len(rs):.0%}" if rs else "0/0"


def section_g(log, lk=None):
    """CLV 1xBet des marchés de stats (journal d'ensemble) et couverture du prix de clôture. Pas de prix sharp sur ces marchés."""
    lk = lk or {"played": set(), "sent": set()}
    rows = [r for r in log if is_stat(r.get("market"))]
    withref = [r for r in rows if r.get("xref") and r.get("commence")]
    good = [r for r in withref if isinstance(r.get("xclose"), dict) and r["xclose"].get("odds") and r["xclose"].get("lead_min") is not None
            and r["xclose"]["lead_min"] <= LEAD_OK_MIN]
    out = ["G. MARCHÉS DE STATS : CLV CONTRE 1xBET (aucun prix sharp n'existe sur ces marchés ; la seule référence est le dernier prix 1xBet)",
           f"   {len(rows)} lignes de stats au journal, dont {len(withref)} avec la référence 1xBet (collectée depuis l'Étape 0b)."]
    cover = len(good) / len(withref) if withref else 0.0
    out.append(f"   Couverture globale : {len(good)}/{len(withref)} lignes avec un relevé 1xBet ≤ {LEAD_OK_MIN} min avant le coup d'envoi = {cover:.0%} "
               f"(seuil {COVERAGE_MIN:.0%}) : " + ("aucune ligne avec référence encore : rien à décider." if not withref else
               "SOUS le seuil : relever réellement le prix 1xBet à la clôture (décision 3 : lignes portant un pari réel ou fantôme, un relevé par match dans les 90 min "
               "avant le coup d'envoi, plafond 40 requêtes/jour, interrupteur de config, à chiffrer avec toi avant activation)." if cover < COVERAGE_MIN
               else "AU-DESSUS du seuil : le cache suffit."))
    out.append("   Rappel : 1xBet ferme les sous-matchs de stats ≈ 30 min avant le match et Kelly ne les relit plus à moins de 45 min : « le dernier prix » est au mieux")
    out.append("   un prix de 45 à 105 min avant le coup d'envoi, pas une vraie clôture ; une couverture basse peut venir de l'HORAIRE (famille relue seulement le matin,")
    out.append("   match de nuit) autant que d'un défaut : lire la couverture par famille et par heure avant de décider d'un relevé réseau.")
    if withref:
        out.append("   Couverture par famille : " + " ; ".join(f"{k} {_cover(g)}" for k, g in sorted(_group(withref, lambda r: family_of(r.get('market'))).items())))
        out.append("   Couverture par heure de coup d'envoi (UTC) : " + " ; ".join(f"{k} {_cover(g)}" for k, g in sorted(_group(withref, _ko_hour).items())))
    out.append("   CLV 1xBet (prix pris / dernier prix 1xBet ≤ 90 min avant le match), marges PAR MATCH ; la décision d'adopter l'essai des tirs lira la ligne « joué » :")
    for kind in ("joué", "dans un combiné envoyé (non joué)", "fantôme"):
        g = [r for r in good if _kind(r, lk) == kind and r.get("xodds")]
        out.append(f"     {kind:<36} {fmt_stats([math.log(r['xodds'] / r['xclose']['odds']) for r in g], [r.get('id') for r in g])}")
    for lane in ("stats", "fill", "sharpgap", None):                                  # par voie (étiquette posée à la construction ; None = lignes d'avant les étiquettes)
        g = [r for r in good if r.get("xodds") and r.get("strategy") == lane]
        if g:
            out.append(f"     voie {lane or 'avant étiquettes':<31} {fmt_stats([math.log(r['xodds'] / r['xclose']['odds']) for r in g], [r.get('id') for r in g])}")
    for k, g in sorted(_group([r for r in good if r.get("xodds")], lambda r: family_of(r.get("market"))).items(), key=lambda kv: -len(kv[1])):
        out.append(f"     famille {k:<28} {fmt_stats([math.log(r['xodds'] / r['xclose']['odds']) for r in g], [r.get('id') for r in g])}")
    if not good:
        out.append("     Aucune ligne avec un relevé ≤ 90 min : aucun CLV 1xBet à rapporter.")
    gone = [r for r in withref if r.get("xgone")]
    if gone:
        out.append(f"   {len(gone)} lignes dont le pari a été retiré par 1xBet avant le match (marché fermé, pas un prix de clôture).")
    return out


def section_f():
    return ["F. CE QUE CE RAPPORT PEUT ET NE PEUT PAS CONCLURE",
            "   - Il décrit une fenêtre courte et des lignes que Kelly a choisies : aucune conclusion d'avantage durable.",
            "   - Le CLV mesuré est celui du marché The Odds API, pas de ton prix 1xBet ; il ne dit rien du prix que tu as obtenu.",
            "   - Le modèle ne peut être jugé contre un marché que sur les lignes du journal (1xBet/sharp) ; l'historique football-data (étape suivante) "
            "ne couvre que le 1N2 et le plus/moins 2,5.",
            "   - Les familles corners, cartons, tirs ne peuvent être jugées qu'ici (fantômes) et par calibration contre les résultats réels."]


def report(rows, log, lk=None):
    return "\n\n".join("\n".join(s) for s in (section_a(rows), section_b(rows), section_c(rows), section_d(log), section_e(rows, log), section_g(log, lk), section_f()))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", required=True, help="dossier qui contient kelly.db (copié, jamais modifié)")
    args = ap.parse_args(argv)
    isolate_env()
    rows, log = load(args.data_dir)
    if not rows and not log:
        print("Aucune donnée (kelly.db absent ou vide).", file=sys.stderr)
        return 1
    print(report(rows, log, links(args.data_dir)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
