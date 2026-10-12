"""Messages plus lisibles et Telegram plus tolérant (hors ligne) : drapeau du pays de la ligue, icône du marché, pastille 🟢 / 🟠, repère 🔗 des matchs déjà
joués, code coupon en haut et en bas, échappement HTML ; lecture des commandes avec une marge de 15 s, une ligne de journal par coupure (pas par essai),
pause de plus en plus longue pendant une coupure, ligne « connexion rétablie » au retour.
Lancer : python -m tests.test_display_outage"""
import contextlib
import io
import os
import re
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kelly-display-")
os.environ.pop("TELEGRAM_TOKEN", None)
os.environ.pop("XBET_PROXY", None)
os.environ.pop("S365_PROXY", None)
os.environ["QUIET_HOURS"] = "off"

from kelly import config as C, fmt, store, telegram  # noqa: E402

KELLY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kelly")
# clés sans pays : coupes d'Europe et des continents, sélections, matchs en direct d'autres ligues
INTERNATIONAL = ("soccer_uefa_", "soccer_fifa_", "soccer_conmebol_", "soccer_concacaf_", "soccer_caf_", "soccer_africa", "soccer_can_",
                 "soccer_world_", "soccer_international_", "soccer_live_")


def leg(i, **kw):
    return {"key": f"k{i}", "id": f"e{i}", "home": f"H{i}", "away": f"A{i}", "start": "19:00", "league": "Liga", "sport_key": "soccer_spain_la_liga",
            "market": "alternate_totals_corners", "pick": "Plus de 9,5 corners", "odds": 1.9, "xodds": 1.9, "prob": 0.66, "p_x": 0.49} | kw


def test_flags_and_icons():
    keys = set()
    for root, _, files in os.walk(KELLY):
        for f in files:
            if f.endswith(".py"):
                with open(os.path.join(root, f), encoding="utf-8") as fh:
                    keys |= set(re.findall(r'"(soccer_[a-z0-9_]+)"', fh.read()))
    assert len(keys) > 80, len(keys)
    missing = sorted(k for k in keys if fmt.flag({"sport_key": k}) == fmt.WORLD and not k.startswith(INTERNATIONAL))
    assert not missing, f"ligues de pays sans drapeau : {missing}"
    assert fmt.flag({"sport_key": "soccer_spain_segunda_division"}) == "🇪🇸" and fmt.flag({"sport_key": "soccer_xb_argentina_2"}) == "🇦🇷"
    assert fmt.flag({"sport_key": "soccer_xb_northern_ireland"}) == "🇬🇧" and fmt.flag({"sport_key": "soccer_league_of_ireland"}) == "🇮🇪"
    assert fmt.flag({"sport_key": "soccer_epl"}) == fmt.flag({"sport_key": "soccer_fa_cup"}) != fmt.flag({"sport_key": "soccer_spl"})
    assert fmt.flag({"sport_key": "soccer_uefa_champs_league"}) == fmt.WORLD and fmt.flag({}) == fmt.WORLD and fmt.flag({"sport_key": "x"}) == fmt.WORLD
    icon = lambda m: fmt.market_icon({"market": m})      # noqa: E731
    assert icon("alternate_totals_corners") == icon("team_corners_home") == "🚩" and icon("alternate_totals_cards") == icon("team_yellow_total") == "🟨"
    assert icon("team_offsides_total") == "🏁" and icon("team_shots_home") == icon("team_sot_total") == "🎯" and icon("team_fouls_total") == "🛑"
    assert icon("team_throws_home") == "🙌" and icon("totals") == icon("team_totals_home") == icon("btts") == "⚽" and icon("double_chance") == "🛡️"
    assert icon("h2h") == "🏆" and icon("player_shots") == "👤" and icon("inconnu") == icon("") == "👉"
    print("ok  drapeau de chaque ligue de pays, icône de chaque marché")


def test_combo_marks():
    legs = [leg(1), leg(2, home="Lyon <B>", prob=0.62, p_x=0.47, market="team_offsides_total", pick="Plus de 3,5 hors-jeu au total"),
            leg(3, prob=0.78, p_x=0.77, market="double_chance", pick="Double chance : H3 ou Nul")]
    msg = str(fmt.fair_combo(616.9, legs, 0.02, title="🎲 COMBINÉ AUDACIEUX", coupon="SKU6L", when="08:50", weak={"k2"}, shared={"e1": 3},
                             note="🎲 Ticket de loterie, mise très petite.", extra={"k2": "mal payé"}))
    head, *blocks = msg.split("\n\n")
    assert head == ("<b>🎲 COMBINÉ AUDACIEUX — cote 616,9</b>\n🎯 3 paris · 2 % de chances\n🎲 Ticket de loterie, mise très petite.\n"
                    "🎟️ <code>SKU6L</code>  <i>touche pour copier</i>"), head
    assert blocks[0] == ("🇪🇸 <i>19h00 · Liga</i>\n<b>1. H1 – A1</b> · 🔗3\n🚩 Plus de 9,5 corners · <b>1,90</b>\n🟢 <i>Kelly 66 % · 1xBet 49 % (+17)</i>"), blocks[0]
    assert blocks[1] == ("🇪🇸 <i>19h00 · Liga</i>\n<b>2. Lyon &lt;B&gt; – A2</b>\n🏁 Plus de 3,5 hors-jeu au total · <b>1,90</b>\n"
                         "🟠 <i>Kelly 62 % · 1xBet 47 % (+15) · mal payé</i>"), blocks[1]       # nom d'équipe échappé
    assert blocks[2] == "🇪🇸 <i>19h00 · Liga</i>\n<b>3. H3 – A3</b>\n🛡️ Double chance : H3 ou Nul · <b>1,90</b>\n🟢 <i>Kelly 78 % · 1xBet 77 %</i>", blocks[2]  # écart < 3 pts
    assert len(blocks) == 4 and blocks[3].startswith("━━━━━━━━━━━━\n<i>🟢 solide · 🟠 moins sûr · 🔗 déjà dans N de tes tickets</i>\n"), blocks
    assert "<i>💰 Mise : fixe ton capital" in blocks[3] and "🎟️ Code : <code>SKU6L</code>" in blocks[3] and "Cotes 1xBet de 08:50" in blocks[3], blocks[3]
    assert "<B>" not in msg and not any(w in msg.lower() for w in fmt.JARGON if w != "score"), msg
    # combiné du matin, avec mise : le montant en tête ; sans pari moins sûr ni match déjà joué, pas de légende
    plain = str(fmt.combo("🛡️ SÛR (cote 5 à 7)", legs[:1], 1.9, 0.66, stake_txt="1 500 FCFA", coupon="AB12"))
    assert plain.startswith("<b>🛡️ SÛR — cote 1,90</b>\n🎯 1 paris · 66 % de chances · espérance +25,4 %\n💰 Mise : <b>1 500 FCFA</b>\n"
                            "🎟️ <code>AB12</code>  <i>touche pour copier</i>\n\n🇪🇸"), plain
    assert "solide" not in plain and "🔗" not in plain and "\n\n━━━━━━━━━━━━\n🎟️ Code : <code>AB12</code>" in plain and "Mon capital" not in plain, plain
    # la marque d'écart : négative quand Kelly est sous 1xBet
    assert fmt.chances_line(leg(4, prob=0.40, p_x=0.50)) == "🟢 <i>Kelly 40 % · 1xBet 50 % (−10)</i>"
    assert fmt.chances_line(leg(4, p_x=None), weak=True) == "🟠 <i>Kelly 66 %</i>"
    print("ok  combiné : code en haut et en bas, pastilles, 🔗, écart en points, légende seulement si utile, HTML échappé")


@contextlib.contextmanager
def fake_net(script):
    """telegram.api rejoue `script` (exception ou liste de mises à jour, une par appel de getUpdates), time.sleep enregistré, stderr capturé."""
    calls, pauses, err = [], [], io.StringIO()
    old_api, old_sleep, old_env = telegram.api, telegram.time.sleep, {k: os.environ.get(k) for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")}
    os.environ.update(TELEGRAM_TOKEN="t", TELEGRAM_CHAT_ID="1")
    steps = iter(script)

    def api(method, http_timeout=40, **p):
        calls.append((method, http_timeout, p.get("timeout")))
        if method != "getUpdates":
            return {"message_id": 1}
        step = next(steps)
        if isinstance(step, Exception):
            raise step
        return step
    telegram.api, telegram.time.sleep = api, pauses.append
    telegram._outage.update(n=0, t0=None)
    try:
        with contextlib.redirect_stderr(err):
            yield calls, pauses, err
    finally:
        telegram.api, telegram.time.sleep = old_api, old_sleep
        for k, v in old_env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def test_outage():
    store.save("telegram.json", {"offset": 0})
    cut = telegram.TelegramError("réseau : Read timed out (read timeout=25)")
    got = []
    script = [cut] * 5 + [[{"update_id": 3, "message": {"text": "état", "chat": {"id": 1}}}]] + [cut] * 2 + [[]]
    with fake_net(script) as (calls, pauses, err):
        for _ in script:
            telegram.poll(got.append, timeout=10)
    log = err.getvalue().splitlines()
    assert {(t, w) for m, t, w in calls if m == "getUpdates"} == {(25, 10)}, calls                  # marge de 15 s au-delà du long polling
    assert pauses == [2, 4, 8, 15, 15, 2, 4], pauses                                                # de plus en plus long, remis à 2 s après la reprise
    assert log == ["[telegram] lecture des commandes échouée : réseau : Read timed out (read timeout=25)",
                   "[telegram] connexion rétablie après 5 échecs (0 s)",
                   "[telegram] lecture des commandes échouée : réseau : Read timed out (read timeout=25)",
                   "[telegram] connexion rétablie après 2 échecs (0 s)"], log                       # une ligne par coupure, pas une par essai
    assert got == ["état"] and store.load("telegram.json", {})["offset"] == 4, (got, store.load("telegram.json", {}))     # rien de perdu
    # longue coupure : un rappel tous les 10 échecs ; --commands (timeout 0) : aucune pause
    with fake_net([cut] * 21) as (calls, pauses, err):
        for _ in range(21):
            telegram.poll(lambda t: None)
    log = err.getvalue().splitlines()
    assert pauses == [] and len(log) == 3 and log[1].startswith("[telegram] connexion toujours coupée : 10 échecs depuis ") and "20 échecs" in log[2], log
    assert {t for m, t, w in calls} == {15}, calls
    telegram._outage.update(n=0, t0=None)
    print("ok  Telegram : marge de 15 s, une ligne par coupure, pauses 2-4-8-15 s, « connexion rétablie », aucun message perdu")


def main():
    test_flags_and_icons()
    test_combo_marks()
    test_outage()
    print("tests affichage et coupures Telegram : OK")


if __name__ == "__main__":
    main()
