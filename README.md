# Bathy3D

A 3D hillshaded viewer for bathymetry and terrain grids. Open any GDAL-readable
raster, orbit it in 3D, and read **depth, slope and position** off the seabed
under the cursor. Click stations to measure distance in metres along the bottom.

Built for the BOEM northern Gulf of Mexico grid
(`BOEM_bathy_WGS84_UTM15N.tif`, 10 729 x 8 004 @ 12.22 m, UTM 15N) but nothing
in it is specific to that file.

---

## Install

The 3D stack is large (~700 MB). Keep the virtual environment **outside**
OneDrive — a synced venv corrupts itself and slows every import.

```
python -m venv C:\Users\<you>\.venvs\bathy3d
C:\Users\<you>\.venvs\bathy3d\Scripts\python.exe -m pip install -r requirements.txt
```

## Run

```
C:\Users\<you>\.venvs\bathy3d\Scripts\python.exe run.py
C:\Users\<you>\.venvs\bathy3d\Scripts\python.exe run.py "C:\path\to\grid.tif"
```

Or drop a `.tif` onto the window. **File › Open** takes `.tif .tiff .vrt .img
.bag .asc .grd .nc` and anything else GDAL handles.

## Using it

| Action | Control |
| --- | --- |
| Orbit | left-drag |
| Zoom | wheel |
| Pan | middle-drag |
| Drop a measuring station | left-click (Measure mode on) |
| Remove last station | **Undo** |
| Reset / plan view | View menu |

The **Readout** panel tracks the cursor: depth, slope angle, downslope bearing,
CRS easting/northing, latitude/longitude, and the source pixel. The **Measured
line** table lists each leg's horizontal distance, depth change, gradient and
bearing, with totals for horizontal, along-seabed and straight-chord distance.
**Export CSV** writes the stations with cumulative distance.

## How it reads the grid

Two grids are held at once:

* **Probe grid** — native resolution, in RAM. Every cursor readout comes from
  here, so depth and slope are the *real* 12.22 m numbers, not smoothed ones.
  Above ~1.2 GB the probe is decimated and the status bar says so.
* **Display mesh** — block-averaged to the point budget set by **Mesh detail**
  (0.4 M – 6 M cells). Changing it reloads the file.

Cells touching a nodata corner are hidden rather than draped over, so survey
gaps read as holes instead of flat sheets.

Geometry is built in **local metres** relative to the raster centre, so a
geographic (degree) CRS works too — pixel spacing is converted at the centre
latitude and distances fall back to geodesic. Vertical exaggeration is applied
as an actor scale, so it is instant even on a 6 M-cell mesh and the hillshade
normals follow it.

## Performance notes on your file

`BOEM_bathy_WGS84_UTM15N.tif` is uncompressed, one strip per row, no overviews —
the worst possible layout for random access. Converting it once pays for itself:

```
gdal_translate -of COG -co COMPRESS=DEFLATE -co PREDICTOR=3 -co BLOCKSIZE=512 ^
  BOEM_bathy_WGS84_UTM15N.tif BOEM_bathy_cog.tif
```

## Layout

```
run.py            launcher
smoke_test.py     headless checks: load, probe vs rasterio, measure, render
bathy3d/
  raster.py       GeoTIFF -> Surface; probe grid, display grid, CRS maths
  viewer.py       PyVista/VTK scene: mesh, hillshade, picking, measuring
  measure.py      stations, legs, totals, CSV  (no VTK)
  targets.py      vessel / ROV markers, trails, drop lines
  ramps.py        colour ramps
  mainwindow.py   PySide6 window, panels, menus, loader thread
```

## Step 2 — live positions over UDP

`targets.py` is the only thing the position feed needs to touch:

```python
view.targets.update("Vessel", easting, northing, 0.0, heading=hdg)
view.targets.update("ROV 1",  easting, northing, -1487.2)
view.targets.update("ROV 2",  easting, northing, None)   # None = sit on the seabed
```

Coordinates are in the **loaded grid's CRS**. If the feed sends WGS 84
lat/lon, transform first with `pyproj.Transformer.from_crs(4326, surface.crs)`.
Markers, labels, drop lines to the seabed, and trails are handled for you;
`mark_stale(name)` dims a target when its fix goes quiet.

A UDP listener should run on its own `QThread` and hand positions to the GUI
thread through a Qt signal — never call `update()` from the socket thread.

**View › Demo target feed (test)** drives three targets in circles so the layer
can be exercised before any real feed exists.

Still to decide for step 2: the wire format (NMEA `$GPGGA` / `$PSIMSSB`, a
vendor binary, or plain JSON), one port per vehicle or one shared port, and
whether ROV depth comes from the feed or from the terrain.

## Testing

```
C:\Users\<you>\.venvs\bathy3d\Scripts\python.exe smoke_test.py
```

Loads the grid, checks probe values against a direct `rasterio` read, verifies
distance against known pixel counts, exercises the measuring maths and the
target layer, and renders off-screen to a PNG under
`%TEMP%\bathy3d_smoke\`.

## Known limits

- Rotated / sheared rasters are rejected — reproject north-up first.
- Band 1 only on multi-band files.
- No contour overlay yet, and no depth-profile plot along the measured line.
