#!/usr/bin/env python
"""Checks the viewer on grids that are not UTM 15N.

Builds a small synthetic seabed in several projections and loads each one,
checking cell size, depth, slope, latitude and longitude, distance, bearing,
the slope box and nodata. Nothing in the app is tied to one zone - the scene
is built in local metres - but that is worth proving rather than assuming.

    python crs_test.py
"""
import os
import sys
import tempfile

sys.path.insert(0, r"C:\Users\mkozh\OneDrive\Desktop\GoWork\bathy3d")
os.environ["BATHY3D_PROFILE"] = "test-crs"

import numpy as np
import rasterio
from rasterio.transform import from_origin
from bathy3d import raster

OUT = tempfile.mkdtemp(prefix="bathy3d_crs_")


def make(path, epsg, x0, y0, cell, w=400, h=320):
    """A small synthetic seabed with real relief, in the given CRS."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    z = (-1500.0
         + 90.0 * np.sin(xx / 28.0) * np.cos(yy / 33.0)
         + 40.0 * np.sin(xx / 7.0)
         - 0.05 * yy)
    z[10:20, 10:20] = -32767.0                      # a nodata hole
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=1,
                       dtype="float32", crs=f"EPSG:{epsg}", nodata=-32767.0,
                       transform=from_origin(x0, y0, cell, cell)) as ds:
        ds.write(z, 1)
    return path


CASES = [
    ("UTM 15N (the usual one)", 32615, 615000.0, 3046500.0, 10.0),
    ("UTM 16N",                 32616, 480000.0, 3210000.0, 10.0),
    ("UTM 31N (North Sea)",     32631, 460000.0, 5900000.0, 10.0),
    ("UTM 15S (southern)",      32715, 500000.0, 8600000.0, 10.0),
    ("UTM 56S (Australia)",     32756, 330000.0, 6250000.0, 10.0),
    ("Geographic WGS 84",        4326, -91.2,      27.5,   0.0001),
]

FAILED = []


def check(name, cond, detail=""):
    print(f"    [{'ok  ' if cond else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
    if not cond:
        FAILED.append(name)


for label, epsg, x0, y0, cell in CASES:
    print(f"\n{label}  (EPSG:{epsg})")
    p = make(os.path.join(OUT, f"g{epsg}.tif"), epsg, x0, y0, cell)
    try:
        s = raster.load(p)
    except Exception as exc:
        check("loads", False, f"{type(exc).__name__}: {exc}")
        continue
    ew, nh = s.extent_m
    print(f"    {s.width}x{s.height}, native cell {s.native_cell_m:.2f} m, "
          f"extent {ew:,.0f} x {nh:,.0f} m, geographic={s.geographic}")

    check("loads", True)
    check("cell size in metres is sane", 5 < s.native_cell_m < 30,
          f"{s.cell_x_m:.2f} x {s.cell_y_m:.2f} m")

    # a point a third of the way in
    x, y = s.crs_from_rowcol(s.height * 0.35, s.width * 0.35)
    pr = s.probe(x, y)
    check("probe returns a depth", pr is not None and np.isfinite(pr.z),
          f"{-pr.z:,.1f} m" if pr else "none")
    if pr:
        lon, lat = pr.lon, pr.lat
        print(f"    probe -> {-pr.z:,.1f} m,  slope {pr.slope:.2f} deg,  "
              f"{lat:.4f} N  {lon:.4f} E")
        check("lat/lon is on Earth", -90 <= lat <= 90 and -180 <= lon <= 180,
              f"{lat:.4f}, {lon:.4f}")
        check("slope is a real angle", np.isfinite(pr.slope) and 0 <= pr.slope < 90,
              f"{pr.slope:.2f} deg")

    # 100 cells east should be 100 cells of ground
    a = s.crs_from_rowcol(s.height * 0.5, s.width * 0.3)
    b = s.crs_from_rowcol(s.height * 0.5, s.width * 0.3 + 100)
    d = s.horizontal_distance(a[0], a[1], b[0], b[1])
    want = 100 * s.cell_x_m          # east-west, so the east-west cell size
    check("distance matches the pixel size", abs(d - want) / want < 0.02,
          f"{d:,.1f} m vs {want:,.1f} m")
    brg = s.bearing(a[0], a[1], b[0], b[1])
    check("bearing reads east", abs(brg - 90.0) < 2.0, f"{brg:.2f} deg")

    # local metres round trip, which is what the whole scene is built in
    rx, ry = s.crs_from_local(*s.local_from_crs(x, y))
    check("local metres round-trip", abs(rx - x) < 1e-6 and abs(ry - y) < 1e-6)

    # and a slope box
    half = 60.0 if not s.geographic else 60.0
    try:
        patch = s.slope_patch(x - half, y - half, x + half, y + half) \
            if not s.geographic else s.slope_patch(x - 0.0006, y - 0.0006,
                                                   x + 0.0006, y + 0.0006)
        st = patch.stats()
        check("slope box works", bool(st),
              f"{patch.shape[1]}x{patch.shape[0]} cells, max {st['max']:.1f} deg")
    except Exception as exc:
        check("slope box works", False, f"{type(exc).__name__}: {exc}")

    # nodata must still be a hole, not a value
    hole = s.crs_from_rowcol(14, 14)
    check("nodata stays a hole", s.probe(*hole) is None)

print()
print("FAILED: " + ", ".join(FAILED) if FAILED else "every CRS loaded and measured")

import sys as _sys
_sys.exit(1 if FAILED else 0)
