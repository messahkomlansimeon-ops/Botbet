"""Stat d'un joueur dans un match pour le règlement des marchés player_* (tirs, tirs cadrés, fautes), PAR IDENTIFIANT et filtrée par équipe.

Pour le jour où une source de stats par joueur revient (aujourd'hui aucune : le marché joueur est coupé, config.PROP_MARKETS). Le dictionnaire
`players` d'un match est {identifiant: {"name", "team_id", "shots", "shots_on_target", "fouls"}} (stats.fixture_stats, /fixtures/players) ; l'ancien
format {nom: {"shots"...}} reste lu (le nom sert d'identifiant, sans équipe). Deux joueurs de même nom dans le match (un par équipe) ne
s'écrasent plus : avec l'équipe du pari (`team` : identifiant ou nom), on prend celui de cette équipe ; sans équipe, un nom présent deux fois est
AMBIGU -> None (le pari n'est pas réglé au hasard)."""
from .sources import names


def _same_team(entry, team):
    if team is None:
        return True
    mine = entry.get("team_id") if entry.get("team_id") is not None else entry.get("team")
    if mine is None:
        return True                                 # ancien format : pas d'équipe connue, pas de filtre
    return str(mine) == str(team) or names.team_sim(str(entry.get("team") or ""), str(team)) >= 0.85


def find(players, name, team=None):
    """(identifiant, stats) du joueur `name` (nom approché : « K. Mbappé », « Kylian Mbappe ») de l'équipe `team` ; None s'il est absent ou ambigu."""
    pool = {pid: e for pid, e in (players or {}).items() if isinstance(e, dict) and _same_team(e, team)}
    label = {pid: str(e.get("name") or pid) for pid, e in pool.items()}
    got = names.player_match(name or "", list(label.values()))
    if got is None:
        return None
    ids = [pid for pid, nm in label.items() if nm == got]
    return (ids[0], pool[ids[0]]) if len(ids) == 1 else None


def stat(fstats, name, field, team=None):
    """Valeur `field` (« shots », « shots_on_target », « fouls ») du joueur `name` dans les stats du match `fstats` ; None s'il est absent (remplaçant
    jamais entré), ambigu ou sans cette donnée. Remplace stats.player_stat (par nom) dans le règlement."""
    got = find((fstats or {}).get("players"), name, team)
    return None if got is None else got[1].get(field)
