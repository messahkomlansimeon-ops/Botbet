"""Lignes de matchs par équipe (FotMob, 365scores, API-Football) : l'identifiant de l'ADVERSAIRE en dernier élément.

Une ligne ancienne est [jour, (ligue,) domicile, puis des paires (pour, contre)] : son nombre d'éléments après les colonnes d'en-tête (`r_stats` = 2 pour FotMob et 365scores, 3 pour
API-Football) est PAIR. Une ligne neuve écrit TOUTES les paires (rien d'omis) puis l'identifiant de l'adversaire : ce nombre est IMPAIR. La parité dit donc, sans ambiguïté, si le dernier
élément est un adversaire ; les lecteurs, tous par indice, ne voient pas ce dernier élément. L'identifiant est celui de la source de la ligne (FotMob, 365scores ou API-Football)."""


def opponent(row, r_stats):
    """Identifiant de l'adversaire d'une ligne, ou None (ligne d'avant ce champ, ou adversaire inconnu)."""
    if not row or (len(row) - r_stats) % 2 != 1 or row[-1] is None:
        return None
    return row[-1]


def with_opponent(row, opp, r_stats):
    """La ligne `row` (en-tête et paires COMPLÈTES) avec l'identifiant `opp` de l'adversaire en dernier élément ; inchangée sans identifiant."""
    if opp is None or (len(row) - r_stats) % 2 != 0:
        return row
    return row + [opp]


def cell(row, j, r_stats):
    """Colonne `j` d'une ligne de statistiques, None si la ligne est plus courte (ligne écrite avant l'ajout de la clé : donnée inconnue, jamais 0) ET jamais le dernier élément d'une
    ligne qui porte l'identifiant de l'adversaire : une clé ajoutée plus tard à la fin de KEYS ne le lira donc jamais comme une statistique."""
    if j >= len(row) or ((len(row) - r_stats) % 2 == 1 and j == len(row) - 1):
        return None
    return row[j]
