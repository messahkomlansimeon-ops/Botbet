"""Confirmation avant une action grave (14/10) : un second message « ⚠️ Confirmer ? » avec deux boutons, ✅ Oui / ❌ Non, avant de faire ce qui ne se défait pas d'un geste.
Un popup Telegram n'a qu'un bouton « OK » : la confirmation est donc un vrai message, dont les boutons portent un court identifiant (callback « cf|<id> » / « cn|<id> »).

Actions gardées :
  - annuler la note d'un pari (bouton « ↩️ Annuler », ou « joue <pari> 0 ») ;
  - changer le capital déjà fixé, ou l'arrêter (« capital 0 ») : le suivi repart d'aujourd'hui ;
  - changer ou retirer la perte max du jour déjà fixée ;
  - supprimer le budget du jour (« budget 0 ») ;
  - donner à la main le résultat d'un ticket (« réglé vm3 gagné ») : le gain est compté tout de suite ;
  - « capital 0 » quand seul un budget existe (il l'efface aussi) ; « capital <même montant> » (le suivi repart d'aujourd'hui).
Jamais gardées : la première fois qu'un capital, une perte max ou un budget est fixé, ni un budget changé pour un autre montant (boutons rapides), ni les trois boutons « ✅ Gagné / ❌ Perdu / ↩️ Remboursé » d'un
« ❓ Résultat introuvable » (Kelly pose elle-même la question, trois choix explicites).
Si le capital, la perte max ou le budget change entre la demande et ✅, la demande est refusée (elle décrirait un autre état).
L'action attendue est gardée dans data/confirm.json (commande écrite ou bouton d'origine), 15 minutes ; un 2e appui sur ✅ ne la refait pas (retirée avant d'être faite)."""
import secrets
from datetime import datetime, timedelta, timezone

from . import config as C, money, store, telegram
from .telegram import Html

FILE = "confirm.json"          # {id: {"cmd": commande écrite | "cb": donnée du bouton d'origine, "msg": message d'origine, "at": instant}}


def _now():
    return datetime.now(timezone.utc)


def _keep(box):
    """Les demandes encore valables (pas plus de C.CONFIRM_MIN minutes)."""
    limit = _now() - timedelta(minutes=C.CONFIRM_MIN)
    out = {}
    for k, v in (box if isinstance(box, dict) else {}).items():
        try:
            if datetime.fromisoformat(str(v.get("at"))) >= limit:
                out[k] = v
        except (ValueError, AttributeError):
            continue
    return out


def _put(entry):
    """Garde `entry`, atomiquement ; -> son identifiant."""
    cid = secrets.token_hex(3)

    def add(box):
        box = _keep(box)
        box[cid] = entry | {"at": _now().isoformat(timespec="seconds")}
        return box, cid
    return store.update(FILE, add, {})


def take(cid):
    """La demande `cid` (retirée en la lisant : jamais faite deux fois) ; None si elle n'existe plus ou a expiré."""
    def pop(box):
        box = _keep(box)
        return box, box.pop(cid, None)
    return store.update(FILE, pop, {})


def message(why):
    """Le message de confirmation (sans boutons) : `why` = ce qui va être fait, en une phrase."""
    return Html("⚠️ <b>Confirmer ?</b>\nTu vas " + why + ".\n<i>Valable " + str(C.CONFIRM_MIN) + " minutes.</i>")


def _buttons(cid):
    return telegram.inline([[("✅ Oui, confirmer", f"cf|{cid}"), ("❌ Non", f"cn|{cid}")]])


def ask(why, cmd=None, cb=None, msg=None, sig=None):
    """Le message « Confirmer ? » avec ses deux boutons ; l'action (commande écrite `cmd` ou bouton d'origine `cb`) attend la réponse."""
    cid = _put({k: v for k, v in (("cmd", cmd), ("cb", cb), ("msg", msg), ("sig", sig)) if v})
    return telegram.with_buttons(message(why), _buttons(cid))


def _sig():
    """Instantané de l'état d'argent au moment de la demande (capital, perte max, budget, départ) : si l'un change avant ✅, la demande est refusée (elle décrirait un autre état)."""
    d = money.load()
    return [money._amount(d.get("capital")), money._amount(d.get("max_day")), money._amount(d.get("daily")), str(d.get("since") or "")[:10]]


def check(cmd):
    """La confirmation à demander avant la commande écrite `cmd` (« /capital 0 »), ou None si elle passe tout de suite."""
    parts = (cmd or "").split()
    if not parts:
        return None
    name, args = parts[0].split("@")[0].lower(), parts[1:]       # « /capital@MonBot 0 » : le nom du robot ne compte pas
    why, sig = None, False
    if name == "/capital" and args:
        amount, err = money._amount_arg(args, "capital")
        d = money.load()
        cur = money._amount(d.get("capital"))
        if err or amount is None:
            return None
        sig = True
        if amount <= 0:
            if cur:
                why = "arrêter le mode capital (plus de mise conseillée)"
            elif money.active():
                why = "effacer ton budget du jour (« capital 0 » efface aussi le budget) : Kelly ne donnera plus de mise conseillée"
        elif cur and round(amount) != round(cur):
            why = f"changer ton capital de {money.money(cur)} à {money.money(amount)} : le suivi repart d'aujourd'hui"
        elif cur and str(d.get("since") or "")[:10] != str(money._today()):
            why = f"refixer ton capital à {money.money(cur)} : le suivi repart d'aujourd'hui (il comptait depuis le {str(d.get('since') or '')[:10]})"
    elif name == "/perte" and args:
        args = [a for a in args if money.played_norm(a) not in ("max", "maximum", "maxi", "du", "jour", "par")]
        amount, err = money._amount_arg(args, "perte max") if args else (None, True)
        cur = money._amount(money.load().get("max_day"))
        if err or amount is None or not cur or not money.capital_mode():
            return None
        sig = True
        if amount <= 0:
            why = "retirer ta perte max du jour"
        elif round(amount) != round(cur):
            why = f"changer ta perte max du jour de {money.money(cur)} à {money.money(amount)}"
    elif name == "/budget" and args:
        amount, _cur = money.parse_amount(args)
        if amount is not None and amount <= 0 and money.active() and not money.capital_mode():
            sig = True
            why = "supprimer ton budget du jour (Kelly ne donnera plus de mise conseillée)"
    elif name in ("/regle", "/reglé", "/régler", "/regler"):
        from . import played
        words = [played.norm(a) for a in args]
        if any(w in played.RESULT_WORDS for w in words) and len(args) >= 2:      # « réglé vm3 gagné » : un ticket ET un résultat ; sinon la commande répond son aide
            why = f"régler à la main « {' '.join(args)} » : le gain ou la perte est compté tout de suite dans ton capital"
    elif name in ("/joue", "/joué", "/jouer") and len(args) >= 2:
        from . import played
        odds, _stake, err = played.parse_odds_stake(args[1:])
        if odds == 0 and not err:                      # une cote à 0 retire la note (« 0 », « 0,00 », « 0 1000 »…)
            t = played.find(args[0], str(store.load("today.json", {}).get("day") or datetime.now(timezone.utc).date())[:10])
            if t is not None and not isinstance(t, str) and t.item.get("played"):
                why = f"annuler la note du {t.label}"
    return ask(why, cmd=cmd, sig=_sig() if sig else None) if why else None


def answer(parts, message_id=None):
    """Réponse à ✅ / ❌ (callbacks « cf|<id> » et « cn|<id> »). -> (action à faire, texte) : ("cmd", commande) ou ("cb", donnée) après ✅ ; (None, texte) sinon."""
    entry = take(parts[1])
    if message_id:
        telegram.edit_buttons(message_id, {"inline_keyboard": []})        # les boutons disparaissent : plus de double appui
    if entry is None:
        return None, "Cette confirmation est trop ancienne ou déjà traitée : redemande l'action si tu la veux encore."
    if parts[0] == "cn":
        return None, "↩️ Annulé : rien n'a changé."
    if entry.get("sig") and entry["sig"] != _sig():
        return None, "Ton capital ou ton budget a changé depuis cette demande : elle ne vaut plus. Redemande l'action si tu la veux encore."
    if entry.get("cmd"):
        return ("cmd", entry["cmd"]), None
    return ("cb", entry.get("cb")), entry.get("msg")
