"""Split Scryfall type lines into supertypes, card types and subtypes.

"Legendary Creature — Human Wizard // Creature — Human Insect" ->
({"Legendary"}, {"Creature"}, {"Human", "Wizard", "Insect"}). All faces count.
"""

from __future__ import annotations

from functools import lru_cache

SUPERTYPES = ("Basic", "Legendary", "Snow", "World", "Ongoing", "Elite", "Host")
CARD_TYPES = (
    "Artifact", "Battle", "Creature", "Enchantment", "Instant", "Kindred", "Land",
    "Planeswalker", "Sorcery", "Tribal", "Conspiracy", "Dungeon", "Phenomenon", "Plane",
    "Scheme", "Vanguard",
)
# Subtypes with a space in them (otherwise subtypes are single words).
_MULTIWORD = ("Time Lord",)


# Cached: filtering a collection parses the same few thousand type lines on every page view.
@lru_cache(maxsize=8192)
def parse(type_line: str | None) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    supers: set[str] = set()
    types: set[str] = set()
    subs: set[str] = set()
    for face in (type_line or "").split(" // "):
        left, _, right = face.partition("—")
        for word in left.split():
            if word in SUPERTYPES:
                supers.add(word)
            elif word in CARD_TYPES:
                types.add(word)
        right = right.strip()
        for multi in _MULTIWORD:
            if multi in right:
                subs.add(multi)
                right = right.replace(multi, " ")
        subs.update(right.split())
    return frozenset(supers), frozenset(types), frozenset(subs)
