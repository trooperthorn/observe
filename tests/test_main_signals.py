"""python -m observe must serve on Windows, whose event loop has no add_signal_handler."""

from __future__ import annotations

import signal
from types import SimpleNamespace

from observe import __main__ as entry


class _Loop:
    def __init__(self, implemented: bool) -> None:
        self.implemented = implemented
        self.handlers: dict[int, object] = {}
        self.scheduled: list[object] = []

    def add_signal_handler(self, sig, cb):  # type: ignore[no-untyped-def]
        if not self.implemented:
            raise NotImplementedError
        self.handlers[sig] = cb

    def call_soon_threadsafe(self, cb):  # type: ignore[no-untyped-def]
        self.scheduled.append(cb)
        cb()


def test_loop_handlers_used_when_implemented():
    loop, server = _Loop(True), SimpleNamespace(should_exit=False)
    entry._install_stop_signals(loop, server)
    assert set(loop.handlers) == {signal.SIGTERM, signal.SIGINT}
    loop.handlers[signal.SIGINT]()
    assert server.should_exit is True


def test_falls_back_to_signal_signal_when_not_implemented(monkeypatch):
    installed: dict[int, object] = {}
    monkeypatch.setattr(entry.signal, "signal", lambda sig, h: installed.setdefault(sig, h))
    loop, server = _Loop(False), SimpleNamespace(should_exit=False)
    entry._install_stop_signals(loop, server)
    assert set(installed) == {signal.SIGTERM, signal.SIGINT}
    installed[signal.SIGINT](signal.SIGINT, None)
    assert server.should_exit is True
    assert len(loop.scheduled) == 1
