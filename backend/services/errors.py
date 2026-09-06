"""Testo leggibile per le eccezioni che finiscono nei log e nella UI.

Sta in un modulo a parte perche' il caso da coprire e' lo stesso ovunque si
parli SSH con la LAN (router, host remoti, docker, terminale): le eccezioni di
asyncssh e i timeout di asyncio arrivano spesso **senza messaggio**, quindi
interpolarli da soli produce una riga che si interrompe dopo i due punti e non
dice niente a chi legge.
"""


def exc_text(e: BaseException) -> str:
    """Messaggio dell'eccezione, o il nome della classe se non ne ha."""
    return str(e).strip() or e.__class__.__name__


def ssh_error(e: BaseException) -> str:
    """Come `exc_text`, ma spiega il caso tipico di un host SSH che non risponde.

    Un "TimeoutError" secco non dice al proprietario cosa guardare: quasi sempre
    significa porta 22 filtrata da un firewall (il caso noto del PC Windows) e
    non un guasto di LANMng.
    """
    if isinstance(e, TimeoutError):        # asyncio.TimeoutError e' un suo alias
        return ("timeout: l'host non ha completato la connessione SSH "
                "(porta 22 chiusa o filtrata da un firewall)")
    return exc_text(e)
