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
from .targets import BASE_POINT_PX, TargetLayer, draw_on_top

#: Colour of measurement furniture.
MEASURE_COLOR = "#f0a93c"

#: Station dot size in screen pixels. Smaller than a vehicle dot
#: (BASE_POINT_PX) on purpose - stations are reference marks, not the
#: thing being tracked.
STATION_POINT_PX = BASE_POINT_PX * 0.62


class TerrainView(QtWidgets.QWidget):
    """Wraps a PyVista Qt interactor and everything drawn inside it."""

    hovered = QtCore.Signal(object)  # Probe | None
    measureChanged = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.plotter = QtInteractor(self)
        layout.addWidget(self.plotter.interactor)

        self.surface = None
        self.line: MeasureLine | None = None
        self.targets = TargetLayer(self.plotter)

        self.ve = 6.0
        self.sun_az = 315.0
        self.sun_alt = 40.0
        self.ramp_name = "Bathy"
        self.color_by = "Depth"
        self.measuring = True

        self._terrain = None  # pv actor
        self._mesh = None
        self._clim_depth = (0.0, 1.0)
        self._clim_slope = (0.0, 30.0)
        self._last_hover = 0.0
        self._press_pos = None
        self._picker = vtkPropPicker()

        self.plotter.set_background("#0d1418", top="#16232a")
        self.plotter.add_axes(interactive=False)
        self._install_observers()

    # ------------------------------------------------------------------ build

    def set_surface(self, surface) -> None:
        self.surface = surface
        self.line = MeasureLine(surface)
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
            scalars, clim, cmap, title = "slope", self._clim_slope, ramps.SLOPE_RAMP, "Slope (deg)"
        else:
            scalars, clim = "elev", self._clim_depth
            cmap = ramps.depth_ramp(self.ramp_name)
            title = "Elevation (m)"
        flat = self.ramp_name == "Hillshade only" and self.color_by == "Depth"
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
        self._redraw_measure()
        self.plotter.renderer.ResetCameraClippingRange()
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
        self.plotter.renderer.ResetCameraClippingRange()
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
        self.plotter.renderer.ResetCameraClippingRange()
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
        self.plotter.renderer.ResetCameraClippingRange()
        self.plotter.render()
        return True

    def plan_view(self) -> None:
        self.set_view(180.0, 89.9, zoom=1.15)

    # ------------------------------------------------------------- picking

    def _install_observers(self) -> None:
        iren = self.plotter.iren
        iren.add_observer("MouseMoveEvent", self._on_move)
        iren.add_observer("LeftButtonPressEvent", self._on_press)
        iren.add_observer("LeftButtonReleaseEvent", self._on_release)

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
        try:
            self._press_pos = self.plotter.iren.interactor.GetEventPosition()
        except Exception:
            self._press_pos = None

    def _on_release(self, *_):
        if not self.measuring or self._press_pos is None or self.line is None:
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

    # ------------------------------------------------------------------ misc

    def screenshot(self, path: str) -> None:
        self.plotter.screenshot(path)

    def close(self) -> None:
        try:
            self.plotter.close()
        except Exception:
            pass
        super().close()
