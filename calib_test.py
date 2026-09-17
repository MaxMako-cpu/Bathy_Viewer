#!/usr/bin/env python
"""Checks for the depth calibration: tie-in points and the model fitted to them.

Two halves. The first drives ``bathy3d.calib`` as plain arithmetic - one point
gives a fixed offset, two spread points give a line, a bad or cramped pair is
refused - with no Qt in the way. The second brings the real window up, runs
both real UDP listeners, ties in a vehicle the way the operator does, and
checks the marker actually moves onto the grid's seabed and the altitude falls
to zero.

    python calib_test.py [grid.tif]
"""

import math
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import os as _os
_os.environ["BATHY3D_PROFILE"] = "test-calib"
from bathy3d import prefs as _prefs          # noqa: E402
_prefs.settings().clear()

GRID = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:\Users\mkozh\OneDrive\Desktop\bathy\BOEM_bathy_WGS84_UTM15N.tif"
PPORT, DPORT = 6491, 6492

from bathy3d import calib                    # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
    if not cond:
        FAILED.append(name)


def tp(grid_depth, feed_depth, vehicle="UHD333"):
    return calib.TiePoint(vehicle=vehicle, x=0.0, y=0.0,
                          grid_depth=grid_depth, feed_depth=feed_depth)


# --------------------------------------------------------------- the maths

print("fitting, with no Qt involved:")

m = calib.fit([])
check("no points means no correction", not m.on and m.apply(1656.0) == 1656.0)

# One tie-in: the only honest answer is the offset measured right there.
m = calib.fit([tp(1616.85, 1656.08)])
check("one point gives a fixed offset",
      m.kind == "fixed" and abs(m.a - (1616.85 - 1656.08)) < 1e-9,
      f"{m.a:+.3f} m")
check("one point lands the vehicle on the grid",
      abs(m.apply(1656.08) - 1616.85) < 1e-9)
check("one point says it only fixes its own depth",
      "only" in m.note.lower(), m.note)

# Two points a few metres apart in depth carry no slope information: the
# difference in offsets divided by a tiny depth difference is noise amplified.
m = calib.fit([tp(1616.85, 1656.08), tp(1620.11, 1659.40)])
check("a cramped pair refuses to fit a slope", m.kind == "fixed", m.kind)
check("and says why", "span only" in m.note, m.note)

# A pure percentage error: the offset doubles when the depth doubles.
K = 1.0 / 1.024                       # grid = feed / 1.024, i.e. 2.4% too deep
pts = [tp(1000.0 * 1.024 * K, 1000.0 * 1.024), tp(2400.0, 2400.0 / K)]
m = calib.fit(pts)
check("a spread pair fits a line", m.kind == "scale", m.kind)
check("the scale comes back as 2.4%", abs(m.scale_pct - (K - 1) * 100) < 1e-6,
      f"{m.scale_pct:+.4f}%")
check("no fixed part invented", abs(m.a) < 1e-6, f"{m.a:+.6f} m")
check("it predicts an untied depth", abs(m.apply(1700.0) - 1700.0 * K) < 1e-6,
      f"{m.apply(1700.0):,.3f} vs {1700.0 * K:,.3f}")

# Fixed and percentage stacked: the fit has to pull them apart.
A, B = 5.0, 0.976
m = calib.fit([tp(A + B * 1000.0, 1000.0), tp(A + B * 2400.0, 2400.0)])
check("a datum shift is separated from the scale",
      abs(m.a - A) < 1e-6 and abs(m.b - B) < 1e-9,
      f"a {m.a:+.4f} m, b {m.b:.6f}")

# A tie-in taken while the vehicle was still flying poisons the slope. A 15%
# sound-velocity error does not exist, so the fit refuses rather than obeys.
m = calib.fit([tp(1000.0, 1000.0), tp(2400.0, 1800.0)])
check("an impossible scale is refused", m.kind == "fixed", m.kind)
check("and the reason names a bad tie-in", "off bottom" in m.note, m.note)

# Two points and two parameters always fit exactly; three start testing it.
m2 = calib.fit([tp(1000.0 * K, 1000.0), tp(2400.0 * K, 2400.0)])
check("two points report no scatter", m2.rms is None and "third" in m2.note,
      m2.note)
m3 = calib.fit([tp(1000.0 * K, 1000.0), tp(1700.0 * K + 3.0, 1700.0),
                tp(2400.0 * K, 2400.0)])
check("three points report scatter", m3.rms is not None and m3.rms > 0.5,
      f"rms {m3.rms:.2f} m")

check("inside the tied range is not flagged", not m3.outside(1700.0))
check("just outside is tolerated", not m3.outside(2400.0 + 40.0))
check("far outside is flagged", m3.outside(3200.0) and m3.outside(200.0))
check("an unfitted model flags nothing", not calib.fit([]).outside(9999.0))

# Settings outlive versions, so a row that cannot be read costs one point.
p = calib.TiePoint("UHD334", 705939.201, 3006546.099, 1616.85, 1656.082,
                   when=1_700_000_000.0, grid="C:/x/y.tif")
back = calib.TiePoint.decode(p.encode())
check("a tie-in survives the round trip",
      back is not None and back.vehicle == p.vehicle
      and abs(back.feed_depth - p.feed_depth) < 1e-3
      and abs(back.x - p.x) < 1e-3 and back.grid == p.grid)
check("a mangled row decodes to nothing",
      calib.TiePoint.decode("nonsense") is None
      and calib.TiePoint.decode("A|1|2|x|4|5") is None)

c = calib.Calibration(enabled=True)
c.load([p.encode(), "rubbish", ""])
check("loading drops only the bad rows", len(c.points) == 1, str(len(c.points)))
check("enabled plus fitted means active", c.active)
c.clear()
check("no points means not active even when enabled", not c.active)

check("the description reads as a sentence",
      "of depth" in calib.fit(pts).describe(), calib.fit(pts).describe())


# ------------------------------------------------------------ the real window

print("\nlive, through the window and both listeners:")

from PySide6 import QtWidgets                # noqa: E402
from bathy3d.feed import DEPTH_ORDER, ORDER  # noqa: E402
from bathy3d.mainwindow import MainWindow    # noqa: E402

# Two sites far enough apart in depth that the fit is allowed a slope.
SHALLOW = (705939.201, 3006546.099)     # grid ~1617 m
DEEP = (671887.942, 2987490.145)        # grid ~2350 m

app = QtWidgets.QApplication(sys.argv[:1])
win = MainWindow(GRID)
win.resize(1200, 800)
win.show()


def pump(ms):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.004)


for _ in range(600):
    pump(100)
    if win.view.surface is not None:
        break
assert win.view.surface is not None, "grid did not load"
surf = win.view.surface

win.port_s.setValue(PPORT)
win.dport_s.setValue(DPORT)
win.listen_b.setChecked(True)
pump(800)
assert win.feed is not None and win.dfeed is not None, "listeners did not start"

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def send(site, uhd333_depth):
    """Put UHD333 exactly on one site at a chosen depth, the others beside it.

    UHD333 is the vehicle every check below ties in, so it has to sit on the
    coordinate the test probes - a body nudged 3 m away lands in the next cell
    and reads a slightly different seabed.
    """
    e, n = site
    home = ORDER.index("UHD333")
    pos = []
    for i, _ in enumerate(ORDER):
        pos += [e + (i - home) * 3.0, n + (i - home) * 3.0]
    depths = [uhd333_depth, uhd333_depth - 8.0,
              uhd333_depth - 160.0, uhd333_depth - 150.0]
    sock.sendto(",".join(f"{v:.3f}" for v in pos).encode(), ("127.0.0.1", PPORT))
    sock.sendto(",".join(f"{v:.3f}" for v in depths).encode(), ("127.0.0.1", DPORT))
    # The parser holds the trailing record until something confirms it, so a
    # second datagram is what makes the first one land.
    pump(120)
    sock.sendto(b"\n", ("127.0.0.1", PPORT))
    sock.sendto(b"\n", ("127.0.0.1", DPORT))
    # Wait for *this* depth, not merely for some depth: on the second site the
    # previous one is still in hand, and tying in on it would put both points
    # at the same depth and quietly cost the fit its slope.
    for _ in range(40):
        pump(80)
        got = win._depths.get("UHD333")
        here = win._positions.get("UHD333")
        if (got is not None and abs(got[0] - uhd333_depth) < 0.01
                and here is not None and abs(here[0] - e) < 0.01):
            return
    raise AssertionError(f"UHD333 never reached {uhd333_depth:,.2f} m at {site}")


check("calibration starts off", not win.calib.enabled and not win.calib.model.on)
check("the switch is greyed out with no points", not win.act_calib.isEnabled())
check("the depth column is unmarked",
      win.tgt_table.horizontalHeaderItem(3).text() == "Depth")

# --- first tie-in, at the shallow site -------------------------------------

grid_shallow = -surf.probe(*SHALLOW).z
FEED_SHALLOW = grid_shallow * 1.024        # feed reads 2.4% deeper than the grid
send(SHALLOW, FEED_SHALLOW)
check("a position and a depth arrived",
      win._positions.get("UHD333") is not None
      and win._depths.get("UHD333") is not None)

raw_before = win.view.targets.targets["UHD333"].z
check("uncalibrated, the marker sits at the raw feed depth",
      abs(-raw_before - FEED_SHALLOW) < 0.05,
      f"z {raw_before:,.2f} vs feed {-FEED_SHALLOW:,.2f}")

win.tie_in("UHD333")
check("one tie-in recorded", len(win.calib.points) == 1, str(len(win.calib.points)))
pt = win.calib.points[0]
check("it stored the raw feed depth, not a corrected one",
      abs(pt.feed_depth - FEED_SHALLOW) < 0.05, f"{pt.feed_depth:,.2f}")
check("it stored the grid's depth at that spot",
      abs(pt.grid_depth - grid_shallow) < 0.05,
      f"{pt.grid_depth:,.2f} vs probe {grid_shallow:,.2f}")
check("it names the grid it was tied on", pt.grid == GRID, pt.grid)
check("the switch is now available", win.act_calib.isEnabled())
check("but nothing moved until it is switched on",
      abs(win.view.targets.targets["UHD333"].z - raw_before) < 1e-9)

win.act_calib.setChecked(True)
pump(200)
check("switched on, the marker lands on the grid's seabed",
      abs(-win.view.targets.targets["UHD333"].z - grid_shallow) < 0.05,
      f"z {win.view.targets.targets['UHD333'].z:,.2f} vs seabed {-grid_shallow:,.2f}")
row = ORDER.index("UHD333")
check("altitude falls to zero", win.tgt_table.item(row, 4).text() in ("0.0", "-0.0"),
      win.tgt_table.item(row, 4).text())
check("the depth column says it is corrected",
      win.tgt_table.horizontalHeaderItem(3).text() == "Depth*")
check("the table shows the corrected depth",
      abs(float(win.tgt_table.item(row, 3).text().replace(",", "")) - grid_shallow) < 0.1,
      win.tgt_table.item(row, 3).text())

# --- second tie-in, deep, so the fit gets a slope --------------------------

grid_deep = -surf.probe(*DEEP).z
FEED_DEEP = grid_deep * 1.024
check("the two sites are far enough apart in depth",
      abs(FEED_DEEP - FEED_SHALLOW) > calib.MIN_SPREAD_M,
      f"{abs(FEED_DEEP - FEED_SHALLOW):,.0f} m apart")

send(DEEP, FEED_DEEP)
win.tie_in("UHD333")
m = win.calib.model
check("two spread tie-ins fit a line", m.kind == "scale", m.kind)
want_b = (grid_deep - grid_shallow) / (FEED_DEEP - FEED_SHALLOW)
check("the slope matches the line through both points",
      abs(m.b - want_b) < 1e-6, f"b {m.b:.6f} vs {want_b:.6f}")
check("the scale reads as roughly -2.3%", abs(m.scale_pct + 2.34) < 0.1,
      f"{m.scale_pct:+.3f}%")
check("it still lands the deep vehicle on the seabed",
      abs(-win.view.targets.targets["UHD333"].z - grid_deep) < 0.05,
      f"z {win.view.targets.targets['UHD333'].z:,.2f} vs seabed {-grid_deep:,.2f}")

mid = (FEED_SHALLOW + FEED_DEEP) / 2.0
check("an untied depth in between is interpolated, not guessed",
      abs(m.apply(mid) - (m.a + m.b * mid)) < 1e-9)
check("a depth far outside the tied range is flagged",
      m.outside(FEED_SHALLOW - 900.0), f"tied {m.lo:,.0f}-{m.hi:,.0f} m")

# --- what it remembers -----------------------------------------------------

rows = _prefs.tiepoints()
check("both points were written to settings", len(rows) == 2, str(len(rows)))
fresh = calib.Calibration(_prefs.view("calib/on"))
fresh.load(rows)
# Points are stored to the millimetre, so the refitted line is not bit-exact.
# What has to survive is the correction itself, right across the range the
# vehicles work in - a micron of drift in the coefficients is not a fact about
# a seabed the grid has wrong by tens of metres.
drift = max(abs(fresh.model.apply(d) - m.apply(d))
            for d in (FEED_SHALLOW, mid, FEED_DEEP))
check("a restart rebuilds the same correction",
      fresh.model.kind == m.kind and drift < 1e-3,
      f"{fresh.model.kind}, worst drift {drift * 1000:.4f} mm")
check("and remembers it was switched on", fresh.enabled and fresh.active)

# --- dropping a bad point --------------------------------------------------

win.show_calib()
pump(150)
dlg = win._calib_dialog
check("the dialog lists both points", dlg.table.rowCount() == 2,
      str(dlg.table.rowCount()))
check("it shows each point's residual against the fit",
      dlg.table.item(0, 6).text() not in ("", "--"), dlg.table.item(0, 6).text())

win.calib.remove(1)
win._after_calib_change()
pump(150)
check("removing one falls back to a fixed offset",
      win.calib.model.kind == "fixed" and win.calib.model.n == 1,
      f"{win.calib.model.kind}, n {win.calib.model.n}")

win.calib.clear()
win._after_calib_change()
pump(150)
check("clearing every point switches calibration off",
      not win.calib.model.on and not win.act_calib.isChecked()
      and not win.act_calib.isEnabled())
check("and the depth column loses its mark",
      win.tgt_table.horizontalHeaderItem(3).text() == "Depth")
check("the vehicle is back at the raw feed depth",
      abs(-win.view.targets.targets["UHD333"].z - FEED_DEEP) < 0.05,
      f"z {win.view.targets.targets['UHD333'].z:,.2f}")

win.listen_b.setChecked(False)
pump(300)
sock.close()
win.close()
pump(200)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all calibration checks passed")
