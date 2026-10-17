"""Accès paresseux à kelly.app depuis les modules issus de son découpage (cmd_*, run_*, analysis, commands) : A.nom lit `kelly.app.nom` AU MOMENT DE L'APPEL (un `app.nom = ...` de test, ou une
fonction ré-exportée, est donc vu), sans import circulaire à la charge."""


class _App:
    def __getattr__(self, name):
        from . import app
        return getattr(app, name)


A = _App()
