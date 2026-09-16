#!/usr/bin/env python
"""Checks for trail retention.

Trims by age, holds a full 24 hours of 1 Hz track, caps the drawn line so the
redraw stays affordable, and keeps the newest point so the line still reaches
the marker.

    python trail_test.py [grid.tif]
"""
import math, os, sys, time
sys.path.insert(0, r"C:\Users\mkozh\OneDrive\Desktop\GoWork\bathy3d")
GRID = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:\Users\mkozh\OneDrive\Desktop\bathy\BOEM_bathy_WGS84_UTM15N.tif"
from PySide6 import QtCore, QtWidgets
from bathy3d.mainwindow import MainWindow, TRAILS
from bathy3d.targets import TRAIL_DRAW_MAX, MAX_TRAIL_POINTS
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

def go():
    st["n"] += 1
    if win.view.surface is None:
        if st["n"] > 300: app.quit()
        return
    t.stop()
    lay = win.view.targets
    s = win.view.surface
    E, N = 706148.701, 3006428.410

    print(f"  options: {', '.join(TRAILS)}")
    check("24 hours offered", TRAILS.get("24 hours") == 86400)

    # --- age trimming: short window must drop old points
    lay.set_trail_seconds(2.0)
    for i in range(12):
        lay.update("UHD333", E + i*0.6, N - i*1.0, None)
        pump(200)
    n_short = len(lay.targets["UHD333"].trail)
    check("short window trims by age", n_short < 12, f"{n_short} points kept over ~2.4 s")

    # --- widening keeps what arrives after
    lay.set_trail_seconds(86400)
    lay.clear_trail()
    for i in range(20):
        lay.update("UHD333", E + i*0.6, N - i*1.0, None)
    check("24h window keeps everything", len(lay.targets["UHD333"].trail) == 20,
          f"{len(lay.targets['UHD333'].trail)}")

    # --- a full 24 h of 1 Hz track, injected with synthetic timestamps
    lay.clear_trail()
    tgt = lay.ensure("UHD334")
    now = time.monotonic()
    span, rate = 86400, 1.0
    n = int(span / rate)
    lx, ly = s.local_from_crs(E, N)
    for i in range(n):
        ang = i / 900.0
        tgt.trail.append((lx + math.cos(ang) * i * 0.05,
                          ly + math.sin(ang) * i * 0.05,
                          -1500.0, now - span + i * rate))
    tgt.x, tgt.y, tgt.z = E, N, -1500.0
    print(f"  injected {len(tgt.trail):,} points (24 h at {1/rate:.0f} Hz)")
    check("24h of track retained", len(tgt.trail) == n, f"{len(tgt.trail):,}")
    check("under the hard ceiling", len(tgt.trail) <= MAX_TRAIL_POINTS)

    t0 = time.perf_counter(); lay._place(tgt); dt = (time.perf_counter()-t0)*1000
    print(f"  drawing a 24 h trail took {dt:.1f} ms")
    check("24h trail draws fast enough for 1 Hz", dt < 250, f"{dt:.1f} ms")
    drawn = lay._actors["UHD334"]["trail"].GetMapper().GetInput().GetNumberOfPoints()
    print(f"  drawn vertices {drawn:,} (cap {TRAIL_DRAW_MAX:,})")
    check("drawn line is capped", drawn <= TRAIL_DRAW_MAX + 1, str(drawn))
    check("history is not destroyed by drawing", len(tgt.trail) == n)

    # newest point must survive subsampling or the line misses the marker
    import numpy as np
    pts = lay._actors["UHD334"]["trail"].GetMapper().GetInput().points
    last_hist = np.array(tgt.trail[-1][:3]); last_hist[2] *= lay._ve
    check("trail still reaches the marker",
          float(np.linalg.norm(np.array(pts[-1]) - last_hist)) < 1e-6)

    # --- trimming down from 24 h
    lay.set_trail_seconds(3600)
    check("dropping to 1 h trims to ~3600", abs(len(tgt.trail) - 3600) <= 2,
          f"{len(tgt.trail)}")
    lay.set_trail_seconds(0)
    check("Off clears trails", len(tgt.trail) == 0)
    check("Off removes the line actor", "trail" not in lay._actors.get("UHD334", {}))

    # --- via the UI combo
    win.trail_c.setCurrentText("24 hours"); pump(150)
    check("combo sets retention", lay.trail_seconds == 86400, str(lay.trail_seconds))
    win.trail_c.setCurrentText("Off"); pump(150)
    check("combo can switch off", lay.trail_seconds == 0)
    QtCore.QTimer.singleShot(200, app.quit)

t = QtCore.QTimer(); t.timeout.connect(go); t.start(100)
app.exec()
print("FAILED: " + ", ".join(FAILED) if FAILED else "all trail checks passed")
sys.exit(1 if FAILED else 0)
