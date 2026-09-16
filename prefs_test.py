#!/usr/bin/env python
"""Checks that the app remembers its files and settings.

Runs three sessions against an isolated settings scope: one that opens a grid
and a shapefile and changes some settings, one that starts with no arguments
and must pick all of it back up, and one after Forget. Missing files must be
ignored rather than break the start.

    python prefs_test.py
"""
import os, sys, time
APP = r"C:\Users\mkozh\OneDrive\Desktop\GoWork\bathy3d"
sys.path.insert(0, APP)
GRID = r"C:\Users\mkozh\OneDrive\Desktop\bathy\BOEM_bathy_WGS84_UTM15N.tif"
SHP = os.path.join(APP, "EGN10_Preplot_SHP.shp")
from PySide6 import QtCore, QtWidgets
from bathy3d import prefs
from bathy3d.mainwindow import MainWindow
FAILED=[]
def check(n,c,d=""):
    print(f"  [{'ok  ' if c else 'FAIL'}] {n}{' - '+d if d else ''}")
    if not c: FAILED.append(n)

# isolate from any real saved settings
QtCore.QCoreApplication.setOrganizationName("Bathy3D-Test")
QtCore.QCoreApplication.setApplicationName("Bathy3D-Test")
prefs.ORG = prefs.APP = "Bathy3D-Test"
prefs.settings().clear()

app = QtWidgets.QApplication(sys.argv[:1])
def pump(ms):
    e=time.monotonic()+ms/1000
    while time.monotonic()<e: app.processEvents(); time.sleep(0.004)

def wait_grid(win, limit=60):
    for _ in range(int(limit/0.1)):
        pump(100)
        if win.view.surface is not None:
            return True
    return False

print("session 1: open grid + shapefile, change settings, close")
w1 = MainWindow(GRID); w1.resize(1300,820); w1.show()
check("first run starts empty of memory", prefs.last_grid() is None)
assert wait_grid(w1), "grid did not load"
w1.add_overlay_path(SHP); pump(300)
w1.ve_s.setValue(115); w1.az_s.setValue(200); w1.al_s.setValue(55)
w1.ramp_c.setCurrentText("Viridis")
w1.trail_c.setCurrentText("6 hours")
w1.port_s.setValue(6462)
w1.lockz_b.setChecked(False)
pump(300)
w1.close(); pump(300)

check("grid remembered", prefs.last_grid() == GRID, str(prefs.last_grid()))
check("overlay remembered", prefs.overlays() == [SHP], str(prefs.overlays()))
check("grid folder remembered", prefs.last_dir("grid") == os.path.dirname(GRID),
      prefs.last_dir("grid"))
check("shapefile folder remembered", prefs.last_dir("shp") == APP, prefs.last_dir("shp"))

print("\nsession 2: open with no arguments at all")
w2 = MainWindow(None); w2.resize(1300,820); w2.show()
check("settings restored before any file loads",
      abs(w2.ve_s.value()/10.0 - 11.5) < 1e-9 and w2.az_s.value() == 200
      and w2.al_s.value() == 55 and w2.ramp_c.currentText() == "Viridis"
      and w2.trail_c.currentText() == "6 hours" and w2.port_s.value() == 6462
      and w2.lockz_b.isChecked() is False,
      f"ve={w2.ve_s.value()/10} az={w2.az_s.value()} ramp={w2.ramp_c.currentText()} "
      f"trail={w2.trail_c.currentText()} port={w2.port_s.value()} lock={w2.lockz_b.isChecked()}")
check("trail retention applied to the layer",
      w2.view.targets.trail_seconds == 21600, str(w2.view.targets.trail_seconds))
check("Z lock applied to the view", w2.view.lock_z is False)

assert wait_grid(w2), "grid did not reopen"
check("grid reopened by itself", w2.view.surface is not None
      and os.path.basename(w2._path) == os.path.basename(GRID), str(w2._path))
for _ in range(60):
    pump(100)
    if w2.ov_list.count():
        break
check("overlay reloaded by itself", w2.ov_list.count() == 1
      and "EGN10_Preplot_SHP" in w2.view.overlays, str(list(w2.view.overlays)))
check("exaggeration actually applied", abs(w2.view.ve - 11.5) < 1e-9, str(w2.view.ve))
w2.close(); pump(200)

print("\nsession 3: after Forget")
prefs.forget_session()
w3 = MainWindow(None); w3.resize(900,600); w3.show(); pump(800)
check("forgetting stops the reopen", w3.view.surface is None)
check("but folders are still remembered", prefs.last_dir("shp") == APP)
w3.close(); pump(200)

# a vanished file must not break startup
prefs.set_last_grid(r"C:\nope\missing.tif")
prefs.set_overlays([r"C:\nope\gone.shp"])
check("missing grid is ignored", prefs.last_grid() is None)
check("missing overlay is ignored", prefs.overlays() == [])
prefs.settings().clear()
print("FAILED: " + ", ".join(FAILED) if FAILED else "all preference checks passed")
sys.exit(1 if FAILED else 0)
