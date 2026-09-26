"""Errors whose message is written for the person using the app.

Anything raised as a `TetoRelayError` is shown as-is - in the terminal, the
control panel or the tray - without a traceback, so its message has to say
what happened, why, and what to do next. Anything else is a bug: the user sees
a short summary and the log gets the traceback.
"""

from __future__ import annotations


class TetoRelayError(RuntimeError):
    """A problem the user can fix. The message says how."""


def describe(exc: BaseException, log_file: str | None = None) -> str:
    """One message for any exception, suitable for a person."""
    if isinstance(exc, TetoRelayError):
        return str(exc)
    where = f" The details are in the log: {log_file}" if log_file else ""
    return (
        f"Teto Relay hit an unexpected error ({type(exc).__name__}: {exc}).{where} "
        "Run `python -m teto_relay --doctor` to check your setup."
    )
