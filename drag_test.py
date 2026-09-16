#!/usr/bin/env python
"""Checks for camera interaction.

Left-drag must slide the map in X and Y without changing camera height or
view direction, shift-left must still orbit, and a click with no drag must
still drop a measuring station.

    python drag_test.py [grid.tif]
"""
import math, sys, time
sys.path.insert(0, r"C:\Users\mkozh\OneDrive\Desktop\GoWork\bathy3d")
GRID = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:\Users\mkozh\OneDrive\Desktop\bathy\BOEM_bathy_WGS84_UTM15N.tif"
from PySide6 import QtCore, QtWidgets
from bathy3d.mainwindow import MainWindow
FAILED = []
def check(n, c, d=""):
    print(f"  [{'ok  ' if c else 'FAIL'}] {n}{' - ' + d if d else ''}")
    if not c: FAILED.append(n)

app = QtWidgets.QApplication(sys.argv[:1])
win = MainWindow(GRID); win.resize(1400, 860); win.show()
st = {"n": 0}
def pump(ms):
    e = time.monotonic() + ms/1000.0
    while time.monotonic() < e:
        app.processEvents(); time.sleep(0.003)

def drag(iren, x0, y0, x1, y1, steps=8, shift=0):
    iren.SetEventInformation(x0, y0, 0, shift)
    iren.InvokeEvent("LeftButtonPressEvent")
    for i in range(1, steps+1):
        iren.SetEventInformation(int(x0+(x1-x0)*i/steps), int(y0+(y1-y0)*i/steps), 0, shift)
        iren.InvokeEvent("MouseMoveEvent")
    iren.SetEventInformation(x1, y1, 0, shift)
    iren.InvokeEvent("LeftButtonReleaseEvent")

def go():
    st["n"] += 1
    if win.view.surface is None:
        if st["n"] > 300: app.quit()
        return
    t.stop()
    pl = win.view.plotter
    iren = pl.iren.interactor
    w, h = pl.window_size
    cam = pl.camera

    def state():
        return (tuple(cam.position), tuple(cam.focal_point))

    print(f"  left_action={win.view.left_action}  lock_z={win.view.lock_z}")
    check("defaults to moving the map", win.view.left_action == "pan")

    # ---- left drag with height lock
    win.meas_b.setChecked(False)          # keep clicks from adding stations
    p0, f0 = state()
    drag(iren, int(w*0.5), int(h*0.5), int(w*0.5)+220, int(h*0.5)+130)
    pump(120)
    p1, f1 = state()
    dz_cam = abs(p1[2]-p0[2]); dz_foc = abs(f1[2]-f0[2])
    dxy = math.hypot(f1[0]-f0[0], f1[1]-f0[1])
    print(f"  focal moved {dxy:,.0f} m in XY; camera Z {p0[2]:,.1f} -> {p1[2]:,.1f}")
    check("camera height unchanged", dz_cam < 1e-6, f"delta {dz_cam:.6f}")
    check("focal height unchanged", dz_foc < 1e-6, f"delta {dz_foc:.6f}")
    check("map actually moved in X/Y", dxy > 100, f"{dxy:,.0f} m")

    # view direction must survive the pan
    d0 = tuple(f0[i]-p0[i] for i in range(3))
    d1 = tuple(f1[i]-p1[i] for i in range(3))
    diff = max(abs(d1[i]-d0[i]) for i in range(3))
    check("view direction unchanged (no rotation)", diff < 1e-6, f"max delta {diff:.6f}")

    # ---- shift-left must still orbit
    p0, f0 = state()
    drag(iren, int(w*0.5), int(h*0.5), int(w*0.5)+200, int(h*0.5), shift=1)
    pump(120)
    p1, f1 = state()
    moved = math.dist(p0, p1)
    check("shift-left still orbits", moved > 100, f"camera moved {moved:,.0f} m")

    # ---- switching to Orbit restores the old behaviour
    win.drag_c.setCurrentText("Orbit"); pump(100)
    p0, _ = state()
    drag(iren, int(w*0.5), int(h*0.5), int(w*0.5)+200, int(h*0.5))
    pump(120)
    p1, _ = state()
    check("Orbit mode rotates on left drag", math.dist(p0, p1) > 100,
          f"camera moved {math.dist(p0,p1):,.0f} m")
    win.drag_c.setCurrentText("Move map"); pump(100)

    # ---- unlocking height lets it drift again (proves the lock is doing it)
    win.lockz_b.setChecked(False); pump(100)
    p0, _ = state()
    drag(iren, int(w*0.5), int(h*0.5), int(w*0.5), int(h*0.5)+180)
    pump(120)
    p1, _ = state()
    check("unlocking height lets Z move again", abs(p1[2]-p0[2]) > 1.0,
          f"delta {abs(p1[2]-p0[2]):,.1f} m")
    win.lockz_b.setChecked(True); pump(100)

    # ---- a click (no drag) still drops a station
    win.view.reset_view(); pump(200)      # back over the terrain first
    win.meas_b.setChecked(True)
    n0 = len(win.view.line)
    iren.SetEventInformation(int(w*0.5), int(h*0.5), 0, 0)
    iren.InvokeEvent("LeftButtonPressEvent")
    iren.SetEventInformation(int(w*0.5), int(h*0.5), 0, 0)
    iren.InvokeEvent("LeftButtonReleaseEvent")
    pump(150)
    check("click still measures", len(win.view.line) == n0+1,
          f"{n0} -> {len(win.view.line)}")
    QtCore.QTimer.singleShot(200, app.quit)

t = QtCore.QTimer(); t.timeout.connect(go); t.start(100)
app.exec()
print("FAILED: " + ", ".join(FAILED) if FAILED else "all drag checks passed")
sys.exit(1 if FAILED else 0)
