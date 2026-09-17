#!/usr/bin/env python
"""Checks for colour choices.

Depth and slope each offer their own ramp menu and remember their own choice.
Every overlay can be recoloured independently, and both survive a restart.

    python colour_test.py [grid.tif] [shapefile.shp]
"""

import os
import sys
import time

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)

GRID = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:\Users\mkozh\OneDrive\Desktop\bathy\BOEM_bathy_WGS84_UTM15N.tif"
SHP = sys.argv[2] if len(sys.argv) > 2 else os.path.join(APP, "EGN10_Preplot_SHP.shp")

from PySide6 import QtCore, QtGui, QtWidgets   # noqa: E402
from bathy3d import prefs, ramps               # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
    if not cond:
        FAILED.append(name)


os.environ["BATHY3D_PROFILE"] = "test-colour"
prefs.settings().clear()

from bathy3d.mainwindow import MainWindow      # noqa: E402

app = QtWidgets.QApplication(sys.argv[:1])


def pump(ms):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.004)


def wait_grid(win, limit=60):
    for _ in range(int(limit / 0.1)):
        pump(100)
        if win.view.surface is not None:
            return True
    return False


print("ramps:")
check("several depth ramps", len(ramps.DEPTH_RAMPS) >= 6,
      f"{len(ramps.DEPTH_RAMPS)}: {', '.join(ramps.DEPTH_RAMPS)}")
check("several slope ramps", len(ramps.SLOPE_RAMPS) >= 6,
      f"{len(ramps.SLOPE_RAMPS)}: {', '.join(ramps.SLOPE_RAMPS)}")
check("a rainbow for depth", "Rainbow" in ramps.DEPTH_RAMPS)
check("the two sets differ", set(ramps.DEPTH_RAMPS) != set(ramps.SLOPE_RAMPS))

# The wide rainbow exists to give a depth range more colours to land in, so
# the things worth asserting are that it really has more of them and that no
# two neighbours are so close they read as one band on screen.
check("a wider rainbow as well", "Rainbow wide" in ramps.DEPTH_RAMPS,
      ", ".join(ramps.DEPTH_RAMPS))
check("the two rainbows are not the same ramp",
      ramps.DEPTH_RAMPS["Rainbow"] is not ramps.DEPTH_RAMPS["Rainbow wide"])
check("the wide one has more than twice the stops",
      len(ramps._RAINBOW_WIDE_STOPS) >= 2 * len(ramps._RAINBOW_STOPS),
      f"{len(ramps._RAINBOW_WIDE_STOPS)} vs {len(ramps._RAINBOW_STOPS)}")
check("its stops run 0 to 1 in order",
      [p for p, _ in ramps._RAINBOW_WIDE_STOPS]
      == sorted(p for p, _ in ramps._RAINBOW_WIDE_STOPS)
      and ramps._RAINBOW_WIDE_STOPS[0][0] == 0.0
      and ramps._RAINBOW_WIDE_STOPS[-1][0] == 1.0)


def _lab(hexes):
    """CIE L*a*b*, so "different colour" means different to an eye."""
    import numpy as np
    c = np.array([[int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
                  for h in hexes])
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    M = np.array([[.4124, .3576, .1805], [.2126, .7152, .0722],
                  [.0193, .1192, .9505]])
    xyz = c @ M.T / np.array([.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.column_stack([116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]),
                            200 * (f[:, 1] - f[:, 2])])


import numpy as _np                            # noqa: E402
_gaps = _np.linalg.norm(
    _np.diff(_lab([h for _, h in ramps._RAINBOW_WIDE_STOPS]), axis=0), axis=1)
check("no two neighbouring colours read as one", _gaps.min() >= 12.0,
      f"tightest pair {_gaps.min():.1f} CIE76, mean {_gaps.mean():.1f}")

win = MainWindow(GRID)
win.resize(1200, 780)
win.show()
assert wait_grid(win), "grid did not load"

print("\nramp menu follows colour-by:")
check("depth ramps listed first",
      [win.ramp_c.itemText(i) for i in range(win.ramp_c.count())]
      == list(ramps.DEPTH_RAMPS))
win.ramp_c.setCurrentText("Rainbow")
pump(250)
check("depth ramp applied", win.view.ramp_name == "Rainbow", win.view.ramp_name)

win.by_c.setCurrentText("Slope")
pump(300)
check("menu swaps to slope ramps",
      [win.ramp_c.itemText(i) for i in range(win.ramp_c.count())]
      == list(ramps.SLOPE_RAMPS))
check("slope gets a slope ramp, not the depth one",
      win.view.ramp_name in ramps.SLOPE_RAMPS, win.view.ramp_name)
win.ramp_c.setCurrentText("Heat")
pump(250)
check("slope ramp applied", win.view.ramp_name == "Heat", win.view.ramp_name)

win.by_c.setCurrentText("Depth")
pump(300)
check("depth remembers its own ramp", win.view.ramp_name == "Rainbow",
      win.view.ramp_name)
win.by_c.setCurrentText("Slope")
pump(300)
check("slope remembers its own ramp", win.view.ramp_name == "Heat",
      win.view.ramp_name)
win.by_c.setCurrentText("Depth")
pump(300)

print("\noverlay colours:")
win.add_overlay_path(SHP)
pump(400)
layer = win.view.overlays.get("EGN10_Preplot_SHP")
check("overlay loaded", layer is not None)
first = layer.color

# Press the real button, the way a user does. Driving this through the view API
# is what let it ship doing nothing: a QListWidget selects nothing when an item
# is added, so currentItem() was None and the handler bailed out silently.
check("a new layer is selected, so the button has a target",
      win.ov_list.currentItem() is not None,
      f"currentRow {win.ov_list.currentRow()}")

opened = {"n": 0}
_real_colour = QtWidgets.QColorDialog.getColor


def _fake_colour(initial, parent=None, title=""):
    opened["n"] += 1
    return QtGui.QColor("#ff00aa")


QtWidgets.QColorDialog.getColor = staticmethod(_fake_colour)
try:
    win.ov_colour_b.click()
    pump(300)
finally:
    QtWidgets.QColorDialog.getColor = _real_colour
check("the Colour button opens the picker", opened["n"] == 1, f"{opened['n']} times")
check("colour changed", win.view.overlays["EGN10_Preplot_SHP"].color == "#ff00aa",
      f"{first} -> {win.view.overlays['EGN10_Preplot_SHP'].color}")
actor = win.view.plotter.renderer.actors.get("ov:EGN10_Preplot_SHP")
rgb = actor.GetProperty().GetColor() if actor else (0, 0, 0)
check("the drawn layer really is that colour",
      abs(rgb[0] - 1.0) < 0.02 and rgb[1] < 0.02 and abs(rgb[2] - 2 / 3) < 0.05,
      f"rgb {tuple(round(c, 3) for c in rgb)}")

# double-click must reach the same place even with nothing selected
win.ov_list.setCurrentItem(None)
opened["n"] = 0
QtWidgets.QColorDialog.getColor = staticmethod(_fake_colour)
try:
    win.ov_list.itemDoubleClicked.emit(win.ov_list.item(0))
    pump(200)
finally:
    QtWidgets.QColorDialog.getColor = _real_colour
check("double-click opens the picker too", opened["n"] == 1, f"{opened['n']} times")

win.close()
pump(300)

print("\nafter a restart:")
win2 = MainWindow(None)
win2.resize(1200, 780)
win2.show()
check("depth ramp restored", win2.view.ramp_name == "Rainbow", win2.view.ramp_name)
check("slope ramp remembered too",
      win2._ramp_choice.get("Slope") == "Heat", str(win2._ramp_choice))
assert wait_grid(win2), "grid did not reopen"
for _ in range(60):
    pump(100)
    if win2.ov_list.count():
        break
lay2 = win2.view.overlays.get("EGN10_Preplot_SHP")
check("overlay colour restored", lay2 is not None and lay2.color == "#ff00aa",
      lay2.color if lay2 else "missing")
win2.close()
pump(200)
prefs.settings().clear()

print()
print("FAILED: " + ", ".join(FAILED) if FAILED else "all colour checks passed")
sys.exit(1 if FAILED else 0)
