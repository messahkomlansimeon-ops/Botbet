"""Statistiques des COUPES (LDC, Europa, Conférence, Libertadores, coupes nationales : C.SEASON_CUPS) dans la voie `stats`.

Une coupe oppose deux équipes de championnats différents : leurs moyennes de corners, cartons, tirs, fautes ne se comparent pas telles quelles (audit du 13/10 : c'est pourquoi les stats
de coupe étaient écartées). Le calcul est celui de la voie `saison` (season.cup_predict) : attente de A = profil de championnat de A x profil de championnat de B, chacun rapporté à la
moyenne de SA ligue, x le niveau moyen des deux ligues, facteur domicile neutre C.SEASON_CUP_HOME (1,10), au moins C.SEASON_MIN_MATCHES matchs de championnat par équipe.
Les jambes de stats de coupe sont des pronostics ordinaires (fair.make_pick, xprice, étiquette `stats`) marqués cup = true. Interrupteur C.STATS_CUPS_MODE : « ghost » (défaut : prix lus, avis et
CLV journalisés, jamais dans un pari ni un combiné), « on » (jambes jouables, à la main), « off ». Leur journal, leur archive et leur règlement sont À PART (data/cup_log.json, arch/cup/) :
ni les poids de l'ensemble, ni les moyennes (means), ni la calibration, ni les objectifs, ni la coupure de la voie stats ne les voient. Règle de retour pré-déclarée : passage à « on » À LA MAIN
quand le CLV 1xBet des jambes de coupe en fantôme est >= 0 sur C.STATS_CUPS_BACK_N lignes réglées (la ligne « stats (coupes) » du bilan le dit). Buts, 1N2 et favoris sharp d'une coupe :
inchangés (voies sharpgap et fill)."""
import sys

from . import config as C, ensemble, fair, prematch, season, seasonlane, store

LOG = "cup_log.json"
ARCH = "arch/cup/"
STAT_MARKETS = frozenset(m for m in seasonlane.MARKETS if m in fair.STAT_SPECS or m in (prematch.CORNERS, prematch.CARDS))


def mode():
    """« ghost », « on » ou « off » (C.STATS_CUPS_MODE) ; « off » sans ensemble (aucune étiquette sans lui)."""
    return C.STATS_CUPS_MODE if C.ENSEMBLE_ENABLED else "off"


def make(scan, now, today=None):
    """Pronostics de stats des matchs de coupe du jour pas commencés (un par marché et par match, comme fair.make : avec leurs lignes en c["alts"]), `cup` = True. [] si la voie est « off »,
    sans coupe au relevé ou sans modèle de championnat des équipes. Ne lève jamais."""
    if mode() == "off":
        return []
    out, today = [], today or now.date()
    try:
        models = None
        for key, evs in (scan.get("leagues") or {}).items():
            if key not in C.SEASON_CUPS:
                continue
            models = seasonlane.cup_models(today) if models is None else models
            if not models:
                return out
            for ev in sorted(evs, key=lambda e: e["commence_time"]):
                if seasonlane._ko(ev) <= now:
                    continue
                pred = season.cup_predict(models, ev["home_team"], ev["away_team"])
                if not pred:
                    continue
                opts, bases = [], {}
                for mk, name, pt, p, base in seasonlane.options(pred, ev["home_team"], ev["away_team"]):
                    if mk in STAT_MARKETS:
                        opts.append((mk, name, pt, p, "coupe : profils de championnat"))
                        bases[(mk, name, pt)] = base
                for b in fair.best_per_family(opts, STAT_MARKETS, bases):
                    c = fair.make_pick(ev, *b, "coupe")
                    c["alts"] = fair.alternatives(opts, bases, b[0])
                    c["mu"], c["n_min"], c["cup"] = raw_mean(pred, b[0]), min(pred["n"]), True
                    fair._with_base(c, bases.get((b[0], b[1], b[2])), pred["qs"].get(seasonlane._stat_of(b[0]), pred["q"]))
                    out.append(c)
    except Exception as e:                          # noqa: BLE001 - les stats de coupe sont facultatives
        print(f"[coupes] stats de coupe ignorées : {type(e).__name__}: {e}", file=sys.stderr)
    return out


def raw_mean(pred, market):
    """Nombre attendu du chiffre d'un marché de coupe d'après la prévision `pred` (season.cup_predict) : total du match (corners, cartons, marchés « _total ») ou d'une équipe (« _home » / « _away »).
    Posé sur le pronostic (« mu ») comme pour les autres lignes : le règlement écrit alors le nombre réel (« val »), ce qui permet de mesurer un biais des moyennes de coupe SANS reconstruire la
    moyenne depuis la proba. Collecte seulement : le règlement des coupes n'apprend rien (settle_cups), aucune correction n'en est tirée. None sans chiffre."""
    s = (pred.get("stats") or {}).get(seasonlane._stat_of(market))
    if not s:
        return None
    whole = market in (prematch.CORNERS, prematch.CARDS) or market.endswith("_total")
    return round(s["h"] + s["a"] if whole else s["h"] if market.endswith("_home") else s["a"], 3)


def split(rows):
    """(lignes de stats de coupe, autres lignes) : les premières vont au journal à part (record), les autres au journal d'ensemble."""
    cup = [r for r in rows if r.get("cup") and r.get("lane") != "saison"]
    return cup, [r for r in rows if not (r.get("cup") and r.get("lane") != "saison")]


def record(rows, day, now):
    """Journal des avis des stats de coupe (data/cup_log.json) : mêmes champs que le journal d'ensemble (pm, px, ps, xref, xclose, strategy, cup). -> lignes ajoutées."""
    if not rows or not ensemble.logging_on():
        return 0
    try:
        return ensemble.record(rows, str(day), now, name=LOG, shadow=False)
    except Exception as e:                          # noqa: BLE001
        print(f"[coupes] journal ignoré : {type(e).__name__}: {e}", file=sys.stderr)
        return 0


def journal_rows():
    log = store.load(LOG, [])
    log = [r for r in log if isinstance(r, dict)] if isinstance(log, list) else []
    seen = {r.get("key") for r in log}
    return log + [r for r in ensemble.archive_rows(prefix=ARCH) if r.get("key") not in seen]
