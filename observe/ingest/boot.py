"""Hub-side boot classification.

Adapted from hostwatch/events/boot.py (hostwatch, same owner). The agent does
the evidence gathering, because only it can read the heartbeat file, pstore, the
watchdog boot status and the previous boot's journal. It sends one boot event
per detected reboot whose kind is boot.<classification>. This module reads that
kind and reduces it to the three states Observe shows and acts on:

- clean: the previous boot ended with a completed shutdown sequence.
- crash: evidence of a panic, a watchdog reset, a power cut or an abrupt end.
- unknown: no evidence either way, including an agent that merely stopped.

Nothing is guessed. An unrecognised boot kind is unknown, never clean, so a
newer agent can not cause a crash to be shown as a clean reboot.
"""

from __future__ import annotations

from .schema import Event

CLEAN = "clean"
CRASH = "crash"
UNKNOWN = "unknown"

# Agent classifications from hostwatch's boot classifier. unclean_shutdown
# appears in older agents and fixtures and is treated as a crash.
# ha_Int_soc (docs/CRASH-FORENSICS.md) classifies with clean_reboot, kernel_fault, silent_stop and
# core_restart. core_restart is a Core-only unclean stop with no host reboot, so it is unknown here
# and still raises its warning event; it never marks the host as cleanly shut down.
_CLEAN_KINDS = {"clean_shutdown", "clean_reboot"}
_CRASH_KINDS = {"kernel_panic", "watchdog_reset", "power_loss", "unknown_unclean",
                "unclean_shutdown", "kernel_fault", "silent_stop"}
BOOT_PREFIX = "boot."


def is_boot_event(ev: Event) -> bool:
    return ev.kind.startswith(BOOT_PREFIX)


def classify_boot(ev: Event) -> str:
    """Reduce a boot event to clean, crash or unknown."""
    name = ev.kind[len(BOOT_PREFIX):]
    if name in _CLEAN_KINDS:
        return CLEAN
    if name in _CRASH_KINDS:
        return CRASH
    return UNKNOWN


def clean_flag(classification: str) -> int | None:
    """Value for hosts.clean_shutdown: 1 clean, 0 crash, None unknown."""
    return {CLEAN: 1, CRASH: 0}.get(classification)


def classify_events(events: list[Event]) -> dict[int, tuple[str, int | None]]:
    """Map the index of each boot event to (classification, clean_shutdown flag)."""
    out: dict[int, tuple[str, int | None]] = {}
    for i, ev in enumerate(events):
        if is_boot_event(ev):
            c = classify_boot(ev)
            out[i] = (c, clean_flag(c))
    return out
