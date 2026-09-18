"""Where a node went after it slid, and learning to guess better next time.

A node is placed on the seabed by manipulator, so where it started is known to
a couple of metres. Sometimes it does not stay: on a slope it slides, and the
job becomes finding a 220 x 275 mm box somewhere downhill of a known point.

**It slides. It never rolls.** The MNode Node-X is a 114 mm slab on a 220 mm
base, and tipping one over needs ground steeper than 63 degrees; the steepest
cell in the BOEM grid is 49. So there is one mechanism to model, and it is the
well-behaved one - the node tracks the fall line until the slope stops driving
it.

**The threshold cannot be calculated, only measured.** Sliding begins when the
downslope pull beats the adhesion between the base and the clay. For a 7.1 kg
(in water) node on 0.0605 m2 that puts the threshold anywhere between about 7
and 60 degrees across the range of shear strengths a Gulf slope plausibly has
- and the strength of the top few centimetres of clay is not something an ROV
can measure. So this module does not try. It starts from a stated default,
holds every confirmed placed-and-recovered case, and narrows the threshold from
those. The recoveries are the instrument.

What a confirmed case measures, precisely:

* it **started** on ``placed_slope``, so the threshold is *at most* that;
* it **stopped** on ``found_slope``, so the threshold is *at least* that.

Cases therefore bracket the answer from both sides rather than estimating it,
and when the bracket crosses over - something stopped on ground steeper than
something else started on - a single threshold cannot explain both, and the fit
says so instead of averaging the contradiction away.

No Qt and no VTK here: arithmetic and a duck-typed surface that answers
``probe()``, so a test can drive all of it.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, field, fields

#: Threshold to use before a single case has been recovered. Deliberately in
#: the middle of what the physics allows rather than at either end, and
#: deliberately reported as a guess wherever it is shown.
DEFAULT_ARREST_DEG = 15.0

#: Corridor half-angle before anything is known about how well the grid's
#: aspect predicts the real track. Wide, because a 12.22 m grid smooths exactly
#: the metre-scale relief that steers a sliding box.
DEFAULT_SPREAD_DEG = 25.0

#: Narrowest the corridor is ever drawn, however good the cases look. Claiming
#: better than this from a handful of recoveries would be false precision.
MIN_SPREAD_DEG = 8.0

#: Cases needed before the fit is called anything better than provisional.
ENOUGH_CASES = 3

#: How far a node is ever assumed to have travelled, metres. A slab this size
#: on a slope does not run kilometres; a path longer than this means the trace
#: has wandered into a channel and is following it, not the node.
MAX_RUNOUT_M = 400.0

#: Node the model was built around. Recorded on each case so a fleet running
#: more than one type does not pool them without noticing.
DEFAULT_NODE = "MNode Node-X"


def _bearing_between(x1, y1, x2, y2) -> float:
    """Grid bearing from one point to another, degrees from north."""
    dx, dy = x2 - x1, y2 - y1
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return float("nan")
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def angle_diff(a: float, b: float) -> float:
    """Smallest signed difference between two bearings, -180 to +180."""
    if not (math.isfinite(a) and math.isfinite(b)):
        return float("nan")
    return (a - b + 180.0) % 360.0 - 180.0


@dataclass
class SlideCase:
    """One node that slid: where it was put, and where it turned up."""

    rov: str                            # feed slot, not the display label
    placed_x: float                     # CRS easting at placement
    placed_y: float
    placed_z: float = float("nan")      # elevation, metres, positive up
    placed_slope: float = float("nan")  # degrees, native resolution
    placed_aspect: float = float("nan")  # downslope bearing at placement
    #: What the pilot saw it do, if anything. NaN means nobody watched.
    observed_dir: float = float("nan")
    opened_at: float = field(default_factory=time.time)

    found_x: float = float("nan")
    found_y: float = float("nan")
    found_z: float = float("nan")
    found_slope: float = float("nan")
    found_at: float = float("nan")

    node: str = DEFAULT_NODE
    grid: str = ""
    note: str = ""

    # ------------------------------------------------------------ measuring

    @property
    def confirmed(self) -> bool:
        """A case only teaches anything once the node has been found."""
        return math.isfinite(self.found_x) and math.isfinite(self.found_y)

    @property
    def runout(self) -> float:
        """Horizontal distance from placement to resting place, metres."""
        if not self.confirmed:
            return float("nan")
        return math.hypot(self.found_x - self.placed_x,
                          self.found_y - self.placed_y)

    @property
    def track(self) -> float:
        """The bearing it actually travelled on."""
        if not self.confirmed:
            return float("nan")
        return _bearing_between(self.placed_x, self.placed_y,
                                self.found_x, self.found_y)

    @property
    def track_error(self) -> float:
        """How far the real track missed the grid's downslope bearing.

        This is the number that sets how wide a corridor has to be drawn, and
        it is a measurement of the *grid*, not of the node.
        """
        return angle_diff(self.track, self.placed_aspect)

    @property
    def drop(self) -> float:
        """Elevation lost, metres. Positive means it went downhill."""
        if not (self.confirmed and math.isfinite(self.placed_z)
                and math.isfinite(self.found_z)):
            return float("nan")
        return self.placed_z - self.found_z

    # --------------------------------------------------------- persistence

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict):
        """Rebuild one case, tolerating rows written by an older build."""
        if not isinstance(d, dict) or not d.get("rov"):
            return None
        known = {f.name for f in fields(SlideCase)}
        kept = {k: v for k, v in d.items() if k in known}
        try:
            return SlideCase(**kept)
        except (TypeError, ValueError):
            return None


@dataclass
class Model:
    """What the recovered cases say about how a node behaves here."""

    arrest_deg: float = DEFAULT_ARREST_DEG
    spread_deg: float = DEFAULT_SPREAD_DEG
    n: int = 0
    #: The bracket the cases put the threshold in, before any midpoint is taken.
    lo: float = float("nan")      # at least this: something stopped on it
    hi: float = float("nan")      # at most this: something started on it
    consistent: bool = True
    runout_mean: float = float("nan")
    runout_max: float = float("nan")
    note: str = ""

    @property
    def provisional(self) -> bool:
        return self.n < ENOUGH_CASES

    def describe(self) -> str:
        if self.n == 0:
            return (f"No recovered cases yet - using the default "
                    f"{self.arrest_deg:.0f} deg arrest angle and a "
                    f"{self.spread_deg:.0f} deg corridor. Treat the corridor "
                    f"as a direction to start looking, not a prediction.")
        head = (f"Arrest angle {self.arrest_deg:.1f} deg, corridor "
                f"+/-{self.spread_deg:.0f} deg, from {self.n} "
                f"recovered case{'s' if self.n != 1 else ''}")
        if math.isfinite(self.lo) and math.isfinite(self.hi):
            head += f" (bracketed {self.lo:.1f}-{self.hi:.1f} deg)"
        if math.isfinite(self.runout_mean):
            head += (f". Runout so far {self.runout_mean:.0f} m mean, "
                     f"{self.runout_max:.0f} m worst")
        if self.provisional:
            head += (f". Provisional - {ENOUGH_CASES} cases before this is "
                     "worth much")
        return head + (f". {self.note}" if self.note else "")


def fit(cases) -> Model:
    """Narrow the arrest angle and corridor width from the confirmed cases."""
    good = [c for c in cases if c.confirmed]
    if not good:
        return Model()

    started = [c.placed_slope for c in good if math.isfinite(c.placed_slope)]
    stopped = [c.found_slope for c in good if math.isfinite(c.found_slope)]
    n = len(good)

    # Each case brackets the threshold from one side. Together they close on it.
    lo = max(stopped) if stopped else float("nan")
    hi = min(started) if started else float("nan")

    note = ""
    consistent = True
    if math.isfinite(lo) and math.isfinite(hi):
        if lo <= hi:
            arrest = (lo + hi) / 2.0
        else:
            # A node stopped on ground steeper than another one started on.
            # No single threshold explains both, and averaging the two ends
            # would hide that rather than report it.
            consistent = False
            arrest = (lo + hi) / 2.0
            note = (f"Cases disagree: something stopped on {lo:.1f} deg while "
                    f"something else started sliding on {hi:.1f} deg. The "
                    "seabed is not the same everywhere, so expect a wide "
                    "corridor until there are more cases.")
    elif math.isfinite(lo):
        arrest = lo
    elif math.isfinite(hi):
        arrest = hi
    else:
        arrest = DEFAULT_ARREST_DEG

    # Corridor width comes from how far real tracks missed the grid's own
    # downslope bearing - a measurement of the terrain model, not the node.
    errs = [abs(c.track_error) for c in good if math.isfinite(c.track_error)]
    if errs:
        worst = max(errs)
        mean = sum(errs) / len(errs)
        # Wide enough for the worst case seen, and never narrower than the
        # floor, because a handful of cases cannot justify a tight corridor.
        spread = max(worst, mean * 1.5, MIN_SPREAD_DEG)
    else:
        spread = DEFAULT_SPREAD_DEG

    runs = [c.runout for c in good if math.isfinite(c.runout)]
    return Model(
        arrest_deg=float(max(0.0, arrest)),
        spread_deg=float(min(spread, 90.0)),
        n=n, lo=lo, hi=hi, consistent=consistent,
        runout_mean=float(sum(runs) / len(runs)) if runs else float("nan"),
        runout_max=float(max(runs)) if runs else float("nan"),
        note=note,
    )


def trace(surface, x: float, y: float, arrest_deg: float,
          start_dir: float = float("nan"), step: float | None = None,
          max_dist: float = MAX_RUNOUT_M):
    """Walk downhill from a placement point until the slope stops driving.

    Returns ``(points, stopped_because)`` where points are (x, y, z) in the
    grid's CRS. ``start_dir``, when given, is the direction the pilot actually
    saw it go: it steers the first step, and the terrain takes over after that.
    """
    if surface is None:
        return [], "no grid"
    step = step or max(surface.native_cell_m * 0.5, 1.0)

    p = surface.probe(x, y)
    if p is None:
        return [], "off grid"
    pts = [(float(x), float(y), float(p.z))]
    travelled = 0.0
    heading = float("nan")
    first = True

    # A step per half cell, capped: a trace that has not arrested within the
    # cap is following a channel rather than a node.
    for _ in range(int(max_dist / step) + 2):
        p = surface.probe(pts[-1][0], pts[-1][1])
        if p is None:
            return pts, "ran off the grid"
        if not math.isfinite(p.slope):
            return pts, "no slope here"
        if p.slope < arrest_deg:
            return pts, f"slope eased to {p.slope:.1f} deg"
        # An observed direction owns the first step and nothing after it: the
        # pilot saw which way it set off, not the whole path. From the second
        # step the terrain is the better guide, and it is what curves the
        # track round a spur.
        if first and math.isfinite(start_dir):
            heading = start_dir
        elif math.isfinite(p.aspect):
            heading = p.aspect
        first = False
        if not math.isfinite(heading):
            return pts, "no downslope direction"
        brg = math.radians(heading)
        nx = pts[-1][0] + step * math.sin(brg)
        ny = pts[-1][1] + step * math.cos(brg)
        np_ = surface.probe(nx, ny)
        if np_ is None:
            return pts, "ran off the grid"
        # Never climb. Rounding on a near-flat cell can point the aspect
        # uphill, and a path that goes up is a tracing artefact, not a node.
        if np_.z > pts[-1][2] + 1e-6:
            return pts, "the ground turns uphill"
        pts.append((nx, ny, float(np_.z)))
        travelled += step
        if travelled >= max_dist:
            return pts, f"reached the {max_dist:.0f} m limit without easing"
    return pts, "step limit"


def corridor(points, spread_deg: float, min_width: float = 5.0):
    """Left and right edges of the search corridor around a traced path.

    The corridor opens with distance, because an error in the initial
    direction costs more the further the node went. ``spread_deg`` is the
    half-angle, measured from how far real tracks missed the grid's own
    downslope bearing.
    """
    if len(points) < 2:
        return [], []
    x0, y0 = points[0][0], points[0][1]
    half = math.radians(max(spread_deg, 0.0))
    # At the placement point itself the bearing from the start is undefined,
    # and falling back to north would lay the corridor's first rung across the
    # path instead of square to it. The first segment is the direction that
    # actually matters there.
    opening = _bearing_between(x0, y0, points[1][0], points[1][1])
    if not math.isfinite(opening):
        opening = 0.0
    left, right = [], []
    for x, y, _z in points:
        d = math.hypot(x - x0, y - y0)
        w = max(d * math.tan(half), min_width)
        brg = _bearing_between(x0, y0, x, y)
        if not math.isfinite(brg):
            brg = opening
        # Perpendicular to the line from the start, which is what an angular
        # error actually opens out around.
        pr = math.radians(brg + 90.0)
        dx, dy = w * math.sin(pr), w * math.cos(pr)
        left.append((x - dx, y - dy))
        right.append((x + dx, y + dy))
    return left, right


# ------------------------------------------------------------------ database

def load_cases(path: str) -> list:
    """Every case ever recorded, from the JSON database.

    A missing or unreadable file is an empty history, not an error: this is a
    log that grows over years and across installs, and a viewer that refuses
    to start because one row is malformed would be worse than useless.
    """
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return []
    rows = raw.get("cases", []) if isinstance(raw, dict) else raw
    out = []
    for row in rows or []:
        case = SlideCase.from_dict(row)
        if case is not None:
            out.append(case)
    return out


def save_cases(path: str, cases) -> None:
    """Write the database, via a temporary file so a crash cannot truncate it."""
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"format": 1, "node": DEFAULT_NODE,
               "saved_at": time.time(),
               "cases": [c.to_dict() for c in cases]}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    os.replace(tmp, path)


CSV_HEADER = ("rov,node,grid,opened_utc,found_utc,placed_x,placed_y,placed_z,"
              "placed_slope_deg,placed_aspect_deg,observed_dir_deg,"
              "found_x,found_y,found_z,found_slope_deg,"
              "runout_m,track_deg,track_error_deg,drop_m,note")


def to_csv(cases) -> str:
    """The database as a table, for a report or someone else's analysis."""
    def t(v):
        return (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(v))
                if math.isfinite(v) and v > 0 else "")

    def f(v, dp=3):
        return "" if not math.isfinite(v) else f"{v:.{dp}f}"

    rows = [CSV_HEADER]
    for c in cases:
        rows.append(",".join((
            c.rov, c.node, os.path.basename(c.grid), t(c.opened_at),
            t(c.found_at), f(c.placed_x), f(c.placed_y), f(c.placed_z, 2),
            f(c.placed_slope, 2), f(c.placed_aspect, 2), f(c.observed_dir, 1),
            f(c.found_x), f(c.found_y), f(c.found_z, 2), f(c.found_slope, 2),
            f(c.runout, 1), f(c.track, 1), f(c.track_error, 1), f(c.drop, 2),
            '"' + c.note.replace('"', "'") + '"' if c.note else "",
        )))
    return "\n".join(rows) + "\n"
