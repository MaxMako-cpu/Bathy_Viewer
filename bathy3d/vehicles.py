"""What each body is called and what colour it is drawn in.

The app moves between vessels, and every vessel names its vehicles
differently - ROV1 on one is Hercules on the next. But the names in
``feed.ORDER`` are not really names: they are *slots*, one per pair of fields
in the wire record, and the whole program is keyed on them. ``TETHERS`` pairs
a TMS to its ROV by slot, the vessel is recognised by slot, every actor in the
scene is named after one, and a calibration tie-in stores the slot it was
taken on - on disk, outliving the session.

So a rename must not be a rename. The slot is permanent; what changes is the
**label** drawn over it. Nothing structural moves, and a tie-in taken as
ROV1 still reads correctly once ROV1 has become Hercules.

Colours work the same way, with one rule of their own: a TMS is not given a
colour, it *inherits* one from the ROV it is tethered to, darkened. That is
what makes the pairing readable at a glance in a scene where the two bodies
are metres apart, and it cannot drift out of step because there is only ever
one colour to set.

No Qt here - this is a table of strings that a test can drive directly.
"""

from __future__ import annotations

import colorsys

from .feed import ORDER, TETHERS, slot_for
from .targets import DEFAULT_TARGETS

#: How much darker a TMS is drawn than its own ROV, as a fraction of the ROV's
#: lightness. The two pairs this shipped with were picked by eye and do not
#: share a rule - the red dropped its saturation to 0.63 of the ROV's while the
#: green kept it, and their lightness ratios were 0.78 and 0.69. Only the
#: darkening is common to both, so only the darkening is applied: it is the
#: part that works for any colour rather than for those two. Expect a shade
#: near the original pairs, not identical to them.
TMS_DARKEN = 0.70

#: Lightness a derived TMS colour is never taken below, so a very dark ROV
#: colour still leaves its TMS visible rather than a black speck on a black
#: seabed.
TMS_MIN_LIGHT = 0.12

#: Slot -> the ROV whose colour it follows. TETHERS reads ROV -> TMS, and the
#: colour runs the other way, so it is inverted once here rather than at every
#: call site.
FOLLOWS = {tms: rov for rov, tms in TETHERS.items()}

#: The slots whose colour is actually chosen. Everything else derives.
CHOOSABLE = tuple(s for s in ORDER if s not in FOLLOWS)


def _hex_to_rgb(text: str) -> tuple:
    t = str(text).strip().lstrip("#")
    if len(t) == 3:
        t = "".join(c * 2 for c in t)
    if len(t) != 6:
        raise ValueError(f"not a colour: {text!r}")
    return tuple(int(t[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _rgb_to_hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02x}" for c in rgb)


def darken(colour: str, factor: float = TMS_DARKEN) -> str:
    """A darker shade of ``colour``, kept in the same hue.

    Lightness is scaled rather than the raw channels, so the result stays the
    same colour rather than sliding towards grey - scaling RGB directly washes
    a saturated red out as it darkens it.
    """
    try:
        r, g, b = _hex_to_rgb(colour)
    except ValueError:
        return colour
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return _rgb_to_hex(colorsys.hls_to_rgb(
        h, max(l * float(factor), TMS_MIN_LIGHT), s))


def default_label(slot: str) -> str:
    """What a slot is called before anyone renames it: the slot itself."""
    return slot


def default_colour(slot: str) -> str:
    """The colour a slot starts at, from the styles ``targets.py`` ships."""
    if slot in FOLLOWS:
        return darken(default_colour(FOLLOWS[slot]))
    return DEFAULT_TARGETS.get(slot, {}).get("color", "#f2c14e")


class Fleet:
    """The labels and colours for one vessel's set of bodies.

    Only labels and chosen colours are stored. A TMS colour is never stored,
    because storing it would let it fall out of step with the ROV it belongs
    to - it is derived every time it is asked for.
    """

    def __init__(self):
        self.labels: dict = {}
        self.colours: dict = {}

    # ------------------------------------------------------------- reading

    def label(self, slot: str) -> str:
        text = str(self.labels.get(slot, "")).strip()
        return text or default_label(slot)

    def colour(self, slot: str) -> str:
        if slot in FOLLOWS:
            return darken(self.colour(FOLLOWS[slot]))
        return self.colours.get(slot) or default_colour(slot)

    def style(self, slot: str) -> dict:
        return {"label": self.label(slot), "color": self.colour(slot)}

    def styles(self) -> dict:
        return {slot: self.style(slot) for slot in ORDER}

    def renamed(self) -> bool:
        """True once anything has been changed from the shipped defaults."""
        return any(self.label(s) != default_label(s)
                   or self.colour(s) != default_colour(s) for s in ORDER)

    # ------------------------------------------------------------- writing

    def set_label(self, slot: str, text: str) -> None:
        """Rename one body. Blank puts the slot's own name back."""
        clean = " ".join(str(text or "").split())[:24]
        if clean and clean != default_label(slot):
            self.labels[slot] = clean
        else:
            self.labels.pop(slot, None)

    def set_colour(self, slot: str, colour: str) -> None:
        """Recolour one body. A TMS follows its ROV and cannot be set."""
        if slot in FOLLOWS:
            raise ValueError(
                f"{slot} takes its colour from {FOLLOWS[slot]} - set that one")
        try:
            _hex_to_rgb(colour)
        except ValueError:
            return
        self.colours[slot] = _rgb_to_hex(_hex_to_rgb(colour))

    def reset(self) -> None:
        self.labels.clear()
        self.colours.clear()

    # --------------------------------------------------------- persistence

    def encode(self) -> list:
        """One row per slot that differs from the default. Blank list = stock."""
        rows = []
        for slot in ORDER:
            label = self.labels.get(slot, "")
            colour = self.colours.get(slot, "") if slot not in FOLLOWS else ""
            if label or colour:
                rows.append(f"{slot}|{label}|{colour}")
        return rows

    def load(self, rows) -> None:
        """Rebuild from :meth:`encode`, ignoring anything unreadable.

        Settings outlive versions and slots can be renamed out of the feed
        tuples, so a row naming a slot that no longer exists is dropped rather
        than resurrecting a body nothing sends.
        """
        self.reset()
        for row in rows or []:
            parts = str(row).split("|")
            if len(parts) < 3:
                continue
            # Rows written before the slots were made generic name one vessel's
            # own vehicles; map those forward rather than dropping them.
            slot = slot_for(parts[0])
            if slot not in ORDER:
                continue
            label, colour = parts[1], parts[2]
            if label:
                self.set_label(slot, label)
            if colour and slot not in FOLLOWS:
                self.set_colour(slot, colour)
