#!/usr/bin/env python
"""Checks for node slide prediction and the case database.

Two halves. The first drives bathy3d.nodes as arithmetic on a synthetic slope
whose answer is known by construction - a plane tilted a chosen amount in a
chosen direction, so a trace down it must run that way and stop where the plane
flattens. The second opens and closes real cases through the window.

    python nodes_test.py [grid.tif]
"""

import math
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import os as _os
_os.environ["BATHY3D_PROFILE"] = "test-nodes"
from bathy3d import prefs as _prefs          # noqa: E402
_prefs.settings().clear()
if os.path.isfile(_prefs.slide_db()):
    os.remove(_prefs.slide_db())

GRID = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:\Users\mkozh\OneDrive\Desktop\bathy\BOEM_bathy_WGS84_UTM15N.tif"
PPORT, DPORT = 6497, 6498

from bathy3d import nodes                    # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
    if not cond:
        FAILED.append(name)


def case(placed_slope, found_slope, runout=40.0, track_off=0.0, aspect=90.0):
    """A confirmed case, built so its runout and track error are known."""
    brg = math.radians(aspect + track_off)
    return nodes.SlideCase(
        rov="ROV1", placed_x=0.0, placed_y=0.0, placed_z=-1000.0,
        placed_slope=placed_slope, placed_aspect=aspect,
        found_x=runout * math.sin(brg), found_y=runout * math.cos(brg),
        found_z=-1010.0, found_slope=found_slope, found_at=time.time())


# ----------------------------------------------------------------- the maths

print("what the cases say, with no Qt involved:")

m = nodes.fit([])
check("no cases means the stated default",
      m.n == 0 and m.arrest_deg == nodes.DEFAULT_ARREST_DEG
      and m.spread_deg == nodes.DEFAULT_SPREAD_DEG)
check("and it says so rather than pretending",
      "no recovered cases" in m.describe().lower(), m.describe()[:70])
check("no cases is provisional", m.provisional)

# One case measures both thresholds at once: it started on 20 and stopped on
# 10. These are two different angles, not one with error bars - a node sets
# off on steeper ground than it comes to rest on - so the arrest angle is the
# one it was seen to stop at, not the middle of the pair.
m = nodes.fit([case(20.0, 10.0)])
check("one case brackets both thresholds",
      abs(m.lo - 10.0) < 1e-9 and abs(m.hi - 20.0) < 1e-9,
      f"stopped on {m.lo:.1f}, started by {m.hi:.1f}")
check("and the arrest angle is the one it stopped at",
      abs(m.arrest_deg - 10.0) < 1e-9, f"{m.arrest_deg:.2f}")
check("it is consistent", m.consistent)

# More cases close the bracket from both sides.
m = nodes.fit([case(20.0, 10.0), case(17.0, 12.0), case(25.0, 11.0)])
check("more cases narrow it",
      abs(m.lo - 12.0) < 1e-9 and abs(m.hi - 17.0) < 1e-9,
      f"{m.lo:.1f}-{m.hi:.1f} from 3 cases")
check("three cases is no longer provisional", not m.provisional)

# A node that stopped on steeper ground than another started on cannot be
# explained by one threshold, and saying so beats averaging it away.
m = nodes.fit([case(12.0, 8.0), case(30.0, 20.0)])
check("contradictory cases are flagged", not m.consistent,
      f"{m.lo:.1f} stopped vs {m.hi:.1f} started")
check("and the reason is stated", "disagree" in m.note.lower(), m.note[:60])

# Corridor width is measured from how far real tracks missed the grid.
m = nodes.fit([case(20.0, 10.0, track_off=0.0),
               case(20.0, 10.0, track_off=3.0)])
check("tight tracks still get the floor width",
      abs(m.spread_deg - nodes.MIN_SPREAD_DEG) < 1e-6, f"{m.spread_deg:.1f}")
m = nodes.fit([case(20.0, 10.0, track_off=40.0),
               case(20.0, 10.0, track_off=-5.0)])
check("a badly missed track widens the corridor", m.spread_deg >= 40.0,
      f"{m.spread_deg:.1f} deg")

c = case(20.0, 10.0, runout=63.0, track_off=25.0, aspect=90.0)
check("runout is measured from the two positions", abs(c.runout - 63.0) < 1e-6,
      f"{c.runout:.2f} m")
check("track bearing comes out right", abs(c.track - 115.0) < 1e-6,
      f"{c.track:.2f}")
check("track error is the miss against the grid",
      abs(c.track_error - 25.0) < 1e-6, f"{c.track_error:+.2f}")
check("an unfound case measures nothing",
      not nodes.SlideCase(rov="ROV1", placed_x=0, placed_y=0).confirmed)

check("bearings wrap the short way",
      abs(nodes.angle_diff(5.0, 355.0) - 10.0) < 1e-9
      and abs(nodes.angle_diff(355.0, 5.0) + 10.0) < 1e-9)


# -------------------------------------------------- tracing a known hillside

print("\ntracing a slope whose answer is known:")


class Plane:
    """A synthetic hillside: constant slope towards a chosen bearing, then flat.

    Everything about a trace down it is known in advance, which is the point -
    a real grid can only be checked for plausibility.
    """

    native_cell_m = 10.0

    def __init__(self, slope_deg, aspect_deg, flat_after=100.0):
        self.slope, self.aspect, self.flat_after = (slope_deg, aspect_deg,
                                                    flat_after)
        self._g = math.tan(math.radians(slope_deg))

    def probe(self, x, y):
        brg = math.radians(self.aspect)
        # distance travelled in the downslope direction
        d = x * math.sin(brg) + y * math.cos(brg)
        if d >= self.flat_after:
            z, slope, aspect = (-self._g * self.flat_after, 0.0, float("nan"))
        else:
            z, slope, aspect = (-self._g * max(d, 0.0), self.slope, self.aspect)
        return type("P", (), {"x": x, "y": y, "z": z, "slope": slope,
                              "aspect": aspect})()


hill = Plane(20.0, 90.0, flat_after=100.0)      # 20 deg, falling due east
path, why = nodes.trace(hill, 0.0, 0.0, arrest_deg=15.0)
check("a trace runs downhill", len(path) > 2, f"{len(path)} points")
end = path[-1]
check("it goes the way the slope falls",
      abs(end[0] - 100.0) < 6.0 and abs(end[1]) < 1e-6,
      f"ended {end[0]:.1f}E {end[1]:.1f}N")
check("it stops where the slope eases", "eased" in why, why)
check("and never climbs",
      all(path[i][2] <= path[i - 1][2] + 1e-6 for i in range(1, len(path))))

# Ground below the threshold should not move the node at all.
flat = Plane(6.0, 90.0, flat_after=500.0)
path, why = nodes.trace(flat, 0.0, 0.0, arrest_deg=15.0)
check("ground under the threshold produces no slide", len(path) == 1, why)

steep = Plane(30.0, 200.0, flat_after=10_000.0)
path, why = nodes.trace(steep, 0.0, 0.0, arrest_deg=15.0)
run = math.hypot(path[-1][0], path[-1][1])
check("an endless slope is capped rather than run forever",
      run <= nodes.MAX_RUNOUT_M + 20.0 and "limit" in why, f"{run:,.0f} m, {why}")

path, _ = nodes.trace(hill, 0.0, 0.0, arrest_deg=15.0, start_dir=270.0)
check("an observed direction steers the first step then terrain takes over",
      len(path) > 2 and path[1][0] < 0.0 and path[-1][0] > path[1][0],
      f"first step {path[1][0]:+.1f}E, ended {path[-1][0]:+.1f}E")

check("no grid means no trace", nodes.trace(None, 0, 0, 15.0)[0] == [])

# Pressing "node slid" says the threshold is at most the ground it left. A
# model that refuses to predict on the very ground a node just slid off is
# contradicted by the event that prompted it - and three quarters of this grid
# is under 5 degrees, so a fixed 15 degree default predicted nothing at all
# across most of where the work happens. That was the bug.
empty = nodes.fit([])
eff = nodes.effective_arrest(empty, 6.36)
check("a gentle placement still gets a usable arrest angle",
      0.0 < eff < 6.36, f"{eff:.2f} deg from 6.36 deg ground")
check("and it is the stated fraction of it",
      abs(eff - 6.36 * nodes.ARREST_FRACTION) < 1e-9, f"{eff:.3f}")
check("a node never arrests on the ground it just left",
      nodes.effective_arrest(nodes.fit([case(3.0, 2.0)]), 2.5) < 2.5,
      f"{nodes.effective_arrest(nodes.fit([case(3.0, 2.0)]), 2.5):.2f}")
check("with cases, the measured stopping angle is used",
      abs(nodes.effective_arrest(nodes.fit([case(30.0, 11.0)]), 25.0)
          - 11.0) < 1e-9)
check("flat ground has no arrest angle to find",
      nodes.effective_arrest(empty, float("nan")) == empty.arrest_deg)

gentle = Plane(6.0, 90.0, flat_after=200.0)
path, why = nodes.trace(gentle, 0.0, 0.0,
                        nodes.effective_arrest(empty, 6.0))
check("so a node does slide on gentle ground once it is known to have slid",
      len(path) > 2, f"{len(path)} points, {why}")

left, right = nodes.corridor([(0, 0, 0), (50, 0, -1), (100, 0, -2)], 20.0)
check("the corridor opens with distance",
      len(left) == 3 and abs(left[0][1]) >= 4.9
      and abs(left[-1][1]) > abs(left[1][1]),
      f"{abs(left[0][1]):.1f} -> {abs(left[-1][1]):.1f} m off centre")
check("and is symmetric about the path",
      abs(left[-1][1] + right[-1][1]) < 1e-6)
check("a corridor needs a path", nodes.corridor([(0, 0, 0)], 20.0) == ([], []))


# ------------------------------------------------------------- the database

print("\nthe case database:")

db = os.path.join(os.path.dirname(_prefs.slide_db()), "nodes_test_db.json")
if os.path.isfile(db):
    os.remove(db)
made = [case(20.0, 10.0, runout=35.0), case(18.0, 12.0, runout=61.0)]
nodes.save_cases(db, made)
back = nodes.load_cases(db)
check("cases survive the round trip", len(back) == 2, str(len(back)))
check("with their numbers intact",
      abs(back[0].runout - 35.0) < 1e-3
      and abs(back[1].found_slope - 12.0) < 1e-9)
check("and refit to the same model",
      abs(nodes.fit(back).arrest_deg - nodes.fit(made).arrest_deg) < 1e-9)
check("a missing database is an empty history, not an error",
      nodes.load_cases(db + ".nope") == [])
with open(db + ".bad", "w", encoding="utf-8") as fh:
    fh.write("{ this is not json")
check("nor is an unreadable one", nodes.load_cases(db + ".bad") == [])
check("a malformed row is dropped, good ones kept",
      len(nodes.load_cases.__call__(db)) == 2)
check("a row with no vehicle is refused",
      nodes.SlideCase.from_dict({"placed_x": 1}) is None)
check("and an unknown field does not stop the rest",
      nodes.SlideCase.from_dict(
          {"rov": "ROV1", "placed_x": 1.0, "placed_y": 2.0,
           "from_a_later_version": True}) is not None)

csv = nodes.to_csv(made)
lines = csv.strip().splitlines()
check("csv has a header and a row per case", len(lines) == 3, str(len(lines)))
check("and carries what an analyst would want",
      "runout_m" in lines[0] and "track_error_deg" in lines[0]
      and "placed_slope_deg" in lines[0])
for f in (db, db + ".bad"):
    if os.path.isfile(f):
        os.remove(f)


# ------------------------------------------------------------- in the window

print("\nthrough the real window:")

from PySide6 import QtWidgets                # noqa: E402
from bathy3d.feed import BOTTOM_ORDER, ORDER  # noqa: E402
from bathy3d.mainwindow import MainWindow    # noqa: E402

app = QtWidgets.QApplication(sys.argv[:1])
win = MainWindow(GRID)
win.resize(1300, 850)
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

check("the menu bar has Nodes",
      any("Nodes" in a.text() for a in win.menuBar().actions()),
      str([a.text() for a in win.menuBar().actions()]))
check("tracking starts off", not win.act_nodes.isChecked())
check("so the slid buttons are greyed",
      not any(a.isEnabled() for a in win.act_slid.values()))
check("there is one per ROV that can place a node",
      set(win.act_slid) == set(BOTTOM_ORDER), str(sorted(win.act_slid)))

win.act_nodes.setChecked(True)
pump(200)
check("switching on enables them",
      all(a.isEnabled() for a in win.act_slid.values()))

# Put ROV1 somewhere genuinely steep, so a slide is plausible.
# Genuinely steep ground, or the node correctly refuses to move and there is
# nothing to test. A coarse sweep of the whole grid, not a few guesses: the
# first version of this test picked 9.5 deg ground and then complained that no
# corridor was drawn, which was the app being right.
best = None
for fr in [i / 24.0 for i in range(1, 24)]:
    for fc in [i / 24.0 for i in range(1, 24)]:
        x, y = surf.crs_from_rowcol(surf.height * fr, surf.width * fc)
        p = surf.probe(x, y)
        if p and math.isfinite(p.slope) and (best is None or p.slope > best[2]):
            best = (x, y, p.slope)
assert best is not None, "found no usable ground at all"
STEEP = (best[0], best[1])
print(f"    placing on {best[2]:.1f} deg ground")
check("the test found ground steep enough for a node to slide",
      best[2] > nodes.DEFAULT_ARREST_DEG, f"{best[2]:.1f} deg")

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
win.port_s.setValue(PPORT)
win.dport_s.setValue(DPORT)
win.listen_b.setChecked(True)
pump(800)


def put(x, y):
    home = ORDER.index("ROV1")
    pos = []
    for i, _ in enumerate(ORDER):
        pos += [x + (i - home) * 3.0, y + (i - home) * 3.0]
    for _ in range(2):
        sock.sendto(",".join(f"{v:.3f}" for v in pos).encode(),
                    ("127.0.0.1", PPORT))
        sock.sendto(b"1656.082,1646.926,1492.150,1508.260",
                    ("127.0.0.1", DPORT))
        pump(150)
    for _ in range(40):
        pump(80)
        here = win._positions.get("ROV1")
        if here and abs(here[0] - x) < 0.01:
            return
    raise AssertionError("ROV1 never reached the test position")


put(*STEEP)
check("ROV1 is on the grid", win._positions.get("ROV1") is not None)

# Open a case without the dialog blocking the test.
p = surf.probe(*STEEP)
opened = nodes.SlideCase(
    rov="ROV1", placed_x=STEEP[0], placed_y=STEEP[1], placed_z=p.z,
    placed_slope=p.slope, placed_aspect=p.aspect,
    observed_dir=float("nan"), grid=win._path or "")
win.slide_open["ROV1"] = opened
win._draw_case("ROV1")
pump(300)
check("a case is open", "ROV1" in win.slide_open)
actors = set(win.view.plotter.renderer.actors)
check("the corridor is drawn on the seabed",
      "slide:ROV1" in actors and "slidel:ROV1" in actors
      and "slider:ROV1" in actors,
      str(sorted(a for a in actors if "slide" in a)))

path, left, right, why = win._predict(opened)
check("the prediction runs downhill from the placement", len(path) >= 2, why)
check("and starts exactly where the node was put",
      abs(path[0][0] - STEEP[0]) < 1e-6 and abs(path[0][1] - STEEP[1]) < 1e-6)

# The real complaint: on ordinary working ground nothing was drawn at all, and
# nothing was said either. Both halves of that are checked here.
GENTLE = None
for fr in [i / 24.0 for i in range(1, 24)]:
    for fc in [i / 24.0 for i in range(1, 24)]:
        gx, gy = surf.crs_from_rowcol(surf.height * fr, surf.width * fc)
        gp = surf.probe(gx, gy)
        if gp and math.isfinite(gp.slope) and 3.0 < gp.slope < 8.0:
            GENTLE = (gx, gy, gp.slope)
            break
    if GENTLE:
        break
assert GENTLE, "no gentle ground found on this grid"
print(f"    and a gentle case on {GENTLE[2]:.1f} deg ground")
win.slide_open.pop("ROV1", None)
gp = surf.probe(GENTLE[0], GENTLE[1])
soft = nodes.SlideCase(
    rov="ROV1", placed_x=GENTLE[0], placed_y=GENTLE[1], placed_z=gp.z,
    placed_slope=gp.slope, placed_aspect=gp.aspect, grid=win._path or "")
win.slide_open["ROV1"] = soft
win.statusBar().clearMessage()
win._draw_case("ROV1")
pump(300)
gpath, _gl, _gr, gwhy = win._predict(soft)
check("gentle working ground still gets a corridor",
      len(gpath) >= 2, f"{len(gpath)} points on {GENTLE[2]:.1f} deg, {gwhy}")
check("and it is drawn",
      "slide:ROV1" in set(win.view.plotter.renderer.actors))
check("and the operator is told what happened",
      "corridor" in win.statusBar().currentMessage().lower(),
      win.statusBar().currentMessage()[:90])
win.node_cancel("ROV1")
pump(150)
win.slide_open["ROV1"] = opened
win._draw_case("ROV1")
pump(200)

# Recover it somewhere along the predicted path, as a pilot would.
mid = path[min(len(path) - 1, max(1, len(path) // 2))]
put(mid[0], mid[1])
win.show_nodes()
pump(200)
before = len(win.slide_cases)
win.node_found("ROV1")
pump(300)
check("recovering closes the case", "ROV1" not in win.slide_open)
check("and files it in the database", len(win.slide_cases) == before + 1)
check("the corridor is taken off the scene",
      "slide:ROV1" not in set(win.view.plotter.renderer.actors))
rec = win.slide_cases[-1]
check("the recovery position was recorded",
      abs(rec.found_x - mid[0]) < 0.05 and math.isfinite(rec.found_slope),
      f"{rec.runout:,.1f} m out, stopped on {rec.found_slope:.1f} deg")
check("the model has learned from it", win.slide_model.n == 1,
      win.slide_model.describe()[:80])
check("it was written to disk",
      len(nodes.load_cases(win.slide_db)) == 1, win.slide_db)

# Cancelling must record nothing at all.
put(*STEEP)
win.slide_open["ROV1"] = opened
win._draw_case("ROV1")
pump(200)
n_before = len(win.slide_cases)
win.node_cancel("ROV1")
pump(200)
check("cancelling closes the case", "ROV1" not in win.slide_open)
check("and records nothing", len(win.slide_cases) == n_before)
check("and clears the drawing",
      "slide:ROV1" not in set(win.view.plotter.renderer.actors))

# Renaming a vehicle must follow through to the menu entries.
win.fleet.set_label("ROV1", "Hercules")
win.apply_fleet()
pump(200)
check("the slid entry follows a rename",
      "Hercules" in win.act_slid["ROV1"].text(), win.act_slid["ROV1"].text())

# A second run must find the case waiting.
win.listen_b.setChecked(False)
pump(300)
sock.close()
win.close()
pump(400)

win2 = MainWindow(GRID)
win2.show()
for _ in range(600):
    pump(100)
    if win2.view.surface is not None:
        break
check("the database survives a restart", len(win2.slide_cases) == 1,
      str(len(win2.slide_cases)))
check("and the model comes back with it", win2.slide_model.n == 1,
      win2.slide_model.describe()[:80])
check("tracking is remembered as on", win2.act_nodes.isChecked())
win2.close()
pump(200)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all node slide checks passed")
