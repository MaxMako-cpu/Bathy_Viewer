"""Surface model for a single-band GeoTIFF.

Two grids are kept side by side:

* ``z_probe`` - native resolution (or as close as the memory budget allows).
  Every cursor readout is answered from this array, so depth and slope are
  never the smoothed values of the display mesh.
* ``z_disp``  - block-averaged for the 3D mesh, sized to a point budget so VTK
  stays interactive on a 100-megapixel raster.

All geometry handed to the viewer is in **local metres** relative to the raster
centre, which keeps the scene metric whether the source CRS is UTM or geographic.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field

import numpy as np
import rasterio
from rasterio.enums import Resampling
from pyproj import CRS as PjCRS, Geod, Transformer

GEOD = Geod(ellps="WGS84")

#: Points in the display mesh. ~1.5 M keeps rotation smooth on integrated GPUs.
DEFAULT_POINT_BUDGET = 1_500_000
#: Ceiling for the native-resolution probe array held in RAM.
DEFAULT_PROBE_BYTES = 1_200_000_000

#: Sentinels seen in the wild when a file declares no nodata value of its own.
SENTINELS = (-32767.0, -32768.0, -9999.0, -99999.0, -3.4028234663852886e38)


class RasterError(RuntimeError):
    """The file cannot be shown as a surface."""


def _block_mean(a: np.ndarray, step: int) -> np.ndarray:
    """Mean of ``step`` x ``step`` blocks, ignoring NaN, padding the far edge."""
    if step == 1:
        return a.astype(np.float32, copy=False)
    h, w = a.shape
    ph, pw = (-h) % step, (-w) % step
    if ph or pw:
        a = np.pad(a, ((0, ph), (0, pw)), constant_values=np.nan)
    hh, ww = a.shape
    blocks = a.reshape(hh // step, step, ww // step, step)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN blocks
        out = np.nanmean(blocks, axis=(1, 3))
    return out.astype(np.float32)


@dataclass
class Probe:
    """What the pointer found at one spot on the seabed."""

    x: float  # CRS easting / longitude
    y: float  # CRS northing / latitude
    z: float  # elevation in metres, positive up (NaN outside data)
    slope: float  # degrees from horizontal
    aspect: float  # downslope bearing, degrees from grid north (NaN if flat)
    row: float
    col: float
    lon: float = float("nan")
    lat: float = float("nan")


@dataclass
class Surface:
    path: str
    width: int
    height: int
    transform: object
    crs: object
    nodata: float | None
    band_count: int
    z_probe: np.ndarray
    probe_step: int
    z_disp: np.ndarray
    step: int
    # derived
    px: float = 0.0  # pixel size, CRS x units
    py: float = 0.0  # pixel size, CRS y units
    mx: float = 1.0  # metres per CRS x unit at the raster centre
    my: float = 1.0  # metres per CRS y unit
    x0: float = 0.0  # CRS x of the west edge
    y0: float = 0.0  # CRS y of the north edge
    cx: float = 0.0  # CRS x of the centre
    cy: float = 0.0
    _to_ll: object = field(default=None, repr=False)

    # ---------------------------------------------------------------- geometry

    @property
    def geographic(self) -> bool:
        return bool(getattr(self.crs, "is_geographic", False))

    @property
    def cell_m(self) -> float:
        """Display-mesh cell size in metres (mean of the two axes)."""
        return (self.px * self.mx * self.step + self.py * self.my * self.step) / 2.0

    @property
    def cell_x_m(self) -> float:
        """Cell size east-west, metres."""
        return self.px * self.mx

    @property
    def cell_y_m(self) -> float:
        """Cell size north-south, metres.

        Equal to :attr:`cell_x_m` on any UTM or other square-metre grid, but
        not on a geographic one: at 27.5 N a 0.0001 degree cell is 9.88 m
        across and 11.08 m tall, and treating it as square gets the slope
        badly wrong.
        """
        return self.py * self.my

    @property
    def native_cell_m(self) -> float:
        """A single figure for labelling. Use the two axes for arithmetic."""
        return (self.px * self.mx + self.py * self.my) / 2.0

    @property
    def extent_m(self) -> tuple[float, float]:
        return self.width * self.px * self.mx, self.height * self.py * self.my

    def local_from_crs(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.cx) * self.mx, (y - self.cy) * self.my

    def crs_from_local(self, lx: float, ly: float) -> tuple[float, float]:
        return lx / self.mx + self.cx, ly / self.my + self.cy

    def rowcol_from_crs(self, x: float, y: float) -> tuple[float, float]:
        """Fractional pixel index, where an integer lands on a pixel *centre*.

        The -0.5 is what makes this the exact inverse of :meth:`crs_from_rowcol`;
        without it every probe samples the corner between four pixels.
        """
        return (self.y0 - y) / self.py - 0.5, (x - self.x0) / self.px - 0.5

    def crs_from_rowcol(self, row: float, col: float) -> tuple[float, float]:
        """CRS coordinates of the centre of the given (fractional) pixel."""
        return self.x0 + (col + 0.5) * self.px, self.y0 - (row + 0.5) * self.py

    def to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        if self._to_ll is None:
            return float("nan"), float("nan")
        lon, lat = self._to_ll.transform(x, y)
        return lon, lat

    # ----------------------------------------------------------------- queries

    def probe(self, x: float, y: float) -> Probe | None:
        """Sample depth, slope and aspect at a CRS position, at native resolution."""
        row, col = self.rowcol_from_crs(x, y)
        r = row / self.probe_step
        c = col / self.probe_step
        z = self._bilinear(r, c)
        if not np.isfinite(z):
            return None
        slope, aspect = self._slope_aspect(int(round(r)), int(round(c)))
        lon, lat = self.to_lonlat(x, y)
        return Probe(x, y, float(z), slope, aspect, row, col, lon, lat)

    def _bilinear(self, r: float, c: float) -> float:
        a = self.z_probe
        h, w = a.shape
        # The outer half pixel sits beyond the last centre; clamp rather than
        # punching a transparent border round the whole grid.
        if -0.5 <= r < 0:
            r = 0.0
        if -0.5 <= c < 0:
            c = 0.0
        if h - 1 < r <= h - 0.5:
            r = h - 1.0
        if w - 1 < c <= w - 0.5:
            c = w - 1.0
        if not (0 <= r <= h - 1 and 0 <= c <= w - 1):
            return float("nan")
        r0, c0 = int(math.floor(r)), int(math.floor(c))
        r1, c1 = min(h - 1, r0 + 1), min(w - 1, c0 + 1)
        fr, fc = r - r0, c - c0
        q = (a[r0, c0], a[r0, c1], a[r1, c0], a[r1, c1])
        if not all(np.isfinite(v) for v in q):
            return float("nan")
        top = q[0] * (1 - fc) + q[1] * fc
        bot = q[2] * (1 - fc) + q[3] * fc
        return float(top * (1 - fr) + bot * fr)

    def _slope_aspect(self, r: int, c: int) -> tuple[float, float]:
        """Slope (deg) and downslope bearing at one probe cell, by Horn.

        The same 8-neighbour weighting :func:`horn_slope` applies to a slope
        box, so the cursor readout and the box agree on the same ground. They
        used to differ: a two-point central difference matches Horn to 0.014
        degrees on average but by up to 9.6 degrees on the steep, noisy cells,
        which are the ones anyone opens a box to look at.
        """
        a = self.z_probe
        h, w = a.shape
        r = min(max(r, 0), h - 1)
        c = min(max(c, 0), w - 1)
        # Clamped indices replicate the edge, matching the pad horn_slope uses.
        rm, rp = max(0, r - 1), min(h - 1, r + 1)
        cm, cp = max(0, c - 1), min(w - 1, c + 1)
        z = a[[rm, rm, rm, r, r, r, rp, rp, rp],
              [cm, c, cp, cm, c, cp, cm, c, cp]].astype(float)
        if not np.all(np.isfinite(z)):
            return float("nan"), float("nan")
        nw, n, ne, ww, _c, ee, sw, ss, se = z
        cell_x = self.cell_x_m * self.probe_step
        cell_y = self.cell_y_m * self.probe_step
        dzdx = ((ne + 2 * ee + se) - (nw + 2 * ww + sw)) / (8.0 * cell_x)
        dzdy = ((sw + 2 * ss + se) - (nw + 2 * n + ne)) / (8.0 * cell_y)
        slope = math.degrees(math.atan(math.hypot(dzdx, dzdy)))
        if dzdx == 0.0 and dzdy == 0.0:
            return slope, float("nan")
        # See horn_slope: dzdy is south-positive, so the downslope bearing is
        # atan2(-dzdx, +dzdy). Negating both mirrored it about east-west.
        aspect = (math.degrees(math.atan2(-dzdx, dzdy)) + 360.0) % 360.0
        return slope, aspect

    def slope_patch(self, x0: float, y0: float, x1: float, y1: float):
        """Recompute slope at native resolution for one rectangle.

        The box is snapped to whole probe cells and read with a one-cell halo,
        so the slope at the very edge is computed from real neighbours rather
        than a clamped short baseline.
        """
        from_ = self.rowcol_from_crs
        r_a, c_a = from_(min(x0, x1), max(y0, y1))     # top-left
        r_b, c_b = from_(max(x0, x1), min(y0, y1))     # bottom-right
        h, w = self.z_probe.shape
        # ceil() already gives the exclusive end past the last wanted cell;
        # adding one more grew a 200 m box to 232 m and let a 1 m box through.
        r0 = max(0, int(math.floor(min(r_a, r_b))))
        r1 = min(h, int(math.ceil(max(r_a, r_b))))
        c0 = max(0, int(math.floor(min(c_a, c_b))))
        c1 = min(w, int(math.ceil(max(c_a, c_b))))
        if r1 - r0 < 2 or c1 - c0 < 2:
            raise RasterError("that box is smaller than one grid cell")
        if (r1 - r0) * (c1 - c0) > MAX_PATCH_CELLS:
            raise RasterError(
                f"{(r1 - r0) * (c1 - c0):,} cells is too large a box - "
                f"keep it under {MAX_PATCH_CELLS:,}")

        hr0, hr1 = max(0, r0 - 1), min(h, r1 + 1)
        hc0, hc1 = max(0, c0 - 1), min(w, c1 + 1)
        halo = self.z_probe[hr0:hr1, hc0:hc1]
        cell_x = self.cell_x_m * self.probe_step
        cell_y = self.cell_y_m * self.probe_step
        cell = (cell_x + cell_y) / 2.0
        slope, aspect = horn_slope(halo, cell_x, cell_y)
        sr, sc = r0 - hr0, c0 - hc0
        z = halo[sr:sr + (r1 - r0), sc:sc + (c1 - c0)]
        slope = slope[sr:sr + (r1 - r0), sc:sc + (c1 - c0)]
        aspect = aspect[sr:sr + (r1 - r0), sc:sc + (c1 - c0)]
        # NaN depths cannot produce a slope; pad-edge would invent one.
        bad = ~np.isfinite(z)
        if bad.any():
            slope = np.where(bad, np.nan, slope)
            aspect = np.where(bad, np.nan, aspect)

        wx0, wy0 = self.crs_from_rowcol(r0 - 0.5, c0 - 0.5)
        wx1, wy1 = self.crs_from_rowcol(r1 - 0.5, c1 - 0.5)
        return SlopePatch(wx0, wy0, wx1, wy1, z, slope, aspect, cell, r0, c0)

    def horizontal_distance(self, x1, y1, x2, y2) -> float:
        """Metres between two CRS positions - grid distance, or geodesic if degrees."""
        if self.geographic:
            _, _, d = GEOD.inv(x1, y1, x2, y2)
            return float(d)
        return float(math.hypot((x2 - x1) * self.mx, (y2 - y1) * self.my))

    def bearing(self, x1, y1, x2, y2) -> float:
        if self.geographic:
            az, _, _ = GEOD.inv(x1, y1, x2, y2)
            return float(az % 360.0)
        return float((math.degrees(math.atan2(x2 - x1, y2 - y1)) + 360.0) % 360.0)

    # ------------------------------------------------------------- mesh arrays

    def display_slope(self) -> np.ndarray:
        """Slope of the *display* grid, degrees - for colour-by-slope only."""
        dx = self.px * self.mx * self.step
        dy = self.py * self.my * self.step
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            gy, gx = np.gradient(self.z_disp.astype(np.float64), dy, dx)
        return np.degrees(np.arctan(np.hypot(gx, gy))).astype(np.float32)


def _detect_nodata(z: np.ndarray, declared) -> float | None:
    if declared is not None and np.isfinite(declared):
        return float(declared)
    finite = z[np.isfinite(z)]
    if finite.size == 0:
        return None
    lo, hi = float(finite.min()), float(finite.max())
    for s in SENTINELS:
        if math.isclose(lo, s, rel_tol=1e-6, abs_tol=1e-3):
            return s
        if math.isclose(hi, -s, rel_tol=1e-6, abs_tol=1e-3) and s < 0:
            return -s
    return None


def load(
    path: str,
    band: int = 1,
    point_budget: int = DEFAULT_POINT_BUDGET,
    probe_bytes: int = DEFAULT_PROBE_BYTES,
    progress=None,
) -> Surface:
    """Open a GeoTIFF (or anything GDAL reads) as a :class:`Surface`."""

    def say(pct, msg):
        if progress:
            progress(pct, msg)

    say(2, "opening")
    with rasterio.open(path) as ds:
        if ds.count < 1:
            raise RasterError("file has no raster bands")
        if band > ds.count:
            band = 1
        tr = ds.transform
        if abs(tr.b) > 1e-9 or abs(tr.d) > 1e-9:
            raise RasterError(
                "rotated / sheared rasters are not supported - reproject to a "
                "north-up grid first (gdalwarp)"
            )

        n_px = ds.width * ds.height
        probe_step = 1
        if n_px * 4 > probe_bytes:
            probe_step = int(math.ceil(math.sqrt(n_px * 4 / probe_bytes)))

        say(8, "reading grid")
        if probe_step == 1:
            z = ds.read(band, out_dtype="float32")
        else:
            # Nearest, never average: averaging would blend the nodata sentinel
            # into real depths and quietly poison every cell next to a gap.
            z = ds.read(
                band,
                out_shape=(ds.height // probe_step, ds.width // probe_step),
                resampling=Resampling.nearest,
                out_dtype="float32",
            )
        meta = dict(
            width=ds.width,
            height=ds.height,
            transform=tr,
            crs=ds.crs,
            nodata=ds.nodata,
            band_count=ds.count,
        )

    say(45, "masking nodata")
    z = np.where(np.isfinite(z), z, np.nan)
    nod = _detect_nodata(z, meta["nodata"])
    if nod is not None:
        z[z == np.float32(nod)] = np.nan
    if not np.isfinite(z).any():
        raise RasterError("every cell is nodata")

    say(60, "building display grid")
    full_step = max(1, int(math.ceil(math.sqrt(n_px / max(point_budget, 1)))))
    step = max(1, int(round(full_step / probe_step)))
    z_disp = _block_mean(z, step)

    say(85, "projecting")
    crs = meta["crs"]
    pj = PjCRS.from_user_input(crs.to_wkt()) if crs else None
    to_ll = None
    if pj is not None:
        try:
            to_ll = Transformer.from_crs(pj, PjCRS.from_epsg(4326), always_xy=True)
        except Exception:
            to_ll = None

    px, py = abs(tr.a), abs(tr.e)
    x0, y0 = tr.c, tr.f
    cx = x0 + meta["width"] * tr.a / 2.0
    cy = y0 + meta["height"] * tr.e / 2.0

    if pj is None:
        mx = my = 1.0
    elif pj.is_geographic:
        _, _, my = GEOD.inv(cx, cy - 0.5, cx, cy + 0.5)
        _, _, mx = GEOD.inv(cx - 0.5, cy, cx + 0.5, cy)
    else:
        try:
            mx = my = float(pj.axis_info[0].unit_conversion_factor)
        except Exception:
            mx = my = 1.0

    surf = Surface(
        path=path,
        width=meta["width"],
        height=meta["height"],
        transform=tr,
        crs=pj if pj is not None else crs,
        nodata=nod,
        band_count=meta["band_count"],
        z_probe=z,
        probe_step=probe_step,
        z_disp=z_disp,
        step=step * probe_step,
        px=px,
        py=py,
        mx=mx,
        my=my,
        x0=x0,
        y0=y0,
        cx=cx,
        cy=cy,
        _to_ll=to_ll,
    )
    say(100, "ready")
    return surf


# ---------------------------------------------------------------- slope patch

#: Refuse a window bigger than this many native cells. A 10 km box is 0.67 M
#: cells and takes 29 ms; this leaves plenty of headroom while stopping someone
#: dragging a box across the whole grid.
MAX_PATCH_CELLS = 6_000_000


@dataclass
class SlopePatch:
    """A window of the grid at its own native resolution.

    The display mesh is decimated; this is not. Everything here is computed
    from the probe grid, so a 200 m box carries its real 16 x 16 cells rather
    than the 4 the display mesh would give it.
    """

    x0: float           # CRS bounds, snapped to whole cells
    y0: float
    x1: float
    y1: float
    z: np.ndarray       # elevations, metres, positive up
    slope: np.ndarray   # degrees
    aspect: np.ndarray  # degrees from grid north, downslope
    cell: float         # metres
    row0: int
    col0: int

    @property
    def shape(self):
        return self.z.shape

    def stats(self) -> dict:
        s = self.slope[np.isfinite(self.slope)]
        z = self.z[np.isfinite(self.z)]
        if s.size == 0 or z.size == 0:
            return {}
        return {
            "cells": int(self.z.size),
            "valid": int(z.size),
            "mean": float(s.mean()),
            "p95": float(np.percentile(s, 95)),
            "max": float(s.max()),
            "depth_min": float(-z.max()),
            "depth_max": float(-z.min()),
            "relief": float(z.max() - z.min()),
            "side_x": abs(self.x1 - self.x0),
            "side_y": abs(self.y1 - self.y0),
        }

    def fraction_over(self, degrees: float) -> float:
        s = self.slope[np.isfinite(self.slope)]
        return float((s > degrees).mean()) if s.size else float("nan")


def horn_slope(z: np.ndarray, cell_x: float, cell_y: float | None = None):
    """Slope and aspect by Horn's 8-neighbour method, in degrees.

    The weighted 3x3 that ArcGIS and GDAL use. A plain two-point central
    difference agrees with it on average but disagrees by up to ~10 degrees on
    the steep, noisy cells - which are the ones slope work is about.

    The two cell sizes are separate because they are not always the same. On a
    geographic grid a cell is wider than it is tall, and sharing one figure
    between the axes turned a real 13.26 degree slope into 8.70.
    """
    if cell_y is None:
        cell_y = cell_x
    a = z.astype(np.float32, copy=False)
    p = np.pad(a, 1, mode="edge")
    dzdx = ((p[:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:]) -
            (p[:-2, :-2] + 2 * p[1:-1, :-2] + p[2:, :-2])) / (8.0 * cell_x)
    dzdy = ((p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:]) -
            (p[:-2, :-2] + 2 * p[:-2, 1:-1] + p[:-2, 2:])) / (8.0 * cell_y)
    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))
    # dzdy is the change per metre SOUTHWARD - it subtracts the northern row
    # from the southern one, and row 0 is north. So the uphill vector in
    # (east, north) is (dzdx, -dzdy) and the downslope bearing is
    # atan2(-dzdx, +dzdy).
    #
    # This read -dzdy for both terms, which mirrored every bearing about the
    # east-west axis: a slope falling due north was reported as falling due
    # south. Due east and due west came out right, because their north
    # component is zero, which is exactly why it went unnoticed.
    aspect = (np.degrees(np.arctan2(-dzdx, dzdy)) + 360.0) % 360.0
    aspect[(dzdx == 0) & (dzdy == 0)] = np.nan
    return slope, aspect
