#!/usr/bin/env python
"""Checks for the native-resolution slope box.

A box picked on the seabed must be recomputed from the probe grid at its own
resolution - not read off the decimated display mesh - so it has to show slope
the display mesh averages away. Also checks the two picking modes, that the
box follows vertical exaggeration, and that it is bounded.

    python slopebox_test.py [grid.tif]
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import os as _os
_os.environ["BATHY3D_PROFILE"] = "test-slopebox"
from bathy3d import prefs as _prefs          # noqa: E402
_prefs.settings().clear()

import numpy as np                            # noqa: E402
from PySide6 import QtCore, QtWidgets         # noqa: E402
from bathy3d import raster                    # noqa: E402
from bathy3d.mainwindow import MainWindow      # noqa: E402

GRID = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:\Users\mkozh\OneDrive\Desktop\bathy\BOEM_bathy_WGS84_UTM15N.tif"

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
    if not cond:
        FAILED.append(name)


app = QtWidgets.QApplication(sys.argv[:1])
win = MainWindow(GRID)
win.resize(1400, 880)
win.show()


def pump(ms):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.004)


for _ in range(600):
    pump(100)
    if win.view.surface is not None:
        break
assert win.view.surface is not None, "grid did not load"
s = win.view.surface
print(f"native {s.native_cell_m:.2f} m, display mesh {s.cell_m:.1f} m\n")

# somewhere with real relief
x, y = s.crs_from_rowcol(s.height * 0.40, s.width * 0.45)

print("the box is computed from the probe grid:")
t0 = time.perf_counter()
win.view.make_patch(x - 100, y - 100, x + 100, y + 100)
dt = (time.perf_counter() - t0) * 1000
pump(300)
p = win.view.patch
check("a box was made", p is not None, f"{dt:.1f} ms")
if p is None:
    sys.exit(1)
st = p.stats()
print(f"  {p.shape[1]} x {p.shape[0]} cells at {p.cell:.2f} m, computed in {dt:.1f} ms")
check("it is at native resolution", abs(p.cell - s.native_cell_m) < 1e-6,
      f"{p.cell:.2f} m vs native {s.native_cell_m:.2f} m")
check("200 m really is about 16 cells across", 15 <= p.shape[1] <= 19,
      f"{p.shape[1]}")
check("fast enough to feel instant", dt < 500, f"{dt:.1f} ms")

# the whole point: it must find slope the display mesh cannot hold
disp = s.display_slope()
r, c = s.rowcol_from_crs(x, y)
dr, dc = int(r / s.step), int(c / s.step)
coarse = disp[max(0, dr - 1):dr + 2, max(0, dc - 1):dc + 2]
coarse_max = float(np.nanmax(coarse))
print(f"\n  native box  : mean {st['mean']:5.2f}  p95 {st['p95']:5.2f}  "
      f"max {st['max']:6.2f} deg")
print(f"  display mesh: max {coarse_max:6.2f} deg over the same ground")
check("the box sees steeper ground than the display mesh",
      st["max"] > coarse_max, f"{st['max']:.2f} vs {coarse_max:.2f} deg")
check("relief inside the box is reported", st["relief"] > 0,
      f"{st['relief']:.1f} m")

# against an independent calculation of the same window
# Only the interior: the real one reads a one-cell halo beyond the box, so its
# edge cells come from true neighbours while a bare recomputation pads the edge.
sl2, _ = raster.horn_slope(p.z, p.cell)
a, b = sl2[1:-1, 1:-1], p.slope[1:-1, 1:-1]
m = np.isfinite(a) & np.isfinite(b)
check("interior matches a direct Horn calculation",
      bool(m.any()) and np.allclose(a[m], b[m], atol=1e-4))
edge_same = np.allclose(sl2[0][np.isfinite(sl2[0])],
                        p.slope[0][np.isfinite(p.slope[0])], atol=1e-4)
check("edge cells use the halo, not a padded edge", not edge_same)

# The point of the box is a more precise reading, so hovering inside it must
# report what the box says - same method, same data, same number.
print("\nthe cursor readout agrees with the box:")
worst_s = worst_a = 0.0
n = 0
for rr in range(1, p.shape[0] - 1):
    for cc in range(1, p.shape[1] - 1):
        x_, y_ = s.crs_from_rowcol(p.row0 + rr, p.col0 + cc)
        pr = s.probe(x_, y_)
        want = p.slope[rr, cc]
        if pr is None or not np.isfinite(want):
            continue
        n += 1
        worst_s = max(worst_s, abs(pr.slope - float(want)))
        wa = p.aspect[rr, cc]
        if np.isfinite(wa) and np.isfinite(pr.aspect):
            d = abs(pr.aspect - float(wa)) % 360.0
            worst_a = max(worst_a, min(d, 360.0 - d))
print(f"  compared {n} cells")
# Not bit-exact: the patch computes in float32 to keep a large box cheap,
# the readout in float64. Anything above this would be a method difference.
check("cursor slope matches the box", worst_s < 0.02,
      f"worst difference {worst_s:.6f} deg")
check("cursor aspect matches the box", worst_a < 0.05,
      f"worst difference {worst_a:.6f} deg")

print("\ndrawing:")
actors = win.view.plotter.renderer.actors
check("patch mesh drawn", "patch" in actors, str([a for a in actors if "patch" in a]))
check("outline drawn", "patchedge" in actors)
z0 = actors["patch"].GetBounds()[4]
win.ve_s.setValue(140)
pump(300)
z1 = win.view.plotter.renderer.actors["patch"].GetBounds()[4]
check("patch follows vertical exaggeration", abs(z1) > abs(z0) * 1.8,
      f"zmin {z0:,.0f} -> {z1:,.0f}")
win.ve_s.setValue(60)
pump(200)

print("\npicking modes:")
win.box_b.setChecked(True)
pump(150)
check("slope box mode turns measure mode off",
      win.view.box_mode and not win.meas_b.isChecked())
win.meas_b.setChecked(True)
pump(150)
check("and measure mode turns slope box off",
      win.view.measuring and not win.box_b.isChecked())

win.box_b.setChecked(True)
win.box_c.setCurrentText("Two corners")
pump(150)
check("two-corner mode selected", win.view.box_size == 0.0)
win.view.clear_patch()
pump(150)
win.view._box_click((x - 400, y - 400))
pump(150)
check("first corner is held, no box yet",
      win.view._box_first is not None and win.view.patch is None)
win.view._box_click((x + 400, y + 400))
pump(250)
p2 = win.view.patch
check("second corner makes the box", p2 is not None)
if p2:
    check("the box is about the size asked for",
          750 < p2.stats()["side_x"] < 850, f"{p2.stats()['side_x']:,.0f} m")

win.box_c.setCurrentText("200 m")
pump(150)
win.view._box_click((x, y))
pump(250)
check("one click places a fixed box",
      win.view.patch is not None and 180 < win.view.patch.stats()["side_x"] < 220,
      f"{win.view.patch.stats()['side_x']:,.0f} m" if win.view.patch else "none")

# A slope box is a sheet of seabed, so anything that belongs on the seabed -
# shapefiles, vehicles, stations - has to stay visible through it. With every
# "draw on top" layer given the same depth bias, the box simply buried them.
SHP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "EGN10_Preplot_SHP.shp")
if os.path.exists(SHP):
    print("\noverlays stay visible inside the box:")
    win.view.clear_patch()
    win.add_overlay_path(SHP)
    pump(500)
    win.view.set_overlay_colour("EGN10_Preplot_SHP", "#ff00ff")
    pump(250)
    op = win.view.overlays["EGN10_Preplot_SHP"].parts[0]
    lx, ly = float(op[:, 0].mean()), float(op[:, 1].mean())
    ox, oy = s.crs_from_local(lx, ly)
    opr = s.probe(ox, oy)
    cam = win.view.plotter.camera

    def _look(d=4200):
        cam.focal_point = (lx, ly, opr.z * win.view.ve)
        cam.position = (lx, ly - d * 0.2, opr.z * win.view.ve + d)
        cam.up = (0, 1, 0)
        win.view.update_clipping()
        pump(300)

    def _magenta():
        img = np.asarray(win.view.plotter.screenshot(return_img=True)).astype(int)
        return int(((img[:, :, 0] > 170) & (img[:, :, 1] < 100)
                    & (img[:, :, 2] > 170)).sum())

    _look()
    n_before = _magenta()
    win.view.make_patch(ox - 1400, oy - 1400, ox + 1400, oy + 1400)
    pump(400)
    _look()
    n_after = _magenta()
    print(f"  overlay pixels {n_before} without the box, {n_after} with it")
    check("the overlay is visible at all", n_before > 0, str(n_before))
    check("the box does not cover the overlay", n_after >= n_before * 0.6,
          f"{100 * n_after / max(n_before, 1):.0f}% kept")
    win.ov_list.setCurrentRow(0)
    win.remove_overlay()
    pump(200)

print("\nlimits and clean-up:")
try:
    s.slope_patch(s.cx - 60000, s.cy - 60000, s.cx + 60000, s.cy + 60000)
    check("a huge box is refused", False, "it was allowed")
except raster.RasterError as exc:
    check("a huge box is refused", True, str(exc)[:60])
try:
    s.slope_patch(x, y, x + 1, y + 1)
    check("a sub-cell box is refused", False, "it was allowed")
except raster.RasterError:
    check("a sub-cell box is refused", True)

win.view.clear_patch()
pump(200)
check("clearing removes the box",
      win.view.patch is None
      and "patch" not in win.view.plotter.renderer.actors
      and "patchedge" not in win.view.plotter.renderer.actors)
check("the readout empties too",
      win.patch_cells["Max slope"].text() == "--",
      win.patch_cells["Max slope"].text())

win.close()
pump(200)
_prefs.settings().clear()
print()
print("FAILED: " + ", ".join(FAILED) if FAILED else "all slope box checks passed")
sys.exit(1 if FAILED else 0)
