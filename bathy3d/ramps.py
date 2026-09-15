"""Colour ramps for the surface."""

from __future__ import annotations

from matplotlib.colors import LinearSegmentedColormap

_BATHY = [
    (0.00, "#081a34"),
    (0.22, "#0e3a66"),
    (0.45, "#166996"),
    (0.68, "#349eb8"),
    (0.86, "#7ecbce"),
    (1.00, "#c4e7e1"),
]

_SLOPE = [
    (0.00, "#1d3b2a"),
    (0.30, "#3f7f4f"),
    (0.55, "#d9c65c"),
    (0.78, "#d1761f"),
    (1.00, "#8e1f16"),
]


def _cmap(name, stops):
    return LinearSegmentedColormap.from_list(name, [(p, c) for p, c in stops])


#: Ramps offered for colour-by-depth, in menu order.
DEPTH_RAMPS = {
    "Bathy": _cmap("bathy", _BATHY),
    "Viridis": "viridis",
    "Terrain": "terrain",
    "Hillshade only": "gray",
}

#: Ramp used when colouring by slope angle.
SLOPE_RAMP = _cmap("slope", _SLOPE)


def depth_ramp(name):
    return DEPTH_RAMPS.get(name, DEPTH_RAMPS["Bathy"])
