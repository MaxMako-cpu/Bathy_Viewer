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
from bathy3d.mainwindow import MainWindow    # noqa: E402
from bathy3d.targets import TMS_DIAMETER_M, TMS_HEIGHT_M        # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
    if not cond:
        FAILED.append(name)


# Real positions from the survey PC, with the two TMS pairs appended in
# sequence as agreed: Vessel, UHD333, UHD334, TMS333, TMS334.
BASE = [706148.701, 3006428.410,      # Vessel
        705939.201, 3006546.099,      # UHD333
        706515.275, 3006391.404,      # UHD334
        705941.900, 3006549.300,      # TMS333
        706512.600, 3006388.100]      # TMS334
# Real depths, read off the survey PC: UHD333, UHD334, TMS333, TMS334.
DEPTHS = [1656.082, 1646.926, 1492.150, 1508.260]


def rec(values):
    return ",".join(f"{v:.3f}" for v in values)


print("wire format:")
fixes, _ = parse_records(rec(BASE), stream=False)
check("position record is 10 fields", POSITION_FIELDS == 10, str(POSITION_FIELDS))
check("five bodies decoded", len(fixes) == 1 and len(fixes[0].pos) == 5,
      str(list(fixes[0].pos) if fixes else []))
check("TMS333 read from the right pair",
      fixes[0].pos["TMS333"] == (BASE[6], BASE[7]), str(fixes[0].pos.get("TMS333")))

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
check("vessel is not placed by the depth feed",
      abs(vessel.z - p.z) < 1e-6 or abs(vessel.z) < 1e-9,
      f"z {vessel.z:,.1f}")

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
for name in ("TMS333", "TMS334"):
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
vis = actors.get("tgt:TMS333")
check("hiding TMS hides the cylinder", vis is not None and not vis.GetVisibility())
check("hiding TMS removes tethers and umbilicals",
      not any(a.startswith("link") for a in win.view.plotter.renderer.actors),
      str([a for a in win.view.plotter.renderer.actors if a.startswith("link")]))
win.tms_b.setChecked(True)
pump(300)
check("showing TMS brings them back",
      actors.get("tgt:TMS333").GetVisibility()
      and any(a.startswith("link") for a in win.view.plotter.renderer.actors))

print("\nwhen the depth feed stops:")
row = {win.tgt_table.item(r, 0).text(): r for r in range(win.tgt_table.rowCount())}
before = tg["UHD333"].z
win._depths["UHD333"] = (win._depths["UHD333"][0], time.monotonic() - 99)
sock.sendto(rec(BASE).encode(), ("127.0.0.1", PPORT))
for _ in range(40):
    pump(80)
    if abs(tg["UHD333"].z - before) > 1e-6:
        break
p = s.probe(tg["UHD333"].x, tg["UHD333"].y)
check("a stale depth falls back to the seabed",
      abs(tg["UHD333"].z - p.z) < 1e-6,
      f"z {tg['UHD333'].z:,.1f} vs seabed {p.z:,.1f}")
check("and the marker is dimmed", tg["UHD333"].stale)

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
