"""ESRI shapefile overlays draped on the seabed.

Reads points, lines and polygons, reprojects them into the loaded grid's CRS,
and hangs every vertex on the terrain so a pipeline route or a lease-block
boundary follows the relief instead of floating through it.

Two details matter more than they look:

* **Densify before draping.** Two vertices 5 km apart are one straight segment,
  and draping only its ends would drive the line straight through whatever
  ridge lies between. Long segments are subdivided at roughly the grid's own
  cell size first.
* **Reproject from the .prj, not from hope.** Shapefiles carry their CRS in a
  sidecar file, which pyshp does not read. A lease block in geographic degrees
  dropped into a UTM scene without transforming lands in the Gulf of Guinea.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np
import shapefile
from pyproj import CRS as PjCRS, Transformer

#: Colours handed to overlays in turn - distinct from the target dots
#: (magenta / red / green) and from the amber measuring furniture. Warm and
#: pale shades lead, because everything underneath is blue: a blue overlay on
#: a blue seabed is legible on paper and invisible in the scene.
PALETTE = (
    "#ffd60a", "#ffffff", "#bf5af2", "#ff9f0a",
    "#30d158", "#ff6482", "#a2d2ff", "#64d2ff",
)

POINT_TYPES = {1, 8, 11, 18, 21, 28}
LINE_TYPES = {3, 13, 23}
POLY_TYPES = {5, 15, 25}

#: Refuse to build an overlay bigger than this; densifying a national
#: coastline at 50 m would otherwise quietly eat the session.
MAX_VERTICES = 1_500_000


class VectorError(RuntimeError):
    pass


@dataclass
class Layer:
    path: str
    name: str
    kind: str                       # "point" | "line" | "polygon"
    parts: list = field(default_factory=list)   # arrays of (lx, ly, elev)
    labels: list = field(default_factory=list)  # (lx, ly, elev, text)
    color: str = PALETTE[0]
    visible: bool = True
    crs_name: str = "unknown"
    n_features: int = 0
    dropped: int = 0                # vertices with no terrain under them

    @property
    def n_vertices(self) -> int:
        return int(sum(len(p) for p in self.parts))

    def summary(self) -> str:
        bits = [f"{self.n_features} {self.kind}s", f"{self.n_vertices:,} vertices"]
        if self.dropped:
            bits.append(f"{self.dropped:,} off grid")
        return ", ".join(bits)


def _read_prj(path: str):
    prj = os.path.splitext(path)[0] + ".prj"
    if not os.path.exists(prj):
        return None
    try:
        with open(prj, encoding="utf-8", errors="replace") as fh:
            return PjCRS.from_wkt(fh.read().strip())
    except Exception:
        return None


def _densify(pts: np.ndarray, step: float) -> np.ndarray:
    """Insert vertices so no segment is longer than ``step`` metres."""
    if len(pts) < 2 or step <= 0:
        return pts
    out = [pts[0]]
    for a, b in zip(pts[:-1], pts[1:]):
        d = float(np.hypot(b[0] - a[0], b[1] - a[1]))
        n = int(d // step)
        if n > 0:
            frac = np.arange(1, n + 1, dtype=float) / (n + 1)
            out.extend(a + (b - a) * frac[:, None])
        out.append(b)
    return np.asarray(out, dtype=float)


def _drape(pts: np.ndarray, surface, lift: float):
    """Split a run of XY into the pieces that have terrain under them."""
    runs, cur, dropped = [], [], 0
    for x, y in pts:
        p = surface.probe(float(x), float(y))
        if p is None:
            dropped += 1
            if len(cur) > 1:
                runs.append(np.asarray(cur, dtype=float))
            cur = []
            continue
        lx, ly = surface.local_from_crs(p.x, p.y)
        cur.append((lx, ly, p.z + lift))
    if len(cur) > 1 or (len(cur) == 1 and not runs):
        runs.append(np.asarray(cur, dtype=float))
    return runs, dropped


def load(path: str, surface, color: str = PALETTE[0], densify_m: float | None = None,
         label_field: str | None = None, max_labels: int = 250) -> Layer:
    """Read a shapefile and drape it on ``surface``."""
    if surface is None:
        raise VectorError("open a grid before adding an overlay - "
                          "the shapes are hung on the terrain")
    if not os.path.exists(path):
        raise VectorError(f"no such file: {path}")

    src = _read_prj(path)
    dst = surface.crs
    tf = None
    crs_name = "none (.prj missing - assuming the grid's CRS)"
    if src is not None:
        crs_name = src.name or "unnamed"
        try:
            if dst is not None and not src.equals(dst):
                tf = Transformer.from_crs(src, dst, always_xy=True)
        except Exception as exc:
            raise VectorError(f"cannot transform from {crs_name}: {exc}") from exc

    step = densify_m if densify_m else max(10.0, min(surface.cell_m, 100.0))
    lift = max(surface.native_cell_m * 0.2, 1.0)

    # A shapefile is really three or four files. Geometry lives in the .shp
    # alone, so a missing .dbf (attributes) or .shx (index) costs labels and
    # speed but not the shapes - open the .shp directly rather than refusing.
    try:
        rdr = shapefile.Reader(path)
    except Exception:
        try:
            rdr = shapefile.Reader(shp=open(path, "rb"))
        except Exception as exc:
            raise VectorError(f"cannot read shapefile: {exc}") from exc

    st = rdr.shapeType
    if st in POINT_TYPES:
        kind = "point"
    elif st in LINE_TYPES:
        kind = "line"
    elif st in POLY_TYPES:
        kind = "polygon"
    else:
        rdr.close()
        raise VectorError(f"unsupported shape type {st}")

    try:
        names = [f[0] for f in rdr.fields[1:]]
    except Exception:
        names = []            # no .dbf: geometry only, no labels
    if label_field is None:
        for cand in ("NAME", "Name", "name", "LABEL", "ID", "BLOCK", "AREA"):
            if cand in names:
                label_field = cand
                break
    li = names.index(label_field) if label_field in names else None

    layer = Layer(path=path, name=os.path.splitext(os.path.basename(path))[0],
                  kind=kind, color=color, crs_name=crs_name)

    if names:
        stream = ((sr.shape, sr.record) for sr in rdr.iterShapeRecords())
    else:
        stream = ((s, None) for s in rdr.iterShapes())

    total = 0
    loose: list = []          # point layers collapse into one array
    try:
        for shp, record in stream:
            if not shp.points:
                continue
            layer.n_features += 1
            # Point shapes carry no part list at all, so default to one part
            # starting at zero - otherwise the loop below runs zero times and
            # the whole layer silently comes back empty.
            bounds = (list(shp.parts) or [0]) + [len(shp.points)]
            for i in range(len(bounds) - 1):
                raw = np.asarray(shp.points[bounds[i]:bounds[i + 1]], dtype=float)
                if len(raw) == 0:
                    continue
                if tf is not None:
                    xs, ys = tf.transform(raw[:, 0], raw[:, 1])
                    raw = np.column_stack([xs, ys])
                if kind == "polygon" and len(raw) > 2 and not np.allclose(raw[0], raw[-1]):
                    raw = np.vstack([raw, raw[0]])
                if kind != "point":
                    raw = _densify(raw, step)
                runs, dropped = _drape(raw, surface, lift)
                layer.dropped += dropped
                for r in runs:
                    if not len(r):
                        continue
                    total += len(r)
                    if kind == "point":
                        loose.extend(r)      # thousands of 1-vertex arrays
                    else:                    # would be pointless overhead
                        layer.parts.append(r)
                if li is not None and record is not None and runs \
                        and len(layer.labels) < max_labels:
                    txt = str(record[li]).strip()
                    if txt and txt.lower() != "none":
                        c = runs[0].mean(axis=0)
                        layer.labels.append((c[0], c[1], c[2], txt))
            if total > MAX_VERTICES:
                raise VectorError(
                    f"overlay too large ({total:,} vertices after densifying at "
                    f"{step:.0f} m). Simplify the shapefile or clip it to the grid.")
    finally:
        rdr.close()

    if loose:
        layer.parts = [np.asarray(loose, dtype=float)]

    if not layer.parts:
        raise VectorError(
            "nothing landed on the grid - the shapefile may cover a different "
            f"area, or be in a CRS the .prj does not describe (read: {crs_name})")
    return layer
