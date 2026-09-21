#!/usr/bin/env python
"""Checks that a partly-deployed fleet decodes onto the right vehicles.

The sender emits a field for every body whether or not it is deployed and
leaves the absent ones empty. Consecutive delimiters collapse, so those empties
never reach the decoder: a ten-field position record arrives as six numbers.
Read as the first six of ten it put the ROV at its TMS's position - 132 m out -
the TMS at the vessel's a second late, and halved the update rate. The depth
feed did the same, showing an ROV 56 m shallow.

The fixtures below are four real records off each feed, one ROV deployed,
captured 2026-09-20 22:58. They are the wire's own bytes minus the logger's
timestamps, so if the decoder ever drifts from the sender this fails.

    python layout_test.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bathy3d.feed import (DEPTH_ORDER, ORDER, active_order, parse_depths,
                          parse_records)

FAILED = []


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}{' - ' + detail if detail else ''}")
    if not cond:
        FAILED.append(name)


#: Vessel, (ROV1 empty), ROV2, (TMS1 empty), TMS2.
POS = [
    "393678.457,3011670.516,,,393745.129,3011781.808,,,393693.311,3011660.803",
    "393678.437,3011670.514,,,393744.868,3011781.485,,,393693.311,3011660.803",
    "393678.437,3011670.514,,,393744.608,3011781.144,,,393693.308,3011661.184",
    "393678.451,3011670.493,,,393744.373,3011780.830,,,393693.308,3011661.184",
]
#: (ROV1 empty), ROV2, (TMS1 empty), TMS2.
DEP = [
    ",1002.395,,946.100",
    ",1002.306,,945.950",
    ",1002.192,,945.950",
    ",1002.057,,945.950",
]

WANT_POS = [
    {"Vessel": (393678.457, 3011670.516), "ROV2": (393745.129, 3011781.808),
     "TMS2": (393693.311, 3011660.803)},
    {"Vessel": (393678.437, 3011670.514), "ROV2": (393744.868, 3011781.485),
     "TMS2": (393693.311, 3011660.803)},
    {"Vessel": (393678.437, 3011670.514), "ROV2": (393744.608, 3011781.144),
     "TMS2": (393693.308, 3011661.184)},
    {"Vessel": (393678.451, 3011670.493), "ROV2": (393744.373, 3011780.830),
     "TMS2": (393693.308, 3011661.184)},
]
WANT_DEP = [{"ROV2": 1002.395, "TMS2": 946.100},
            {"ROV2": 1002.306, "TMS2": 945.950},
            {"ROV2": 1002.192, "TMS2": 945.950},
            {"ROV2": 1002.057, "TMS2": 945.950}]


print("which bodies a partly-deployed sender fills:")
check("both chains out means the whole fleet",
      active_order(("ROV1", "ROV2")) == ORDER, str(active_order(("ROV1", "ROV2"))))
check("one chain out drops its ROV and its TMS together",
      active_order(("ROV2",)) == ("Vessel", "ROV2", "TMS2"),
      str(active_order(("ROV2",))))
check("and the other way round",
      active_order(("ROV1",)) == ("Vessel", "ROV1", "TMS1"),
      str(active_order(("ROV1",))))
check("the vessel is in no chain, so it always stays",
      "Vessel" in active_order(()), str(active_order(())))
check("depths have no vessel to keep",
      active_order(("ROV2",), DEPTH_ORDER) == ("ROV2", "TMS2"),
      str(active_order(("ROV2",), DEPTH_ORDER)))


print("\nthe real wire, one ROV deployed:")
order = active_order(("ROV2",))
wire = "".join(POS)
fixes, carry = parse_records(wire, stream=False, order=order)
check("every record decodes, none merged", len(fixes) == len(POS),
      f"{len(fixes)} of {len(POS)}, carry {len(carry)}")
worst = 0.0
for got, want in zip(fixes, WANT_POS):
    check(f"record carries exactly {len(want)} bodies",
          set(got.pos) == set(want), str(sorted(got.pos)))
    for nm, (e, n) in want.items():
        ge, gn = got.pos.get(nm, (float("nan"),) * 2)
        worst = max(worst, abs(ge - e), abs(gn - n))
check("and every easting and northing is exact", worst < 1e-6,
      f"worst {worst:.9f} m")
check("an undeployed vehicle is simply absent",
      not any("ROV1" in f.pos or "TMS1" in f.pos for f in fixes))

dfx, _ = parse_depths("".join(DEP), stream=False,
                      order=active_order(("ROV2",), DEPTH_ORDER))
check("every depth record decodes", len(dfx) == len(DEP), str(len(dfx)))
dworst = max(abs(dfx[i].depths[k] - v)
             for i, want in enumerate(WANT_DEP) for k, v in want.items())
check("and every depth is exact", dworst < 1e-9, f"worst {dworst:.9f} m")


print("\nwhat it used to do - the shape of the bug:")
wrong, _ = parse_records(wire, stream=False, order=ORDER)
check("reading the full fleet halves the record count",
      len(wrong) == len(POS) // 2, f"{len(wrong)} from {len(POS)}")
if wrong:
    rov2 = wrong[0].pos.get("ROV2", (0.0, 0.0))
    truth = WANT_POS[0]["ROV2"]
    off = ((rov2[0] - truth[0]) ** 2 + (rov2[1] - truth[1]) ** 2) ** 0.5
    check("and puts the ROV over a hundred metres from where it is",
          off > 100.0, f"{off:,.0f} m out")


print("\nreading a datagram by field position - what actually decodes the feed:")
# The empty fields say which vehicles are reporting, so nothing has to be
# configured. This matters because the alternative - telling the decoder which
# chains were deployed - conflated what the sender fills with what the
# operator wants to look at. Deselecting a deployed ROV to clear the view then
# read one datagram as three vessel fixes 130 m apart.
from bathy3d.feed import parse_fielded

got = parse_fielded(POS[0], ORDER, 2)
check("a one-ROV position record decodes straight",
      got is not None and len(got) == 1 and set(got[0]) == {"Vessel", "ROV2", "TMS2"},
      str(sorted(got[0])) if got else "None")
check("onto the right bodies",
      got and abs(got[0]["ROV2"][0] - 393745.129) < 1e-9
      and abs(got[0]["TMS2"][1] - 3011660.803) < 1e-9)
gotd = parse_fielded(DEP[0], DEPTH_ORDER, 1)
check("and a one-ROV depth record, leading empty field and all",
      gotd is not None and gotd[0] == {"ROV2": [1002.395], "TMS2": [946.100]},
      str(gotd))

full_p = ("706148.701,3006428.410,705939.201,3006546.099,706515.275,"
          "3006391.404,705941.900,3006549.300,706512.600,3006388.100")
check("a full fleet still decodes to five bodies",
      len(parse_fielded(full_p, ORDER, 2)[0]) == 5)
check("a terminator does not upset it",
      parse_fielded(DEP[0] + "\n", DEPTH_ORDER, 1) == gotd)
check("half a coordinate is refused, not published",
      parse_fielded("393678.457,,,,393745.129,3011781.808,,,"
                    "393693.311,3011660.803", ORDER, 2) is None)
check("and so is anything that is not a whole number of records",
      parse_fielded("1,2,3", ORDER, 2) is None)

print("\nwith nothing selected at all:")
# Deselecting both chains leaves the depth feed with no bodies - unlike
# positions, it has no vessel to fall back on. The record length was floored
# at 1 where the layout is set, but the decoders recompute it from the layout
# itself, so a zero reached a // and killed the listening thread on a vessel.
none_d = active_order((), DEPTH_ORDER)
none_p = active_order(())
check("the depth layout really is empty", none_d == (), str(none_d))
check("while positions keep the vessel", none_p == ("Vessel",), str(none_p))
try:
    got, carry = parse_depths(DEP[0], stream=False, order=none_d)
    crashed = None
except Exception as exc:                                   # noqa: BLE001
    crashed, got, carry = exc, [], ""
check("decoding does not raise", crashed is None, repr(crashed))
check("it simply decodes nothing", got == [] and carry == "",
      f"{len(got)} records, {len(carry)} carried")
check("and nothing is carried, so the buffer cannot grow for ever",
      carry == "")
try:
    parse_records(POS[0], stream=False, order=())
    ok = True
except Exception as exc:                                   # noqa: BLE001
    ok = False
check("an empty position layout is equally harmless", ok)

print("\nthe carry offset counts numbers, not separators:")
# Empty fields are separators that carry no number. Cutting the buffer by
# separator count left it mid-record, and every record after the first came
# out rotated by one body - the vessel wearing the ROV's position.
part = POS[0] + POS[1] + POS[2][:20]
got, carry = parse_records(part, stream=True, order=order)
# Two whole records and a fragment arrive; one is published. The trailing
# whole record is held too, because nothing has yet confirmed it is whole -
# a datagram cut inside a coordinate would otherwise publish a truncated one.
check("only a confirmed record is published", len(got) == 1,
      f"{len(got)} published, {len(carry)} chars held")
check("and it is the first, in full",
      abs(got[0].pos["Vessel"][0] - WANT_POS[0]["Vessel"][0]) < 1e-6
      and abs(got[0].pos["TMS2"][1] - WANT_POS[0]["TMS2"][1]) < 1e-6)
rest, _ = parse_records(carry + POS[2][20:] + POS[3], stream=False, order=order)
check("and the carry resumes on a record boundary",
      rest and set(rest[0].pos) == {"Vessel", "ROV2", "TMS2"}
      and abs(rest[0].pos["Vessel"][0] - WANT_POS[2]["Vessel"][0]) < 1e-6,
      str(rest[0].pos.get("Vessel")) if rest else "nothing decoded")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all layout checks passed")
