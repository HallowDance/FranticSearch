"""Card lists as text for other tools (PLAN.md § Core workflows 3, phase 5).

An item is a mapping with ``qty``, ``name``, ``set_code``, ``collector_number``,
``finish`` and optionally ``board``. Formats:

- ``text``: ``2 Sol Ring``
- ``moxfield``: ``2 Sol Ring (C21) 263 *F*``; deck exports get board headers, which
  the deck importer reads back
- ``tcgplayer``: TCGplayer mass entry, ``2 Sol Ring [C21]``
- ``csv``: Moxfield collection CSV columns (plus ``Board`` for decks)
"""

from __future__ import annotations

import csv
import io

FORMATS = {"moxfield": "Moxfield", "text": "Plain text", "tcgplayer": "TCGplayer mass entry", "csv": "CSV"}
_FLAG = {"foil": " *F*", "etched": " *E*"}
_SECTION = {"commander": "Commander", "main": "Deck", "side": "Sideboard", "maybe": "Maybeboard"}


def _line(item, fmt: str) -> str:
    if fmt == "moxfield":
        return (f"{item['qty']} {item['name']} ({item['set_code'].upper()}) {item['collector_number']}"
                + _FLAG.get(item["finish"], ""))
    if fmt == "tcgplayer":
        return f"{item['qty']} {item['name']} [{item['set_code'].upper()}]"
    return f"{item['qty']} {item['name']}"


def render(items, fmt: str, boards: bool = False) -> str:
    """The list in ``fmt``. ``boards``: group by board with section headers (decks)."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown export format {fmt!r}")
    items = list(items)
    if fmt == "csv":
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        w.writerow(["Count", "Name", "Edition", "Collector Number", "Foil"] + (["Board"] if boards else []))
        for i in items:
            w.writerow([i["qty"], i["name"], i["set_code"], i["collector_number"],
                        "" if i["finish"] == "nonfoil" else i["finish"]] + ([i["board"]] if boards else []))
        return out.getvalue()
    if not boards:
        return "".join(_line(i, fmt) + "\n" for i in items)
    parts = []
    for board, title in _SECTION.items():
        rows = [i for i in items if i["board"] == board]
        if rows:
            parts.append(title + "\n" + "".join(_line(i, fmt) + "\n" for i in rows))
    return "\n".join(parts)
