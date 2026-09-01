"""Typed holes for delegated sessions. Do not catch these to invent behavior."""

from __future__ import annotations


class SessionTodo(NotImplementedError):
    """Raised by an architecture stub that a later session must fill in."""

    def __init__(self, session: str, symbol: str, hint: str = ""):
        self.session = session
        self.symbol = symbol
        msg = (
            f"Session {session} must implement {symbol}. "
            f"See LOOP.md."
        )
        if hint:
            msg = f"{msg} {hint}"
        super().__init__(msg)
