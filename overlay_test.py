#!/usr/bin/env python
"""Checks for shapefile overlays.

Loads a shapefile through the real window and confirms it is draped on the
terrain, toggles, follows vertical exaggeration, and removes cleanly.

    python overlay_test.py [shapefile.shp] [grid.tif]
"""
import os, sys, time
APP = r"C:\Users\mkozh\OneDrive\Desktop\GoWork\bathy3d"
sys.path.insert(0, APP)
GRID = r"C:\Users\mkozh\OneDrive\Desktop\bathy\BOEM_bathy_WGS84_UTM15N.tif"
SHP = sys.argv[1] if len(sys.argv) > 1 else os.path.join(APP, "EGN10_Preplot_SHP.shp")
if len(sys.argv) > 2:
    GRID = sys.argv[2]
import tempfile
OUT = os.path.join(tempfile.gettempdir(), "bathy3d_smoke")
os.makedirs(OUT, exist_ok=True)
from PySide6 import QtCore, QtWidgets
from bathy3d.mainwindow import MainWindow
FAILED = []
def check(n, c, d=""):
    print(f"  [{'ok  ' if c else 'FAIL'}] {n}{' - ' + d if d else ''}")
    if not c: FAILED.append(n)

app = QtWidgets.QApplication(sys.argv[:1])
win = MainWindow(GRID); win.resize(1500, 900); win.show()
st = {"n": 0}
def pump(ms):
    e = time.monotonic() + ms/1000.0
    while time.monotonic() < e:
        app.processEvents(); time.sleep(0.004)

def go():
    st["n"] += 1
    if win.view.surface is None:
        if st["n"] > 300: app.quit()
        return
    t.stop()
    t0 = time.time()
    win.add_overlay_path(SHP)
    pump(400)
    print(f"  add took {time.time()-t0:.2f}s")
    check("overlay registered", len(win.view.overlays) == 1, str(list(win.view.overlays)))
    lay = win.view.overlays.get("EGN10_Preplot_SHP")
    check("layer present", lay is not None)
    if lay:
        check("vertices draped", lay.n_vertices > 0, f"{lay.n_vertices}")
        check("nothing dropped off grid", lay.dropped == 0, f"{lay.dropped}")
        check("kind recognised", lay.kind in ("point", "line", "polygon"), lay.kind)
    check("list item added", win.ov_list.count() == 1)
    check("actor in scene", "ov:EGN10_Preplot_SHP" in win.view.plotter.renderer.actors,
          str([a for a in win.view.plotter.renderer.actors if a.startswith("ov")]))

    # visibility toggle
    it = win.ov_list.item(0)
    it.setCheckState(QtCore.Qt.Unchecked); pump(200)
    check("unchecking hides it",
          "ov:EGN10_Preplot_SHP" not in win.view.plotter.renderer.actors)
    it.setCheckState(QtCore.Qt.Checked); pump(200)
    check("rechecking shows it",
          "ov:EGN10_Preplot_SHP" in win.view.plotter.renderer.actors)

    # exaggeration must carry the overlay with the seabed
    z0 = win.view.plotter.renderer.actors["ov:EGN10_Preplot_SHP"].GetBounds()[4]
    win.ve_s.setValue(140); pump(300)
    z1 = win.view.plotter.renderer.actors["ov:EGN10_Preplot_SHP"].GetBounds()[4]
    check("overlay follows vertical exaggeration", abs(z1) > abs(z0) * 1.8,
          f"zmin {z0:,.0f} -> {z1:,.0f}")
    win.ve_s.setValue(60); pump(300)

    win.view.plan_view(); pump(300)
    win.view.plotter.screenshot(os.path.join(OUT, "shp_plan.png"))
    win.view.reset_view(); pump(300)
    win.view.plotter.screenshot(os.path.join(OUT, "shp_3d.png"))

    win.ov_list.setCurrentRow(0)
    win.remove_overlay(); pump(200)
    check("remove clears scene and list",
          not win.view.overlays and win.ov_list.count() == 0
          and "ov:EGN10_Preplot_SHP" not in win.view.plotter.renderer.actors)
    QtCore.QTimer.singleShot(300, app.quit)

t = QtCore.QTimer(); t.timeout.connect(go); t.start(100)
app.exec()
print("FAILED: " + ", ".join(FAILED) if FAILED else "all overlay checks passed")
sys.exit(1 if FAILED else 0)
