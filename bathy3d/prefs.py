"""What the app remembers between runs.

Stored with QSettings, so it lands in the registry on Windows and in a plist
or ini elsewhere - no file for anyone to lose track of. Nothing here is
required: every read falls back to a default, so a first run, a wiped profile
and a settings file from an older version all behave the same.
"""

from __future__ import annotations

import os

from PySide6 import QtCore

ORG = "Bathy3D"
APP = "Bathy3D"


#: Set BATHY3D_PROFILE to keep a run's settings apart from the everyday ones.
#: The test suites use it so they neither read nor overwrite real preferences -
#: without it one test leaves an exaggeration behind and the next one fails on
#: it, and a test run quietly rewrites what the user had set.
PROFILE_ENV = "BATHY3D_PROFILE"


def settings() -> QtCore.QSettings:
    profile = os.environ.get(PROFILE_ENV, "").strip()
    return QtCore.QSettings(ORG, f"{APP}-{profile}" if profile else APP)


def _get(key, default=None, cast=None):
    v = settings().value(key, default)
    if v is None:
        return default
    if cast is bool:
        return v if isinstance(v, bool) else str(v).lower() in ("true", "1", "yes")
    if cast is not None:
        try:
            return cast(v)
        except (TypeError, ValueError):
            return default
    return v


def set_value(key, value) -> None:
    settings().setValue(key, value)


# ------------------------------------------------------------------- files

def last_grid() -> str | None:
    p = _get("files/grid")
    return p if p and os.path.exists(p) else None


def set_last_grid(path: str) -> None:
    set_value("files/grid", path)


#: Stored as "#rrggbb|path". Neither a colour nor a Windows path can contain
#: the separator, so splitting once from the left is safe.
_SEP = "|"


def overlays() -> list:
    """(path, colour) for each shapefile open last time, minus any now gone.

    Entries written before colours were stored are plain paths; those come back
    with no colour and the caller picks one.
    """
    v = settings().value("files/overlays", [])
    if isinstance(v, str):
        v = [v]
    out = []
    for item in v or []:
        if not item:
            continue
        colour, text = None, str(item)
        if text.startswith("#") and _SEP in text:
            colour, text = text.split(_SEP, 1)
        if os.path.exists(text):
            out.append((text, colour))
    return out


def set_overlays(entries) -> None:
    """``entries`` is an iterable of (path, colour)."""
    set_value("files/overlays",
              [f"{c}{_SEP}{p}" if c else str(p) for p, c in entries])


def last_dir(kind: str) -> str:
    """Folder the file dialog should open at. ``kind`` is "grid" or "shp"."""
    d = _get(f"dirs/{kind}")
    if d and os.path.isdir(d):
        return d
    other = _get("dirs/grid" if kind == "shp" else "dirs/shp")
    if other and os.path.isdir(other):
        return other
    return os.path.expanduser("~")


def set_last_dir(kind: str, path: str) -> None:
    d = path if os.path.isdir(path) else os.path.dirname(path)
    if d:
        set_value(f"dirs/{kind}", d)


def tiepoints() -> list:
    """Encoded depth-calibration tie-ins, newest last.

    Kept as flat strings so a hand-edited or older-version entry costs one row
    rather than the whole set - :meth:`calib.TiePoint.decode` drops what it
    cannot read.
    """
    v = settings().value("calib/points", [])
    if isinstance(v, str):
        v = [v]
    return [str(x) for x in (v or []) if x]


def set_tiepoints(rows) -> None:
    set_value("calib/points", [str(r) for r in rows])


def restore_on_start() -> bool:
    return _get("session/restore", True, bool)


def set_restore_on_start(on: bool) -> None:
    set_value("session/restore", bool(on))


def forget_session() -> None:
    s = settings()
    for key in ("files/grid", "files/overlays"):
        s.remove(key)


# -------------------------------------------------------------- view state

#: key -> (default, type). Kept in one table so saving and restoring cannot
#: drift apart.
VIEW = {
    "view/ve": (6.0, float),
    "view/sun_az": (315, int),
    "view/sun_alt": (40, int),
    "view/ramp": ("Bathy", str),
    "view/ramp_slope": ("Green to red", str),
    "view/color_by": ("Depth", str),
    "view/detail": ("Medium (1.5 M cells)", str),
    "view/left_action": ("rotate", str),
    "view/lock_z": (True, bool),
    "view/trail": ("10 minutes", str),
    "feed/port": (6451, int),
    "feed/depth_port": (6452, int),
    "view/show_tms": (True, bool),
    # Off until the operator has tied in and decided it works. A correction
    # applied without being asked for would silently move every vehicle.
    "calib/on": (False, bool),
}


def view(key: str):
    default, cast = VIEW[key]
    return _get(key, default, cast)


def set_view(key: str, value) -> None:
    set_value(key, value)


def geometry():
    return settings().value("win/geometry")


def set_geometry(data) -> None:
    set_value("win/geometry", data)
