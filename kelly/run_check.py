"""Contrôle des sources et du bot (`kelly check`) : issu du découpage d'app.py (le code lit app via A.nom)."""

import os
import re
import requests
from . import config as C, apistats, budget, cache, catalogue, chat, learn, odds, sources, stats, telegram, track
from ._appref import A


def _check_odds():
    """-> (ok, lignes). N'utilise que /sports et /events (gratuits) : aucun crédit dépensé."""
    try:
        sports = odds.get("/sports")
    except odds.OddsError as e:
        if e.code == "NO_KEY":
            hint = "ajoute le secret ODDS_API_KEY (GitHub > Settings > Secrets and variables > Actions)"
        elif e.code == "OUT_OF_USAGE_CREDITS":
            hint = "quota du mois épuisé : attends le renouvellement ou passe à un forfait payant"
        elif e.status in (401, 403):
            hint = "clé refusée : recopie la clé de the-odds-api.com dans le secret ODDS_API_KEY (sans espace ni guillemets)"
        elif e.status == 429:
            hint = "trop de requêtes d'un coup : relance dans une minute"
        elif e.status is None:
            hint = "the-odds-api.com injoignable (réseau, proxy ou panne)"
        else:
            hint = "voir le message ci-dessus"
        return False, [f"❌ The Odds API : {e}", f"   → {hint}"]
    rem, used = odds.CREDITS["remaining"], odds.CREDITS["used"]
    lines = ["✅ The Odds API : clé valide",
             f"   Crédits : {rem if rem is not None else '?'} restants, {used if used is not None else '?'} utilisés"]
    chosen = odds.select_sports(sports, limit=False)
    lines.append(f"   Ligues suivies en saison : {len(chosen)} sur {len(C.LEAGUE_KEYS)} du catalogue (au plus {C.MAX_SPORTS} payées "
                 "par jour) : " + (", ".join(s["key"] for s in chosen[:8]) + (" …" if len(chosen) > 8 else "") or "aucune"))
    active = {s["key"] for s in sports}
    absent = [p for p in C.SPORTS if not p.endswith("_") and p not in active]
    if absent:
        lines.append(f"   Hors saison ou clé inconnue : {len(absent)} ligue(s) (ex. {', '.join(absent[:4])})")
    if not chosen:
        lines.append("   ⚠️ Aucun sport actif ne correspond à SPORTS : Kelly n'aura aucun match.")
    lines.append(f"   Allocation du jour : {budget.allowance()} crédits (matin {budget.share('daily')}, clôture "
                 f"{budget.share('closing')}), {budget.days_left()} jour(s) avant la remise à zéro "
                 f"(jour {C.ODDS_RESET_DAY} du mois ; forfait {C.ODDS_MONTHLY_CREDITS}/mois)")
    scan = odds.scan_today(A.now_utc().date(), A.now_utc(), sports=chosen)      # gratuit, relu du cache du matin si possible
    counts = {k: len(v) for k, v in scan["leagues"].items()}
    cov = odds.covered_leagues(scan)
    playing = [f"{k} ({counts[k]})" for k in catalogue.prioritise(counts, C.SPORTS, cov)]
    lines.append("   Ligues avec matchs aujourd'hui (gratuit) : " + (", ".join(playing) or "aucune"))
    if playing:
        lines.append("   Payées ce matin dans cet ordre (région, ligues majeures, nombre de matchs) : "
                     + ", ".join(catalogue.prioritise(counts, C.SPORTS, cov)[:C.MAX_SPORTS]))
    if rem is not None and rem < C.MIN_CREDITS:
        lines.append(f"   ⚠️ Sous le seuil MIN_CREDITS={C.MIN_CREDITS} : le matin ne dépense plus, la réserve sert à clôturer.")
    return True, lines


def _check_stats():
    """-> (ok|None si ignoré, lignes)."""
    key = os.environ.get("STATS_API_KEY")
    if not key:
        return None, ["⏭️ API-Football : STATS_API_KEY absente (facultatif : sans elle, corners/cartons/stats joueurs "
                      "restent « non réglés »)"]
    rapid = "" if re.fullmatch(r"[0-9a-fA-F]{32}", key.strip()) else (
        " Ta clé n'a pas le format api-sports (32 caractères hexadécimaux) : une clé RapidAPI ne marche pas sur "
        "v3.football.api-sports.io, prends celle de dashboard.api-football.com.")
    try:
        r = requests.get(A.STATS_STATUS_URL, headers={"x-apisports-key": key.strip()}, timeout=20)
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        return False, [f"❌ API-Football : {type(e).__name__} (api-sports.io injoignable ou réponse illisible)",
                       "   → vérifie le réseau, puis la clé STATS_API_KEY." + rapid]
    errors = body.get("errors") if isinstance(body, dict) else None
    resp = body.get("response") if isinstance(body, dict) else None
    if errors or not isinstance(resp, dict) or not resp:
        msg = "; ".join(f"{k}: {v}" for k, v in errors.items()) if isinstance(errors, dict) else (
            "; ".join(map(str, errors)) if errors else f"HTTP {r.status_code}")
        return False, [f"❌ API-Football : {msg[:200]}",
                       "   → clé invalide ou compte non activé : copie la clé du tableau de bord api-sports (en-tête "
                       "x-apisports-key)." + rapid]
    sub, req = resp.get("subscription") or {}, resp.get("requests") or {}
    lines = [f"✅ API-Football : plan {sub.get('plan', '?')}{'' if sub.get('active', True) else ' (INACTIF)'}, "
             f"requêtes aujourd'hui {req.get('current', '?')}/{req.get('limit_day', '?')} (Kelly s'arrête à {stats._cap()})"]
    if sub.get("active") is False:
        lines.append("   → abonnement inactif : réactive-le sur dashboard.api-football.com.")
    lines += apistats.check_lines()
    return sub.get("active") is not False, lines


def _check_telegram_bot():
    """-> (ok, lignes, chat_ok)."""
    token, chat = telegram.creds()
    if not token:
        return False, ["❌ Telegram : TELEGRAM_TOKEN absent", "   → crée un bot avec @BotFather et copie son token dans le secret."], False
    try:
        me = telegram.api("getMe") or {}
    except telegram.TelegramError as e:
        hint = ("token refusé : recopie-le depuis @BotFather (format 123456:ABC…, sans espace)"
                if e.code in (401, 404) else "Telegram injoignable ou erreur inattendue")
        return False, [f"❌ Telegram getMe : {e}", f"   → {hint}"], False
    lines = [f"✅ Telegram : bot @{me.get('username', '?')} joignable"]
    if not chat:
        lines += ["❌ Telegram : TELEGRAM_CHAT_ID absent",
                  "   → envoie /start à ton bot puis lis ton chat id via https://api.telegram.org/bot<TOKEN>/getUpdates"]
        return False, lines, False
    return True, lines, True


def _telegram_hint(err):
    e = err.lower()
    if "chat not found" in e:
        return ("le bot ne connaît pas ce chat : ouvre-le dans Telegram et envoie-lui /start d'abord, puis vérifie "
                "TELEGRAM_CHAT_ID (un nombre, pas un @pseudo)")
    if "blocked" in e or "forbidden" in e:
        return "le bot est bloqué ou exclu du chat : débloque-le / renvoie /start"
    if "unauthorized" in e:
        return "token refusé : recopie-le depuis @BotFather"
    return "voir le message d'erreur Telegram ci-dessus"


def run_check():
    """Vérifie chaque service configuré et affiche un rapport. Ne dépense aucun crédit
    The Odds API (seuls /sports et /events, gratuits). Renvoie True si tout ce qui est configuré marche."""
    print("🔎 KELLY — DIAGNOSTIC")
    from . import configactive
    configactive.refresh()                           # CONFIG_ACTIVE.md : les réglages actifs de cette machine (jamais un secret)
    ok_odds, l_odds = A._check_odds()
    ok_stats, l_stats = A._check_stats()
    l_src = sources.report().splitlines()           # facultatif : n'influe pas sur le verdict
    l_cache = [cache.line(cache.today_totals())]    # cumul du jour, tous passages confondus
    try:                                            # chiffres techniques de l'apprentissage : ici et dans /bilan détail
        l_learn = learn.describe(learn.load(), track.load()).splitlines()
    except Exception as e:                          # noqa: BLE001
        l_learn = [f"(apprentissage illisible : {type(e).__name__})"]
    ok_bot, l_bot, chat_ok = A._check_telegram_bot()
    lines = l_odds + l_stats + l_src + l_cache + l_learn + l_bot
    ok_send = False
    if chat_ok:
        summary = "\n".join(lines)
        if telegram.send(telegram.topic("✅ Kelly est en ligne\n\n" + summary, "tech"), silent=True):
            ok_send = True
            lines.append("✅ Telegram : message de test envoyé à ton chat")
        else:
            lines += [f"❌ Telegram sendMessage : {telegram.LAST_ERROR}",
                      f"   → {A._telegram_hint(telegram.LAST_ERROR)}"]
    print("\n".join(lines))
    cache.flush()
    ok = ok_odds and ok_stats is not False and ok_bot and ok_send
    print("\n" + ("✅ Tout est prêt." if ok else "⚠️ Au moins un service est mal configuré (voir ❌ ci-dessus)."))
    return ok
