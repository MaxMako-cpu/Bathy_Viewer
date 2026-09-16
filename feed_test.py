#!/usr/bin/env python
"""Checks for the UDP position feed.

Part one is pure parsing - no sockets, no Qt. Part two brings the real window
up, starts the real listener, fires real datagrams at it over the loopback, and
confirms the dots land where the coordinates say they should.

    python feed_test.py [grid.tif]
"""

from __future__ import annotations

import math
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Each suite gets its own settings profile, wiped on entry: the app now
# remembers exaggeration, ramps and the rest, so without this one test
# leaves state behind that the next one fails on - and a test run would
# quietly overwrite real preferences.
import os as _os
_os.environ["BATHY3D_PROFILE"] = "test-feed"
from bathy3d import prefs as _prefs      # noqa: E402
_prefs.settings().clear()

DEFAULT = r"C:\Users\mkozh\OneDrive\Desktop\bathy\BOEM_bathy_WGS84_UTM15N.tif"
PORT = 6471  # not 6451, so this never fights a real feed on the same machine

SAMPLE = [
    "2026-09-15T21:39:04.4609743Z,707364.210,3009048.400,707036.708,3009161.042,707634.048,3009049.775",
    "2026-09-15T21:39:05.4746984Z,707364.232,3009047.349,707036.708,3009161.042,707634.041,3009049.218",
    "2026-09-15T21:39:06.4587674Z,707364.221,3009046.850,707036.756,3009160.174,707634.034,3009048.623",
    "2026-09-15T21:39:07.4740694Z,707364.221,3009046.850,707036.795,3009159.306,707634.019,3009048.052",
    "2026-09-15T21:39:08.4596334Z,707364.155,3009045.862,707036.826,3009158.443,707634.029,3009047.471",
]
EXPECT = [[float(x) for x in r.split(",")[1:]] for r in SAMPLE]

#: The live format, transcribed from the survey PC's ASCII decode window.
#: Six fields per record - Vessel E/N, UHD333 E/N, UHD334 E/N - with no
#: timestamp and no separator of any kind between one record and the next.
LIVE = [
    "706148.701,3006428.410,705939.201,3006546.099,706515.275,3006391.404",
    "706147.905,3006427.067,705938.649,3006545.291,706514.526,3006390.021",
    "706147.394,3006426.146,705938.073,3006544.431,706514.284,3006389.585",
    "706147.132,3006425.672,705937.781,3006543.999,706513.567,3006388.189",
]

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
    if not cond:
        FAILED.append(name)
    return cond


def part1_parsing():
    print("parsing:")
    from bathy3d.feed import ORDER, parse_records, parse_timestamp

    for tag, buf in (
        ("glued, no separator", "".join(SAMPLE)),
        ("newline", "\n".join(SAMPLE)),
        ("CRLF", "\r\n".join(SAMPLE)),
        ("trailing CRLF", "\r\n".join(SAMPLE) + "\r\n"),
        ("leading whitespace", "  " + "\n".join(SAMPLE)),
        ("spaces round commas", "\n".join(s.replace(",", " , ") for s in SAMPLE)),
    ):
        fixes, tail = parse_records(buf, stream=False)
        vals = [[v for nm in ORDER for v in f.pos[nm]] for f in fixes]
        check(f"framing: {tag}", len(fixes) == 5 and vals == EXPECT,
              f"{len(fixes)} fixes, tail={tail!r}")

    fixes, _ = parse_records(SAMPLE[0], stream=False)
    f = fixes[0]
    check("field mapping", f.pos["Vessel"] == (707364.210, 3009048.400)
          and f.pos["UHD333"] == (707036.708, 3009161.042)
          and f.pos["UHD334"] == (707634.048, 3009049.775))
    check("7-digit .NET timestamp", f.t.hour == 21 and f.t.minute == 39
          and f.t.second == 4 and f.t.microsecond == 460974,
          f"{f.t.isoformat()}")

    # a record split across two reads must survive via the carry
    whole = "".join(SAMPLE)
    cut = len(SAMPLE[0]) + 40
    a, tail_a = parse_records(whole[:cut])
    b, _ = parse_records(tail_a + whole[cut:], stream=False)
    check("record split across datagrams", len(a) + len(b) == 5,
          f"{len(a)} + {len(b)}")

    # Replay the real capture the way a socket actually delivers it: the feed
    # has no line terminators, so datagram boundaries fall mid-record.
    whole = "".join(SAMPLE)
    for size in (7, 13, 31, 64, 97, 128, 256, 485, 1500):
        carry, got = "", []
        for k in range(0, len(whole), size):
            fixes, carry = parse_records(carry + whole[k:k + size])
            got.extend(fixes)
        tail_fixes, carry = parse_records(carry, stream=False)   # idle flush
        got.extend(tail_fixes)
        vals = [[v for nm in ORDER for v in f.pos[nm]] for f in got]
        check(f"stream resyncs at {size}-byte datagrams",
              vals == EXPECT, f"{len(got)}/5 records, carry {len(carry)}")

    for tag, junk in (("empty", ""), ("garbage", "hello world"),
                      ("truncated record", SAMPLE[0][:60]),
                      ("too few fields", "2026-09-15T21:39:04.4609743Z,1.0,2.0"),
                      ("half a timestamp", "3009049.775" + "2026-09-1"),
                      ("odd coordinate count", "707364.210,3009048.400,707036.708")):
        fixes, _ = parse_records(junk, stream=False)
        check(f"rejects {tag}", fixes == [])

    # Delimiter-free senders: numbers written end to end, no commas, no
    # timestamp - the datagram itself is the only record boundary.
    glued = "".join(f"{v:.3f}" for v in EXPECT[-1])
    fixes, carry = parse_records(glued, stream=False)
    ok = (len(fixes) == 1 and not carry and not fixes[0].timed
          and [round(v, 3) for nm in ORDER for v in fixes[0].pos[nm]]
          == [round(v, 3) for v in EXPECT[-1]])
    check("delimiter-free record decodes", ok,
          f"{len(fixes)} fixes from {glued[:40]}...")

    fixes, _ = parse_records(glued + glued, stream=False)
    check("two delimiter-free records in one datagram", len(fixes) == 2,
          f"{len(fixes)} fixes")

    # ...but never when this sender is known to use timestamps: a mid-record
    # slice of a timestamped stream is all digits too.
    fixes, _ = parse_records(glued, stream=False, allow_bare=False)
    check("delimiter-free parsing off for a timestamped feed", fixes == [])

    mid = "3009045.862707036.826"          # a slice, not a whole record
    fixes, _ = parse_records(mid, stream=False)
    check("rejects a delimiter-free slice with the wrong field count",
          fixes == [], f"{len(fixes)} fixes")

    # The format actually on the wire, read off the survey PC's own decode
    # window: six comma-separated fields per record, and consecutive records
    # written back to back with NOTHING between them - no timestamp, no
    # terminator. The only mark of a boundary is a field's three decimals
    # running straight into the next field's digits.
    live_wire = "".join(LIVE)
    expect_live = [[float(x) for x in r.split(",")] for r in LIVE]
    check("real format: no separator between records",
          "3006391.404706147.905" in live_wire)

    fixes, carry = parse_records(live_wire, stream=False)
    vals = [[v for nm in ORDER for v in f.pos[nm]] for f in fixes]
    check("real format decodes whole", vals == expect_live,
          f"{len(fixes)}/{len(LIVE)} records, carry {len(carry)}")

    for size in (7, 19, 40, 67, 68, 101, 256, 1500):
        carry, got = "", []
        for k in range(0, len(live_wire), size):
            f2, carry = parse_records(carry + live_wire[k:k + size])
            got.extend(f2)
        f3, carry = parse_records(carry, stream=False)
        got.extend(f3)
        vals = [[v for nm in ORDER for v in f.pos[nm]] for f in got]
        check(f"real format resyncs at {size}-byte datagrams",
              vals == expect_live, f"{len(got)}/{len(LIVE)} records")

    # every decoded value must be a plausible UTM 15N position, not a
    # truncation - eastings ~7.0e5, northings ~3.0e6 for this survey
    fixes, _ = parse_records(live_wire, stream=False)
    sane = all(6.9e5 < e < 7.2e5 and 3.00e6 < n < 3.01e6
               for f in fixes for e, n in f.pos.values())
    check("no truncated value passed as a coordinate", sane)

    # Timestamp-free senders: bare coordinate records, with and without lines.
    bare = ",".join(f"{v:.3f}" for v in EXPECT[-1])
    for tag, buf in (("bare record", bare),
                     ("bare, newline", bare + "\n"),
                     ("two bare records", bare + "\n" + bare)):
        fixes, _ = parse_records(buf, stream=False)
        ok = fixes and all(not f.timed for f in fixes) and \
            [round(v, 3) for nm in ORDER for v in fixes[0].pos[nm]] == \
            [round(v, 3) for v in EXPECT[-1]]
        check(f"timestamp-free: {tag}", bool(ok), f"{len(fixes)} fixes")


def part2_live(grid):
    print("\nlive feed through the real window:")
    from PySide6 import QtCore, QtWidgets
    from bathy3d.feed import ORDER
    from bathy3d.mainwindow import MainWindow

    app = QtWidgets.QApplication(sys.argv[:1])
    win = MainWindow(grid)
    win.resize(1500, 900)
    win.show()
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_feed_shot.png")
    state = {"n": 0, "sent": 0}

    def pump(ms=260):
        end = time.monotonic() + ms / 1000.0
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.005)

    def go():
        state["n"] += 1
        if win.view.surface is None:
            if state["n"] > 300:
                print("  TIMEOUT waiting for the grid")
                app.quit()
            return
        timer.stop()
        try:
            win.port_s.setValue(PORT)
            win.listen_b.setChecked(True)
            pump(700)
            check("listener started", win.feed is not None
                  and "Listening" in win.feed_status.text(),
                  win.feed_status.text())

            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            for rec in SAMPLE:
                sock.sendto((rec + "\r\n").encode(), ("127.0.0.1", PORT))
                state["sent"] += 1
                pump(160)
            # fix arrives on the feed thread but is applied on the GUI thread,
            # and each one re-renders the mesh - let the queue drain first.
            for _ in range(40):
                pump(50)
                if all(len(t.trail) >= 5 for t in win.view.targets.targets.values()) \
                        and len(win.view.targets.targets) == len(ORDER):
                    break

            check("every datagram decoded",
                  win.feed.records == 5 and win.feed.bad == 0,
                  f"{win.feed.packets} pkt, {win.feed.records} rec, {win.feed.bad} bad")

            # dots must sit at the coordinates from the last record
            last = EXPECT[-1]
            s = win.view.surface
            for i, nm in enumerate(ORDER):
                t = win.view.targets.targets.get(nm)
                if not check(f"{nm} tracked", t is not None):
                    continue
                dx = abs(t.x - last[2 * i])
                dy = abs(t.y - last[2 * i + 1])
                p = s.probe(t.x, t.y)
                on_seabed = abs(t.z - p.z) < 1e-6
                check(f"{nm} at the reported position", dx < 1e-6 and dy < 1e-6,
                      f"dE={dx:.4g} dN={dy:.4g}")
                check(f"{nm} sitting on the seabed", on_seabed,
                      f"z={t.z:.1f} seabed={p.z:.1f}")
                check(f"{nm} trail built", len(t.trail) == 5, f"{len(t.trail)} points")

            colours = {nm: win.view.targets.targets[nm].color for nm in ORDER}
            check("colours as asked", colours["UHD333"].lower() == "#ff3b30"
                  and colours["UHD334"].lower() == "#2ecc50"
                  and colours["Vessel"].lower() == "#ff3ad2", str(colours))

            row = {win.tgt_table.item(r, 0).text(): r for r in range(win.tgt_table.rowCount())}
            v = win.tgt_table.item(row["UHD333"], 1).text()
            d = win.tgt_table.item(row["UHD333"], 3).text()
            check("table shows the fix", v.replace(",", "") == "707036.83",
                  f"easting cell = {v!r}, seabed cell = {d!r}")

            # vessel to the surface and back
            win.surf_b.setChecked(True)
            sock.sendto((SAMPLE[-1] + "\r\n").encode(), ("127.0.0.1", PORT))
            pump(320)
            check("vessel at sea surface when asked",
                  abs(win.view.targets.targets["Vessel"].z) < 1e-9,
                  f"z={win.view.targets.targets['Vessel'].z}")
            win.surf_b.setChecked(False)
            sock.sendto((SAMPLE[-1] + "\r\n").encode(), ("127.0.0.1", PORT))
            pump(320)
            check("vessel back on the seabed",
                  win.view.targets.targets["Vessel"].z < -100)

            # two records in one datagram
            before = win.feed.records
            sock.sendto(("".join(SAMPLE[:2])).encode(), ("127.0.0.1", PORT))
            # The trailing record is held until something confirms it; with the
            # sender silent that is the idle flush at 1.5 s.
            for _ in range(60):
                pump(80)
                if win.feed.records >= before + 2:
                    break
            check("two records in one datagram, second after idle flush",
                  win.feed.records == before + 2, f"{before} -> {win.feed.records}")

            # The real wire: one continuous run of records, no terminators,
            # sliced at boundaries that fall inside coordinates.
            win.view.targets.clear_trail()
            before = win.feed.records
            whole = "".join(SAMPLE)
            for k in range(0, len(whole), 37):
                sock.sendto(whole[k:k + 37].encode(), ("127.0.0.1", PORT))
                pump(45)
            for _ in range(60):
                pump(80)
                t = win.view.targets.targets["UHD334"]
                if win.feed.records >= before + 5 and abs(t.x - EXPECT[-1][4]) < 1e-6:
                    break
            check("glued stream in 37-byte slices decodes whole",
                  win.feed.records == before + 5,
                  f"{before} -> {win.feed.records}")
            t = win.view.targets.targets["UHD334"]
            check("no truncated coordinate reached the scene",
                  abs(t.x - EXPECT[-1][4]) < 1e-6 and abs(t.y - EXPECT[-1][5]) < 1e-6,
                  f"UHD334 at {t.x:.3f}E {t.y:.3f}N")
            for nm, i in (("Vessel", 0), ("UHD333", 2), ("UHD334", 4)):
                tt = win.view.targets.targets[nm]
                sane = all(abs(a - b) < 2000 for a, b in
                           ((tt.x, EXPECT[-1][i]), (tt.y, EXPECT[-1][i + 1])))
                check(f"{nm} never jumped off the survey area", sane,
                      f"{tt.x:.1f}E {tt.y:.1f}N")

            check("no stale drop line after returning to the seabed",
                  "stem" not in win.view.targets._actors.get("Vessel", {}))
            vz = [p[2] for p in win.view.targets.targets["Vessel"].trail]
            check("vessel trail holds one height, no surface-to-seabed spike",
                  not vz or (max(vz) - min(vz)) < 50.0,
                  f"{len(vz)} points, z spread {(max(vz) - min(vz)) if vz else 0:.1f} m")
            win.view.targets.clear_trail()
            pump(120)
            check("clear trails removes the line actors",
                  all("trail" not in b for b in win.view.targets._actors.values()))

            before = win.view.plotter.camera.position
            win.zoom_to_targets()
            pump(200)
            after = win.view.plotter.camera.position
            foc = win.view.plotter.camera.focal_point
            s2 = win.view.surface
            tgt_local = [s2.local_from_crs(t.x, t.y)
                         for t in win.view.targets.targets.values()]
            cx = sum(p[0] for p in tgt_local) / len(tgt_local)
            cy = sum(p[1] for p in tgt_local) / len(tgt_local)
            check("zoom to targets moves the camera", before != after)
            check("camera now centred on the vehicles",
                  math.hypot(foc[0] - cx, foc[1] - cy) < 1.0,
                  f"focal off by {math.hypot(foc[0]-cx, foc[1]-cy):.1f} m")
            dist = math.dist(after, foc)
            check("framed tight enough to see them", dist < 5000,
                  f"eye {dist:,.0f} m from the group")

            win.view.plotter.screenshot(out)
            print(f"  wrote {out}")

            sock.close()
            win.listen_b.setChecked(False)
            pump(700)
            check("listener stops cleanly", win.feed is None
                  and win.feed_status.text() == "Stopped", win.feed_status.text())
        except Exception:
            import traceback
            traceback.print_exc()
            FAILED.append("exception in live test")
        QtCore.QTimer.singleShot(300, app.quit)

    timer = QtCore.QTimer()
    timer.timeout.connect(go)
    timer.start(100)
    app.exec()


if __name__ == "__main__":
    grid = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    part1_parsing()
    part2_live(grid)
    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}): " + ", ".join(FAILED))
        sys.exit(1)
    print("all feed checks passed")
