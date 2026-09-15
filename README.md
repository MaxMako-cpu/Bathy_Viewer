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
feed_test.py      UDP checks: wire parsing + live datagrams into the window
bathy3d/
  raster.py       GeoTIFF -> Surface; probe grid, display grid, CRS maths
  viewer.py       PyVista/VTK scene: mesh, hillshade, picking, measuring
  measure.py      stations, legs, totals, CSV  (no VTK)
  targets.py      vessel / ROV markers, trails, drop lines
  feed.py         UDP listener + record parser
  ramps.py        colour ramps
  mainwindow.py   PySide6 window, panels, menus, loader thread
```

## Live positions over UDP

**Position feed** panel: set the port (default **6451**) and press *Start
listening*. Depth is read from the terrain, so a grid must be open first.

Wire format, one record per datagram at about 1 Hz:

```
2026-09-15T21:39:04.4609743Z,707364.210,3009048.400,707036.708,3009161.042,707634.048,3009049.775
|___ ISO-8601 UTC, .NET "O" ___| |__ Vessel E/N __| |__ UHD333 E/N __| |__ UHD334 E/N __|
```

Eastings and northings are read in **the loaded grid's CRS** — no transform, so
the feed and the grid must agree (UTM 15N for the BOEM file).

| Target | Dot |
| --- | --- |
| Vessel | magenta |
| UHD333 | red |
| UHD334 | green |

Dots are drawn at screen-constant size and sit **on the seabed** beneath their
E/N, because the feed carries no depth. *Vessel at sea surface* lifts the vessel
to z = 0 and draws a drop line to the bottom instead. **Zoom to targets** frames
the group — worth knowing, since the vehicles work a few hundred metres apart on
a grid over 100 km wide and are a couple of pixels at full extent.

The **Live positions** table shows each vehicle's easting, northing, the seabed
depth under it, and the age of the last fix. Targets dim after 5 s without a
datagram (`feed.STALE_AFTER`).

### Notes on the format

* Framing does not matter. Records are decoded whether they arrive one per
  datagram, LF- or CRLF-separated, or run together with no separator at all.
  That last case needs care: `...3009049.775` followed immediately by
  `2026-09-15T...` reads as one 15-digit number unless the parser refuses to let
  a coordinate swallow the next record's year.
* Timestamps are .NET round-trip format with 7 fractional digits; Python's
  `datetime` takes 6, so the last digit is dropped.
* The sender repeats the previous position when it has no new fix, so a target
  can be receiving packets while not moving. Staleness is judged on packet
  arrival, not on position change.
* A position outside the grid shows as `off grid` in the table and is not drawn.
* The listener does **not** set `SO_REUSEADDR`. If another program on the
  machine already holds the port, binding fails with a message rather than
  quietly taking the datagrams off it - on Windows two UDP sockets sharing a
  port deliver unicast to whichever bound last. To feed two consumers, have
  the sender repeat to a second port.

### Changing the feed

Field order lives in one place — `ORDER` in `bathy3d/feed.py`:

```python
ORDER = ("Vessel", "UHD333", "UHD334")
```

Add or reorder vehicles there and give each a colour in `DEFAULT_TARGETS`
(`bathy3d/targets.py`). Nothing else needs to change. If a future feed carries
depth, pass it as `z` to `TargetLayer.update()` instead of reading the terrain;
if it carries heading, `_glyph()` already has a directional vessel marker.

## Testing

```
C:\Users\<you>\.venvs\bathy3d\Scripts\python.exe smoke_test.py
```

Loads the grid, checks probe values against a direct `rasterio` read, verifies
distance against known pixel counts, exercises the measuring maths and the
target layer, and renders off-screen to a PNG under
`%TEMP%\bathy3d_smoke\`.


```
C:\Users\<you>\.venvs\bathy3d\Scripts\python.exe feed_test.py
```

Parses the wire format across six framing variants and four malformed inputs,
then brings the real window up, starts the real listener, fires real datagrams
at it over loopback on port 6471, and checks the dots land on the reported
coordinates at the right seabed depth in the right colours.

## Known limits

- Rotated / sheared rasters are rejected — reproject north-up first.
- Band 1 only on multi-band files.
- No contour overlay yet, and no depth-profile plot along the measured line.
- The feed carries no depth or heading, so dot height is terrain-derived and
  the vessel marker has no orientation.
- No auto-follow: the camera does not track the vehicles as they move.
