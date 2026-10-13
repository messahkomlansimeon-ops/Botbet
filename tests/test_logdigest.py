"""🧾 Résumé du journal (« erreurs ») : fenêtre de temps (heure de la dernière ligne datée), regroupement, plantages avec leur ligne de code, compteurs,
rien de secret. Lancer : python -m tests.test_logdigest"""
import os
import tempfile
from datetime import datetime, timezone

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="kelly-log-"))

from kelly import app, chat, logdigest  # noqa: E402

LOG = """[termux 2026-10-12 08:00:00Z] 🔥 Démarrage de Kelly sur GrapheneOS / Termux...
[settle] scores soccer_epl ignorés : vieux
[termux 2026-10-13 12:00:00Z] 🔥 Démarrage de Kelly sur GrapheneOS / Termux...
[termux 2026-10-13 12:00:01Z] ✅ Bot Telegram actif (PID 4242)
[1xbet] proxy en panne (réseau indisponible (ProxyError)) : essai de la session Webshare n° 21
[1xbet] proxy en panne (réseau indisponible (ProxyError)) : essai de la session Webshare n° 22
[progression] 40 % · règlement des paris d'hier (fin vers 04h16)
[termux 2026-10-13 13:05:00Z] ⚠️ bot.py --serve s'est arrêté (code 1) après 3 s ; arrêts rapprochés n°1 -> relance dans 15 s (plafond 900 s)
Traceback (most recent call last):
  File "/data/data/com.termux/files/home/kelly/bot.py", line 180, in <module>
    main(sys.argv[1:])
  File "/data/data/com.termux/files/home/kelly/kelly/app.py", line 2894, in run_commands
    telegram.poll(handler)
KeyError: 'offset'
[telegram] envoi échoué : Bad Request https://api.telegram.org/bot123456:ABCdef_secret/sendMessage
[termux 2026-10-13 13:40:00Z] ⏱️ --track --own-lock dépasse 1500 s : interrompu
[s365] ignoré : proxy http://user-GB-20:motdepasse@p.webshare.io:80 injoignable
[termux 2026-10-13 13:41:00Z] signal de vie : https://hc-ping.com/1c695e6a-bc5b-4585-bc6c-0fe6f1c8de31 erreur
"""


def test_digest():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "termux.log")
    open(p, "w", encoding="utf-8").write(LOG)
    out = logdigest.digest(24, now=datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc), files=[p])
    assert out.startswith("🧾 Journal de Kelly — 24 h (13/10 12h00 → 13/10 14h00 UTC)"), out
    assert "Démarrages : 1 · écoute Telegram relancée : 1 · passages trop longs : 1" in out, out           # le démarrage de la veille est hors fenêtre
    assert "• 1× KeyError: 'offset' — kelly/app.py ligne 2894 (run_commands) (dernier 13/10 13h05)" in out, out
    assert "• 2× [1xbet] proxy en panne (réseau indisponible (ProxyError)) : essai de la session Webshare n° 22" in out, out
    assert "scores soccer_epl" not in out and "[progression]" not in out, out                              # hors fenêtre ; ligne normale
    assert "ABCdef_secret" not in out and "motdepasse" not in out and "1c695e6a" not in out, out           # rien de secret
    assert "bot***" in out and "://***@" in out and "hc-ping.com/***" in out
    assert "aucune" not in out.lower()
    assert "Pas de journal" in logdigest.digest(files=[])
    calm = os.path.join(d, "calme.log")
    open(calm, "w").write("[termux 2026-10-13 12:00:00Z] ✅ Bot Telegram actif (PID 1)\n")
    assert "✅ Aucune erreur ni avertissement" in logdigest.digest(now=datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc), files=[calm])
    assert chat.route("erreurs")[0] == "/erreurs" and chat.route("erreurs 48")[0] == "/erreurs 48" and "erreurs" in str(app.HELP)
    print("ok  « erreurs » : 24 h (heure de la dernière ligne datée), sortes regroupées, plantages avec leur ligne de code, compteurs, rien de secret")


def main():
    test_digest()
    print("tests résumé du journal : OK")


if __name__ == "__main__":
    main()
