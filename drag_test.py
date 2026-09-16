#!/usr/bin/env python
"""Checks for camera interaction.

With Lock Z on, a left-drag swings the map round at a fixed viewing angle:
the heading turns while the tilt and the distance to the target stay put.
Shift-left slides the map without changing altitude, the wheel still zooms,
and a click with no drag still drops a measuring station.

    python drag_test.py [grid.tif]
"""
import math, sys, time
sys.path.insert(0, r"C:\Users\mkozh\OneDrive\Desktop\GoWork\bathy3d")

# Each suite gets its own settings profile, wiped on entry: the app now
# remembers exaggeration, ramps and the rest, so without this one test
# leaves state behind that the next one fails on - and a test run would
# quietly overwrite real preferences.
import os as _os
_os.environ["BATHY3D_PROFILE"] = "test-drag"
from bathy3d import prefs as _prefs      # noqa: E402
_prefs.settings().clear()
GRID = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:\Users\mkozh\OneDrive\Desktop\bathy\BOEM_bathy_WGS84_UTM15N.tif"
from PySide6 import QtCore, QtWidgets
from bathy3d.mainwindow import MainWindow
FAILED=[]
def check(n,c,d=""):
    print(f"  [{'ok  ' if c else 'FAIL'}] {n}{' - '+d if d else ''}")
    if not c: FAILED.append(n)
app = QtWidgets.QApplication(sys.argv[:1])
win = MainWindow(GRID); win.resize(1400,860); win.show()
st={'n':0}
def pump(ms):
    e=time.monotonic()+ms/1000
    while time.monotonic()<e: app.processEvents(); time.sleep(0.003)

def geom(cam):
    p,f = cam.position, cam.focal_point
    v = [p[i]-f[i] for i in range(3)]
    horiz = math.hypot(v[0],v[1])
    return (math.degrees(math.atan2(v[0],v[1]))%360,          # compass heading
            math.degrees(math.atan2(v[2],horiz)),             # tilt above horizon
            math.hypot(horiz,v[2]))                           # distance

def drag(iren,x0,y0,x1,y1,steps=8,shift=0):
    iren.SetEventInformation(x0,y0,0,shift); iren.InvokeEvent("LeftButtonPressEvent")
    for i in range(1,steps+1):
        iren.SetEventInformation(int(x0+(x1-x0)*i/steps),int(y0+(y1-y0)*i/steps),0,shift)
        iren.InvokeEvent("MouseMoveEvent")
    iren.SetEventInformation(x1,y1,0,shift); iren.InvokeEvent("LeftButtonReleaseEvent")

def go():
    st['n']+=1
    if win.view.surface is None:
        if st['n']>300: app.quit()
        return
    t.stop()
    pl=win.view.plotter; iren=pl.iren.interactor; cam=pl.camera
    w,h=pl.window_size
    win.meas_b.setChecked(False)
    print(f"  left_action={win.view.left_action} lock_z={win.view.lock_z}")
    check("left drag rotates by default", win.view.left_action=="rotate")
    check("Z locked by default", win.view.lock_z is True)

    # horizontal drag -> map swings round, viewing angle kept
    b0,t0,d0 = geom(cam)
    drag(iren,int(w*.5),int(h*.5),int(w*.5)+240,int(h*.5)); pump(120)
    b1,t1,d1 = geom(cam)
    print(f"  horizontal drag: heading {b0:.2f}->{b1:.2f}  tilt {t0:.2f}->{t1:.2f}")
    check("horizontal drag swings the map round", abs(b1-b0) > 5,
          f"{abs(b1-b0):.2f} deg")
    check("horizontal drag keeps the viewing angle", abs(t1-t0) < 1e-6,
          f"{abs(t1-t0):.6f} deg")
    check("distance to target unchanged", abs(d1-d0) < 1e-3, f"{abs(d1-d0):.4f} m")

    # vertical drag -> must NOT tilt with the lock on
    b0,t0,d0 = geom(cam)
    drag(iren,int(w*.5),int(h*.5),int(w*.5),int(h*.5)+160); pump(120)
    b1,t1,d1 = geom(cam)
    print(f"  vertical drag: heading {b0:.2f}->{b1:.2f}  tilt {t0:.2f}->{t1:.2f}")
    check("vertical drag does not tilt", abs(t1-t0) < 1e-6, f"{abs(t1-t0):.6f} deg")
    check("vertical drag keeps the distance", abs(d1-d0) < 1e-3, f"{abs(d1-d0):.4f} m")

    # unlock -> tilting comes back
    win.lockz_b.setChecked(False); pump(80)
    _,t0,_ = geom(cam)
    drag(iren,int(w*.5),int(h*.5),int(w*.5),int(h*.5)+160); pump(120)
    _,t1,_ = geom(cam)
    check("unlocking lets it tilt again", abs(t1-t0) > 3, f"{abs(t1-t0):.2f} deg")
    win.lockz_b.setChecked(True); pump(80)
    win.view.reset_view(); pump(150)

    # shift-left moves the map, height held
    p0 = tuple(cam.position); f0 = tuple(cam.focal_point)
    drag(iren,int(w*.5),int(h*.5),int(w*.5)+200,int(h*.5)+120,shift=1); pump(120)
    p1 = tuple(cam.position); f1 = tuple(cam.focal_point)
    dxy = math.hypot(f1[0]-f0[0], f1[1]-f0[1])
    check("shift-left moves the map", dxy > 100, f"{dxy:,.0f} m")
    check("moving keeps camera height", abs(p1[2]-p0[2]) < 1e-6,
          f"delta {abs(p1[2]-p0[2]):.6f}")

    # wheel still zooms, unchanged
    _,_,d0 = geom(cam)
    for _ in range(3):
        iren.SetEventInformation(int(w*.5),int(h*.5),0,0)
        iren.InvokeEvent("MouseWheelForwardEvent")
    pump(120)
    _,_,d1 = geom(cam)
    check("wheel still zooms in", d1 < d0*0.99, f"{d0:,.0f} -> {d1:,.0f} m")
    for _ in range(3):
        iren.SetEventInformation(int(w*.5),int(h*.5),0,0)
        iren.InvokeEvent("MouseWheelBackwardEvent")
    pump(120)
    _,_,d2 = geom(cam)
    check("wheel still zooms out", d2 > d1*1.01, f"{d1:,.0f} -> {d2:,.0f} m")

    # zoom must reach close in without the near plane clipping the seabed
    win.view.reset_view(); pump(150)
    _,_,far = geom(cam)
    for _ in range(70):
        iren.SetEventInformation(int(w*.5), int(h*.55), 0, 0)
        iren.InvokeEvent("MouseWheelForwardEvent")
    pump(150)
    _,_,near_d = geom(cam)
    near_plane = cam.clipping_range[0]
    print(f"  zoom: {far:,.0f} m -> {near_d:,.2f} m, near plane {near_plane:.3f} m")
    check("zooms right in", near_d < 5, f"{near_d:,.2f} m")
    check("near plane follows the camera", near_plane < near_d,
          f"near {near_plane:.3f} vs distance {near_d:.2f}")
    import numpy as _np
    img = _np.asarray(pl.screenshot(return_img=True))
    bg = _np.array([13,20,24])
    covered = (_np.abs(img.astype(int)-bg).sum(axis=2) > 40).mean()
    check("seabed still fills the frame when close", covered > 0.55,
          f"{covered*100:.1f}% covered")
    for _ in range(200):
        iren.SetEventInformation(int(w*.5), int(h*.5), 0, 0)
        iren.InvokeEvent("MouseWheelBackwardEvent")
    pump(150)
    _,_,out = geom(cam)
    check("zoom out is capped", out < 2e6, f"{out:,.0f} m")

    # click still measures
    win.view.reset_view(); pump(200); win.meas_b.setChecked(True)
    n0=len(win.view.line)
    iren.SetEventInformation(int(w*.5),int(h*.5),0,0); iren.InvokeEvent("LeftButtonPressEvent")
    iren.SetEventInformation(int(w*.5),int(h*.5),0,0); iren.InvokeEvent("LeftButtonReleaseEvent")
    pump(150)
    check("click still measures", len(win.view.line)==n0+1, f"{n0} -> {len(win.view.line)}")
    QtCore.QTimer.singleShot(200, app.quit)

t=QtCore.QTimer(); t.timeout.connect(go); t.start(100); app.exec()
print("FAILED: " + ", ".join(FAILED) if FAILED else "all camera checks passed")
sys.exit(1 if FAILED else 0)
