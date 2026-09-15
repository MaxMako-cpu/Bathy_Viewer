"""Moving targets on the surface: the vessel and the ROVs.

This is the layer the live position feed drives. It knows nothing about where
positions come from - call :meth:`TargetLayer.update` with CRS coordinates from
a UDP listener, a serial NMEA parser, or a test fixture, and the scene follows.

Step 2 (UDP) plugs in here and nowhere else.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pyvista as pv

#: Default look of the three tracked bodies. Names match feed.ORDER.
#: The feed carries no heading, so all three are dots - the directional vessel
#: glyph is kept in _glyph() for when a heading source exists.
DEFAULT_TARGETS = {
    "Vessel": {"color": "#ff3ad2", "kind": "dot", "size": 1.15},   # magenta
    "UHD333": {"color": "#ff3b30", "kind": "dot", "size": 0.9},    # red
    "UHD334": {"color": "#2ecc50", "kind": "dot", "size": 0.9},    # green
}

#: Dot diameter in screen pixels, scaled by each target's ``size``.
BASE_POINT_PX = 17.0


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
    trail: list = field(default_factory=list)  # local-metre points

    @property
    def fix(self) -> bool:
        return math.isfinite(self.x) and math.isfinite(self.y) and math.isfinite(self.z)


class TargetLayer:
    """Marker + label + trail actors for a handful of moving bodies."""

    def __init__(self, plotter, surface=None, trail_len: int = 600):
        self.plotter = plotter
        self.surface = surface
        self.trail_len = trail_len
        self.targets: dict[str, Target] = {}
        self._actors: dict[str, dict] = {}
        self._scale = 1.0  # metres per glyph unit, from the raster extent
        self._ve = 1.0
        self.visible = True

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
        t.x, t.y, t.z, t.heading = float(x), float(y), float(z), float(heading)
        t.stale = False
        if t.fix and self.surface is not None:
            lx, ly = self.surface.local_from_crs(t.x, t.y)
            t.trail.append((lx, ly, t.z))
            if len(t.trail) > self.trail_len:
                del t.trail[: len(t.trail) - self.trail_len]
        self._place(t)
        return t

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

        # Screen-constant dots. A world-space glyph big enough to see across a
        # 131 km grid would be wider than the vehicles are apart.
        bag["marker"] = self.plotter.add_points(
            np.array([[lx, ly, lz]], dtype=float), color=t.color,
            point_size=BASE_POINT_PX * t.size, render_points_as_spheres=True,
            name=f"tgt:{t.name}", render=False, pickable=False,
            opacity=0.45 if t.stale else 1.0,
        )
        bag["marker"].SetVisibility(self.visible)

        bag["label"] = self.plotter.add_point_labels(
            np.array([[lx, ly, lz]], dtype=float), [t.name], name=f"lbl:{t.name}",
            font_size=11, text_color=t.color, shape=None, show_points=False,
            always_visible=True, render=False,
        )
        bag["label"].SetVisibility(self.visible)

        # Drop line to the seabed, so depth reads against the terrain.
        p = self.surface.probe(t.x, t.y)
        if p is not None and abs(t.z - p.z) > 1e-6:
            bag["stem"] = self.plotter.add_mesh(
                pv.Line((lx, ly, p.z * self._ve), (lx, ly, lz)), color=t.color,
                line_width=1, opacity=0.5, name=f"stem:{t.name}",
                render=False, pickable=False,
            )
            bag["stem"].SetVisibility(self.visible)
        elif "stem" in bag:
            # Back on the seabed - drop the line rather than leaving it hanging.
            self.plotter.remove_actor(bag.pop("stem"), render=False)

        if len(t.trail) > 1:
            pts = np.asarray(t.trail, dtype=float).copy()
            pts[:, 2] *= self._ve
            bag["trail"] = self.plotter.add_mesh(
                pv.lines_from_points(pts), color=t.color, line_width=2, opacity=0.8,
                name=f"trail:{t.name}", render=False, pickable=False,
            )
            bag["trail"].SetVisibility(self.visible)
