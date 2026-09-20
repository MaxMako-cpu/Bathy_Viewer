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

#: Default look of each tracked body, keyed by feed.ORDER's slots. The slots
#: are generic wire positions - what a vessel calls its vehicles is a label,
#: set in Vehicles > Names and colours and held in vehicles.Fleet.
#: The feed carries no heading, so all three are dots - the directional vessel
#: glyph is kept in _glyph() for when a heading source exists.
DEFAULT_TARGETS = {
    # No drop line for the vessel: it is on the surface with a kilometre and a
    # half of water under it, so a line to the seabed says nothing useful and
    # runs the height of the scene. Its umbilicals do the connecting instead.
    "Vessel": {"color": "#ff3ad2", "kind": "dot", "size": 1.15,
               "stem": False},                                          # magenta
    "ROV1": {"color": "#ff3b30", "kind": "dot", "size": 0.9},          # red
    "ROV2": {"color": "#2ecc50", "kind": "dot", "size": 0.9},          # green
    # Each TMS in a darker shade of its own ROV, so the pairing reads at a
    # glance without having to check the labels.
    "TMS1": {"color": "#c0392b", "kind": "cylinder", "size": 1.0},     # dark red
    "TMS2": {"color": "#1e8e3e", "kind": "cylinder", "size": 1.0},     # dark green
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

#: Bounds on how long a marker takes to glide from one fix to the next.
#:
#: The feed is a 1 Hz step function - measured on a real capture: 1.000 s
#: between records, 0.635 m of travel in each - so without this a vehicle sits
#: still for a second and then teleports. The glide runs between two *reported*
#: fixes and never past the newest one, so it invents no position beyond "it
#: was here, then it was there".
#:
#: Capped because a feed that has been away for a minute must not spend a
#: minute crawling back; it should arrive promptly and then hold.
MIN_GLIDE_S = 0.15
MAX_GLIDE_S = 2.5


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
    #: The slot: what the feed identifies this body by, and what every actor,
    #: tether pairing and calibration tie-in is keyed on. Never changes.
    name: str
    #: What the operator sees. Renaming a body changes only this, so a vessel
    #: can call its ROV whatever it likes without moving anything structural.
    label: str = ""
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

    #: Where it is *drawn*, as against x/y/z which are what the feed reported.
    #: Everything else - the table, the calibration, a slide case - reads the
    #: reported values, so only the picture is interpolated.
    dx: float = float("nan")
    dy: float = float("nan")
    dz: float = float("nan")
    #: The glide currently under way: where it started and when.
    gx: float = float("nan")
    gy: float = float("nan")
    gz: float = float("nan")
    glide_at: float = 0.0
    glide_for: float = 0.0

    @property
    def fix(self) -> bool:
        return math.isfinite(self.x) and math.isfinite(self.y) and math.isfinite(self.z)

    @property
    def shown(self) -> str:
        """What to draw: the operator's name for it, or the slot if unnamed."""
        return self.label or self.name


class TargetLayer:
    """Marker + label + trail actors for a handful of moving bodies."""

    def __init__(self, plotter, surface=None,
                 trail_seconds: float = DEFAULT_TRAIL_SECONDS):
        self.plotter = plotter
        self.surface = surface
        self.trail_seconds = float(trail_seconds)
        self.targets: dict[str, Target] = {}
        #: Per-slot label and colour overrides. Survives clear(), so a grid
        #: reload does not put every vehicle back to its shipped name.
        self.styles: dict[str, dict] = {}
        self._actors: dict[str, dict] = {}
        self._scale = 1.0  # metres per glyph unit, from the raster extent
        self._ve = 1.0
        self._mpp = 1.0
        #: Per-body swell factor. Per body, not one for the scene: with a
        #: perspective camera two bodies at different distances need different
        #: scales to end up the same size on screen.
        self._tms_scales: dict[str, float] = {}
        self._cam = None            # camera position, scene coordinates
        self._k = 0.0               # metres per pixel per metre of distance
        self._parallel_mpp = None   # set instead of _k under parallel projection
        self.visible = True
        self.tms_visible = True
        #: Body -> the ROV whose chain it belongs to, so switching one ROV off
        #: takes its TMS, tethers and trails with it. Set from outside: this
        #: layer is told how the fleet is wired rather than knowing the feed.
        self.chain_of: dict[str, str] = {}
        #: Chains switched off. Anything not in a chain - the vessel - is
        #: unaffected, since it belongs to both ROVs and to neither.
        self.hidden_chains: set[str] = set()
        #: Link pairs seen so far, so advance() can redraw them mid-glide.
        self._links: dict = {}
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
            base.update(self.styles.get(name, {}))
            base.update(style)
            self.targets[name] = Target(name=name, **base)
        return self.targets[name]

    def set_styles(self, styles: dict) -> None:
        """Rename and recolour bodies, including ones already on screen.

        A dot's actor is re-added on every ``_place``, so a colour change
        would take by itself; a cylinder's is built once and kept, and a
        label is baked into its actor. Rather than remember which is which,
        every actor for a restyled body is dropped and rebuilt.
        """
        self.styles = {k: dict(v) for k, v in (styles or {}).items()}
        for name, style in self.styles.items():
            t = self.targets.get(name)
            if t is None:
                continue
            for key, value in style.items():
                setattr(t, key, value)
            for actor in (self._actors.pop(name, None) or {}).values():
                try:
                    self.plotter.remove_actor(actor, render=False)
                except Exception:
                    pass
        for t in self.targets.values():
            self._place(t)

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
        gap = now - t.updated_at if t.updated_at else 0.0
        if t.fix and t.updated_at:
            step = math.hypot(float(x) - t.x, float(y) - t.y)
            if gap > 0.05:
                # Lightly smoothed: the sender repeats the previous position
                # when it has no new fix, which would read as a dead stop.
                inst = step / gap
                if math.isfinite(t.speed):
                    t.speed = 0.6 * t.speed + 0.4 * inst
                else:
                    t.speed = inst
        # Glide from wherever it is drawn *now* to the new fix, so a fix that
        # lands mid-glide carries on from what is on screen rather than
        # snapping back. The duration is the interval the feed has actually
        # been running at, so this follows the sender rather than assuming it.
        t.gx = t.dx if math.isfinite(t.dx) else float(x)
        t.gy = t.dy if math.isfinite(t.dy) else float(y)
        t.gz = t.dz if math.isfinite(t.dz) else float(z)
        t.glide_at = now
        t.glide_for = (min(max(gap, MIN_GLIDE_S), MAX_GLIDE_S)
                       if t.updated_at and gap > 0 else 0.0)
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

    def advance(self, now: float | None = None) -> bool:
        """Step every glide forward. True if anything actually moved.

        Called at frame rate. The fraction is clamped to 1, so a marker that
        has arrived sits at its last reported fix and waits - this interpolates
        between two real positions and never extrapolates past the newest one.
        """
        now = time.monotonic() if now is None else now
        moved = False
        for t in self.targets.values():
            if not t.fix:
                continue
            if t.glide_for > 0.0:
                f = (now - t.glide_at) / t.glide_for
                f = 0.0 if f < 0.0 else (1.0 if f > 1.0 else f)
            else:
                f = 1.0
            if not math.isfinite(t.gx):
                t.gx, t.gy, t.gz = t.x, t.y, t.z
            nx = t.gx + (t.x - t.gx) * f
            ny = t.gy + (t.y - t.gy) * f
            nz = t.gz + (t.z - t.gz) * f
            if (not math.isfinite(t.dx) or abs(nx - t.dx) > 1e-6
                    or abs(ny - t.dy) > 1e-6 or abs(nz - t.dz) > 1e-6):
                t.dx, t.dy, t.dz = nx, ny, nz
                moved = True
        if moved:
            for t in self.targets.values():
                if t.fix:
                    self._place(t, trail=False)
            if self._links:
                self.draw_links(self._links)
        return moved

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

    def _chain_visible(self, name: str) -> bool:
        """False for a body belonging to a chain that has been switched off."""
        return self.chain_of.get(name, None) not in self.hidden_chains

    def shows(self, t: Target) -> bool:
        """Everything that decides whether one body is on screen at all."""
        return (self.visible and self._kind_visible(t)
                and self._chain_visible(t.name))

    def set_chains(self, chain_of: dict) -> None:
        """Say which bodies belong to which ROV's chain."""
        self.chain_of = dict(chain_of or {})
        self.refresh()

    def set_chain_hidden(self, rov: str, hidden: bool) -> None:
        """Switch one ROV's whole chain off, or back on.

        Hiding an ROV hides its TMS, their tether and umbilical, and both
        trails. Half a chain on screen is worse than none: the tether would
        run to a body that is not there.
        """
        if hidden:
            self.hidden_chains.add(rov)
        else:
            self.hidden_chains.discard(rov)
        self.refresh()

    def visible_targets(self) -> list:
        """The bodies actually on screen, with a fix - what the camera frames."""
        return [t for t in self.targets.values() if t.fix and self.shows(t)]

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
        self._tms_scales.clear()
        self._links.clear()

    def set_visible(self, on: bool) -> None:
        self.visible = on
        for bag in self._actors.values():
            for a in bag.values():
                try:
                    a.SetVisibility(bool(on))
                except Exception:
                    pass

    # ---------------------------------------------------------------- drawing

    def set_view_scale(self, cam_pos, k: float,
                       parallel_mpp: float | None = None) -> None:
        """Tell the layer where the camera is, so bodies can be sized properly.

        ``k`` converts a distance into metres per screen pixel: with a
        perspective camera a thing twice as far away is half the size, so the
        scale depends on the distance to *that body*, not to anything else.

        This used to take a single figure for the whole scene, measured at the
        focal point, and apply it to every body. That is exact only for a body
        sitting at the focal point. Measured: framed on the vehicles a TMS drew
        at 20 px as intended, but with the focal point moved away across the
        grid - which is all it takes to zoom in on something else - the same
        body drew at 1,212 px and filled the screen. Zoom to targets appeared
        to fix it because it puts the focal point back on the vehicles.
        """
        self._cam = tuple(cam_pos) if cam_pos is not None else None
        self._k = float(k)
        self._parallel_mpp = parallel_mpp
        if parallel_mpp:
            self._mpp = parallel_mpp
        elif self._cam is not None:
            self._mpp = max(self._k * 1.0, 1e-9)
        for name, bag in self._actors.items():
            t = self.targets.get(name)
            if t is None or t.kind != "cylinder" or "marker" not in bag:
                continue
            want = self._body_scale(t)
            had = self._tms_scales.get(name, 0.0)
            if had and abs(want - had) / max(had, 1e-9) < 0.02:
                continue
            self._tms_scales[name] = want
            bag["marker"].SetScale(want, want, want)

    def _mpp_at(self, pos) -> float:
        """Metres per screen pixel at one point in the scene."""
        if self._parallel_mpp:
            return self._parallel_mpp
        if self._cam is None or not self._k:
            return self._mpp
        d = math.dist(self._cam, pos)
        return max(self._k * d, 1e-9)

    def _body_scale(self, t: Target) -> float:
        """How much to swell one body so it keeps its minimum screen size.

        The TMS keeps its true 3 m x 2 m shape but is scaled up once it would
        otherwise fall below a few pixels, which is the only way a body that
        size stays findable on this grid.
        """
        if self.surface is None or not t.fix:
            return self._tms_scales.get(t.name, 1.0)
        lx, ly = self.surface.local_from_crs(t.x, t.y)
        mpp = self._mpp_at((lx, ly, t.z * self._ve))
        return max(1.0, TMS_MIN_PX * mpp / TMS_DIAMETER_M)

    def _dashed(self, a, b):
        """A tether drawn as separate dashes - VTK line stipple is unreliable."""
        a, b = np.asarray(a, float), np.asarray(b, float)
        span = float(np.linalg.norm(b - a))
        if span < 1e-6:
            return None
        # Measured where the line actually is. Using a single scene-wide
        # figure made the dashes on a near tether as coarse as the distance to
        # whatever the camera happened to be looking at.
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0, (a[2] + b[2]) / 2.0)
        dash = max(span / 60.0, TETHER_DASH_PX * self._mpp_at(mid))
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
        (vessel down to each TMS), so the whole chain reads as one line. The
        pairs are remembered so a glide can carry the lines along with the
        bodies they join, rather than leaving them behind for a second.
        """
        self._links.update(pairs)
        for lower, upper in self._links.items():
            name = f"link:{lower}"
            self.plotter.remove_actor(name, render=False)
            a, b = self.targets.get(lower), self.targets.get(upper)
            # A tether to a hidden TMS is a line to nowhere, so it goes with it.
            if not (self.visible and self.tethers_visible and self.tms_visible
                    and a is not None and b is not None and a.fix and b.fix
                    and self.surface is not None
                    and self._chain_visible(lower)
                    and self._chain_visible(upper)):
                continue
            pa = (*self.surface.local_from_crs(a.dx, a.dy), a.dz * self._ve)
            pb = (*self.surface.local_from_crs(b.dx, b.dy), b.dz * self._ve)
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

    def _place(self, t: Target, trail: bool = True) -> None:
        """Move every actor belonging to one target to its *drawn* position.

        Everything here rides on an actor transform or an in-place point
        update, so it is cheap enough to run at frame rate while a marker
        glides between fixes. ``trail=False`` skips the one thing that is not:
        rebuilding a polyline of up to 4 000 vertices. The trail only changes
        when a real fix lands anyway, and its newest segment is under a metre
        long, so the marker gliding a fraction behind its own tail is not
        something anyone can see.
        """
        if self.surface is None or not t.fix:
            return
        if not math.isfinite(t.dx):
            t.dx, t.dy, t.dz = t.x, t.y, t.z
        lx, ly = self.surface.local_from_crs(t.dx, t.dy)
        lz = t.dz * self._ve
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
            # Same 2% dead band the camera path uses. Without it the cylinder
            # is resized on every fix, so any camera drift between fixes makes
            # it pulse once a second instead of holding still.
            want = self._body_scale(t)
            had = self._tms_scales.get(t.name, 0.0)
            if not had or abs(want - had) / max(had, 1e-9) >= 0.02:
                self._tms_scales[t.name] = want
                marker.SetScale(want, want, want)
            marker.GetProperty().SetOpacity(0.45 if t.stale else 1.0)
        else:
            # Screen-constant dots. A world-space glyph big enough to see across
            # a 131 km grid would be wider than the vehicles are apart.
            #
            # Built once at the origin and moved by its transform, not re-added
            # each time: re-creating the actor every frame of a glide costs far
            # more than moving it, and made the marker flicker.
            if "marker" not in bag:
                bag["marker"] = self.plotter.add_points(
                    np.zeros((1, 3), dtype=float), color=t.color,
                    point_size=BASE_POINT_PX * t.size,
                    render_points_as_spheres=True,
                    name=f"tgt:{t.name}", render=False, pickable=False,
                )
            bag["marker"].SetPosition(lx, ly, lz)
            bag["marker"].GetProperty().SetOpacity(0.45 if t.stale else 1.0)
        bag["marker"].SetVisibility(self.shows(t))
        draw_on_top(bag["marker"])

        # The label is a text actor, which is expensive to rebuild, so its one
        # point is moved in place instead.
        lpd = bag.get("label_pd")
        if lpd is None or bag.get("label_text") != t.shown:
            lpd = pv.PolyData(np.array([[lx, ly, lz]], dtype=float))
            lpd["labels"] = [t.shown]
            bag["label_pd"] = lpd
            bag["label_text"] = t.shown
            bag["label"] = self.plotter.add_point_labels(
                lpd, "labels", name=f"lbl:{t.name}",
                font_size=11, text_color=t.color, shape=None,
                show_points=False, always_visible=True, render=False,
            )
        else:
            lpd.points[0] = (lx, ly, lz)
            lpd.Modified()
        bag["label"].SetVisibility(self.shows(t))

        # Drop line to the seabed, so depth reads against the terrain. Probed
        # under where the marker is drawn, so the foot of the line stays under
        # the body while it glides.
        p = self.surface.probe(t.dx, t.dy)
        if t.stem and p is not None and abs(t.dz - p.z) > 1e-6:
            foot, head = (lx, ly, p.z * self._ve), (lx, ly, lz)
            spd = bag.get("stem_pd")
            if spd is None:
                spd = pv.Line(foot, head)
                bag["stem_pd"] = spd
                bag["stem"] = self.plotter.add_mesh(
                    spd, color=t.color, line_width=1, opacity=0.5,
                    name=f"stem:{t.name}", render=False, pickable=False,
                )
                draw_on_top(bag["stem"])
            else:
                spd.points[0] = foot
                spd.points[1] = head
                spd.Modified()
            bag["stem"].SetVisibility(self.shows(t))
        elif "stem" in bag:
            # Back on the seabed - drop the line rather than leaving it hanging.
            self.plotter.remove_actor(bag.pop("stem"), render=False)
            bag.pop("stem_pd", None)

        if not trail:
            return
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
            bag["trail"].SetVisibility(self.shows(t))
            draw_on_top(bag["trail"])
