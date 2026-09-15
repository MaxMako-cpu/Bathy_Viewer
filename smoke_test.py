#!/usr/bin/env python
"""Headless checks for Bathy3D.

Runs the real code paths against a real grid - loads it, builds the mesh,
renders off-screen to a PNG, probes known pixels against a direct rasterio
read, and exercises the measuring maths and the target layer.

    python smoke_test.py [grid.tif]
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

DEFAULT = r"C:\Users\mkozh\OneDrive\Desktop\bathy\BOEM_bathy_WGS84_UTM15N.tif"
OUT = os.path.join(tempfile.gettempdir(), "bathy3d_smoke")

FAILED = []


def check(name, cond, detail=""):
    tag = "ok  " if cond else "FAIL"
    print(f"  [{tag}] {name}{(' - ' + detail) if detail else ''}")
    if not cond:
        FAILED.append(name)
    return cond


def main(path):
    os.makedirs(OUT, exist_ok=True)
    print(f"grid: {path}")

    # -- load -----------------------------------------------------------------
    from bathy3d import raster

    t0 = time.time()
    surf = raster.load(path, point_budget=1_500_000)
    print(f"\nload: {time.time() - t0:.1f}s")
    print(f"  {surf.width}x{surf.height} native, probe step {surf.probe_step}, "
          f"mesh {surf.z_disp.shape[1]}x{surf.z_disp.shape[0]} (step {surf.step})")
    print(f"  CRS {getattr(surf.crs, 'name', surf.crs)}  geographic={surf.geographic}")
    print(f"  native cell {surf.native_cell_m:.3f} m   mesh cell {surf.cell_m:.1f} m")
    ew, nh = surf.extent_m
    print(f"  extent {ew / 1000:.1f} x {nh / 1000:.1f} km   nodata {surf.nodata}")

    check("mesh has cells", surf.z_disp.size > 1000)
    check("some data is valid", bool(np.isfinite(surf.z_disp).any()))

    # -- probe vs a direct rasterio read --------------------------------------
    print("\nprobe accuracy (vs direct rasterio read):")
    import rasterio

    with rasterio.open(path) as ds:
        band = ds.read(1)
        nod = surf.nodata
        rng = np.random.default_rng(7)
        worst = 0.0
        tested = 0
        while tested < 12:
            r = int(rng.integers(2, ds.height - 2))
            c = int(rng.integers(2, ds.width - 2))
            truth = float(band[r, c])
            if nod is not None and math.isclose(truth, nod, abs_tol=1e-3):
                continue
            if not math.isfinite(truth):
                continue
            x, y = surf.crs_from_rowcol(r, c)
            p = surf.probe(x, y)
            if p is None:
                continue
            worst = max(worst, abs(p.z - truth))
            tested += 1
        print(f"  tested {tested} pixels, worst |delta| = {worst:.4f} m")
        tol = 0.01 if surf.probe_step == 1 else 60.0
        check("probe matches source", worst < tol, f"tolerance {tol} m")

    # -- coordinates ----------------------------------------------------------
    print("\ncoordinates:")
    cx, cy = surf.cx, surf.cy
    lon, lat = surf.to_lonlat(cx, cy)
    print(f"  centre {cx:,.1f} {cy:,.1f}  ->  {lat:.5f} N  {lon:.5f} E")
    check("lon/lat sane", -180 <= lon <= 180 and -90 <= lat <= 90)
    rx, ry = surf.crs_from_local(*surf.local_from_crs(cx + 1234.0, cy - 987.0))
    check("local<->crs round trip", abs(rx - (cx + 1234.0)) < 1e-6
          and abs(ry - (cy - 987.0)) < 1e-6)

    # -- distance -------------------------------------------------------------
    print("\ndistance:")
    a = surf.crs_from_rowcol(surf.height * 0.4, surf.width * 0.3)
    b = surf.crs_from_rowcol(surf.height * 0.4, surf.width * 0.3 + 1000)
    d = surf.horizontal_distance(a[0], a[1], b[0], b[1])
    want = 1000 * surf.native_cell_m
    print(f"  1000 pixels east = {d:,.1f} m (expected ~{want:,.1f} m)")
    check("distance matches pixel size", abs(d - want) / want < 0.01)
    brg = surf.bearing(a[0], a[1], b[0], b[1])
    check("bearing due east", abs(brg - 90.0) < 1.5, f"{brg:.2f} deg")

    # -- slope ----------------------------------------------------------------
    print("\nslope:")
    slopes = []
    rng = np.random.default_rng(3)
    for _ in range(4000):
        r = int(rng.integers(2, surf.z_probe.shape[0] - 2))
        c = int(rng.integers(2, surf.z_probe.shape[1] - 2))
        s, _asp = surf._slope_aspect(r, c)
        if math.isfinite(s):
            slopes.append(s)
    slopes = np.array(slopes)
    print(f"  {slopes.size} samples  mean {slopes.mean():.2f} deg  "
          f"p99 {np.percentile(slopes, 99):.2f}  max {slopes.max():.2f}")
    check("slopes in range", 0 <= slopes.min() and slopes.max() < 90)

    # -- measuring ------------------------------------------------------------
    print("\nmeasuring:")
    from bathy3d.measure import MeasureLine, Station, compass

    line = MeasureLine(surf)
    for fr, fc in ((0.35, 0.30), (0.45, 0.48), (0.38, 0.62)):
        x, y = surf.crs_from_rowcol(surf.height * fr, surf.width * fc)
        p = surf.probe(x, y)
        if p:
            line.add(Station(p.x, p.y, p.z, p.lon, p.lat))
    check("three stations placed", len(line) == 3, f"got {len(line)}")
    for i, lg in enumerate(line.legs(), 1):
        print(f"  leg {i}: {lg.horizontal:,.0f} m horiz, dz {lg.dz:+.1f} m, "
              f"grad {lg.gradient:.2f} deg, brg {compass(lg.bearing)} {lg.bearing:.0f}")
    t = line.totals()
    print(f"  totals: horiz {t['horizontal']:,.0f} m, seabed {t['slant']:,.0f} m, "
          f"chord {t['chord']:,.0f} m")
    check("seabed >= horizontal", t["slant"] >= t["horizontal"] - 1e-6)
    check("chord <= seabed", t["chord"] <= t["slant"] + 1e-6)
    csv = line.to_csv()
    check("csv has a row per station", len(csv.strip().splitlines()) == 4)
    open(os.path.join(OUT, "line.csv"), "w", encoding="utf-8").write(csv)

    # -- off-screen render ----------------------------------------------------
    print("\nrender:")
    import pyvista as pv

    pv.OFF_SCREEN = True
    z = surf.z_disp[::-1, :]
    ny, nx = z.shape
    dx = surf.px * surf.mx * surf.step
    dy = surf.py * surf.my * surf.step
    valid = np.isfinite(z)
    fill = float(np.nanmedian(z))
    zf = np.where(valid, z, fill).astype(np.float32)
    grid = pv.ImageData(dimensions=(nx, ny, 1), spacing=(dx, dy, 1.0),
                        origin=(0.0, 0.0, 0.0))
    grid.point_data["elev"] = zf.ravel(order="C")
    grid.set_active_scalars("elev")
    mesh = grid.warp_by_scalar("elev", factor=1.0)
    bad = ~valid
    cell_bad = bad[:-1, :-1] | bad[:-1, 1:] | bad[1:, :-1] | bad[1:, 1:]
    idx = np.flatnonzero(cell_bad.ravel(order="C"))
    check("hide_cells available", hasattr(mesh, "hide_cells"))
    if hasattr(mesh, "hide_cells") and idx.size:
        mesh.hide_cells(idx, inplace=True)
    print(f"  mesh: {mesh.n_points:,} points, {mesh.n_cells:,} cells, "
          f"{idx.size:,} hidden ({100 * idx.size / max(mesh.n_cells, 1):.1f}%)")

    from bathy3d import ramps

    pl = pv.Plotter(off_screen=True, window_size=(1280, 820))
    pl.set_background("#0d1418", top="#16232a")
    act = pl.add_mesh(mesh, scalars="elev", cmap=ramps.depth_ramp("Bathy"),
                      smooth_shading=True, ambient=0.28, diffuse=0.9,
                      show_scalar_bar=True)
    act.SetScale(1.0, 1.0, 6.0)
    pl.remove_all_lights()
    a, e = math.radians(315.0), math.radians(40.0)
    r = 1e6
    sun = pv.Light(position=(math.sin(a) * math.cos(e) * r,
                             math.cos(a) * math.cos(e) * r,
                             math.sin(e) * r),
                   focal_point=(0, 0, 0), light_type="scene light")
    sun.positional = False
    pl.add_light(sun)
    b = act.GetBounds()
    c = ((b[0] + b[1]) / 2, (b[2] + b[3]) / 2, (b[4] + b[5]) / 2)
    rad = max(b[1] - b[0], b[3] - b[2])
    d = rad * 1.75
    av, ev = math.radians(210.0), math.radians(28.0)
    pl.camera_position = [
        (c[0] + d * math.cos(ev) * math.sin(av),
         c[1] + d * math.cos(ev) * math.cos(av),
         c[2] + d * math.sin(ev)), c, (0, 0, 1)]
    png = os.path.join(OUT, "render.png")
    t0 = time.time()
    pl.screenshot(png)
    pl.close()
    print(f"  wrote {png} in {time.time() - t0:.1f}s")
    ok = os.path.exists(png) and os.path.getsize(png) > 20_000
    check("screenshot written", ok, f"{os.path.getsize(png):,} bytes" if ok else "missing")

    # -- target layer ---------------------------------------------------------
    print("\ntargets:")
    from bathy3d.targets import TargetLayer

    pl2 = pv.Plotter(off_screen=True, window_size=(640, 400))
    layer = TargetLayer(pl2)
    layer.set_surface(surf)
    layer.set_ve(6.0)
    x, y = surf.crs_from_rowcol(surf.height * 0.5, surf.width * 0.5)
    layer.update("Vessel", x, y, 0.0, heading=45.0)
    layer.update("ROV 1", x + 400, y + 400, None)
    layer.update("ROV 2", x - 400, y - 400, -1500.0)
    check("three targets tracked", len(layer.targets) == 3)
    check("ROV 1 snapped to seabed", math.isfinite(layer.targets["ROV 1"].z),
          f"z = {layer.targets['ROV 1'].z:.1f} m")
    for i in range(6):
        layer.update("ROV 1", x + 400 + i * 60, y + 400, None)
    check("trail accumulates", len(layer.targets["ROV 1"].trail) == 7,
          f"{len(layer.targets['ROV 1'].trail)} points")
    pl2.close()

    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}): " + ", ".join(FAILED))
        return 1
    print("all checks passed")
    print(f"artifacts in {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT))
