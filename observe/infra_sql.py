"""SQL that more than one infrastructure module needs, kept here so the modules that write the
map tables and the service that triggers them do not import each other."""

from __future__ import annotations


def current_ids_sql(where: str = "") -> str:
    """SQL selecting the ids of the current row of every (switch, port, name): the newest
    observation, with the later id breaking a tie. Every reader of "the current value" uses
    this, never MAX(id)."""
    return ("SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY switch_id, port_key, "
            "name ORDER BY observed_at DESC, id DESC) AS rn FROM port_properties "
            f"{where}) WHERE rn=1")
