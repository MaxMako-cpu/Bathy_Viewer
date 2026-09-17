#!/usr/bin/env python
"""Checks for the UDP feeds' framing.

Both feeds are comma-separated, three decimals a field, with no timestamp and
**no separator between records**. Datagram boundaries therefore fall inside
records, so the decoder has to resync - and must never publish a coordinate a
datagram cut in half. Part two drives the real listener through a real socket.

    python feed_test.py [grid.tif]
"""

from __future__ import annotations

import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import os as _os
_os.environ["BATHY3D_PROFILE"] = "test-feed"
from bathy3d import prefs as _prefs          # noqa: E402
_prefs.settings().clear()

DEFAULT = r"C:\Users\mkozh\OneDrive\Desktop\bathy\BOEM_bathy_WGS84_UTM15N.tif"
PORT = 6471   # not the live ports, so this never fights a real feed

#: Real positions off the survey PC, with the TMS pairs appended in sequence.
LIVE = [
    [706148.701, 3006428.410, 705939.201, 3006546.099, 706515.275, 3006391.404,
     705941.900, 3006549.300, 706512.600, 3006388.100],
    [706147.905, 3006427.067, 705938.649, 3006545.291, 706514.526, 3006390.021,
     705941.100, 3006548.100, 706511.900, 3006386.800],
    [706147.394, 3006426.146, 705938.073, 3006544.431, 706514.284, 3006389.585,
     705940.400, 3006547.000, 706511.300, 3006385.600],
    [706147.132, 3006425.672, 705937.781, 3006543.999, 706513.567, 3006388.189,
     705939.700, 3006546.100, 706510.700, 3006384.500],
]
#: Real depths off the survey PC: ROV1, ROV2, TMS1, TMS2.
DEPTHS = [
    [1656.082, 1646.926, 1492.150, 1508.260],
    [1656.111, 1646.949, 1492.190, 1509.330],
    [1656.148, 1646.965, 1492.190, 1510.220],
    [1656.158, 1646.981, 1492.300, 1510.220],
]

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
    if not cond:
        FAILED.append(name)


def rec(values):
    return ",".join(f"{v:.3f}" for v in values)


def part1_framing():
    from bathy3d.feed import (DEPTH_ORDER, ORDER, parse_depths, parse_records,
                              unglue)

    print("framing:")
    wire = "".join(rec(r) for r in LIVE)
    check("records really do run together", "3006388.100706147.905" in wire,
          wire[95:125])
    check("the missing separator is restored",
          unglue("3006391.404706147.905") == "3006391.404,706147.905")

    fixes, carry = parse_records(wire, stream=False)
    vals = [[v for nm in ORDER for v in f.pos[nm]] for f in fixes]
    check("positions decode whole", vals == LIVE,
          f"{len(fixes)}/{len(LIVE)} records, carry {len(carry)}")

    dwire = "".join(rec(r) for r in DEPTHS)
    dfx, _ = parse_depths(dwire, stream=False)
    dvals = [[f.depths[nm] for nm in DEPTH_ORDER] for f in dfx]
    check("depths decode whole", dvals == DEPTHS, f"{len(dfx)}/{len(DEPTHS)}")

    # the way a socket really delivers it: boundaries inside records
    for label, buf, decode, order, expect in (
        ("positions", wire, parse_records, ORDER, LIVE),
        ("depths", dwire, parse_depths, DEPTH_ORDER, DEPTHS),
    ):
        for size in (7, 19, 40, 67, 101, 256, 1500):
            carry, got = "", []
            for k in range(0, len(buf), size):
                f, carry = decode(carry + buf[k:k + size])
                got.extend(f)
            tail, carry = decode(carry, stream=False)     # idle flush
            got.extend(tail)
            if order is ORDER:
                vals = [[v for nm in order for v in f.pos[nm]] for f in got]
            else:
                vals = [[f.depths[nm] for nm in order] for f in got]
            check(f"{label} resync at {size}-byte datagrams", vals == expect,
                  f"{len(got)}/{len(expect)} records")

    print("\nrefusing what it cannot read:")
    for tag, junk in (("empty", ""),
                      ("garbage", "hello world"),
                      ("half a number", "706148.7"),
                      ("wrong field count", rec(LIVE[0][:6]))):
        fixes, _ = parse_records(junk, stream=False)
        check(f"rejects {tag}", fixes == [])

    # every value must be a plausible position, never a truncation
    fixes, _ = parse_records(wire, stream=False)
    sane = all(6.9e5 < e < 7.2e5 and 3.00e6 < n < 3.01e6
               for f in fixes for e, n in f.pos.values())
    check("no truncated value passed as a coordinate", sane)


def part2_live(grid):
    print("\nlive, through the real listener:")
    from PySide6 import QtWidgets
    from bathy3d.feed import ORDER
    from bathy3d.mainwindow import MainWindow

    app = QtWidgets.QApplication(sys.argv[:1])
    win = MainWindow(grid)
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
    if win.view.surface is None:
        check("grid loaded", False)
        return

    win.port_s.setValue(PORT)
    win.dport_s.setValue(PORT + 1)
    win.listen_b.setChecked(True)
    pump(800)
    check("listener started", win.feed is not None
          and "listening" in win.feed_status.text().lower(),
          win.feed_status.text().splitlines()[0])

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # sliced at a boundary that falls inside a record, as it does on the wire
    wire = "".join(rec(r) for r in LIVE)
    for k in range(0, len(wire), 37):
        sock.sendto(wire[k:k + 37].encode(), ("127.0.0.1", PORT))
        pump(60)
    # The trailing record is held until something confirms it; with the sender
    # silent that is the idle flush at 1.5 s.
    last = LIVE[-1]
    for _ in range(80):
        pump(80)
        t = win.view.targets.targets.get("Vessel")
        if t is not None and abs(t.x - last[0]) < 1e-6:
            break

    check("all five bodies tracked", len(win.view.targets.targets) == len(ORDER),
          str(sorted(win.view.targets.targets)))
    for i, nm in enumerate(ORDER):
        t = win.view.targets.targets.get(nm)
        if t is None:
            check(f"{nm} tracked", False)
            continue
        check(f"{nm} at the reported position",
              abs(t.x - last[2 * i]) < 1e-6 and abs(t.y - last[2 * i + 1]) < 1e-6,
              f"{t.x:,.3f}E {t.y:,.3f}N")

    colours = {nm: win.view.targets.targets[nm].color for nm in ORDER}
    check("colours as asked",
          colours["Vessel"] == "#ff3ad2" and colours["ROV1"] == "#ff3b30"
          and colours["ROV2"] == "#2ecc50", str(colours))

    check("table has a row per body", win.tgt_table.rowCount() == len(ORDER))
    check("trails accumulate",
          all(len(t.trail) >= 2 for t in win.view.targets.targets.values()),
          str({k: len(v.trail) for k, v in win.view.targets.targets.items()}))
    check("framed the vehicles on the first fix", win._framed_feed is True)

    sock.close()
    win.listen_b.setChecked(False)
    pump(700)
    check("listeners stop cleanly", win.feed is None and win.dfeed is None)
    win.close()
    pump(200)


if __name__ == "__main__":
    part1_framing()
    part2_live(sys.argv[1] if len(sys.argv) > 1 else DEFAULT)
    _prefs.settings().clear()
    print()
    print("FAILED: " + ", ".join(FAILED) if FAILED else "all feed checks passed")
    sys.exit(1 if FAILED else 0)
