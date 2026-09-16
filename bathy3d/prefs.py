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


def settings() -> QtCore.QSettings:
    return QtCore.QSettings(ORG, APP)


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


def overlays() -> list:
    """Shapefiles that were open last time, minus any that have since gone."""
    v = settings().value("files/overlays", [])
    if isinstance(v, str):
        v = [v]
    return [p for p in (v or []) if p and os.path.exists(p)]


def set_overlays(paths) -> None:
    set_value("files/overlays", list(paths))


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
    "view/color_by": ("Depth", str),
    "view/detail": ("Medium (1.5 M cells)", str),
    "view/left_action": ("rotate", str),
    "view/lock_z": (True, bool),
    "view/trail": ("10 minutes", str),
    "feed/port": (6451, int),
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
