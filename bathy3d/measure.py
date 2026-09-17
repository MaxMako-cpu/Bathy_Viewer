"""Station-to-station measuring along the seabed. Pure geometry, no VTK."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

OCTANTS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)


def compass(bearing: float) -> str:
    if not math.isfinite(bearing):
        return "--"
    return OCTANTS[int(round(bearing / 22.5)) % 16]


def bearing_text(bearing: float) -> str:
    """Degrees, written the way a bearing is written on a survey plot.

    Three digits before the point and zero-padded - 007.2, not 7.2 - so a
    column of them lines up and cannot be misread for something else. The
    compass point beside it says roughly where at a glance; this says exactly.
    """
    if not math.isfinite(bearing):
        return "--"
    return f"{bearing % 360.0:05.1f}"


@dataclass
class Station:
    x: float  # CRS
    y: float
    z: float  # elevation, metres, positive up
    lon: float = float("nan")
    lat: float = float("nan")


@dataclass
class Leg:
    horizontal: float  # metres
    dz: float  # metres, positive = shallower
    slant: float  # metres along the straight line
    gradient: float  # degrees
    bearing: float  # degrees from grid north


@dataclass
class MeasureLine:
    """An ordered run of stations plus the numbers surveyors ask for."""

    surface: object
    stations: list[Station] = field(default_factory=list)

    def add(self, st: Station) -> None:
        self.stations.append(st)

    def undo(self) -> None:
        if self.stations:
            self.stations.pop()

    def clear(self) -> None:
        self.stations.clear()

    def __len__(self) -> int:
        return len(self.stations)

    def legs(self) -> list[Leg]:
        out = []
        for a, b in zip(self.stations, self.stations[1:]):
            hz = self.surface.horizontal_distance(a.x, a.y, b.x, b.y)
            dz = b.z - a.z
            slant = math.hypot(hz, dz)
            grad = math.degrees(math.atan2(abs(dz), hz)) if hz > 0 else 90.0
            out.append(Leg(hz, dz, slant, grad, self.surface.bearing(a.x, a.y, b.x, b.y)))
        return out

    def totals(self) -> dict:
        legs = self.legs()
        if not legs:
            return {"horizontal": 0.0, "slant": 0.0, "chord": 0.0, "drop": 0.0}
        a, b = self.stations[0], self.stations[-1]
        hz = self.surface.horizontal_distance(a.x, a.y, b.x, b.y)
        return {
            "horizontal": sum(l.horizontal for l in legs),
            "slant": sum(l.slant for l in legs),
            "chord": math.hypot(hz, b.z - a.z),
            "drop": b.z - a.z,
        }

    def to_csv(self) -> str:
        """Station table, ready for a report or a nav system."""
        rows = [
            "station,x,y,longitude,latitude,elevation_m,depth_m,"
            "leg_horizontal_m,leg_dz_m,leg_gradient_deg,leg_bearing_deg,cumulative_m"
        ]
        legs = self.legs()
        cum = 0.0
        for i, st in enumerate(self.stations):
            if i == 0:
                lg = ["", "", "", ""]
            else:
                l = legs[i - 1]
                cum += l.horizontal
                lg = [f"{l.horizontal:.2f}", f"{l.dz:.2f}", f"{l.gradient:.3f}", f"{l.bearing:.2f}"]
            rows.append(
                f"{i + 1},{st.x:.3f},{st.y:.3f},{st.lon:.7f},{st.lat:.7f},"
                f"{st.z:.2f},{-st.z:.2f}," + ",".join(lg) + f",{cum:.2f}"
            )
        return "\n".join(rows) + "\n"
