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
        assert out.startswith("🩺 État de Kelly — 08h30") and "⚙️ Services" in out and "📡 Sources de stats" in out and "🔌 API et quotas" in out, out
        assert out.endswith("1 point à regarder (❌ ci-dessus)."), out
        for w in ("status", "Status", "/status", "statut", "santé", "/status@KellyBot"):
            assert chat.quick(w) and chat.quick(w).startswith("🩺"), w
        assert chat.quick("état").startswith(("Pas encore", "⏳", "✅", "⚠️")), "« état » reste la progression du matin"
        assert chat.quick("Statut Quo") is None
        assert app._handle_command("/status").startswith("🩺")
    finally:
        status.service_lines, status.api_lines = old
    assert status._when("Sat 2026-10-10 05:18:43 GMT", NOW) == "05h18" and status._when("", NOW) == "" and status._when("n/a", NOW) == ""
    print("ok  rapport « status » : services, sources, API ; mots reconnus ; « état » inchangé")


def test_learning():
    from kelly import calib, live, store
    store.save(calib.FILE, {"goals": [29, 22, 19.0], "shots": [70, 30, 45.5]})
    calib._memo.clear()
    store.save(live.CALIB_FILE, {"src|365scores": [12, 9, 8.0], "goals|60-75": [5, 4, 3.5]})          # ancien format : ignoré
    assert "Live : aucun pari live retenu et réglé" in "\n".join(status.learning_lines())
    store.save(live.CALIB_FILE, {"v": live.CALIB_VERSION, "k": {"h2h|45-60": [12, 9, 8.0], "goals|60-75": [5, 4, 3.5]}})
    lines = status.learning_lines()
    text = "\n".join(lines)
    assert "• Buts : 29/60 · 76 % gagnés pour 66 % annoncés · trop prudente, correction dans 31 résultats" in text, text
    assert "• Tirs : 70 réglés · 43 % gagnés pour 65 % annoncés · trop confiante, correction active (×" in text, text
    assert "Ensemble (avis de Kelly face au marché) : buts 0/100" in text and "Live 1N2 45-60 : 12/60" in text and "Live buts 60-75 : 5/60" in text, text
    assert "données légères" not in text
    print("ok  section « 🧠 Apprentissage » : avant-match, ensemble, live par famille et par tranche")


def main():
    test_services()
    test_report_and_words()
    test_learning()
    print("tests status : OK")


if __name__ == "__main__":
    main()
