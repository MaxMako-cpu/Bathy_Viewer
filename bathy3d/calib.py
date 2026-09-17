"""Depth calibration: tying the depth feed to the grid.

The vehicles and the terrain get their depth from two unrelated places. A
vehicle's depth comes from a pressure sensor and is a conversion from pressure
through an assumed water density; the grid's depth is a conversion from
seismic two-way time through an assumed sound velocity. Neither is a direct
measurement of the seabed, so they do not have to agree - and on a regional
grid they do not. On the BOEM file the disagreement is tens of metres, which
is why a vehicle known to be on the bottom draws well clear of a preplot
draped on the same spot.

A **tie-in point** is one moment when the vehicle was certainly on the bottom.
It records what the grid said and what the feed said at that instant. The
difference is the error, there, at that depth.

The important part is that the error is usually a *percentage*, not a fixed
number of metres: a velocity error of 2.4% is 21 m at 900 m and 59 m at
2450 m. One tie-in therefore only fixes its own neighbourhood. Two at clearly
different depths give a line through both, and the line separates the two
causes by itself - the part that does not change with depth is a datum or
sensor-zero difference, the part that grows with depth is velocity.

Everything here is plain arithmetic on numbers; no Qt and no VTK, so it can be
driven straight from a test.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

#: Least depth spread, in metres, before two tie-ins are allowed to set a
#: slope. Points 15 m apart in depth carry no information about how the error
#: grows: dividing the difference in offsets by a tiny difference in depth
#: turns a few centimetres of noise into a wild percentage. Below this the fit
#: falls back to a plain average offset, which is honest about knowing less.
MIN_SPREAD_M = 200.0

#: Largest believable scale error, as a fraction. A sound-velocity assumption
#: is not 15% wrong; a fit that says so is being driven by a bad tie-in, so it
#: is refused and the average offset used instead.
MAX_SCALE = 0.15

#: How far outside the tied depth range the model is still trusted, as a
#: fraction of that range, with a floor in metres. Beyond it the model is
#: extrapolating past everything it has been shown.
EDGE_FRAC = 0.15
EDGE_MIN_M = 50.0


@dataclass
class TiePoint:
    """One moment a vehicle was known to be sitting on the bottom."""

    vehicle: str
    x: float            # CRS easting of the fix
    y: float            # CRS northing
    grid_depth: float   # metres below datum, positive down, from the probe
    feed_depth: float   # metres, positive down, exactly as the feed sent it
    when: float = field(default_factory=time.time)
    grid: str = ""      # the grid file this was tied on

    @property
    def offset(self) -> float:
        """Metres to add to this feed depth to land on the grid's seabed."""
        return self.grid_depth - self.feed_depth

    def encode(self) -> str:
        return "|".join((
            self.vehicle, f"{self.x:.3f}", f"{self.y:.3f}",
            f"{self.grid_depth:.3f}", f"{self.feed_depth:.3f}",
            f"{self.when:.0f}", self.grid))

    @staticmethod
    def decode(text: str):
        """Rebuild from :meth:`encode`. Returns None on anything malformed.

        Settings outlive versions, so a row written by an older build - or
        edited by hand - must not stop the app starting.
        """
        parts = str(text).split("|")
        if len(parts) < 6:
            return None
        try:
            return TiePoint(
                vehicle=parts[0],
                x=float(parts[1]), y=float(parts[2]),
                grid_depth=float(parts[3]), feed_depth=float(parts[4]),
                when=float(parts[5]),
                grid=parts[6] if len(parts) > 6 else "")
        except (TypeError, ValueError):
            return None


@dataclass
class Model:
    """``corrected = a + b * feed_depth``, fitted from the tie-in points."""

    kind: str = "none"          # "none" | "fixed" | "scale"
    a: float = 0.0
    b: float = 1.0
    n: int = 0
    rms: float | None = None    # spread of the points about the fit, metres
    lo: float = float("nan")    # tied feed-depth range
    hi: float = float("nan")
    note: str = ""              # why this kind was chosen

    # ------------------------------------------------------------- applying

    @property
    def on(self) -> bool:
        return self.kind != "none"

    def apply(self, feed_depth):
        """Correct one depth. An unfitted model returns it untouched."""
        if not self.on or feed_depth is None or not math.isfinite(feed_depth):
            return feed_depth
        return self.a + self.b * float(feed_depth)

    def offset_at(self, feed_depth: float) -> float:
        """What the correction amounts to at this depth, in metres."""
        if not self.on or not math.isfinite(feed_depth):
            return 0.0
        return self.apply(feed_depth) - float(feed_depth)

    @property
    def scale_pct(self) -> float:
        """The part of the error that grows with depth, as a percentage."""
        return (self.b - 1.0) * 100.0

    def outside(self, feed_depth: float) -> bool:
        """True where the model is extrapolating past its tie-ins."""
        if not self.on or feed_depth is None or not math.isfinite(feed_depth):
            return False
        if not (math.isfinite(self.lo) and math.isfinite(self.hi)):
            return False
        edge = max((self.hi - self.lo) * EDGE_FRAC, EDGE_MIN_M)
        return feed_depth < self.lo - edge or feed_depth > self.hi + edge

    # ----------------------------------------------------------- describing

    def describe(self) -> str:
        """One line for the status bar and the dialog."""
        if not self.on:
            return "No calibration - depths drawn exactly as the feed sends them."
        if self.kind == "fixed":
            head = f"Fixed offset {self.a:+.1f} m"
        else:
            head = (f"{self.a:+.1f} m fixed, {self.scale_pct:+.2f}% of depth"
                    f" (that is {self.offset_at(1650.0):+.1f} m at 1650 m)")
        tail = f" - from {self.n} tie-in{'s' if self.n != 1 else ''}"
        if math.isfinite(self.lo) and self.hi - self.lo > 1.0:
            tail += f" over {self.lo:,.0f}-{self.hi:,.0f} m"
        elif math.isfinite(self.lo):
            tail += f" at {self.lo:,.0f} m"
        if self.rms is not None:
            tail += f", scatter {self.rms:.1f} m"
        return head + tail + (f". {self.note}" if self.note else "")


def fit(points) -> Model:
    """Build the correction from however many tie-in points there are.

    One point can only give a fixed offset. Two or more spread over a real
    depth range give a line, which is what lets the correction follow the
    depth instead of being right in one place and wrong everywhere else.
    """
    pts = [p for p in points
           if math.isfinite(p.feed_depth) and math.isfinite(p.grid_depth)]
    if not pts:
        return Model()

    feed = np.array([p.feed_depth for p in pts], dtype=float)
    grid = np.array([p.grid_depth for p in pts], dtype=float)
    lo, hi = float(feed.min()), float(feed.max())
    n = len(pts)

    def averaged(note: str) -> Model:
        off = float(np.mean(grid - feed))
        # One parameter, so the scatter means something from two points up.
        rms = (float(np.sqrt(np.mean(((grid - feed) - off) ** 2)))
               if n >= 2 else None)
        return Model(kind="fixed", a=off, b=1.0, n=n, rms=rms,
                     lo=lo, hi=hi, note=note)

    if n == 1:
        return Model(kind="fixed", a=float(grid[0] - feed[0]), b=1.0, n=1,
                     lo=lo, hi=hi,
                     note="One tie-in fixes its own depth only - add another "
                          "a few hundred metres deeper or shallower.")

    spread = hi - lo
    if spread < MIN_SPREAD_M:
        return averaged(
            f"Tie-ins span only {spread:,.0f} m of depth; "
            f"{MIN_SPREAD_M:,.0f} m is needed before the slope means anything.")

    b, a = np.polyfit(feed, grid, 1)
    if abs(b - 1.0) > MAX_SCALE:
        return averaged(
            f"A {abs(b - 1.0) * 100:.0f}% scale is not physical - "
            "check the tie-ins for one taken off bottom.")

    # Two points and two parameters fit perfectly whatever the data, so the
    # scatter only starts saying something at three.
    rms = (float(np.sqrt(np.mean((grid - (a + b * feed)) ** 2)))
           if n >= 3 else None)
    return Model(kind="scale", a=float(a), b=float(b), n=n, rms=rms,
                 lo=lo, hi=hi,
                 note="" if n >= 3 else
                      "Two tie-ins always fit a line exactly - a third tests it.")


class Calibration:
    """The tie-in points, the model fitted to them, and the on/off switch."""

    def __init__(self, enabled: bool = False):
        self.points: list[TiePoint] = []
        self.enabled = bool(enabled)
        self.model = Model()

    # ------------------------------------------------------------ the points

    def add(self, point: TiePoint) -> None:
        self.points.append(point)
        self.refit()

    def remove(self, index: int) -> None:
        if 0 <= index < len(self.points):
            del self.points[index]
            self.refit()

    def clear(self) -> None:
        self.points.clear()
        self.refit()

    def refit(self) -> None:
        self.model = fit(self.points)

    # ------------------------------------------------------------- using it

    @property
    def active(self) -> bool:
        return self.enabled and self.model.on

    def apply(self, feed_depth):
        """Correct a depth if calibration is both fitted and switched on."""
        return self.model.apply(feed_depth) if self.active else feed_depth

    def encode(self) -> list:
        return [p.encode() for p in self.points]

    def load(self, rows) -> None:
        self.points = [p for p in (TiePoint.decode(r) for r in rows or [])
                       if p is not None]
        self.refit()
