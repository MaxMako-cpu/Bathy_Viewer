"""Moving targets on the surface: the vessel and the ROVs.

This is the layer the live position feed drives. It knows nothing about where
positions come from - call :meth:`TargetLayer.update` with CRS coordinates from
a UDP listener, a serial NMEA parser, or a test fixture, and the scene follows.

Step 2 (UDP) plugs in here and nowhere else.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np
import pyvista as pv

#: Default look of the three tracked bodies. Names match feed.ORDER.
#: The feed carries no heading, so all three are dots - the directional vessel
#: glyph is kept in _glyph() for when a heading source exists.
DEFAULT_TARGETS = {
    # No drop line for the vessel: it is on the surface with a kilometre and a
    # half of water under it, so a line to the seabed says nothing useful and
    # runs the height of the scene. Its umbilicals do the connecting instead.
    "Vessel": {"color": "#ff3ad2", "kind": "dot", "size": 1.15,
               "stem": False},                                          # magenta
    "UHD333": {"color": "#ff3b30", "kind": "dot", "size": 0.9},        # red
    "UHD334": {"color": "#2ecc50", "kind": "dot", "size": 0.9},        # green
    # Each TMS in a darker shade of its own ROV, so the pairing reads at a
    # glance without having to check the labels.
    "TMS333": {"color": "#c0392b", "kind": "cylinder", "size": 1.0},   # dark red
    "TMS334": {"color": "#1e8e3e", "kind": "cylinder", "size": 1.0},   # dark green
}

#: Real TMS dimensions, metres.
TMS_DIAMETER_M = 3.0
TMS_HEIGHT_M = 2.0

#: A 3 m body on a 130 km grid is sub-pixel at survey zoom, so the cylinder is
#: drawn at true size but never allowed below this on screen. Without it the
#: TMS is invisible until you have closed right in - the same trap the vehicle
#: dots fell into when they were world-space spheres. Measured: at 9 px it is
#: a speck next to its own tether, which is why this is larger than the ROV dot.
TMS_MIN_PX = 20.0

#: Tether dash length on screen, so the line reads as dotted at any zoom.
TETHER_DASH_PX = 5.0

#: Dot diameter in screen pixels, scaled by each target's ``size``. Small
#: enough that a short trail emerges from under the marker rather than hiding
#: beneath it.
BASE_POINT_PX = 12.0

#: How much track to keep, in seconds. Selectable in the UI up to 24 hours.
DEFAULT_TRAIL_SECONDS = 600.0

#: Vertices actually drawn per trail. A 24-hour track at 1 Hz is 86 400
#: points, and rebuilding that polyline for three targets every second costs
#: real frame time for detail no one can see - so the drawn line is
#: subsampled while the full history is kept.
TRAIL_DRAW_MAX = 4000

#: Hard ceiling on retained points, in case a feed runs far faster than 1 Hz.
MAX_TRAIL_POINTS = 400_000


#: Depth bias for things drawn over the terrain. Larger means nearer the
#: viewer, so these are layers, not a single "on top": a slope box is a sheet
#: of terrain and has to sit *under* the overlays and tracking marks, or it
#: buries everything inside it.
ON_TOP_SHEET = 8_000       # slope box - just clear of the terrain
ON_TOP_MARK = 66_000       # overlays, targets, tethers, measuring furniture


def draw_on_top(actor, bias: int = ON_TOP_MARK) -> None:
    """Draw this actor over the terrain instead of letting relief bury it.

    A target sitting on the seabed is at exactly the depth of the surface under
    it, so any ridge between it and the camera hides it - and a tracking mark
    you cannot see is worse than useless. Bias its depth towards the viewer,
    by ``bias``, so several such layers still stack in a sensible order.
    """
    try:
        m = actor.GetMapper()
        m.SetResolveCoincidentTopologyToPolygonOffset()
        m.SetRelativeCoincidentTopologyPointOffsetParameter(-bias)
        m.SetRelativeCoincidentTopologyLineOffsetParameters(-bias, -bias)
        m.SetRelativeCoincidentTopologyPolygonOffsetParameters(-bias, -bias)
    except Exception:
        pass


@dataclass
class Target:
    name: str
    color: str = "#f2c14e"
    kind: str = "rov"
    size: float = 1.0
    x: float = float("nan")  # CRS
    y: float = float("nan")
    z: float = float("nan")  # elevation, metres, positive up
    heading: float = float("nan")  # degrees from grid north
    stale: bool = True
    #: (local x, local y, elevation, monotonic time) - the clock is what
    #: lets the trail be trimmed by age rather than by point count.
    trail: deque = field(default_factory=lambda: deque(maxlen=MAX_TRAIL_POINTS))
    speed: float = float("nan")   # metres per second over the ground
    updated_at: float = 0.0       # monotonic clock of the last fix
    stem: bool = True             # draw a drop line down to the seabed

    @property
    def fix(self) -> bool:
        return math.isfinite(self.x) and math.isfinite(self.y) and math.isfinite(self.z)


class TargetLayer:
    """Marker + label + trail actors for a handful of moving bodies."""

    def __init__(self, plotter, surface=None,
                 trail_seconds: float = DEFAULT_TRAIL_SECONDS):
        self.plotter = plotter
        self.surface = surface
        self.trail_seconds = float(trail_seconds)
        self.targets: dict[str, Target] = {}
        self._actors: dict[str, dict] = {}
        self._scale = 1.0  # metres per glyph unit, from the raster extent
        self._ve = 1.0
        self._mpp = 1.0
        self._tms_scale = 1.0
        self.visible = True
        self.tms_visible = True
        self.tethers_visible = True

    # ------------------------------------------------------------------ setup

    def set_surface(self, surface) -> None:
        self.surface = surface
        if surface is not None:
            w, h = surface.extent_m
            self._scale = max(w, h) / 90.0  # glyph ~1% of the scene
        self.clear()

    def set_ve(self, ve: float) -> None:
        """Targets live in the same exaggerated space as the terrain."""
        self._ve = ve
        for name in self._actors:
            self._place(self.targets[name])

    def ensure(self, name: str, **style) -> Target:
        if name not in self.targets:
            base = dict(DEFAULT_TARGETS.get(name, {}))
            base.update(style)
            self.targets[name] = Target(name=name, **base)
        return self.targets[name]

    # ----------------------------------------------------------------- update

    def update(self, name: str, x: float, y: float, z: float, heading: float = float("nan")) -> Target:
        """Move a target to a CRS position. Creates it on first sight.

        ``z`` is elevation in metres, positive up - so an ROV 1500 m down is
        ``-1500``. Pass ``None`` to have it dropped onto the seabed.
        """
        t = self.ensure(name)
        if z is None and self.surface is not None:
            p = self.surface.probe(x, y)
            z = p.z if p else float("nan")
        now = time.monotonic()
        if t.fix and t.updated_at:
            gap = now - t.updated_at
            step = math.hypot(float(x) - t.x, float(y) - t.y)
            if gap > 0.05:
                # Lightly smoothed: the sender repeats the previous position
                # when it has no new fix, which would read as a dead stop.
                inst = step / gap
                if math.isfinite(t.speed):
                    t.speed = 0.6 * t.speed + 0.4 * inst
                else:
                    t.speed = inst
        t.updated_at = now
        t.x, t.y, t.z, t.heading = float(x), float(y), float(z), float(heading)
        t.stale = False
        if t.fix and self.surface is not None and self.trail_seconds > 0:
            lx, ly = self.surface.local_from_crs(t.x, t.y)
            t.trail.append((lx, ly, t.z, now))
            self._trim(t, now)
        elif self.trail_seconds <= 0 and t.trail:
            t.trail.clear()
        self._place(t)
        return t

    def _trim(self, t: Target, now: float) -> None:
        cutoff = now - self.trail_seconds
        while t.trail and t.trail[0][3] < cutoff:
            t.trail.popleft()

    def set_trail_seconds(self, seconds: float) -> None:
        """Change how much track is kept. Shortening trims immediately."""
        self.trail_seconds = max(0.0, float(seconds))
        now = time.monotonic()
        for t in self.targets.values():
            if self.trail_seconds <= 0:
                t.trail.clear()
            else:
                self._trim(t, now)
            bag = self._actors.get(t.name)
            if bag and "trail" in bag and len(t.trail) < 2:
                self.plotter.remove_actor(bag.pop("trail"), render=False)
            self._place(t)

    def _kind_visible(self, t: Target) -> bool:
        return self.tms_visible if t.kind == "cylinder" else True

    def set_tms_visible(self, on: bool) -> None:
        self.tms_visible = bool(on)
        self.refresh()

    def mark_stale(self, name: str) -> None:
        if name in self.targets:
            self.targets[name].stale = True

    def refresh(self) -> None:
        """Redraw every target - used after a staleness change."""
        for t in self.targets.values():
            self._place(t)

    def clear_trail(self, name: str | None = None) -> None:
        for t in self.targets.values():
            if name is not None and t.name != name:
                continue
            t.trail.clear()
            # _place only draws a trail of 2+ points, so an emptied trail would
            # otherwise leave the previous line actor on screen for ever.
            bag = self._actors.get(t.name)
            if bag and "trail" in bag:
                self.plotter.remove_actor(bag.pop("trail"), render=False)
            self._place(t)

    def clear(self) -> None:
        for bag in self._actors.values():
            for a in bag.values():
                try:
                    self.plotter.remove_actor(a, render=False)
                except Exception:
                    pass
        self._actors.clear()
        self.targets.clear()

    def set_visible(self, on: bool) -> None:
        self.visible = on
        for bag in self._actors.values():
            for a in bag.values():
                try:
                    a.SetVisibility(bool(on))
                except Exception:
                    pass

    # ---------------------------------------------------------------- drawing

    def set_metres_per_pixel(self, mpp: float) -> None:
        """Ground scale, so world-space bodies stay visible.

        Called whenever the camera moves. The TMS keeps its true 3 m x 2 m
        shape but is scaled up once it would otherwise fall below a few pixels,
        which is the only way a body that size stays findable on this grid.
        """
        if not mpp > 0:
            return
        self._mpp = mpp
        want = max(1.0, TMS_MIN_PX * mpp / TMS_DIAMETER_M)
        if abs(want - self._tms_scale) / max(self._tms_scale, 1e-9) < 0.02:
            return
        self._tms_scale = want
        for name, bag in self._actors.items():
            t = self.targets.get(name)
            if t is not None and t.kind == "cylinder" and "marker" in bag:
                bag["marker"].SetScale(want, want, want)

    def _dashed(self, a, b):
        """A tether drawn as separate dashes - VTK line stipple is unreliable."""
        a, b = np.asarray(a, float), np.asarray(b, float)
        span = float(np.linalg.norm(b - a))
        if span < 1e-6:
            return None
        dash = max(span / 60.0, TETHER_DASH_PX * self._mpp)
        n = max(int(span / (dash * 2.0)), 1)
        pts, cells = [], []
        for i in range(n):
            t0 = (2.0 * i) / (2.0 * n)
            t1 = (2.0 * i + 1.0) / (2.0 * n)
            pts.append(a + (b - a) * t0)
            pts.append(a + (b - a) * t1)
            cells.extend([2, 2 * i, 2 * i + 1])
        return pv.PolyData(np.asarray(pts), lines=np.asarray(cells, dtype=np.int64))

    def draw_links(self, pairs: dict) -> None:
        """A thin dotted line between each pair of bodies.

        Used for both the tethers (TMS down to its ROV) and the umbilicals
        (vessel down to each TMS), so the whole chain reads as one line.
        """
        for lower, upper in pairs.items():
            name = f"link:{lower}"
            self.plotter.remove_actor(name, render=False)
            a, b = self.targets.get(lower), self.targets.get(upper)
            # A tether to a hidden TMS is a line to nowhere, so it goes with it.
            if not (self.visible and self.tethers_visible and self.tms_visible
                    and a is not None and b is not None and a.fix and b.fix
                    and self.surface is not None):
                continue
            pa = (*self.surface.local_from_crs(a.x, a.y), a.z * self._ve)
            pb = (*self.surface.local_from_crs(b.x, b.y), b.z * self._ve)
            mesh = self._dashed(pa, pb)
            if mesh is None:
                continue
            actor = self.plotter.add_mesh(
                mesh, color=a.color, line_width=1, opacity=0.85, name=name,
                render=False, pickable=False)
            draw_on_top(actor)

    def _glyph(self, t: Target) -> pv.PolyData:
        s = self._scale * t.size
        if t.kind == "vessel":
            # A hull-ish wedge so heading reads at a glance.
            return pv.Cone(direction=(0, 1, 0), height=3.2 * s, radius=1.1 * s, resolution=4)
        return pv.Sphere(radius=0.9 * s, theta_resolution=18, phi_resolution=18)

    def _place(self, t: Target) -> None:
        """Move every actor belonging to one target.

        The marker rides on the actor transform (cheap, called at fix rate).
        Label, drop line and trail change shape, so they are re-added under the
        same ``name`` - pyvista swaps the actor rather than stacking a new one.
        """
        if self.surface is None or not t.fix:
            return
        lx, ly = self.surface.local_from_crs(t.x, t.y)
        lz = t.z * self._ve
        bag = self._actors.setdefault(t.name, {})

        if t.kind == "cylinder":
            # Real geometry at real size: a TMS is a 3 m x 2 m body and should
            # look like one next to the terrain it is flying over.
            if "marker" not in bag:
                body = pv.Cylinder(direction=(0, 0, 1),
                                   radius=TMS_DIAMETER_M / 2.0,
                                   height=TMS_HEIGHT_M, resolution=24)
                # Plenty of ambient: a small body lit only by the survey sun
                # goes black whenever the sun is behind it, and a tracking
                # mark that disappears with the lighting is no use.
                bag["marker"] = self.plotter.add_mesh(
                    body, color=t.color, smooth_shading=False,
                    ambient=0.55, diffuse=0.65, specular=0.15,
                    name=f"tgt:{t.name}", render=False, pickable=False)
            marker = bag["marker"]
            marker.SetPosition(lx, ly, lz)
            marker.SetScale(self._tms_scale, self._tms_scale, self._tms_scale)
            marker.GetProperty().SetOpacity(0.45 if t.stale else 1.0)
        else:
            # Screen-constant dots. A world-space glyph big enough to see across
            # a 131 km grid would be wider than the vehicles are apart.
            bag["marker"] = self.plotter.add_points(
                np.array([[lx, ly, lz]], dtype=float), color=t.color,
                point_size=BASE_POINT_PX * t.size, render_points_as_spheres=True,
                name=f"tgt:{t.name}", render=False, pickable=False,
                opacity=0.45 if t.stale else 1.0,
            )
        bag["marker"].SetVisibility(self.visible and self._kind_visible(t))
        draw_on_top(bag["marker"])

        bag["label"] = self.plotter.add_point_labels(
            np.array([[lx, ly, lz]], dtype=float), [t.name], name=f"lbl:{t.name}",
            font_size=11, text_color=t.color, shape=None, show_points=False,
            always_visible=True, render=False,
        )
        bag["label"].SetVisibility(self.visible and self._kind_visible(t))

        # Drop line to the seabed, so depth reads against the terrain.
        p = self.surface.probe(t.x, t.y)
        if t.stem and p is not None and abs(t.z - p.z) > 1e-6:
            bag["stem"] = self.plotter.add_mesh(
                pv.Line((lx, ly, p.z * self._ve), (lx, ly, lz)), color=t.color,
                line_width=1, opacity=0.5, name=f"stem:{t.name}",
                render=False, pickable=False,
            )
            bag["stem"].SetVisibility(self.visible and self._kind_visible(t))
            draw_on_top(bag["stem"])
        elif "stem" in bag:
            # Back on the seabed - drop the line rather than leaving it hanging.
            self.plotter.remove_actor(bag.pop("stem"), render=False)

        if len(t.trail) > 1:
            pts = np.asarray(t.trail, dtype=float)[:, :3].copy()
            if len(pts) > TRAIL_DRAW_MAX:
                # Subsample for drawing, always keeping the newest point so the
                # line still reaches the marker.
                step = int(np.ceil(len(pts) / TRAIL_DRAW_MAX))
                keep = np.arange(0, len(pts), step)
                if keep[-1] != len(pts) - 1:
                    keep = np.append(keep, len(pts) - 1)
                pts = pts[keep]
            pts[:, 2] *= self._ve
            bag["trail"] = self.plotter.add_mesh(
                pv.lines_from_points(pts), color=t.color, line_width=2, opacity=0.9,
                name=f"trail:{t.name}", render=False, pickable=False,
            )
            bag["trail"].SetVisibility(self.visible and self._kind_visible(t))
            draw_on_top(bag["trail"])
