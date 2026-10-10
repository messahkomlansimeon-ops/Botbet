"""Pronostics stats solides que 1xBet n'a pas encore ouverts (il ouvre tirs / fautes / touches quelques heures avant le match) : montrés « ⏳ Pas encore
ouverts chez 1xBet » dans /analyse et /match (au lieu de « Aucun pronostic assez solide »), relus en direct quand le match est dans la fenêtre des stats,
et la fenêtre horaire relit d'abord les matchs les plus proches du coup d'envoi. Hors ligne. Lancer : python -m tests.test_pending_stats"""
from datetime import timedelta

from tests import test_combi_track as T     # noqa: I001 - fixe DATA_DIR et les faux réseaux avant d'importer kelly
from kelly import app, config as C, statswindow, store, telegram, xprice  # noqa: E402

NOW = T.at(10)


def shot(i, hours, **kw):
    """Pronostic « Parma<i> : moins de 3,5 tirs cadrés » à 74 %, vu « absent » au relevé du matin (xnone), match dans `hours` heures."""
    return T.prono(i, id=f"p{i}", home=f"Inter{i}", away=f"Parma{i}", event=f"Inter{i} - Parma{i}", market="team_sot_away", name="Under",
                   point=3.5, key=f"p{i}|team_sot_away||3.5|Under", pick=f"Parma{i} : moins de 3,5 tirs cadrés", prob=0.74, p0=0.74, min_odds=1.42,
                   xodds=None, xodds0=None, xref=None, xev=None, xnone=1, psh={}, league="Serie A",
                   commence=(NOW + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:00Z"), start=(NOW + timedelta(hours=hours)).strftime("%H:%M")) | kw


def test_pending():
    rows = [shot(1, 6), shot(1, 6, market="team_sot_home", key="p1|team_sot_home||5.5|Over", prob=0.5), shot(2, 0.3), T.prono(3, xodds=None, xnone=1)]
    got = app._pending(rows, NOW)
    assert [c["key"] for c in got] == ["p1|team_sot_away||3.5|Under"], "solide, stats, pas sur le point de commencer ; ni 50 %, ni buts, ni match imminent"
    txt = app.pending_lines(rows, NOW)
    assert "⏳ Pas encore ouverts chez 1xBet" in txt and "toutes les 3 h, puis chaque heure dans les 4 dernières heures" in txt, txt
    assert "• Parma1 : moins de 3,5 tirs cadrés · Kelly 74 % · joue dès 1,42" in txt, txt
    assert app.pending_lines([T.prono(5)], NOW) == ""
    print("ok  pronostics stats solides en attente de leur cote : repérés, avec la cote mini et l'heure de relecture")


def test_commands():
    T.reset()
    store.save("today.json", {"day": T.DAY, "picks": [], "pronos": [shot(1, 6)]})
    out = app._handle_command("/match Parma1")
    assert "⏳ Pas encore ouverts chez 1xBet" in out and "Aucun pronostic assez solide" not in out, out
    seen, sent = [], []
    old = (xprice.live_check, telegram.send)
    xprice.live_check = lambda ids, now=None, fetch=None, fresh_min=None: (seen.append(set(ids)), ([shot(1, 2, xodds=1.55, xnone=None)], set(ids), "10:00"))[1]
    telegram.send = lambda t, *a, **k: sent.append(t) or True
    try:
        assert app.reread_match([shot(1, 30)], "Parma1", NOW) == [shot(1, 30)] and not seen, "hors fenêtre (30 h) : pas de relecture"
        got = app.reread_match([shot(1, 2)], "Parma1", NOW)
        assert seen == [{"p1"}] and got[0]["xodds"] == 1.55 and "viennent d'ouvrir" in sent[0], (seen, sent)
    finally:
        xprice.live_check, telegram.send = old
    print("ok  /match montre l'attente ; /analyse et /match relisent le match en direct dans la fenêtre des stats")


def test_window_order():
    pr = [shot(i, h, xgid=700 + i) for i, h in ((1, 3.5), (2, 1.0), (3, 2.0))]
    doc = statswindow.load(T.DAY)
    old = C.STATS_WINDOW_MAX_MATCHES
    try:
        C.STATS_WINDOW_MAX_MATCHES = 2
        assert [m for m, _ in statswindow.select(pr, NOW, doc)] == ["p2", "p3"], "les plus proches du coup d'envoi d'abord (dernière chance)"
    finally:
        C.STATS_WINDOW_MAX_MATCHES = old
    print("ok  fenêtre des stats : les matchs les plus proches du coup d'envoi relus d'abord")


def test_wide_window():
    """1xBet ouvre ces paris à des heures variables (13 h avant pour un match, fermés 11 h avant pour un autre du même championnat) : fenêtre large,
    relue toutes les 3 h de loin et chaque heure dans les 4 dernières heures."""
    assert C.STATS_WINDOW_MAX_H == 24 and C.STATS_WINDOW_NEAR_H == 4
    assert statswindow.in_window(shot(1, 13), NOW) and statswindow.in_window(shot(1, 1), NOW) and not statswindow.in_window(shot(1, 0.5), NOW)
    assert statswindow.retry_min(shot(1, 13), NOW) == 180 and statswindow.retry_min(shot(1, 3), NOW) == C.STATS_WINDOW_RETRY_MIN
    doc = statswindow.load(T.DAY)
    doc["reads"] = {"p1": {"n": 1, "at": (NOW - timedelta(minutes=90)).isoformat()}, "p2": {"n": 1, "at": (NOW - timedelta(minutes=90)).isoformat()}}
    pr = [shot(1, 10, xgid=701), shot(2, 3, xgid=702), shot(3, 12, xgid=703)]
    assert [m for m, _ in statswindow.select(pr, NOW, doc)] == ["p2", "p3"], "p1 (10 h) relu il y a 1 h 30 : attend 3 h ; p2 (3 h) : chaque heure ; p3 jamais lu"
    print("ok  fenêtre large : relu toutes les 3 h de loin, chaque heure près du match")


def test_picker_shows_pending():
    """Les types stats en attente chez 1xBet restent cochables (« ⏳ »), sinon on ne pourrait pas les demander le matin."""
    T.reset()
    store.save("today.json", {"day": T.DAY, "picks": [], "pronos": [T.prono(1)] + [shot(i, 30 + i) for i in range(3)]})
    text, mk = app.kinds_picker(20)
    texts = [b["text"] for row in mk["inline_keyboard"] for b in row]
    assert "⬜ Tirs cadrés (0 + 3 ⏳)" in texts and "⏳ pas encore ouverts chez 1xBet" in text, (texts, text)
    print("ok  cases à cocher : les types stats pas encore ouverts restent cochables (⏳)")


def test_open_stats_throttle():
    """« combi » ne relit en direct que des pronostics solides de matchs connus de 1xBet, pas plus souvent que le passage horaire."""
    app._OPEN_READ.clear()
    seen, old = [], (xprice.live_check, telegram.send)
    xprice.live_check = lambda ids, now=None, fetch=None, fresh_min=None: (seen.append(set(ids)), ([], set(ids), "10:00"))[1]
    telegram.send = lambda *a, **k: True
    try:
        rows = [shot(1, 2, xgid=701), shot(2, 2, xgid=None), shot(3, 2, xgid=703, prob=0.5)]       # p2 inconnu de 1xBet, p3 pas solide
        app._open_stats(rows, None, NOW)
        app._open_stats(rows, None, NOW)                                                  # relu il y a un instant : pas de nouvelle requête
    finally:
        xprice.live_check, telegram.send = old
    assert seen == [{"p1"}], seen
    print("ok  relecture en direct de « combi » : pronostics solides connus de 1xBet, au rythme du passage horaire")


def test_live_check_stamps():
    """Une cote relue en direct porte l'heure de cette relecture (c["xat"]) : /analyse ne la date plus du relevé du matin."""
    T.reset()
    store.save("today.json", {"day": T.DAY, "picks": [], "pronos": [shot(1, 3, xgid=701)]})
    fake = lambda events, deadline=None, wanted=None, live=False, fresh_min=None: {"p1": {"gid": 701, "px": {}, "refs": {}}}   # noqa: E731
    old = xprice.price
    xprice.price = lambda c, px, refs=None: (c.update(xodds=1.6, xev=0.1), "value")[1]
    try:
        pr, got, hhmm = xprice.live_check({"p1"}, NOW, fetch=fake)
    finally:
        xprice.price = old
    assert got == {"p1"} and store.load("today.json", {})["pronos"][0]["xat"] == NOW.strftime("%H:%M"), store.load("today.json", {})["pronos"][0]
    print("ok  cote relue en direct : datée de sa relecture")


def test_later_expires():
    """Un message mis de côté depuis plus d'une heure n'est pas rejoué en silence : Kelly le dit."""
    import time as _t
    sent, handled = [], []
    old = telegram.send
    telegram.send = lambda text, **k: sent.append(str(text)) or True
    try:
        store.save(telegram.LATER, [{"cb": False, "args": ["combi 20"], "at": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(_t.time() - 7200))},
                                    {"cb": False, "args": ["bilan"], "at": _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime())}])
        assert telegram._run_later(lambda t: handled.append(t) or "ok", None)
    finally:
        telegram.send = old
    assert handled == ["bilan"] and "« combi 20 »" in sent[0] and "n'a pas été fait" in sent[0], (handled, sent)
    print("ok  message mis de côté trop longtemps : pas rejoué, et c'est dit")


def main():
    test_pending()
    test_commands()
    test_window_order()
    test_wide_window()
    test_picker_shows_pending()
    test_open_stats_throttle()
    test_live_check_stamps()
    test_later_expires()
    print("tests pronostics stats en attente : OK")


if __name__ == "__main__":
    main()
