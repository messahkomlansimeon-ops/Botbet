"""« status » sur Telegram (kelly/status.py) : services systemd (en cours, dernier passage, échec, prochain), sources, API ; répondu sans le verrou.
Hors ligne (systemctl et API simulés). Lancer : python -m tests.test_status"""
import os
import tempfile
from datetime import datetime, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-status-")
os.environ.pop("TELEGRAM_TOKEN", None)

from kelly import app, chat, status  # noqa: E402

NOW = datetime(2026, 10, 10, 8, 30, tzinfo=timezone.utc)
UNITS = {
    "kelly-serve.service": {"ActiveState": "active", "Result": "success", "ExecMainStartTimestamp": "Sat 2026-10-10 07:56:01 GMT", "LoadState": "loaded"},
    "kelly-daily.service": {"ActiveState": "inactive", "Result": "success", "ExecMainExitTimestamp": "Sat 2026-10-10 05:18:43 GMT", "LoadState": "loaded"},
    "kelly-daily.timer": {"NextElapseUSecRealtime": "Sun 2026-10-11 04:00:00 GMT", "LoadState": "loaded"},
    "kelly-track.service": {"ActiveState": "activating", "Result": "success", "ExecMainStartTimestamp": "Sat 2026-10-10 08:05:00 GMT", "LoadState": "loaded"},
    "kelly-track.timer": {"NextElapseUSecRealtime": "Sat 2026-10-10 09:05:00 GMT", "LoadState": "loaded"},
    "kelly-xbet.service": {"ActiveState": "failed", "Result": "timeout", "ExecMainExitTimestamp": "Fri 2026-10-09 15:30:00 GMT", "LoadState": "loaded"},
    "kelly-xbet.timer": {"NextElapseUSecRealtime": "Sat 2026-10-10 15:00:00 GMT", "LoadState": "loaded"},
    "kelly-backup.service": {"ActiveState": "inactive", "Result": "success", "LoadState": "loaded"},
    "kelly-backup.timer": {"LastTriggerUSec": "Fri 2026-10-09 23:00:16 GMT", "NextElapseUSecRealtime": "Sat 2026-10-10 23:00:00 GMT", "LoadState": "loaded"},
    "kelly-evening.service": {"ActiveState": "inactive", "Result": "success", "LoadState": "loaded"},
    "kelly-evening.timer": {"NextElapseUSecRealtime": "Sat 2026-10-10 12:00:00 GMT", "LoadState": "loaded"},
    "kelly-tune.service": {"LoadState": "not-found"},
}


def test_services():
    old = status._show
    status._show = lambda unit, props: UNITS.get(unit, {})
    try:
        lines = status.service_lines(NOW)
    finally:
        status._show = old
    assert lines == ["✅ Bot Telegram : actif depuis 07h56",
                     "✅ Passage du matin : dernier 05h18 · prochain demain 04h00",
                     "⏳ Passage horaire : en cours depuis 08h05 · prochain 09h05",
                     "❌ Relevé 1xBet de 15h : échec (trop long, coupé) à hier 15h30 · prochain 15h00",
                     "⚪ Relecture de 12h : pas encore lancé · prochain 12h00",
                     "✅ Sauvegarde de 23h : dernier hier 23h00 · prochain 23h00"], lines          # réglage absent : pas de ligne
    status._show = lambda unit, props: {}
    try:
        assert status.service_lines(NOW) == [], "hors systemd (GitHub Actions) : pas de section services"
    finally:
        status._show = old
    print("ok  services : en cours, dernier passage, échec, prochain ; rien hors systemd")


def test_report_and_words():
    old = (status.service_lines, status.api_lines)
    status.service_lines = lambda now=None: ["✅ Bot Telegram : actif depuis 07h56"]
    status.api_lines = lambda: ["✅ The Odds API : 448 crédits restants (52 utilisés ce mois)", "❌ Telegram : TELEGRAM_TOKEN absent"]
    try:
        out = status.report(NOW)
        assert out.startswith("<b>🩺 Kelly — 08h30</b> · 1 point à regarder ❌\n\n<b>⚙️ Services</b>\n✅ Bot Telegram"), out     # le verdict en tête
        assert "\n\n<b>📡 Sources</b>\n" in out and "\n\n<b>🔌 Quotas</b>\n" in out and "Tout va bien" not in out, out
        for w in ("status", "Status", "/status", "statut", "santé", "/status@KellyBot"):
            assert chat.quick(w) and chat.quick(w).startswith("<b>🩺"), w
        assert chat.quick("état").startswith(("Pas encore", "⏳", "✅", "⚠️")), "« état » reste la progression du matin"
        assert chat.quick("Statut Quo") is None
        assert app._handle_command("/status").startswith("<b>🩺")
        status.api_lines = lambda: ["✅ Telegram joignable"]
        assert status.report(NOW).startswith("<b>🩺 Kelly — 08h30</b> · tout va bien 👍")
    finally:
        status.service_lines, status.api_lines = old
    assert status._when("Sat 2026-10-10 05:18:43 GMT", NOW) == "05h18" and status._when("", NOW) == "" and status._when("n/a", NOW) == ""
    print("ok  rapport « status » : services, sources, API ; mots reconnus ; « état » inchangé")


def test_learning():
    from kelly import calib, live, store
    store.save(calib.FILE, {"goals": [29, 22, 19.0], "shots": [70, 30, 45.5]})
    calib._memo.clear()
    store.save(live.CALIB_FILE, {"src|365scores": [12, 9, 8.0], "goals|60-75": [5, 4, 3.5]})          # ancien format : ignoré
    assert "💤 Live : rien de réglé pour l'instant" in status.learning_lines()
    store.save(live.CALIB_FILE, {"v": live.CALIB_VERSION, "k": {"h2h|45-60": [12, 9, 8.0], "goals|60-75": [5, 4, 3.5]}})
    lines = status.learning_lines()
    text = "\n".join(lines)
    assert lines[0] == "🔴 Tirs · 70 · trop confiante (43 % / 65 %) → écartés des combinés · nouvelle règle 0/30", lines        # correction sous VIABLE_MIN_K
    assert "⏳ Buts · 29/60" in lines and "⏳ Live 1N2 45-60 · 12/60" in lines and "⏳ Live buts 60-75 · 5/60" in lines, lines
    assert "⚖️ Avis face au marché : buts, double chance, les 2 marquent, 1N2 : modèle à 0 % (figé, audit du 14/10)" in lines, lines        # poids figé : plus d'effectif à attendre
    store.save(calib.FILE, {"goals": [131, 93, 90.4], "dc": [106, 82, 76.3]})
    calib._memo.clear()
    lines = status.learning_lines()
    assert lines[:2] == ["🟢 Buts · 131 · juste (71 % / 69 %)", "🟢 Double chance · 106 · un peu prudente (77 % / 72 %)"], lines
    assert "données légères" not in text
    print("ok  section « 🧠 Apprentissage » : avant-match, ensemble, live par famille et par tranche")


def test_sources_and_quotas():
    from kelly.sources import SOURCES, s365
    fake = {"football-data": {"ok": True, "rows": 11440, "last_success": "2026-10-10T04:03:00+00:00", "blocked_until": None,
                              "last_error": "E2 2526 : délai dépassé, téléchargement reporté au prochain passage ; E2 2425 : délai dépassé, téléchargement reporté au prochain passage"},
            "Understat": {"ok": False, "rows": 0, "last_success": None, "blocked_until": None, "last_error": "EPL 2026 : robots.txt interdit /league/EPL/2026"},
            "FBref": {"ok": False, "rows": 0, "last_success": None, "blocked_until": "2099-01-01T00:00:00+00:00", "last_error": "robots.txt refusé"},
            "FotMob": {"ok": True, "rows": 1988, "last_success": "2026-10-09T04:08:00+00:00", "blocked_until": None,
                       "last_error": "soccer_x : délai dépassé, téléchargement reporté au prochain passage"},
            "BetExplorer": {"ok": True, "rows": 3792, "last_success": "2026-10-10T04:12:00+00:00", "blocked_until": None,
                            "last_error": "soccer_australia_aleague : structure de la page modifiée : aucun match avec cotes"},
            "365scores": {"ok": True, "rows": 1655, "last_success": "2026-10-10T08:10:00+00:00", "blocked_until": None, "last_error": None}}
    old = {m: m.health for m in (*SOURCES, s365)}
    for m in old:
        m.health = (lambda label: lambda: fake[label])(m.LABEL)
    try:
        lines = status.source_lines(NOW)
    finally:
        for m, h in old.items():
            m.health = h
    assert lines == ["✅ football-data · 11 440 · 04h03", "✅ FotMob · 1 988 · 09/10 04h08", "✅ BetExplorer · 3 792 · 04h12", "✅ 365scores · 1 655 matchs · 08h10",
                     "💤 Understat, FBref : bloqués par les sites (normal)", "⚠️ 3 téléchargements reportés au prochain passage",
                     "⚠️ 1 page sans données (détail : python bot.py --check)"], lines
    assert status._gauge("The Odds API", 87, 500, " ce mois") == "The Odds API ▰▱▱▱▱▱▱▱▱▱ 87/500 ce mois"
    assert status._gauge("API-Football", 61, 100, " aujourd'hui") == "API-Football ▰▰▰▰▰▰▱▱▱▱ 61/100 aujourd'hui"
    print("ok  sources en lignes courtes (bloquées regroupées, avertissements comptés) ; quotas en jauges")


def main():
    test_services()
    test_sources_and_quotas()
    test_report_and_words()
    test_learning()
    print("tests status : OK")


if __name__ == "__main__":
    main()
