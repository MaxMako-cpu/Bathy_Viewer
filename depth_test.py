#!/usr/bin/env python
"""Checks for the five-body feed: positions on one port, depths on another.

Drives both real listeners over loopback with the real wire formats and
confirms each vehicle ends up at its reported depth, the TMS bodies are
cylinders that can be hidden, the tethers join each TMS to its own ROV, and a
depth that stops arriving falls back to the seabed rather than freezing.

    python depth_test.py [grid.tif]
"""

import math
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import os as _os
_os.environ["BATHY3D_PROFILE"] = "test-depth"
from bathy3d import prefs as _prefs          # noqa: E402
_prefs.settings().clear()

GRID = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:\Users\mkozh\OneDrive\Desktop\bathy\BOEM_bathy_WGS84_UTM15N.tif"
PPORT, DPORT = 6481, 6482

from PySide6 import QtCore, QtWidgets        # noqa: E402
from bathy3d.feed import (DEPTH_FIELDS, DEPTH_ORDER, ORDER, POSITION_FIELDS,
                          TETHERS, UMBILICALS, parse_depths,
                          parse_records)  # noqa: E402
from bathy3d.mainwindow import MainWindow, TRAILS   # noqa: E402
from bathy3d.targets import TMS_DIAMETER_M, TMS_HEIGHT_M        # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
    if not cond:
        FAILED.append(name)


# Real positions from the survey PC, with the two TMS pairs appended in
# sequence as agreed: Vessel, ROV1, ROV2, TMS1, TMS2.
BASE = [706148.701, 3006428.410,      # Vessel
        705939.201, 3006546.099,      # ROV1
        706515.275, 3006391.404,      # ROV2
        705941.900, 3006549.300,      # TMS1
        706512.600, 3006388.100]      # TMS2
# Real depths, read off the survey PC: ROV1, ROV2, TMS1, TMS2.
DEPTHS = [1656.082, 1646.926, 1492.150, 1508.260]


def rec(values):
    return ",".join(f"{v:.3f}" for v in values)


print("wire format:")
fixes, _ = parse_records(rec(BASE), stream=False)
check("position record is 10 fields", POSITION_FIELDS == 10, str(POSITION_FIELDS))
check("five bodies decoded", len(fixes) == 1 and len(fixes[0].pos) == 5,
      str(list(fixes[0].pos) if fixes else []))
check("TMS1 read from the right pair",
      fixes[0].pos["TMS1"] == (BASE[6], BASE[7]), str(fixes[0].pos.get("TMS1")))

dfx, _ = parse_depths(rec(DEPTHS), stream=False)
check("depth record is 4 fields", DEPTH_FIELDS == 4, str(DEPTH_FIELDS))
check("depths mapped in order",
      dfx[0].depths == dict(zip(DEPTH_ORDER, DEPTHS)), str(dfx[0].depths))

# glued records, as they actually arrive
glued = rec(BASE) + rec(BASE)
got, carry = parse_records(glued, stream=False)
check("glued position records split", len(got) == 2, f"{len(got)}, carry {len(carry)}")
gluedd = rec(DEPTHS) + rec(DEPTHS) + rec(DEPTHS)
got, _ = parse_depths(gluedd, stream=False)
check("glued depth records split", len(got) == 3, str(len(got)))

check("each TMS sits above its ROV",
      all(dfx[0].depths[t] < dfx[0].depths[r] for r, t in TETHERS.items()),
      ", ".join(f"{t} {dfx[0].depths[t]:.0f} over {r} {dfx[0].depths[r]:.0f}"
                for r, t in TETHERS.items()))

print("\nlive, through both listeners:")
app = QtWidgets.QApplication(sys.argv[:1])
win = MainWindow(GRID)
win.resize(1400, 880)
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

win.port_s.setValue(PPORT)
win.dport_s.setValue(DPORT)
win.listen_b.setChecked(True)
pump(800)
check("both listeners started", win.feed is not None and win.dfeed is not None)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for i in range(6):
    moved = list(BASE)
    for k in range(0, len(moved), 2):
        moved[k] += i * 0.5
        moved[k + 1] -= i * 0.8
    sock.sendto(rec(moved).encode(), ("127.0.0.1", PPORT))
    sock.sendto(rec([d + i * 0.4 for d in DEPTHS]).encode(), ("127.0.0.1", DPORT))
    pump(160)
for _ in range(40):
    pump(80)
    if len(win.view.targets.targets) == len(ORDER) and win.feed.records >= 4:
        break

tg = win.view.targets.targets
check("all five bodies tracked", len(tg) == 5, str(sorted(tg)))
s = win.view.surface

for name in DEPTH_ORDER:
    t = tg.get(name)
    if t is None:
        check(f"{name} tracked", False)
        continue
    want = win._depths.get(name, (None, 0))[0]
    check(f"{name} sits at its reported depth",
          want is not None and abs(-t.z - want) < 1e-6,
          f"z {t.z:,.3f} vs feed depth {want}")

vessel = tg["Vessel"]
p = s.probe(vessel.x, vessel.y)
# A vessel floats. It sends no depth and is drawn at the surface, always -
# there is no longer a setting that puts it on the bottom beneath itself.
check("vessel sits at the sea surface", abs(vessel.z) < 1e-9,
      f"z {vessel.z:,.3f}")
check("and not on the seabed under it", abs(vessel.z - p.z) > 1.0,
      f"seabed {p.z:,.1f}")
check("the vessel's depth reads zero",
      win.tgt_table.item(ORDER.index("Vessel"), 3).text() in ("0.0", "-0.0"),
      win.tgt_table.item(ORDER.index("Vessel"), 3).text())

print("\nheights relative to the seabed:")
for name in DEPTH_ORDER:
    t = tg[name]
    p = s.probe(t.x, t.y)
    alt = (-p.z) - (-t.z)
    print(f"  {name}: depth {-t.z:8,.1f} m   seabed {-p.z:8,.1f} m   alt {alt:8,.1f} m")

for rov, tms in TETHERS.items():
    check(f"{tms} is above {rov}", tg[tms].z > tg[rov].z,
          f"{tg[tms].z:,.1f} vs {tg[rov].z:,.1f}")

print("\nTMS bodies and tethers:")
for name in ("TMS1", "TMS2"):
    check(f"{name} is a cylinder", tg[name].kind == "cylinder", tg[name].kind)
check("TMS is 3 m across and 2 m tall",
      TMS_DIAMETER_M == 3.0 and TMS_HEIGHT_M == 2.0)
actors = win.view.plotter.renderer.actors
for rov in TETHERS:
    check(f"tether drawn for {rov}", f"link:{rov}" in actors,
          str([a for a in actors if a.startswith("link")]))
for tms in UMBILICALS:
    check(f"umbilical drawn from vessel to {tms}", f"link:{tms}" in actors,
          str([a for a in actors if a.startswith("link")]))
check("no drop line hanging off the vessel",
      "stem:Vessel" not in actors,
      str([a for a in actors if a.startswith("stem")]))

win.tms_b.setChecked(False)
pump(300)
vis = actors.get("tgt:TMS1")
check("hiding TMS hides the cylinder", vis is not None and not vis.GetVisibility())
check("hiding TMS removes tethers and umbilicals",
      not any(a.startswith("link") for a in win.view.plotter.renderer.actors),
      str([a for a in win.view.plotter.renderer.actors if a.startswith("link")]))
win.tms_b.setChecked(True)
pump(300)
check("showing TMS brings them back",
      actors.get("tgt:TMS1").GetVisibility()
      and any(a.startswith("link") for a in win.view.plotter.renderer.actors))

print("\nshowing one ROV chain at a time:")
check("there is a button per ROV", set(win.chain_b) == set(DEPTH_ORDER[:2]),
      str(sorted(win.chain_b)))
check("both start selected",
      all(b.isChecked() for b in win.chain_b.values()))

win.chain_b["ROV2"].setChecked(False)
pump(400)
live = win.view.plotter.renderer.actors


def vis(name):
    a = live.get(name)
    return a is not None and bool(a.GetVisibility())


check("the deselected ROV goes", not vis("tgt:ROV2"))
check("and its TMS goes with it", not vis("tgt:TMS2"),
      "half a chain on screen leaves a tether to nothing")
check("its trail goes too", not vis("trail:ROV2"))
check("the selected ROV stays", vis("tgt:ROV1") and vis("tgt:TMS1"))
check("the vessel stays - it belongs to both chains", vis("tgt:Vessel"))
check("the hidden chain's tether and umbilical are gone",
      "link:ROV2" not in live and "link:TMS2" not in live,
      str([a for a in live if a.startswith("link")]))
check("the shown chain keeps both of its links",
      "link:ROV1" in live and "link:TMS1" in live)

hidden_rows = [ORDER[r] for r in range(win.tgt_table.rowCount())
               if win.tgt_table.isRowHidden(r)]
check("the table disregards it too", sorted(hidden_rows) == ["ROV2", "TMS2"],
      str(hidden_rows))
check("and only the shown bodies are framed",
      {t.name for t in win.view.targets.visible_targets()}
      == {"Vessel", "ROV1", "TMS1"},
      str(sorted(t.name for t in win.view.targets.visible_targets())))

# A grid reload rebuilds the whole target layer, so everything configured on
# it has to be put back - this used to lose trail retention, vehicle colours
# and Show TMS as well, silently.
win.view.set_surface(win.view.surface)
win._configure_targets()
pump(300)
check("a grid reload keeps the selection",
      win.view.targets.hidden_chains == {"ROV2"},
      str(win.view.targets.hidden_chains))
check("and keeps the vehicle colours",
      win.view.targets.styles.get("ROV1", {}).get("color") is not None)
check("and the trail retention",
      win.view.targets.trail_seconds == TRAILS.get(win.trail_c.currentText()),
      f"{win.view.targets.trail_seconds:.0f} s")

win.chain_b["ROV2"].setChecked(True)
pump(400)
check("re-selecting brings the whole chain back",
      not win.view.targets.hidden_chains
      and not any(win.tgt_table.isRowHidden(r)
                  for r in range(win.tgt_table.rowCount())))

# Reloading emptied the target layer, so the feed has to refill it before the
# checks below have anything to look at.
for i in range(6):
    moved = list(BASE)
    for k in range(0, len(moved), 2):
        moved[k] += i * 0.5
        moved[k + 1] -= i * 0.8
    sock.sendto(rec(moved).encode(), ("127.0.0.1", PPORT))
    sock.sendto(rec([d + i * 0.4 for d in DEPTHS]).encode(), ("127.0.0.1", DPORT))
    pump(160)
for _ in range(40):
    pump(80)
    if len(win.view.targets.targets) == len(ORDER):
        break
tg = win.view.targets.targets
actors = win.view.plotter.renderer.actors
check("the feed refills the layer after a reload", len(tg) == len(ORDER),
      str(sorted(tg)))

print("\nwhen the depth feed stops:")
row = {win.tgt_table.item(r, 0).text(): r for r in range(win.tgt_table.rowCount())}
before = tg["ROV1"].z
win._depths["ROV1"] = (win._depths["ROV1"][0], time.monotonic() - 99)
sock.sendto(rec(BASE).encode(), ("127.0.0.1", PPORT))
for _ in range(40):
    pump(80)
    if abs(tg["ROV1"].z - before) > 1e-6:
        break
p = s.probe(tg["ROV1"].x, tg["ROV1"].y)
check("a stale depth falls back to the seabed",
      abs(tg["ROV1"].z - p.z) < 1e-6,
      f"z {tg['ROV1'].z:,.1f} vs seabed {p.z:,.1f}")
check("and the marker is dimmed", tg["ROV1"].stale)

sock.close()
win.listen_b.setChecked(False)
pump(600)
check("both listeners stop", win.feed is None and win.dfeed is None)
win.close()
pump(200)
_prefs.settings().clear()

print()
print("FAILED: " + ", ".join(FAILED) if FAILED else "all depth checks passed")
sys.exit(1 if FAILED else 0)
