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
