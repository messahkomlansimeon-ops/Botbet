"""Progression du passage du matin (app.run_daily) : étapes pondérées par leur durée aux derniers passages (data/kelly.db, progress_times.json :
moyenne glissante, valeurs par défaut au premier passage). Visible à trois endroits :
  - les logs : « [progression] 45 % · championnats 1xBet hors The Odds API (fin vers 04h48) » à chaque étape ;
  - `systemctl --user status kelly-daily` : ligne « Status: » (sd_notify STATUS= ; l'unité doit avoir NotifyAccess=all, python est lancé par flock) ;
  - Telegram : « état » ou /etat, lu dans progress.json SANS le verrou des données (réponse immédiate pendant le passage).
Ne lève jamais : la progression ne doit pas faire tomber le matin."""
import os
import socket
import sys
from datetime import datetime, timedelta, timezone

from . import store

FILE = "progress.json"
TIMES = "progress_times.json"
SMOOTH = 0.5                    # poids du dernier passage dans la durée attendue d'une étape
CAP = 0.95                      # une étape qui dure plus que prévu plafonne à 95 % de son poids (le pourcentage ne dépasse pas l'étape suivante)
# (clé, libellé, durée attendue en secondes au premier passage : passages des 09 et 10/10/2026)
STEPS = (("sources", "téléchargement des stats (football-data, FotMob, BetExplorer)", 600),
         ("bilan", "règlement des paris d'hier et bilan", 110),
         ("monde", "tour du monde des matchs du jour", 45),
         ("profils", "stats mondiales API-Football", 240),
         ("cotes", "cotes du jour (The Odds API)", 10),
         ("stats", "stats d'avant-match", 5),
         ("xworld", "championnats 1xBet hors The Odds API", 150),
         ("pronos", "pronostics de tous les matchs", 180),
         ("xbet", "paris réels aux cotes 1xBet", 60),
         ("combis", "construction des combinés", 30),
         ("envoi", "envoi des messages Telegram", 15),
         ("cloture", "relevé des cotes de clôture", 30))
LABELS = {k: label for k, label, _ in STEPS}
_CUR = {}                       # passage en cours dans CE processus (vide : step() et finish() ne font rien, ex. --sample)


def _now():
    return datetime.now(timezone.utc)


def expected():
    """Durée attendue (s) de chaque étape : progress_times.json, sinon la valeur par défaut."""
    try:
        saved = store.load(TIMES, {})
    except OSError:
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    return {k: float(saved.get(k) or d) for k, _, d in STEPS}


def _pct(state, now=None):
    """(pourcentage 0-100, fin estimée datetime) d'un état progress.json, l'étape en cours comptée au temps déjà passé (plafonné à CAP)."""
    w = state.get("weights") or expected()
    keys = [k for k, _, _ in STEPS]
    total = sum(w.get(k, 0) for k in keys) or 1
    cur = state.get("step")
    if state.get("finished") or cur not in keys:
        return (100 if state.get("finished") else 0), None
    i = keys.index(cur)
    now = now or _now()
    spent = max(0.0, (now - datetime.fromisoformat(state["step_at"])).total_seconds())
    wk = w.get(cur, 0)
    done = sum(w.get(k, 0) for k in keys[:i]) + min(spent, CAP * wk)
    left = max(wk - spent, 0.05 * wk) + sum(w.get(k, 0) for k in keys[i + 1:])
    return min(99, int(100 * done / total)), now + timedelta(seconds=left)


def _hhmm(dt):
    return dt.strftime("%Hh%M") if dt else "?"


def _notify(text):
    """STATUS= pour systemd (sd_notify) : rien hors systemd. Ne lève jamais."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(f"STATUS={text}".encode())
    except OSError:
        pass


def _publish():
    pct, eta = _pct(_CUR)
    if _CUR.get("finished"):
        line = f"terminé en {_CUR.get('minutes', '?')} min"
    else:
        line = f"{pct} % · {LABELS.get(_CUR['step'], _CUR['step'])}" + (f" (fin vers {_hhmm(eta)})" if eta else "")
    print(f"[progression] {line}", file=sys.stderr)
    _notify(line)
    try:
        store.save(FILE, _CUR)
    except OSError as e:
        print(f"[progression] état non écrit : {e}", file=sys.stderr)


def start(kind="daily"):
    """Début d'un passage : première étape « sources »."""
    try:
        now = _now().isoformat()
        _CUR.clear()
        _CUR.update(kind=kind, pid=os.getpid(), started=now, step=STEPS[0][0], step_at=now, durations={}, weights=expected())
        _publish()
    except Exception as e:                          # noqa: BLE001
        print(f"[progression] ignorée : {type(e).__name__}: {e}", file=sys.stderr)


def step(key):
    """L'étape `key` commence (l'étape précédente est finie et sa durée notée)."""
    if not _CUR or _CUR.get("finished"):
        return
    try:
        now = _now()
        prev = _CUR["step"]
        _CUR["durations"][prev] = _CUR["durations"].get(prev, 0) + (now - datetime.fromisoformat(_CUR["step_at"])).total_seconds()
        _CUR.update(step=key, step_at=now.isoformat())
        _publish()
    except Exception as e:                          # noqa: BLE001
        print(f"[progression] ignorée : {type(e).__name__}: {e}", file=sys.stderr)


def finish():
    """Fin du passage : durées des étapes mêlées aux précédentes (moyenne glissante), état « terminé »."""
    if not _CUR or _CUR.get("finished"):
        return
    try:
        now = _now()
        prev = _CUR["step"]
        _CUR["durations"][prev] = _CUR["durations"].get(prev, 0) + (now - datetime.fromisoformat(_CUR["step_at"])).total_seconds()
        old = expected()
        store.save(TIMES, {k: round(SMOOTH * _CUR["durations"][k] + (1 - SMOOTH) * old[k], 1) if k in _CUR["durations"] else old[k]
                           for k, _, _ in STEPS})
        _CUR.update(finished=now.isoformat(), minutes=round((now - datetime.fromisoformat(_CUR["started"])).total_seconds() / 60))
        _publish()
    except Exception as e:                          # noqa: BLE001
        print(f"[progression] ignorée : {type(e).__name__}: {e}", file=sys.stderr)


def _alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (OSError, ValueError, TypeError):
        return True
    return True


def describe(now=None):
    """Texte de « état » / /etat : où en est le passage du matin (ou quand il s'est terminé). Lu sans le verrou des données."""
    try:
        st = store.load(FILE, {})
    except OSError:
        st = {}
    if not isinstance(st, dict) or not st.get("started"):
        return "Pas encore de passage du matin suivi : la progression s'affichera au prochain (04h00)."
    started = datetime.fromisoformat(st["started"])
    if st.get("finished"):
        end = datetime.fromisoformat(st["finished"])
        return f"✅ Passage du matin terminé à {_hhmm(end)} (commencé à {_hhmm(started)}, {st.get('minutes', '?')} min)."
    label = LABELS.get(st.get("step"), st.get("step"))
    if not _alive(st.get("pid")):
        return (f"⚠️ Passage du matin interrompu pendant : {label} (commencé à {_hhmm(started)}). "
                "Il sera refait au prochain lancement.")
    pct, eta = _pct(st, now)
    return (f"⏳ Passage du matin : {pct} % · {label}\n"
            f"Commencé à {_hhmm(started)}, fin estimée vers {_hhmm(eta)}.")


def busy_suffix():
    """« (passage du matin : 65 %, fin vers 04h48) » pendant un passage suivi en cours, '' sinon : ajouté au message « Kelly est en plein travail »."""
    try:
        st = store.load(FILE, {})
        if isinstance(st, dict) and st.get("started") and not st.get("finished") and _alive(st.get("pid")):
            pct, eta = _pct(st)
            return f" (passage du matin : {pct} %, fin vers {_hhmm(eta)})"
    except Exception:                               # noqa: BLE001
        pass
    return ""

