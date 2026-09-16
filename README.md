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
| Swing the map round | left-drag left/right |
| Move the map | shift + left-drag, or middle-drag |
| Zoom | wheel |
| Drop a measuring station | left-click (Measure mode on) |
| Remove last station | **Undo** |
| Reset / plan view | View menu |

**Lock Z axis** (on by default) is what makes a left-drag usable on a chart.
It holds the Z axis still whichever way the drag is moving the camera:

* **Rotating** — VTK's trackball changes the tilt as well as the heading, so a
  drag tips the chart out of whatever viewing angle you set. With the lock on,
  the camera's height above the target and its distance from it are both held,
  so it stays on one horizontal circle: dragging left and right swings the map
  round, and the viewing angle you chose survives. The horizon is kept level
  too. Dragging up and down does nothing until you turn the lock off.
* **Moving** — VTK pans in the plane of the screen, so on a tilted view sliding
  sideways also changes your altitude and the scene creeps away. Camera and
  focal point shift together, so putting both heights back keeps the horizontal
  part of the move and nothing else.

**Zoom** anchors on the seabed under the pointer rather than the middle of the
scene. VTK dollies towards the focal point, which sits in open water above the
bottom, so pulling on the wheel used to drive the camera through the seabed.
The clipping range is also derived from the camera distance instead of the
scene bounds — VTK clamps the near plane to far/1000, which on a 130 km grid
pinned it near 110 m and clipped the bottom away as soon as you got close.
Between them the wheel now runs from 902 km out to 1 m off the seabed with the
view still full. Both ends are capped so the wheel cannot run away.

Turn it off and you get normal orbiting. **Left drag** in the Pointer panel
swaps the buttons over if you would rather move the map with the plain
left-drag and rotate with shift.

**Colour by** switches between depth and slope, and each keeps its own ramp
menu and its own last choice, so flipping between them does not lose your
setting. Depth offers Bathy, Rainbow, Turbo, Spectral, Ocean, Viridis, Terrain,
Grey and Hillshade only; slope offers Green to red, Steep alert, Turbo, Heat,
Yellow-orange-red, Viridis, Magma and Grey. The rainbow ramps band the range
harder than viridis on purpose — small depth changes separate into visibly
different colours, which is what a survey eye is usually looking for.

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
overlay_test.py   shapefile checks: drape, toggle, exaggeration, remove
trail_test.py     trail checks: age trimming, 24 h volume, draw cost
drag_test.py      camera checks: level rotation, Z lock, zoom depth, click
prefs_test.py     restart checks: files, folders and settings remembered
colour_test.py    colour checks: per-mode ramps, overlay colours, restart
depth_test.py     five-body checks: both feeds, depths, TMS, tethers
bathy3d/
  raster.py       GeoTIFF -> Surface; probe grid, display grid, CRS maths
  viewer.py       PyVista/VTK scene: mesh, hillshade, picking, measuring
  measure.py      stations, legs, totals, CSV  (no VTK)
  targets.py      vessel / ROV markers, trails, drop lines
  feed.py         UDP listener + record parser
  vectors.py      shapefile reader; reproject, densify, drape
  prefs.py        what is remembered between runs
  ramps.py        colour ramps
  mainwindow.py   PySide6 window, panels, menus, loader thread
```

## What it remembers

The grid and the shapefiles you had open are reopened on the next start, and
the file dialogs come back to the folders you used. Settings come back too:
exaggeration, sun, ramp, colour-by, mesh detail, trail retention, UDP port,
left-drag mode and the Z lock, plus the window size and position.

Files that have moved or been deleted are skipped rather than reported as
errors. **File › Forget remembered files** clears the grid and overlays so the
next start opens empty; the remembered folders survive that. Passing a grid on
the command line takes precedence over the remembered one.

Settings live in QSettings — the registry on Windows — so there is no file to
mislay. Setting `BATHY3D_PROFILE` puts a run in its own settings profile; every
test suite does that and wipes it on entry, so tests neither read nor overwrite
real preferences and cannot leave state behind for one another.

## Shapefile overlays

**Overlays** panel: *Add shapefile…*, or drop a `.shp` onto the window. Points,
lines and polygons are read, reprojected into the loaded grid's CRS, and hung on
the terrain so a route or a boundary follows the relief instead of floating
through it. Tick to show or hide, select and *Remove* to drop one. Each layer gets its own
colour: double-click it in the list, or select it and press *Colour…*. The
choice is remembered with the file.

Two details that decide whether an overlay is right or merely present:

* **Long segments are densified before draping.** Two vertices 5 km apart are a
  single straight segment, and draping only its ends would drive the line
  through whatever ridge lies between. Segments are subdivided at roughly the
  grid's own cell size first.
* **The CRS comes from the `.prj` sidecar.** Reprojection is to the grid's CRS.
  With no `.prj` the grid's own CRS is assumed and the panel says so - check the
  overlay lands where you expect before trusting it.

A shapefile is really three or four files. Geometry lives in the `.shp` alone,
so a missing `.dbf` costs labels and a missing `.shx` costs a little speed, but
neither stops the shapes loading. Labels are drawn for point layers when a
`.dbf` is present and carries a `NAME`, `LABEL`, `ID`, `BLOCK` or `AREA` field.

Vertices with no terrain under them are dropped and counted, so a file that
overhangs the grid loads with the part that fits rather than failing. Overlays
are cleared when a different grid is opened, since they were draped on the old
one. Requires `pyshp`.

## Live positions and depths over UDP

**Position feed** panel: set the two ports and press *Start listening*. A grid
must be open first, since positions are placed on the terrain.

Positions (default **6451**), about 1 Hz:

```
706148.701,3006428.410,705939.201,3006546.099,706515.275,3006391.404,705941.900,3006549.300,706512.600,3006388.100
|__ Vessel E/N __| |__ UHD333 E/N __| |__ UHD334 E/N __| |__ TMS333 E/N __| |__ TMS334 E/N __|
```

Depths (default **6452**), metres below the surface, positive down:

```
1656.082,1646.926,1492.150,1508.260
| UHD333 | UHD334 | TMS333 | TMS334
```

Eastings and northings are read in **the loaded grid's CRS** — no transform, so
the feed and the grid must agree (UTM 15N for the BOEM file).

| Body | Drawn as |
| --- | --- |
| Vessel | magenta dot, at the surface or on the bottom beneath itself |
| UHD333 / UHD334 | red and green dots, at their reported depth |
| TMS333 / TMS334 | darker red and green cylinders, 3 m × 2 m, at their depth |

Each TMS is joined to its own ROV by a thin dotted tether, and **Show TMS**
hides the cylinders and their tethers together. A drop line runs from each body
to the seabed beneath it, so height off bottom reads at a glance.

The two feeds are independent and carry no timestamps, so there is no honest
way to time-align them. Each vehicle keeps its latest position and its latest
depth, and the scene is rebuilt from whichever has just changed. A depth more
than 15 s old is treated as gone: the vehicle rests on the seabed and its
marker dims, rather than hanging at a frozen depth while looking live.

**The Alt column is the one to watch.** Altitude is seabed depth minus vehicle
depth, and it cannot be negative — a vehicle reading as *below* the bottom
means a datum or sign mismatch between the depth feed and the grid, not a
flying ROV. It turns red rather than being drawn silently buried in the
terrain.

Cylinders are drawn at their true 3 m × 2 m size but never allowed below 20
screen pixels; a body that size is otherwise invisible until you have closed
right in. They are also given plenty of ambient light, since a small object lit
only by the survey sun goes black whenever the sun is behind it.

### Scale is the thing to understand

Viewing the whole grid puts the camera ~215 km back, where one pixel is about
90 m of seabed — so a vehicle making 0.6 m/s moves a *sixth of a pixel per
second* and looks frozen, trail and all. The first fix of a session therefore
frames the vehicles automatically. **Zoom to targets** re-frames them on
demand, and **Follow targets** keeps the camera centred as they move without
changing your zoom.

Markers, trails, tethers and drop lines are drawn over the terrain rather than
depth-tested against it, so relief between a body and the camera cannot hide it.

**Trail** sets how much track is kept, from *Off* up to *24 hours*. Trails are
trimmed by age, not by point count. A 24-hour trail at 1 Hz is 86 400 points
per vehicle; the full history is retained but the drawn line is subsampled to
4 000 vertices, keeping the per-fix redraw near 25 ms.

The **Live positions** table shows each vehicle's easting, northing, depth,
altitude, speed over the ground and the age of the last fix. Targets dim after
5 s without a datagram.

### Notes on the format

* **There are no separators between records.** They run straight together, so
  a field's three decimals running into the next field's digits is the only
  boundary marker. A datagram that begins mid-record is normal, not a fault.
* Because of that, the trailing record in each datagram is held until something
  confirms it — the next datagram, a terminator, or 1.5 s of silence. That
  costs up to ~1 s of latency and is deliberate: a datagram boundary inside a
  coordinate turns `3009049.775` into `300904`, which puts a vehicle 2700 km
  away. A late position beats a wrong one.
* A position outside the grid shows as `off grid` and is not drawn.
* The listener does **not** set `SO_REUSEADDR`. If another program already
  holds the port, binding fails with a message rather than quietly taking the
  datagrams off it. To feed two consumers, have the sender repeat to a second
  port.

### Changing the feeds

Field order lives in two tuples in `bathy3d/feed.py`:

```python
ORDER = ("Vessel", "UHD333", "UHD334", "TMS333", "TMS334")
DEPTH_ORDER = ("UHD333", "UHD334", "TMS333", "TMS334")
```

Add or reorder vehicles there, give each a colour and a kind in
`DEFAULT_TARGETS` (`bathy3d/targets.py`), and pair any new TMS to its ROV in
`TETHERS`. The record lengths follow from the tuples; nothing else needs
changing.

`python -m bathy3d.feed 6451` sniffs the position feed and `python -m
bathy3d.feed 6452` the depth feed, printing each datagram and what it decoded
to. Use `-u` or Python buffers the output and it looks dead.

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
- Shapefile polygons are drawn as outlines, not filled.
- Neither feed carries heading, so no marker has an orientation.
- The vessel has no depth of its own; it sits at the surface or on the bottom.
- No auto-follow: the camera does not track the vehicles as they move.
