"""Colour ramps for the surface.

Two sets, because depth and slope are not the same kind of quantity. Depth runs
between two ends and reads well on any smooth ramp; slope starts at flat and
matters most where it is steep, so its ramps hold their strong colour back for
the top of the range.

Values are either a matplotlib colormap name or a colormap object; PyVista
takes both.
"""

from __future__ import annotations

from matplotlib.colors import LinearSegmentedColormap


def _cmap(name, stops):
    return LinearSegmentedColormap.from_list(name, list(stops))


#: Deep water to shallow.
_BATHY = (
    (0.00, "#081a34"), (0.22, "#0e3a66"), (0.45, "#166996"),
    (0.68, "#349eb8"), (0.86, "#7ecbce"), (1.00, "#c4e7e1"),
)

#: Flat ground to the steepest flanks.
_SLOPE_GR = (
    (0.00, "#1d3b2a"), (0.30, "#3f7f4f"), (0.55, "#d9c65c"),
    (0.78, "#d1761f"), (1.00, "#8e1f16"),
)

#: Quiet until it matters: near-flat seabed stays dark and unobtrusive, and
#: only the steep ground lights up.
_SLOPE_ALERT = (
    (0.00, "#14232b"), (0.25, "#1f4f5e"), (0.50, "#3f9e8c"),
    (0.72, "#e8d05a"), (0.88, "#e2762c"), (1.00, "#a01414"),
)

#: A survey rainbow: violet in the deeps, through blue, green and yellow, to
#: red on the highs. More banding than viridis, which is the point here - small
#: depth changes separate into visibly different colours.
_RAINBOW_STOPS = (
    (0.00, "#4b0c6b"), (0.16, "#2b3f9e"), (0.34, "#1f9ec4"),
    (0.52, "#3fbf6f"), (0.68, "#d9d93f"), (0.84, "#e88a2a"),
    (1.00, "#b30326"),
)

#: Offered when colouring by depth, in menu order.
DEPTH_RAMPS = {
    "Bathy": _cmap("bathy", _BATHY),
    "Rainbow": _cmap("bathy_rainbow", _RAINBOW_STOPS),
    "Turbo": "turbo",
    "Spectral": "Spectral_r",
    "Ocean": "ocean",
    "Viridis": "viridis",
    "Terrain": "terrain",
    "Grey": "gray",
    "Hillshade only": "gray",
}

#: Offered when colouring by slope angle.
SLOPE_RAMPS = {
    "Green to red": _cmap("slope_gr", _SLOPE_GR),
    "Steep alert": _cmap("slope_alert", _SLOPE_ALERT),
    "Turbo": "turbo",
    "Heat": "inferno",
    "Yellow-orange-red": "YlOrRd",
    "Viridis": "viridis",
    "Magma": "magma",
    "Grey": "gray",
}

#: Ramp that paints nothing and leaves the hillshade to carry the relief.
FLAT_RAMP = "Hillshade only"


def ramps_for(color_by: str) -> dict:
    return SLOPE_RAMPS if color_by == "Slope" else DEPTH_RAMPS


def ramp(color_by: str, name: str):
    table = ramps_for(color_by)
    if name in table:
        return table[name]
    return next(iter(table.values()))


def default_ramp(color_by: str) -> str:
    return next(iter(ramps_for(color_by)))
