#!/usr/bin/env python
"""Checks for renaming and recolouring the vehicles.

The point of the feature is that a rename is *only* a rename: the slot the
feed, the tether pairing and every calibration tie-in are keyed on must not
move. So the interesting checks are not that the label changed - they are that
nothing else did.

    python vehicles_test.py [grid.tif]
"""

import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import os as _os
_os.environ["BATHY3D_PROFILE"] = "test-vehicles"
from bathy3d import prefs as _prefs          # noqa: E402
_prefs.settings().clear()

GRID = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:\Users\mkozh\OneDrive\Desktop\bathy\BOEM_bathy_WGS84_UTM15N.tif"
PPORT, DPORT = 6495, 6496

from bathy3d import vehicles                 # noqa: E402
from bathy3d.feed import ORDER, TETHERS      # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
    if not cond:
        FAILED.append(name)


# ------------------------------------------------------------- the registry

print("labels and colours, with no Qt involved:")

f = vehicles.Fleet()
check("stock fleet reports itself unchanged", not f.renamed())
check("a slot starts out labelled with its own name",
      f.label("UHD333") == "UHD333")
check("and coloured as targets.py ships it", f.colour("UHD333") == "#ff3b30",
      f.colour("UHD333"))

f.set_label("UHD333", "Hercules")
check("a rename takes", f.label("UHD333") == "Hercules")
check("and is noticed", f.renamed())
check("the other bodies are untouched", f.label("UHD334") == "UHD334")

f.set_label("UHD333", "   Deep   Rover  ")
check("whitespace is tidied", f.label("UHD333") == "Deep Rover",
      repr(f.label("UHD333")))
f.set_label("UHD333", "x" * 60)
check("an absurd name is capped", len(f.label("UHD333")) == 24,
      str(len(f.label("UHD333"))))
f.set_label("UHD333", "")
check("blank puts the slot name back", f.label("UHD333") == "UHD333")
check("and the fleet is stock again", not f.renamed())

# A TMS has no colour of its own, so there is nothing that can drift.
f.set_colour("UHD334", "#3366ff")
check("an ROV takes a new colour", f.colour("UHD334") == "#3366ff")
check("its TMS follows it, darker",
      f.colour("TMS334") == vehicles.darken("#3366ff"), f.colour("TMS334"))
check("the darker shade really is darker",
      vehicles.darken("#3366ff") != "#3366ff")
try:
    f.set_colour("TMS334", "#ffffff")
    refused = False
except ValueError:
    refused = True
check("a TMS colour cannot be set directly", refused)
check("the TMS still follows its ROV",
      f.colour("TMS334") == vehicles.darken("#3366ff"))

check("only the vessel and the ROVs are choosable",
      vehicles.CHOOSABLE == ("Vessel", "UHD333", "UHD334"),
      str(vehicles.CHOOSABLE))
check("every TMS is accounted for as a follower",
      set(vehicles.FOLLOWS) == set(TETHERS.values()), str(vehicles.FOLLOWS))

check("a nonsense colour is ignored rather than stored",
      (f.set_colour("UHD334", "not a colour") or f.colour("UHD334")) == "#3366ff")
check("darkening a nonsense colour returns it unchanged",
      vehicles.darken("zzz") == "zzz")
check("darkening never reaches black", vehicles.darken("#000000") != "#000000",
      vehicles.darken("#000000"))

# Only what differs from stock is written, so an untouched fleet stores nothing.
f2 = vehicles.Fleet()
check("a stock fleet encodes to nothing", f2.encode() == [], str(f2.encode()))
f.set_label("Vessel", "Island Pride")
rows = f.encode()
back = vehicles.Fleet()
back.load(rows)
check("labels and colours survive the round trip",
      back.label("Vessel") == "Island Pride"
      and back.colour("UHD334") == "#3366ff"
      and back.colour("TMS334") == f.colour("TMS334"), str(rows))
check("no TMS colour is stored, so it cannot go stale",
      not any(r.startswith("TMS") and r.split("|")[2] for r in rows), str(rows))
bad = vehicles.Fleet()
bad.load(["rubbish", "NoSuchBody|Name|#ffffff", "UHD333|Keeper|"])
check("unreadable rows are dropped, good ones kept",
      bad.label("UHD333") == "Keeper" and bad.encode() == ["UHD333|Keeper|"],
      str(bad.encode()))


# ------------------------------------------------------------- in the window

print("\nlive, through the window:")

from PySide6 import QtWidgets                # noqa: E402
from bathy3d.calib import TiePoint           # noqa: E402
from bathy3d.mainwindow import MainWindow    # noqa: E402

SITE = (705939.201, 3006546.099)

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

win.port_s.setValue(PPORT)
win.dport_s.setValue(DPORT)
win.listen_b.setChecked(True)
pump(800)

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
home = ORDER.index("UHD333")
pos = []
for i, _ in enumerate(ORDER):
    pos += [SITE[0] + (i - home) * 3.0, SITE[1] + (i - home) * 3.0]
for _ in range(2):
    sock.sendto(",".join(f"{v:.3f}" for v in pos).encode(), ("127.0.0.1", PPORT))
    sock.sendto(b"1656.082,1646.926,1492.150,1508.260", ("127.0.0.1", DPORT))
    pump(150)
for _ in range(40):
    pump(80)
    if len(win.view.targets.targets) == len(ORDER) and win._depths.get("UHD333"):
        break
check("all five bodies tracked", len(win.view.targets.targets) == len(ORDER),
      str(sorted(win.view.targets.targets)))

row = ORDER.index("UHD333")
check("the table starts with the slot name",
      win.tgt_table.item(row, 0).text() == "UHD333")
check("the tie-in menu names it too",
      "UHD333" in win.act_tie["UHD333"].text(), win.act_tie["UHD333"].text())

# Tie in first, then rename: the point must survive under the new name.
win.tie_in("UHD333")
check("a tie-in was taken", len(win.calib.points) == 1)
tied_slot = win.calib.points[0].vehicle

win.fleet.set_label("UHD333", "Hercules")
win.fleet.set_colour("UHD333", "#00a3ff")
win.apply_fleet()
pump(200)

check("the marker carries the new label",
      win.view.targets.targets["UHD333"].shown == "Hercules",
      win.view.targets.targets["UHD333"].shown)
check("and the new colour",
      win.view.targets.targets["UHD333"].color == "#00a3ff",
      win.view.targets.targets["UHD333"].color)
check("its TMS was recoloured to match",
      win.view.targets.targets["TMS333"].color == vehicles.darken("#00a3ff"),
      win.view.targets.targets["TMS333"].color)
check("the table row renamed", win.tgt_table.item(row, 0).text() == "Hercules")
check("the tie-in menu entry renamed",
      "Hercules" in win.act_tie["UHD333"].text(), win.act_tie["UHD333"].text())

# The whole point: nothing structural moved.
check("the slot itself never changed", "UHD333" in win.view.targets.targets,
      str(sorted(win.view.targets.targets)))
check("the tie-in still points at the slot",
      win.calib.points[0].vehicle == tied_slot == "UHD333")
check("so the calibration still fits", win.calib.model.on)
win.show_calib()
pump(150)
check("and the calibration dialog shows the new name",
      win._calib_dialog.table.item(0, 0).text() == "Hercules",
      win._calib_dialog.table.item(0, 0).text())

check("the tether pairing is untouched", TETHERS["UHD333"] == "TMS333")
win.view.targets.draw_links(TETHERS)
pump(100)
check("so the tether still draws",
      win.view.plotter.renderer.actors.get("link:UHD333") is not None)

# A body renamed after the trail started keeps its trail.
check("the trail survived the rename",
      len(win.view.targets.targets["UHD333"].trail) > 0,
      str(len(win.view.targets.targets["UHD333"].trail)))

# What it remembers.
saved = _prefs.fleet()
check("the rename was written to settings", any("Hercules" in r for r in saved),
      str(saved))
reloaded = vehicles.Fleet()
reloaded.load(saved)
check("a restart brings back name and colour",
      reloaded.label("UHD333") == "Hercules"
      and reloaded.colour("UHD333") == "#00a3ff"
      and reloaded.colour("TMS333") == vehicles.darken("#00a3ff"))

win.show_fleet()
pump(150)
dlg = win._fleet_dialog
check("the dialog offers a row per body", len(dlg._rows) == len(ORDER),
      str(len(dlg._rows)))
check("the TMS colour button is not clickable",
      not dlg._rows["TMS333"][1].isEnabled())
check("the vessel's is", dlg._rows["Vessel"][1].isEnabled())
check("the dialog shows the current name",
      dlg._rows["UHD333"][0].text() == "Hercules",
      dlg._rows["UHD333"][0].text())

win.fleet.reset()
win.apply_fleet()
pump(150)
check("reset puts every name back",
      all(win.fleet.label(s) == s for s in ORDER))
check("and every colour", win.fleet.colour("UHD333") == "#ff3b30")
check("the table went back too", win.tgt_table.item(row, 0).text() == "UHD333")
check("and the tie-in point still survives",
      len(win.calib.points) == 1 and win.calib.points[0].vehicle == "UHD333")

win.listen_b.setChecked(False)
pump(300)
sock.close()

# Shutting down with the dialog open used to crash: a line edit losing focus
# during teardown emits editingFinished, which reached a table Qt had already
# deleted. The dialog is deliberately left open here.
dlg._rows["UHD334"][0].setFocus()
pump(100)
closed_cleanly = True
try:
    win.close()
    pump(300)
except RuntimeError as exc:
    closed_cleanly = False
    print(f"    {exc}")
check("closing with the vehicles dialog open is clean", closed_cleanly)
pump(200)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all vehicle checks passed")
