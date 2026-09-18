"""The 3D terrain view: mesh build, hillshade lighting, hover and picking."""

from __future__ import annotations

import math
import time

import numpy as np
import pyvista as pv
from PySide6 import QtCore, QtWidgets
from pyvistaqt import QtInteractor
from vtkmodules.vtkRenderingCore import vtkPropPicker

from . import ramps
from .measure import MeasureLine, Station
from . import ramps as _ramps
from .targets import (BASE_POINT_PX, ON_TOP_SHEET, TargetLayer,
                      draw_on_top)

#: Colour of measurement furniture.
MEASURE_COLOR = "#f0a93c"

#: Station dot size in screen pixels. Smaller than a vehicle dot
#: (BASE_POINT_PX) on purpose - stations are reference marks, not the
#: thing being tracked.
STATION_POINT_PX = BASE_POINT_PX * 0.62

#: Shapefile vertices are drawn smaller again - an overlay is context,
#: and a preplot can run to thousands of points.
OVERLAY_POINT_PX = 5.0

#: Outline and corner marker for the slope box.
BOX_COLOR = "#ffffff"

#: Closest the camera may come to what it is looking at. Verified usable:
#: with the pointer on the seabed the view still fills the frame at 1 m.
MIN_ZOOM_M = 1.0

#: Furthest out, as a multiple of the terrain's diagonal. Without a cap
#: the wheel runs away to astronomical distances and the grid vanishes.
MAX_ZOOM_SPANS = 6.0

#: Distance multiplier per wheel notch.
ZOOM_STEP = 1.25


class TerrainView(QtWidgets.QWidget):
    """Wraps a PyVista Qt interactor and everything drawn inside it."""

    hovered = QtCore.Signal(object)  # Probe | None
    measureChanged = QtCore.Signal()
    patchChanged = QtCore.Signal(object)   # SlopePatch | None
    boxProgress = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.plotter = QtInteractor(self)
        layout.addWidget(self.plotter.interactor)

        self.surface = None
        self.line: MeasureLine | None = None
        self.overlays: dict = {}
        self.targets = TargetLayer(self.plotter)

        self.ve = 6.0
        self.sun_az = 315.0
        self.sun_alt = 40.0
        self.ramp_name = "Bathy"
        self.color_by = "Depth"
        self.measuring = True
        self.box_mode = False        # clicks define a slope box, not a station
        self.box_size = 200.0        # metres; 0 means pick two corners
        self.patch = None            # the SlopePatch on screen, if any
        self._box_first = None       # first corner while one is being picked
        self.left_action = "rotate"  # left-drag swings the map; shift-left slides it
        self.lock_z = True           # spin level, and no height drift

        self._terrain = None  # pv actor
        self._mesh = None
        self._clim_depth = (0.0, 1.0)
        self._clim_slope = (0.0, 30.0)
        self._last_hover = 0.0
        self._press_pos = None
        self._drag_button = None
        self._drag_mode = None
        self._drag_z = None
        self._drag_rot = None
        self._picker = vtkPropPicker()

        self.plotter.set_background("#0d1418", top="#16232a")
        self.plotter.add_axes(interactive=False)
        self._install_observers()
        # Per frame, not per camera call: VTK moves the camera by routes this
        # class never sees - its own dolly, a window resize, camera.Zoom() -
        # and anything sized against the ground scale goes stale when it does.
        # set_metres_per_pixel ignores changes under 2%, so this is cheap.
        try:
            self.plotter.renderer.AddObserver(
                "StartEvent", lambda *_: self._sync_world_scale())
        except Exception:
            pass

    # ------------------------------------------------------------------ build

    def set_surface(self, surface) -> None:
        self.surface = surface
        self.line = MeasureLine(surface)
        self.overlays = {}      # draped on the previous terrain, so not reusable
        self.patch = None
        self._box_first = None
        self.plotter.clear()
        self.plotter.add_axes(interactive=False)
        self.targets = TargetLayer(self.plotter)
        self.targets.set_surface(surface)

        z = surface.z_disp[::-1, :]  # row 0 becomes the southern edge
        ny, nx = z.shape
        s = surface
        dx = s.px * s.mx * s.step
        dy = s.py * s.my * s.step
        x_first = (s.x0 + s.step * s.px / 2.0 - s.cx) * s.mx
        y_first = (s.y0 - ((ny - 1) * s.step + s.step / 2.0) * s.py - s.cy) * s.my

        valid = np.isfinite(z)
        fill = float(np.nanmedian(z)) if valid.any() else 0.0
        zf = np.where(valid, z, fill).astype(np.float32)

        slope = s.display_slope()[::-1, :]
        slope = np.where(np.isfinite(slope), slope, 0.0).astype(np.float32)

        grid = pv.ImageData(dimensions=(nx, ny, 1), spacing=(dx, dy, 1.0),
                            origin=(x_first, y_first, 0.0))
        grid.point_data["elev"] = zf.ravel(order="C")
        grid.point_data["slope"] = slope.ravel(order="C")
        grid.set_active_scalars("elev")
        mesh = grid.warp_by_scalar("elev", factor=1.0)

        # Drop any cell that touches a nodata corner rather than draping over it.
        bad = ~valid
        cell_bad = bad[:-1, :-1] | bad[:-1, 1:] | bad[1:, :-1] | bad[1:, 1:]
        if cell_bad.any():
            idx = np.flatnonzero(cell_bad.ravel(order="C"))
            if hasattr(mesh, "hide_cells"):
                mesh.hide_cells(idx, inplace=True)
            else:  # pragma: no cover - older pyvista
                keep = np.ones(mesh.n_cells, bool)
                keep[idx] = False
                mesh = mesh.extract_cells(np.flatnonzero(keep))

        self._mesh = mesh
        zv = z[valid]
        self._clim_depth = (float(zv.min()), float(zv.max()))
        sv = slope[valid]
        self._clim_slope = (0.0, float(np.percentile(sv, 99.0)) if sv.size else 30.0)

        self._add_terrain()
        self._apply_light()
        self.set_ve(self.ve)
        self.reset_view()
        self.measureChanged.emit()

    def _add_terrain(self) -> None:
        if self._mesh is None:
            return
        if self.color_by == "Slope":
            scalars, clim, title = "slope", self._clim_slope, "Slope (deg)"
        else:
            scalars, clim, title = "elev", self._clim_depth, "Elevation (m)"
        cmap = ramps.ramp(self.color_by, self.ramp_name)
        flat = self.ramp_name == ramps.FLAT_RAMP and self.color_by == "Depth"
        kwargs = dict(
            scalars=scalars,
            clim=clim,
            cmap=cmap,
            name="terrain",
            smooth_shading=True,
            ambient=0.20 if flat else 0.28,
            diffuse=1.0 if flat else 0.90,
            specular=0.02,
            lighting=True,
            show_scalar_bar=not flat,
        )
        if not flat:
            # Vertical: on a horizontal bar VTK crowds the title onto the tick
            # labels whatever height it is given.
            kwargs["scalar_bar_args"] = dict(
                title=title, n_labels=6, fmt="%.0f", color="#cfdde1",
                title_font_size=13, label_font_size=11, vertical=True,
                width=0.028, height=0.48, position_x=0.90, position_y=0.26,
            )
        self._terrain = self.plotter.add_mesh(self._mesh, **kwargs)
        self._picker.InitializePickList()
        self._picker.AddPickList(self._terrain)
        self._picker.PickFromListOn()
        self._terrain.SetScale(1.0, 1.0, self.ve)

    # --------------------------------------------------------------- settings

    def set_ve(self, ve: float) -> None:
        self.ve = float(ve)
        if self._terrain is not None:
            self._terrain.SetScale(1.0, 1.0, self.ve)
        self.targets.set_ve(self.ve)
        for layer in self.overlays.values():
            self._draw_overlay(layer)
        if self.patch is not None:
            self._draw_patch()
        self._redraw_measure()
        self.update_clipping()
        self.plotter.render()

    def set_sun(self, az: float | None = None, alt: float | None = None) -> None:
        if az is not None:
            self.sun_az = float(az)
        if alt is not None:
            self.sun_alt = float(alt)
        self._apply_light()
        self.plotter.render()

    def _apply_light(self) -> None:
        self.plotter.remove_all_lights()
        a, e = math.radians(self.sun_az), math.radians(self.sun_alt)
        r = 1.0e6
        pos = (math.sin(a) * math.cos(e) * r, math.cos(a) * math.cos(e) * r, math.sin(e) * r)
        sun = pv.Light(position=pos, focal_point=(0, 0, 0), color="white",
                       light_type="scene light")
        sun.positional = False
        sun.intensity = 1.0
        self.plotter.add_light(sun)
        fill = pv.Light(position=(-pos[0], -pos[1], r * 0.6), focal_point=(0, 0, 0),
                        color="#9fc0d0", light_type="scene light")
        fill.positional = False
        fill.intensity = 0.22
        self.plotter.add_light(fill)

    def set_ramp(self, name: str) -> None:
        self.ramp_name = name
        self._rebuild_terrain()

    def set_surface_colours(self, color_by: str, ramp_name: str) -> None:
        """Change what is coloured and which ramp at once - one rebuild, not two."""
        self.color_by = color_by
        self.ramp_name = ramp_name
        self._rebuild_terrain()

    def set_color_by(self, what: str) -> None:
        self.color_by = what
        self._rebuild_terrain()

    def _rebuild_terrain(self) -> None:
        if self._mesh is None:
            return
        self.plotter.remove_actor("terrain", render=False)
        try:
            self.plotter.remove_scalar_bar(render=False)
        except Exception:
            pass
        self._add_terrain()
        self.plotter.render()

    # ----------------------------------------------------------------- camera

    def _focus(self):
        if self._terrain is None:
            return (0, 0, 0), 1000.0
        b = self._terrain.GetBounds()
        c = ((b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2)
        r = max(b[1] - b[0], b[3] - b[2], 1.0)
        return c, r

    def set_view(self, az: float, el: float, zoom: float = 1.0) -> None:
        c, r = self._focus()
        d = r * 1.75 / max(zoom, 0.05)
        a, e = math.radians(az), math.radians(el)
        pos = (c[0] + d * math.cos(e) * math.sin(a),
               c[1] + d * math.cos(e) * math.cos(a),
               c[2] + d * math.sin(e))
        up = (0, 0, 1) if el < 88 else (0, 1, 0)
        self.plotter.camera_position = [pos, c, up]
        self.update_clipping()
        self.plotter.render()

    def reset_view(self) -> None:
        self.set_view(210.0, 28.0)

    def follow_targets(self) -> bool:
        """Re-centre on the vehicles without changing zoom or view angle.

        Separate from :meth:`zoom_to_targets` because once you have framed the
        vehicles you want to keep your chosen scale as they move, not have it
        re-fitted every second.
        """
        if self.surface is None:
            return False
        pts = [(*self.surface.local_from_crs(t.x, t.y), t.z * self.ve)
               for t in self.targets.targets.values() if t.fix]
        if not pts:
            return False
        xs, ys, zs = zip(*pts)
        centre = np.array([sum(xs) / len(xs), sum(ys) / len(ys), sum(zs) / len(zs)])
        cam = self.plotter.camera
        offset = np.asarray(cam.position, float) - np.asarray(cam.focal_point, float)
        self.plotter.camera_position = [tuple(centre + offset), tuple(centre), (0, 0, 1)]
        self.update_clipping()
        return True

    def metres_per_pixel(self) -> float:
        """Ground scale at the focal point - how much motion a pixel is worth."""
        cam = self.plotter.camera
        d = float(np.linalg.norm(np.asarray(cam.position, float)
                                 - np.asarray(cam.focal_point, float)))
        h = max(self.plotter.window_size[1], 1)
        return 2.0 * math.tan(math.radians(cam.view_angle) / 2.0) * d / h

    def zoom_to_targets(self) -> bool:
        """Frame the tracked vehicles, keeping the current view direction.

        Needed in practice: the vehicles sit a few hundred metres apart on a
        grid over 100 km wide, so at full extent they are a couple of pixels.
        """
        if self.surface is None:
            return False
        pts = [(*self.surface.local_from_crs(t.x, t.y), t.z * self.ve)
               for t in self.targets.targets.values() if t.fix]
        if not pts:
            return False
        xs, ys, zs = zip(*pts)
        centre = (sum(xs) / len(xs), sum(ys) / len(ys), sum(zs) / len(zs))
        spread = max(max(xs) - min(xs), max(ys) - min(ys), 300.0)
        cam = self.plotter.camera
        eye = np.asarray(cam.position, float) - np.asarray(cam.focal_point, float)
        norm = float(np.linalg.norm(eye))
        direction = eye / norm if norm > 1e-9 else np.array([0.0, -1.0, 0.6])
        direction = direction / float(np.linalg.norm(direction))
        pos = np.asarray(centre, float) + direction * spread * 3.2
        self.plotter.camera_position = [tuple(pos), centre, (0, 0, 1)]
        self.update_clipping()
        self.plotter.render()
        return True

    def plan_view(self) -> None:
        self.set_view(180.0, 89.9, zoom=1.15)

    # ------------------------------------------------------------- picking

    def _install_observers(self) -> None:
        # Everything hangs off the interactor *style*, not the interactor. The
        # style aborts each event once it has handled it, so observers on the
        # interactor miss button presses entirely - which silently cost the
        # click-to-measure the first time this was wired up.
        self.set_left_action(self.left_action)

    def _attach_style_observers(self) -> None:
        try:
            style = self.plotter.iren.interactor.GetInteractorStyle()
        except Exception:
            return
        style.AddObserver("MouseMoveEvent", self._style_move, -1.0)
        style.AddObserver("LeftButtonPressEvent", self._style_press, -1.0)
        style.AddObserver("LeftButtonReleaseEvent", self._style_release, -1.0)
        style.AddObserver("MouseWheelForwardEvent", self._style_wheel_in, -1.0)
        style.AddObserver("MouseWheelBackwardEvent", self._style_wheel_out, -1.0)

    # ----------------------------------------------------------------- zoom

    def _scene_span(self) -> float:
        """Diagonal of the terrain, used to scale the zoom limits."""
        if self._terrain is None:
            return 1000.0
        b = self._terrain.GetBounds()
        span = math.dist((b[0], b[2], b[4]), (b[1], b[3], b[5]))
        return span if span > 1.0 else 1000.0

    def _sync_world_scale(self) -> None:
        """Tell the target layer the ground scale, for the TMS bodies."""
        try:
            self.targets.set_metres_per_pixel(self.metres_per_pixel())
        except Exception:
            pass

    def update_clipping(self) -> None:
        """Set the clipping range from the camera distance, not scene bounds.

        VTK's ResetCameraClippingRange derives the range from what is in the
        scene and then clamps the near plane to far/1000. On a 130 km grid that
        pins it near 110 m, so closing to within 110 m of the seabed clips the
        seabed away and zooming appears to stop. Deriving both planes from the
        current distance keeps the ratio sane at every scale instead.
        """
        cam = self.plotter.camera
        d = math.dist(cam.position, cam.focal_point)
        near = max(d * 0.0015, 0.02)
        cam.clipping_range = (near, d + self._scene_span() * 2.0)
        self._sync_world_scale()

    def _style_wheel_in(self, _style, _event):
        self.zoom(1.0 / ZOOM_STEP)

    def _style_wheel_out(self, _style, _event):
        self.zoom(ZOOM_STEP)

    def _pick_world(self):
        """World point under the pointer, or None if it missed the terrain."""
        if self._terrain is None:
            return None
        try:
            x, y = self.plotter.iren.interactor.GetEventPosition()
        except Exception:
            return None
        if not self._picker.Pick(x, y, 0, self.plotter.renderer):
            return None
        return np.asarray(self._picker.GetPickPosition(), dtype=float)

    def zoom(self, factor: float) -> None:
        """Zoom about the seabed under the pointer.

        VTK dollies towards the focal point, which sits at the middle of the
        scene - in open water above the bottom. Keep pulling on the wheel and
        the camera arrives there and then passes through the seabed, which is
        what made close zoom useless. Anchoring on the point under the pointer
        keeps that spot still on screen and converges the camera onto the
        surface instead, so closing right in stays meaningful.
        """
        cam = self.plotter.camera
        pos = np.asarray(cam.position, dtype=float)
        foc = np.asarray(cam.focal_point, dtype=float)
        anchor = self._pick_world()
        if anchor is None:
            anchor = foc
        pos = anchor + (pos - anchor) * factor
        foc = anchor + (foc - anchor) * factor

        span = self._scene_span()
        d = float(np.linalg.norm(pos - foc))
        if d > 1e-12:
            capped = min(max(d, MIN_ZOOM_M), span * MAX_ZOOM_SPANS)
            if abs(capped - d) > 1e-9:
                pos = foc + (pos - foc) * (capped / d)
        cam.position = tuple(pos)
        cam.focal_point = tuple(foc)
        self.update_clipping()
        self.plotter.render()

    # An observer on a style replaces its default handling, so each of these
    # invokes the default itself and then adds our own behaviour.

    def _style_move(self, style, _event):
        try:
            style.OnMouseMove()
        except Exception:
            pass
        self._apply_z_lock()
        self._on_move()

    def _style_press(self, style, _event):
        self._on_press()
        try:
            style.OnLeftButtonDown()
        except Exception:
            pass

    def _style_release(self, style, _event):
        try:
            style.OnLeftButtonUp()
        except Exception:
            pass
        self._on_release()

    # --------------------------------------------------------- camera control

    def set_left_action(self, action: str) -> None:
        """What a left-drag does: rotate the view, or slide the map.

        Rotate is the default, but with Lock Z on the camera stays on one
        horizontal circle: the map swings round and the viewing angle you set
        is kept. Shift-left does the other one.
        """
        self.left_action = "pan" if action == "pan" else "rotate"
        try:
            self.plotter.enable_custom_trackball_style(
                left=self.left_action,
                shift_left="pan" if self.left_action == "rotate" else "rotate",
                middle="pan", right="dolly",
            )
        except Exception:
            pass
        # enable_custom_trackball_style builds a fresh style object, so the
        # observers have to be hung on the new one every time.
        self._attach_style_observers()

    def _apply_z_lock(self):
        """Hold the Z axis still, whichever way the drag is moving the camera.

        Rotating: VTK's trackball changes the tilt as well as the heading, so
        a drag tips the chart out of the viewing angle you set. With the lock
        on, the camera's height above the target and its distance from it are
        both held, so the camera stays on one horizontal circle and a drag
        only swings the map round - the compass turns, the tilt does not.

        Panning: VTK pans in the plane of the screen, so on a tilted view
        sliding sideways also changes your altitude and the scene creeps away.
        Camera and focal point shift together, so putting both heights back
        keeps the horizontal part of the move and nothing else.
        """
        if not self.lock_z or self._drag_mode is None:
            return
        cam = self.plotter.camera
        pos, foc = list(cam.position), list(cam.focal_point)

        if self._drag_mode == "pan":
            if self._drag_z is None:
                return
            pz, fz = self._drag_z
            if abs(pos[2] - pz) > 1e-9 or abs(foc[2] - fz) > 1e-9:
                pos[2], foc[2] = pz, fz
                cam.position, cam.focal_point = tuple(pos), tuple(foc)
                self.update_clipping()
            return

        if self._drag_rot is None:
            return
        radius, rise = self._drag_rot
        vx, vy, _vz = (pos[i] - foc[i] for i in range(3))
        heading = math.atan2(vx, vy)
        horiz = math.sqrt(max(radius * radius - rise * rise, 0.0))
        if horiz < 1e-9:
            return                      # straight overhead: no heading to turn
        nx, ny = math.sin(heading) * horiz, math.cos(heading) * horiz
        cam.position = (foc[0] + nx, foc[1] + ny, foc[2] + rise)
        # Keep the horizon level, except looking almost straight down where
        # "up is +Z" stops meaning anything.
        if abs(rise) / radius < 0.999:
            cam.up = (0.0, 0.0, 1.0)
        self.update_clipping()

    def _pick_crs(self):
        """CRS (x, y) under the pointer, or None if the ray missed the surface."""
        if self._terrain is None or self.surface is None:
            return None
        try:
            x, y = self.plotter.iren.interactor.GetEventPosition()
        except Exception:
            return None
        if not self._picker.Pick(x, y, 0, self.plotter.renderer):
            return None
        wx, wy, _wz = self._picker.GetPickPosition()
        return self.surface.crs_from_local(wx, wy)

    def _on_move(self, *_):
        now = time.monotonic()
        if now - self._last_hover < 0.033:  # ~30 Hz is plenty for a readout
            return
        self._last_hover = now
        pos = self._pick_crs()
        self.hovered.emit(self.surface.probe(*pos) if pos else None)

    def _on_press(self, *_):
        iren = self.plotter.iren.interactor
        try:
            self._press_pos = iren.GetEventPosition()
        except Exception:
            self._press_pos = None
        self._drag_button = "left"
        # Shift swaps whatever the left button normally does, so work out
        # which it is now and lock the matching axis for this drag.
        try:
            shift = bool(iren.GetShiftKey())
        except Exception:
            shift = False
        base = self.left_action
        other = "pan" if base == "rotate" else "rotate"
        self._drag_mode = other if shift else base
        cam = self.plotter.camera
        pos, foc = cam.position, cam.focal_point
        self._drag_z = (pos[2], foc[2])
        v = [pos[i] - foc[i] for i in range(3)]
        # Distance to the target and how far the camera sits above it:
        # hold both and only the compass heading is free to change.
        self._drag_rot = (math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2), v[2])

    def _on_release(self, *_):
        self._drag_button = None
        self._drag_mode = None
        self._drag_z = None
        self._drag_rot = None
        if not (self.measuring or self.box_mode) or self._press_pos is None \
                or self.line is None:
            self._press_pos = None
            return
        try:
            now = self.plotter.iren.interactor.GetEventPosition()
        except Exception:
            self._press_pos = None
            return
        moved = abs(now[0] - self._press_pos[0]) + abs(now[1] - self._press_pos[1])
        self._press_pos = None
        if moved > 4:  # that was a rotate, not a click
            return
        pos = self._pick_crs()
        if not pos:
            return
        if self.box_mode:
            self._box_click(pos)
            return
        p = self.surface.probe(*pos)
        if p is None:
            return
        self.line.add(Station(p.x, p.y, p.z, p.lon, p.lat))
        self._redraw_measure()
        self.measureChanged.emit()

    # --------------------------------------------------------------- measuring

    def clear_measure(self) -> None:
        if self.line:
            self.line.clear()
            self._redraw_measure()
            self.measureChanged.emit()

    def undo_measure(self) -> None:
        if self.line:
            self.line.undo()
            self._redraw_measure()
            self.measureChanged.emit()

    def _redraw_measure(self) -> None:
        for nm in ("mline", "mpts", "mlabels"):
            self.plotter.remove_actor(nm, render=False)
        if not self.line or not self.line.stations or self.surface is None:
            self.plotter.render()
            return
        pts = np.array(
            [(*self.surface.local_from_crs(s.x, s.y), s.z * self.ve) for s in self.line.stations],
            dtype=float,
        )
        # Screen-constant, and deliberately smaller than a vehicle dot: a
        # station marks a spot you picked, the vehicles are what you watch.
        # A world-space sphere sized off a 131 km grid came out over a
        # kilometre across, which swallowed the seabed once the camera moved in.
        mark = self.plotter.add_points(
            pts, color=MEASURE_COLOR, point_size=STATION_POINT_PX,
            render_points_as_spheres=True, name="mpts",
            render=False, pickable=False,
        )
        draw_on_top(mark)
        if len(pts) > 1:
            line = self.plotter.add_mesh(
                pv.lines_from_points(pts), color=MEASURE_COLOR, line_width=2,
                name="mline", render=False, pickable=False,
            )
            draw_on_top(line)
        self.plotter.add_point_labels(
            pts, [str(i + 1) for i in range(len(pts))], name="mlabels",
            font_size=12, text_color="#ffe0b0", shape=None, show_points=False,
            always_visible=True, render=False,
        )
        self.plotter.render()

    # -------------------------------------------------------------- overlays

    def add_overlay(self, layer) -> None:
        self.overlays[layer.name] = layer
        self._draw_overlay(layer)
        self.plotter.render()

    def remove_overlay(self, name: str) -> None:
        self.overlays.pop(name, None)
        for nm in (f"ov:{name}", f"ovl:{name}"):
            self.plotter.remove_actor(nm, render=False)
        self.plotter.render()

    def set_overlay_colour(self, name: str, colour: str) -> None:
        layer = self.overlays.get(name)
        if layer is None:
            return
        layer.color = colour
        self._draw_overlay(layer)
        self.plotter.render()

    def set_overlay_visible(self, name: str, on: bool) -> None:
        layer = self.overlays.get(name)
        if layer is None:
            return
        layer.visible = bool(on)
        self._draw_overlay(layer)
        self.plotter.render()

    def clear_overlays(self) -> None:
        for name in list(self.overlays):
            self.remove_overlay(name)

    def _draw_overlay(self, layer) -> None:
        for nm in (f"ov:{layer.name}", f"ovl:{layer.name}"):
            self.plotter.remove_actor(nm, render=False)
        if not layer.visible or not layer.parts:
            return
        if layer.kind == "point":
            pts = layer.parts[0].copy()
            pts[:, 2] *= self.ve
            actor = self.plotter.add_points(
                pts, color=layer.color, point_size=OVERLAY_POINT_PX,
                render_points_as_spheres=True, name=f"ov:{layer.name}",
                render=False, pickable=False)
        else:
            # One PolyData with explicit line connectivity. pv.merge over
            # thousands of separate parts is far slower for the same result.
            scaled = []
            cells = []
            off = 0
            for part in layer.parts:
                p = part.copy()
                p[:, 2] *= self.ve
                scaled.append(p)
                n = len(p)
                cells.append(np.concatenate([[n], np.arange(off, off + n)]))
                off += n
            poly = pv.PolyData(np.vstack(scaled),
                               lines=np.concatenate(cells).astype(np.int64))
            actor = self.plotter.add_mesh(
                poly, color=layer.color, line_width=2, name=f"ov:{layer.name}",
                render=False, pickable=False)
        draw_on_top(actor)
        actor.SetVisibility(True)

        if layer.labels:
            lp = np.array([[a, b, c * self.ve] for a, b, c, _ in layer.labels], float)
            self.plotter.add_point_labels(
                lp, [t for *_, t in layer.labels], name=f"ovl:{layer.name}",
                font_size=9, text_color=layer.color, shape=None,
                show_points=False, always_visible=True, render=False)

    # ----------------------------------------------------------- node slides

    def draw_slide(self, name: str, path, left, right, colour="#f0a93c"):
        """A predicted slide path and its search corridor, draped on the seabed.

        Drawn like a shapefile overlay and for the same reason: it is a line on
        the bottom, and a line on the bottom has to follow the relief or it
        means nothing. The corridor edges are dashed so they never read as the
        prediction itself - the corridor is where to look, the centre line is
        only the most likely track within it.
        """
        self.clear_slide(name)
        if self.surface is None or len(path) < 2:
            return
        lift = max(self.surface.native_cell_m * 0.2, 1.0)

        def drape(pairs):
            out = []
            for item in pairs:
                x, y = item[0], item[1]
                p = self.surface.probe(x, y)
                if p is None or not np.isfinite(p.z):
                    continue
                lx, ly = self.surface.local_from_crs(x, y)
                out.append((lx, ly, (p.z + lift) * self.ve))
            return np.asarray(out, dtype=float)

        centre = drape(path)
        if len(centre) >= 2:
            actor = self.plotter.add_mesh(
                pv.lines_from_points(centre), color=colour, line_width=3,
                name=f"slide:{name}", render=False, pickable=False)
            draw_on_top(actor)
        for side, pts in (("l", left), ("r", right)):
            edge = drape(pts)
            if len(edge) < 2:
                continue
            actor = self.plotter.add_mesh(
                pv.lines_from_points(edge), color=colour, line_width=2,
                opacity=0.55, name=f"slide{side}:{name}",
                render=False, pickable=False)
            draw_on_top(actor)
        # The placement point, so the start of the search is unmistakable.
        if len(centre):
            actor = self.plotter.add_points(
                centre[:1], color=colour, point_size=14,
                render_points_as_spheres=True, name=f"slidep:{name}",
                render=False, pickable=False)
            draw_on_top(actor)
        self.plotter.render()

    def clear_slide(self, name: str) -> None:
        for prefix in ("slide", "slidel", "slider", "slidep"):
            self.plotter.remove_actor(f"{prefix}:{name}", render=False)

    def clear_slides(self) -> None:
        for actor in list(getattr(self.plotter, "actors", {}) or {}):
            if str(actor).startswith(("slide:", "slidel:", "slider:",
                                      "slidep:")):
                self.plotter.remove_actor(actor, render=False)
        self.plotter.render()

    # ------------------------------------------------------------- slope box

    def set_box_mode(self, on: bool) -> None:
        self.box_mode = bool(on)
        if not on:
            self._box_first = None
            self.plotter.remove_actor("boxcorner", render=False)
            self.plotter.render()

    def _box_click(self, pos) -> None:
        """One click centres a fixed box; two clicks define a free rectangle."""
        x, y = pos
        if self.box_size and self.box_size > 0:
            half = self.box_size / 2.0
            self.make_patch(x - half, y - half, x + half, y + half)
            return
        if self._box_first is None:
            self._box_first = (x, y)
            self._mark_corner(x, y)
            self.boxProgress.emit("First corner set - click the opposite one")
            return
        (x0, y0), self._box_first = self._box_first, None
        self.plotter.remove_actor("boxcorner", render=False)
        self.make_patch(x0, y0, x, y)

    def _mark_corner(self, x, y) -> None:
        p = self.surface.probe(x, y)
        if p is None:
            return
        pt = np.array([[*self.surface.local_from_crs(x, y), p.z * self.ve]])
        actor = self.plotter.add_points(
            pt, color=BOX_COLOR, point_size=9, render_points_as_spheres=True,
            name="boxcorner", render=False, pickable=False)
        draw_on_top(actor)
        self.plotter.render()

    def make_patch(self, x0, y0, x1, y1) -> None:
        """Recompute slope at native resolution for this rectangle and draw it."""
        if self.surface is None:
            return
        try:
            patch = self.surface.slope_patch(x0, y0, x1, y1)
        except Exception as exc:
            self.boxProgress.emit(str(exc))
            return
        self.patch = patch
        self._draw_patch()
        self.patchChanged.emit(patch)

    def clear_patch(self) -> None:
        self.patch = None
        self._box_first = None
        for nm in ("patch", "patchedge", "boxcorner"):
            self.plotter.remove_actor(nm, render=False)
        self.plotter.render()
        self.patchChanged.emit(None)

    def _draw_patch(self) -> None:
        """The window drawn at its own resolution, on its own colour scale."""
        for nm in ("patch", "patchedge"):
            self.plotter.remove_actor(nm, render=False)
        p = self.patch
        if p is None or self.surface is None:
            return
        s = self.surface
        z = p.z[::-1, :]
        sl = p.slope[::-1, :]
        ny, nx = z.shape
        # cell centres, in the same local metres the terrain uses
        x_first, y_first = s.local_from_crs(
            *s.crs_from_rowcol(p.row0 + ny - 1, p.col0))
        dx = s.px * s.mx * s.probe_step
        dy = s.py * s.my * s.probe_step
        valid = np.isfinite(z)
        fill = float(np.nanmedian(z)) if valid.any() else 0.0
        grid = pv.ImageData(dimensions=(nx, ny, 1), spacing=(dx, dy, 1.0),
                            origin=(x_first, y_first, 0.0))
        grid.point_data["elev"] = np.where(valid, z, fill).ravel(order="C").astype(np.float32)
        grid.point_data["slope"] = np.where(np.isfinite(sl), sl, 0.0
                                            ).ravel(order="C").astype(np.float32)
        grid.set_active_scalars("elev")
        mesh = grid.warp_by_scalar("elev", factor=1.0)
        bad = ~valid
        cb = bad[:-1, :-1] | bad[:-1, 1:] | bad[1:, :-1] | bad[1:, 1:]
        if cb.any() and hasattr(mesh, "hide_cells"):
            mesh.hide_cells(np.flatnonzero(cb.ravel(order="C")), inplace=True)

        # Its own colour range: a box on a flat basin and one on a salt flank
        # need different scales, and sharing the main map's would waste it.
        good = p.slope[np.isfinite(p.slope)]
        hi = float(np.percentile(good, 99)) if good.size else 1.0
        actor = self.plotter.add_mesh(
            mesh, scalars="slope", clim=(0.0, max(hi, 0.5)),
            cmap=_ramps.SLOPE_RAMPS["Green to red"], name="patch",
            smooth_shading=True, ambient=0.30, diffuse=0.85,
            show_scalar_bar=False, render=False, pickable=False)
        actor.SetScale(1.0, 1.0, self.ve)
        # Only just clear of the terrain: the box is a sheet of seabed, and
        # anything drawn on the seabed - overlays, vehicles, stations - has to
        # stay visible through it rather than be covered by it.
        draw_on_top(actor, ON_TOP_SHEET)
        self._draw_patch_edge()

    def _draw_patch_edge(self) -> None:
        p = self.patch
        s = self.surface
        if p is None or s is None:
            return
        corners = [(p.x0, p.y0), (p.x1, p.y0), (p.x1, p.y1), (p.x0, p.y1)]
        pts = []
        for x, y in corners + [corners[0]]:
            pr = s.probe(x, y)
            zz = pr.z if pr else float(np.nanmedian(p.z))
            pts.append((*s.local_from_crs(x, y), zz * self.ve))
        actor = self.plotter.add_mesh(
            pv.lines_from_points(np.asarray(pts, float)), color=BOX_COLOR,
            line_width=2, name="patchedge", render=False, pickable=False)
        draw_on_top(actor)
        self.plotter.render()

    # ------------------------------------------------------------------ misc

    def screenshot(self, path: str) -> None:
        self.plotter.screenshot(path)

    def close(self) -> None:
        try:
            self.plotter.close()
        except Exception:
            pass
        super().close()
