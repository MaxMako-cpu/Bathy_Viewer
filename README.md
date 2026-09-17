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

Geometry is built in **local metres** relative to the raster centre, so the
projection does not matter. Verified on synthetic grids in UTM 15N, 16N, 31N,
15S and 56S and in geographic WGS 84: cell size, depth, slope, latitude and
longitude, distance, bearing, the slope box and nodata all come out right in
each (`crs_test.py`). A geographic CRS works too — spacing is converted at the
centre latitude and distances fall back to geodesic.

The two cell sizes are kept **separate**, because they are not always equal: at
27.5 N a 0.0001 degree cell is 9.88 m across and 11.08 m tall. Sharing one
figure between the axes reported a real 13.26 degree slope as 8.70. Square-cell
grids, which is every UTM, are unaffected either way.

**The feeds are the part that is not CRS-aware.** Positions arrive as bare
eastings and northings with nothing to say which zone they are in, and are read
in the loaded grid's CRS. A UTM 15N easting is a perfectly valid UTM 16N
easting, so a mismatch between the feed and the grid places vehicles somewhere
plausible and wrong. The same applies to a shapefile with no `.prj`. Vertical exaggeration is applied
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
slopebox_test.py  slope box: native resolution, halo, picking, limits
crs_test.py       other projections: UTM 16N/31N/15S/56S and geographic
calib_test.py     depth calibration: the fit, then tie-ins through the window
vehicles_test.py  renaming: labels, colours, and that no slot moved
bathy3d/
  raster.py       GeoTIFF -> Surface; probe grid, display grid, CRS maths
  viewer.py       PyVista/VTK scene: mesh, hillshade, picking, measuring
  measure.py      stations, legs, totals, CSV  (no VTK)
  targets.py      vessel / ROV markers, trails, drop lines
  feed.py         UDP listener + record parser
  vectors.py      shapefile reader; reproject, densify, drape
  calib.py        depth tie-in points and the correction fitted to them
  vehicles.py     per-vessel names and colours, keyed on the feed slots
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

Vehicle names and colours come back, and so do depth calibration tie-in
points, along with whether the correction was switched on — though a
remembered "on" with no points left unticks itself rather than showing a
switch that corrects nothing.

Settings live in QSettings — the registry on Windows — so there is no file to
mislay. Setting `BATHY3D_PROFILE` puts a run in its own settings profile; every
test suite does that and wipes it on entry, so tests neither read nor overwrite
real preferences and cannot leave state behind for one another.

## Slope box

Turn on **Slope box** in the Pointer panel and click the seabed. Slope inside
that rectangle is recomputed from the **probe grid at its native resolution**
and drawn over the terrain on its own colour scale, with the statistics beside
it: mean, p95 and maximum slope, relief, depth range, and the fraction steeper
than a threshold you set.

This exists because the display mesh cannot hold the answer. At the Medium
setting a 200 m box is about **4 cells**; at native resolution it is **272**.
Everything between those is currently averaged into one number.

*Box size* takes 200 m, 500 m, 1 km, 2 km, 5 km, or *Two corners* for a free
rectangle — the first click sets one corner, the second the opposite one.
Only one box at a time.

It is cheap enough to feel instant: a 200 m box is 16 x 16 cells, a 10 km box
0.67 M cells and 29 ms. Boxes over 6 M cells are refused rather than attempted.

The box is a sheet of seabed, so it is drawn *under* everything that belongs on
the seabed - shapefile overlays, vehicles, tethers, measuring stations - and
only just clear of the terrain itself. Those layers each carry a depth bias
(`ON_TOP_SHEET` and `ON_TOP_MARK` in `targets.py`); giving them all the same
one is what made the box bury its own contents.

Two details worth knowing. The window is read with a **one-cell halo**, so slope
at the very edge of the box comes from real neighbours rather than a clamped
short baseline. And the patch sits at **true elevations while the terrain around
it is block-averaged**, so expect a small step at the boundary — typically a
metre or two, more on steep ground. That step is the decimation error made
visible, not a drawing fault.

The box and the **cursor readout** both use Horn's 8-neighbour method, the one
ArcGIS and GDAL use, on the same native grid — so hovering inside a box reports
exactly what the box is coloured by. Verified cell by cell across a box: worst
slope difference 0.0005 degrees, worst aspect difference 0.007 degrees, which
is float32 against float64 arithmetic rather than any difference of method.

That agreement is the point of the feature. The readout was already at native
resolution, but it used a two-point central difference, which matches Horn to
0.014 degrees on average and diverges by up to **9.6 degrees** on the steep,
noisy cells — exactly the ones worth opening a box over.

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
overhangs the grid loads with the part that fits rather than failing.

Overlays survive a grid reload. Changing **Mesh detail** reloads the file, and
anything draped on the terrain goes with it, so each layer's path and colour
are remembered and re-draped onto the new surface automatically. Depths come
from the native-resolution probe, which Mesh detail does not touch, so they
land in exactly the same places. Layers that fall outside a newly opened grid
are dropped with a line in the status bar naming them, rather than a dialog per
file. Requires `pyshp`.

## Live positions and depths over UDP

**Position feed** panel: set the two ports and press *Start listening*. A grid
must be open first, since positions are placed on the terrain.

Positions (default **6451**), about 1 Hz:

```
706148.701,3006428.410,705939.201,3006546.099,706515.275,3006391.404,705941.900,3006549.300,706512.600,3006388.100
|__ Vessel E/N __| |__ ROV1 E/N __| |__ ROV2 E/N __| |__ TMS1 E/N __| |__ TMS2 E/N __|
```

Depths (default **6452**), metres below the surface, positive down:

```
1656.082,1646.926,1492.150,1508.260
| ROV1 | ROV2 | TMS1 | TMS2
```

Eastings and northings are read in **the loaded grid's CRS** — no transform, so
the feed and the grid must agree (UTM 15N for the BOEM file).

| Body | Drawn as |
| --- | --- |
| Vessel | magenta dot, at the surface or on the bottom beneath itself |
| ROV1 / ROV2 | red and green dots, at their reported depth |
| TMS1 / TMS2 | darker red and green cylinders, 3 m × 2 m, at their depth |

Thin dotted lines run the length of the chain: an **umbilical** from the vessel
down to each TMS, and a **tether** from each TMS down to its own ROV. **Show
TMS** hides the cylinders and both sets of lines together.

A drop line runs from each subsea body to the seabed beneath it, so height off
bottom reads at a glance. The vessel has none — it floats over a kilometre and
a half of water, so a line to the bottom says nothing and runs the height of
the scene. Its umbilicals do the connecting instead.

For the umbilicals to read correctly the vessel wants to be at the surface:
turn on *Vessel at sea surface*, or it is drawn on the bottom beneath itself
and its umbilicals run upward.

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
ORDER = ("Vessel", "ROV1", "ROV2", "TMS1", "TMS2")
DEPTH_ORDER = ("ROV1", "ROV2", "TMS1", "TMS2")
```

Add or reorder vehicles there, give each a colour and a kind in
`DEFAULT_TARGETS` (`bathy3d/targets.py`), and pair any new TMS to its ROV in
`TETHERS`. The record lengths follow from the tuples, and so does
`BOTTOM_ORDER` — the bodies offered a calibration tie-in, being those that
carry a depth and are not a TMS. Nothing else needs changing.

`python -m bathy3d.feed 6451` sniffs the position feed and `python -m
bathy3d.feed 6452` the depth feed, printing each datagram and what it decoded
to. Use `-u` or Python buffers the output and it looks dead.

## Naming the vehicles

The app moves between vessels, and every vessel calls its vehicles something
different. **Vehicles › Names and colours…** renames each body and sets the
colour it is drawn in — markers, labels, trails, tethers, the Live positions
table and the calibration dialog all follow.

The defaults are deliberately generic — `ROV1`, `ROV2`, `TMS1`, `TMS2` — because
no two vessels use the same names. Set yours once and they are remembered.

**A rename is only a rename.** The names in `feed.ORDER` are not really names,
they are *slots* — one per pair of fields in the wire record — and the program
is keyed on them throughout: `TETHERS` pairs a TMS to its ROV by slot, the
vessel is recognised by slot, every actor in the scene is named after one, and
a calibration tie-in stores the slot it was taken on, on disk, outliving the
session. So the slot never changes; what changes is the label drawn over it.
Tie in as ROV1, rename it Hercules, and the point still reads correctly —
under the new name.

A **TMS is not given a colour.** It inherits its ROV's, darkened, which is
what keeps the pairing readable when the two bodies are metres apart, and it
cannot fall out of step because there is only ever one colour to set. Its
swatch in the dialog is disabled and says which vehicle to set instead.

The two pairs this shipped with were picked by eye and follow no single rule —
the red dropped its saturation to 0.63 of the ROV's while the green kept it,
and their lightness ratios were 0.78 and 0.69. Only the darkening is common to
both, so only the darkening is applied (`TMS_DARKEN`, lightness × 0.70, in the
same hue): it is the part that works for any colour rather than for those two.
Expect a shade near the original pairs, not identical to them.

Names are capped at 24 characters and tidied of stray whitespace; blank puts
the slot's own name back. **Reset to default names** returns everything to
stock. Only what differs from stock is stored, so an untouched fleet writes
nothing, and no TMS colour is ever stored — it is derived every time it is
asked for.

The number of bodies is fixed at five, because the wire format is: ten
position fields and four depth fields. Renaming does not change that.

The slots were once one vessel's own vehicle names (`UHD333` and friends).
Settings written then — calibration tie-ins especially, which outlive a
session — still carry them, so `feed.LEGACY_SLOTS` maps them forward on the
way in rather than letting them orphan.

## Depth calibration

A vehicle sitting on the bottom draws well clear of a preplot draped on the
same spot. That is not a drawing fault — the two get their depth from
unrelated places:

| | Where its depth comes from |
| --- | --- |
| Preplot / overlay | X and Y only; the depth is looked up from **the grid** |
| Vehicle | `-depth` straight from **the pressure feed** |

Neither number is a measurement of the seabed. The grid's is a conversion from
seismic two-way time through an assumed sound velocity; the vehicle's is a
conversion from pressure through an assumed water density. On the BOEM grid
they disagree by tens of metres, and vertical exaggeration multiplies whatever
gap is left — at the default 6x a real 4 m gap draws as 24 m.

**Calibration › Tie in \<vehicle\>** records one moment when the vehicle was
certainly on the bottom: what the grid said, what the feed said, and where.
The difference is the error, there, at that depth. **Apply depth calibration**
switches the correction on; it starts off and stays off until asked, because a
correction applied unbidden silently moves every vehicle.

Only the **UHDs** are offered. A TMS carries a depth, but it hangs off the
umbilical in mid-water and never lands, so it can never witness the seabed and
its depth ties to nothing. The list comes from `BOTTOM_ORDER` in `feed.py`,
which is derived from `DEPTH_ORDER` minus everything named in `TETHERS` — add
a vehicle to the feed tuples and it appears here on its own. The correction
fitted from the UHDs is applied to **every** vehicle, TMS included: the error
belongs to the two depth scales, not to any one body.

### Why one tie-in is not enough

The error is usually a **percentage**, not a fixed number of metres. A 2.4%
velocity difference is 21 m at 900 m and 59 m at 2450 m — and this grid spans
886 m to 2459 m. Tie in once at 1600 m and sail to the shallow edge and you
have swapped one wrong picture for another.

So the correction is fitted, not stored flat:

* **One point** — a fixed offset, and the dialog says it only fixes its own
  depth.
* **Two or more spread over 200 m of depth** — a line through them:
  `corrected = a + b x feed`. The part that does not change with depth (`a`)
  is a datum or sensor-zero difference; the part that grows with it (`b`) is
  velocity. The fit separates them without anyone deciding which is which.
* **Two or more that are *not* spread** — a plain average offset, and it says
  why. Points 15 m apart in depth carry no slope information; dividing their
  difference in offsets by a tiny difference in depth turns centimetres of
  noise into a wild percentage.

Three guards, because this is a correction applied to live positions:

* A fitted scale beyond **15%** is refused and the average offset used. A
  sound-velocity assumption is not 15% wrong; a fit that says so is being
  driven by a tie-in taken while the vehicle was still flying.
* **Two points always fit a line exactly**, whatever the data, so no scatter
  is reported until the third. From three on, the scatter is the part of the
  grid's error that is *not* smooth — a metre or two means the correction
  holds across the grid, fifteen means the grid is lumpy here and no offset or
  scale will save it.
* Outside the depth range anything was tied in at, the correction is
  extrapolating. The **Depth** column turns amber and says so.

**Calibration › Tie-in points…** lists every point with its position, the two
depths, the difference, and its residual against the fit — a point far off its
own line turns red, and that is the one to suspect. Delete it and the fit
follows. Clearing the last point unticks the switch.

When the correction is on, the table header reads **Depth\*** and its tooltip
gives the model. The corrected depth drives everything — marker, drop line,
altitude and table — so they cannot disagree.

Two things it does not do. It corrects the smooth, predictable part of the
error only: where the grid is simply wrong about the *shape* of the ground, a
canyon it smoothed over, no offset or scale helps. And it does not touch the
overlays — a preplot stays on the grid, because the grid is what it was draped
on. Calibration moves the vehicle onto the grid's seabed, not the seabed onto
the vehicle.

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


```
C:\Users\<you>\.venvs\bathy3d\Scripts\python.exe calib_test.py
```

Drives the fit as plain arithmetic — one point, a spread pair, a cramped pair,
an impossible scale, scatter from three — then ties in a vehicle through the
real window at two sites 752 m apart in depth and checks the marker lands on
the grid's seabed with the altitude at zero.

## Known limits

- Rotated / sheared rasters are rejected — reproject north-up first.
- Band 1 only on multi-band files.
- No contour overlay yet, and no depth-profile plot along the measured line.
- Shapefile polygons are drawn as outlines, not filled.
- Neither feed carries heading, so no marker has an orientation.
- The vessel has no depth of its own; it sits at the surface or on the bottom.
- Depth calibration corrects the vehicles, never the grid or the overlays
  draped on it, and only the smooth part of the grid's error.
