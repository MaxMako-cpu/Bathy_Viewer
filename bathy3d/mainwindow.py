"""Main window: control rail, 3D view, readout and measurement panels."""

from __future__ import annotations

import math
import os
import time
import traceback

import numpy as np

from PySide6 import QtCore, QtGui, QtWidgets

from . import raster
from .feed import (DEFAULT_DEPTH_PORT, DEFAULT_PORT, DEPTH_ORDER,
                   DEPTH_STALE_AFTER, DepthFeed, DepthFix, ORDER,
                   POSITION_FIELDS, PositionFeed, STALE_AFTER, TETHERS,
                   UMBILICALS, explain)
from .measure import compass
from . import ramps
from .targets import DEFAULT_TARGETS, DEFAULT_TRAIL_SECONDS
from . import prefs, vectors
from .viewer import TerrainView

OPEN_FILTER = (
    "Raster grids (*.tif *.tiff *.vrt *.img *.bag *.asc *.grd *.nc);;All files (*)"
)
SHP_FILTER = "Shapefiles (*.shp);;All files (*)"

#: Trail retention offered in the UI, in seconds. 0 turns trails off.
TRAILS = {
    "Off": 0,
    "1 minute": 60,
    "5 minutes": 300,
    "10 minutes": 600,
    "30 minutes": 1800,
    "1 hour": 3600,
    "3 hours": 10800,
    "6 hours": 21600,
    "12 hours": 43200,
    "24 hours": 86400,
}

DETAIL = {
    "Low (0.4 M cells)": 400_000,
    "Medium (1.5 M cells)": 1_500_000,
    "High (3 M cells)": 3_000_000,
    "Max (6 M cells)": 6_000_000,
}

STYLE = """
QMainWindow, QWidget { background: #0f181c; color: #d7e3e7;
    font-family: "Segoe UI", sans-serif; font-size: 12px; }
QDockWidget { titlebar-close-icon: none; }
QDockWidget::title { background: #16242a; padding: 6px 9px; font-weight: 600;
    letter-spacing: .08em; text-transform: uppercase; font-size: 10px; color: #8fa8b1; }
QGroupBox { border: 1px solid #22343c; border-radius: 3px; margin-top: 16px;
    padding: 10px 9px 9px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px;
    color: #8fa8b1; font-size: 10px; font-weight: 600; letter-spacing: .10em; }
QLabel#big { font-family: Consolas, monospace; font-size: 27px; color: #f0a93c; }
QLabel#unit { color: #7f98a1; font-size: 13px; }
QLabel#mono { font-family: Consolas, monospace; font-size: 12px; color: #e3eef1; }
QLabel#key { color: #8fa8b1; }
QLabel#hint { color: #7f98a1; font-size: 11px; }
QPushButton { background: #1b2b32; border: 1px solid #2b414a; border-radius: 3px;
    padding: 5px 11px; }
QPushButton:hover { border-color: #f0a93c; color: #f0a93c; }
QPushButton:disabled { color: #5c7079; border-color: #22343c; }
QPushButton:checked { background: #f0a93c; color: #14100a; font-weight: 600;
    border-color: #f0a93c; }
QComboBox { background: #1b2b32; border: 1px solid #2b414a; border-radius: 3px;
    padding: 4px 7px; }
QComboBox QAbstractItemView { background: #16242a; selection-background-color: #f0a93c;
    selection-color: #14100a; }
QSlider::groove:horizontal { height: 3px; background: #2b414a; border-radius: 2px; }
QSlider::handle:horizontal { background: #f0a93c; width: 12px; height: 12px;
    margin: -5px 0; border-radius: 6px; }
QTableWidget { background: #121e23; gridline-color: #22343c; border: 1px solid #22343c;
    font-family: Consolas, monospace; font-size: 11px; }
QHeaderView::section { background: #16242a; color: #8fa8b1; border: 0;
    border-bottom: 1px solid #22343c; padding: 4px; font-size: 10px; font-weight: 600; }
QStatusBar { background: #16242a; color: #8fa8b1; font-family: Consolas, monospace;
    font-size: 11px; }
QMenuBar { background: #16242a; } QMenuBar::item:selected { background: #2b414a; }
QMenu { background: #16242a; border: 1px solid #2b414a; }
QMenu::item:selected { background: #2b414a; }
QProgressDialog { background: #16242a; }
"""


def fmt(v, dp=0):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return "--"
    return f"{v:,.{dp}f}"


class Loader(QtCore.QThread):
    done = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    progress = QtCore.Signal(int, str)

    def __init__(self, path, budget):
        super().__init__()
        self.path, self.budget = path, budget

    def run(self):
        try:
            surf = raster.load(
                self.path,
                point_budget=self.budget,
                progress=lambda p, m: self.progress.emit(int(p), m),
            )
            self.done.emit(surf)
        except Exception as exc:  # surfaced in a dialog, not swallowed
            self.failed.emit(f"{exc}\n\n{traceback.format_exc(limit=3)}")


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, path: str | None = None):
        super().__init__()
        self.setWindowTitle("Bathy3D")
        self.resize(1560, 940)
        self.setStyleSheet(STYLE)
        self.setAcceptDrops(True)
        self._path = None

        self.view = TerrainView(self)
        self.setCentralWidget(self.view)
        self.view.hovered.connect(self._on_hover)
        self.view.measureChanged.connect(self._refresh_measure)

        self._loader = None
        self._demo = None
        self.feed = None
        self.dfeed = None
        self._last_fix = None
        self._last_depth = None
        self._positions = {}
        self._depths = {}
        self._framed_feed = None
        self._ramp_choice = {}
        self._build_controls()
        self._build_readout()
        self._build_menu()
        self._apply_saved_view()
        self.statusBar().showMessage("No grid loaded - File › Open, or drop a GeoTIFF here")

        self._stale_timer = QtCore.QTimer(self)
        self._stale_timer.timeout.connect(self._check_stale)
        self._stale_timer.start(1000)

        self._restore_overlays = None
        start = path
        if start is None and prefs.restore_on_start():
            start = prefs.last_grid()
            if start:
                self._restore_overlays = prefs.overlays()
        if start:
            QtCore.QTimer.singleShot(60, lambda p=start: self.open_path(p))
        geo = prefs.geometry()
        if geo is not None:
            try:
                self.restoreGeometry(geo)
            except Exception:
                pass

    # ------------------------------------------------------------- left rail

    def _build_controls(self):
        dock = QtWidgets.QDockWidget("Controls", self)
        dock.setAllowedAreas(QtCore.Qt.LeftDockWidgetArea | QtCore.Qt.RightDockWidgetArea)
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(10)

        relief = QtWidgets.QGroupBox("Relief")
        rl = QtWidgets.QFormLayout(relief)
        rl.setLabelAlignment(QtCore.Qt.AlignLeft)
        self.ve_s, self.ve_l = self._slider(10, 250, 60, lambda v: f"{v / 10:.1f}×")
        self.ve_s.valueChanged.connect(lambda v: self.view.set_ve(v / 10.0))
        self.az_s, self.az_l = self._slider(0, 359, 315, lambda v: f"{v}°")
        self.az_s.valueChanged.connect(lambda v: self.view.set_sun(az=v))
        self.al_s, self.al_l = self._slider(5, 85, 40, lambda v: f"{v}°")
        self.al_s.valueChanged.connect(lambda v: self.view.set_sun(alt=v))
        for lbl, s, o in (("Exaggeration", self.ve_s, self.ve_l),
                          ("Sun azimuth", self.az_s, self.az_l),
                          ("Sun altitude", self.al_s, self.al_l)):
            row = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(s, 1)
            h.addWidget(o)
            rl.addRow(self._key(lbl), row)
        v.addWidget(relief)

        surf = QtWidgets.QGroupBox("Surface")
        sl = QtWidgets.QFormLayout(surf)
        self.by_c = QtWidgets.QComboBox()
        self.by_c.addItems(["Depth", "Slope"])
        self.by_c.currentTextChanged.connect(self._color_by_changed)
        self.ramp_c = QtWidgets.QComboBox()
        self.ramp_c.addItems(list(ramps.DEPTH_RAMPS))
        self.ramp_c.currentTextChanged.connect(self._ramp_changed)
        self.detail_c = QtWidgets.QComboBox()
        self.detail_c.addItems(list(DETAIL))
        self.detail_c.setCurrentText("Medium (1.5 M cells)")
        self.detail_c.currentTextChanged.connect(self._reload_for_detail)
        sl.addRow(self._key("Colour by"), self.by_c)
        sl.addRow(self._key("Ramp"), self.ramp_c)
        sl.addRow(self._key("Mesh detail"), self.detail_c)
        v.addWidget(surf)

        meas = QtWidgets.QGroupBox("Pointer")
        ml = QtWidgets.QVBoxLayout(meas)
        self.meas_b = QtWidgets.QPushButton("Measure mode")
        self.meas_b.setCheckable(True)
        self.meas_b.setChecked(True)
        self.meas_b.toggled.connect(lambda on: setattr(self.view, "measuring", on))
        ml.addWidget(self.meas_b)
        drow = QtWidgets.QWidget()
        dh = QtWidgets.QHBoxLayout(drow)
        dh.setContentsMargins(0, 0, 0, 0)
        dh.addWidget(self._key("Left drag"))
        self.drag_c = QtWidgets.QComboBox()
        self.drag_c.addItems(["Rotate", "Move map"])
        self.drag_c.currentTextChanged.connect(
            lambda t: self.view.set_left_action("pan" if t == "Move map" else "rotate"))
        dh.addWidget(self.drag_c, 1)
        ml.addWidget(drow)
        self.lockz_b = QtWidgets.QPushButton("Lock Z axis")
        self.lockz_b.setCheckable(True)
        self.lockz_b.setChecked(True)
        self.lockz_b.setToolTip(
            "Rotating then swings the map round at a fixed viewing angle, and "
            "moving it keeps your altitude instead of drifting.")
        self.lockz_b.toggled.connect(lambda on: setattr(self.view, "lock_z", on))
        ml.addWidget(self.lockz_b)
        hint = QtWidgets.QLabel(
            "Left-drag moves the map · shift-left orbits\n"
            "wheel zooms · a click drops a station."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        ml.addWidget(hint)
        v.addWidget(meas)

        ov = QtWidgets.QGroupBox("Overlays")
        ol = QtWidgets.QVBoxLayout(ov)
        self.ov_list = QtWidgets.QListWidget()
        self.ov_list.setMaximumHeight(96)
        self.ov_list.itemChanged.connect(self._overlay_toggled)
        self.ov_list.itemDoubleClicked.connect(self.pick_overlay_colour)
        ol.addWidget(self.ov_list)
        orow = QtWidgets.QWidget()
        oh = QtWidgets.QHBoxLayout(orow)
        oh.setContentsMargins(0, 0, 0, 0)
        add_b = QtWidgets.QPushButton("Add shapefile…")
        add_b.clicked.connect(self.add_overlay_dialog)
        self.ov_colour_b = QtWidgets.QPushButton("Colour…")
        self.ov_colour_b.clicked.connect(self.pick_overlay_colour)
        rm_b = QtWidgets.QPushButton("Remove")
        rm_b.clicked.connect(self.remove_overlay)
        oh.addWidget(add_b, 1)
        oh.addWidget(self.ov_colour_b)
        oh.addWidget(rm_b)
        ol.addWidget(orow)
        self.ov_hint = QtWidgets.QLabel(
            "Points, lines and polygons, draped on the seabed.\n"
            "Double-click a layer to recolour it.")
        self.ov_hint.setObjectName("hint")
        self.ov_hint.setWordWrap(True)
        ol.addWidget(self.ov_hint)
        v.addWidget(ov)

        tg = QtWidgets.QGroupBox("Position feed")
        tl = QtWidgets.QVBoxLayout(tg)
        prow = QtWidgets.QWidget()
        ph = QtWidgets.QHBoxLayout(prow)
        ph.setContentsMargins(0, 0, 0, 0)
        ph.addWidget(self._key("Positions"))
        self.port_s = QtWidgets.QSpinBox()
        self.port_s.setRange(1, 65535)
        self.port_s.setValue(DEFAULT_PORT)
        self.port_s.setGroupSeparatorShown(False)
        ph.addWidget(self.port_s, 1)
        ph.addWidget(self._key("Depths"))
        self.dport_s = QtWidgets.QSpinBox()
        self.dport_s.setRange(1, 65535)
        self.dport_s.setValue(DEFAULT_DEPTH_PORT)
        self.dport_s.setGroupSeparatorShown(False)
        ph.addWidget(self.dport_s, 1)
        tl.addWidget(prow)

        self.listen_b = QtWidgets.QPushButton("Start listening")
        self.listen_b.setCheckable(True)
        self.listen_b.toggled.connect(self.toggle_feed)
        tl.addWidget(self.listen_b)

        self.feed_status = QtWidgets.QLabel("Stopped")
        self.feed_status.setObjectName("hint")
        self.feed_status.setWordWrap(True)
        tl.addWidget(self.feed_status)
        self.feed_stats = QtWidgets.QLabel("")
        self.feed_stats.setObjectName("mono")
        tl.addWidget(self.feed_stats)

        self.tgt_b = QtWidgets.QPushButton("Show targets")
        self.tgt_b.setCheckable(True)
        self.tgt_b.setChecked(True)
        self.tgt_b.toggled.connect(lambda on: (self.view.targets.set_visible(on),
                                               self.view.plotter.render()))
        tl.addWidget(self.tgt_b)
        self.tms_b = QtWidgets.QPushButton("Show TMS")
        self.tms_b.setCheckable(True)
        self.tms_b.setChecked(True)
        self.tms_b.setToolTip(
            "The tether management systems, drawn as 3 m x 2 m cylinders at "
            "their own depth, each on a dotted tether to its ROV.")
        self.tms_b.toggled.connect(self._tms_toggled)
        tl.addWidget(self.tms_b)
        self.surf_b = QtWidgets.QPushButton("Vessel at sea surface")
        self.surf_b.setCheckable(True)
        self.surf_b.setChecked(False)
        # Without this the trail keeps the points from the other height and
        # draws a kilometre-high spike between the surface and the seabed.
        self.surf_b.toggled.connect(
            lambda _on: (self.view.targets.clear_trail("Vessel"),
                         self.view.plotter.render()))
        tl.addWidget(self.surf_b)
        self.zoom_b = QtWidgets.QPushButton("Zoom to targets")
        self.zoom_b.clicked.connect(self.zoom_to_targets)
        tl.addWidget(self.zoom_b)
        self.follow_b = QtWidgets.QPushButton("Follow targets")
        self.follow_b.setCheckable(True)
        self.follow_b.setToolTip(
            "Keep the camera centred on the vehicles as they move, "
            "without changing zoom.")
        tl.addWidget(self.follow_b)
        trow = QtWidgets.QWidget()
        th = QtWidgets.QHBoxLayout(trow)
        th.setContentsMargins(0, 0, 0, 0)
        th.addWidget(self._key("Trail"))
        self.trail_c = QtWidgets.QComboBox()
        self.trail_c.addItems(list(TRAILS))
        self.trail_c.setCurrentText(
            next(k for k, v in TRAILS.items() if v == int(DEFAULT_TRAIL_SECONDS)))
        self.trail_c.currentTextChanged.connect(self._trail_changed)
        th.addWidget(self.trail_c, 1)
        tl.addWidget(trow)
        trails = QtWidgets.QPushButton("Clear trails")
        trails.clicked.connect(lambda: (self.view.targets.clear_trail(),
                                        self.view.plotter.render()))
        tl.addWidget(trails)
        v.addWidget(tg)

        v.addStretch(1)
        grid = QtWidgets.QGroupBox("Grid")
        gl = QtWidgets.QFormLayout(grid)
        self.info = {}
        for k in ("File", "CRS", "Size", "Native cell", "Mesh cell", "Range"):
            lab = QtWidgets.QLabel("--")
            lab.setObjectName("mono")
            lab.setWordWrap(True)
            self.info[k] = lab
            gl.addRow(self._key(k), lab)
        v.addWidget(grid)

        # Scrolls, so the Grid box at the bottom survives a short window.
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(w)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        dock.setWidget(scroll)
        dock.setMinimumWidth(286)
        self.addDockWidget(QtCore.Qt.LeftDockWidgetArea, dock)
        self.dock_controls = dock

    def _slider(self, lo, hi, val, fmtf):
        s = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(val)
        out = QtWidgets.QLabel(fmtf(val))
        out.setObjectName("mono")
        out.setMinimumWidth(46)
        out.setAlignment(QtCore.Qt.AlignRight)
        s.valueChanged.connect(lambda v: out.setText(fmtf(v)))
        return s, out

    def _key(self, text):
        lab = QtWidgets.QLabel(text)
        lab.setObjectName("key")
        return lab

    # ------------------------------------------------------------ right rail

    def _build_readout(self):
        dock = QtWidgets.QDockWidget("Readout", self)
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(10)

        cur = QtWidgets.QGroupBox("At cursor")
        cl = QtWidgets.QFormLayout(cur)
        self.big = QtWidgets.QLabel("--")
        self.big.setObjectName("big")
        self.big_key = QtWidgets.QLabel("Depth")
        self.big_key.setObjectName("key")
        row = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.big)
        u = QtWidgets.QLabel("m")
        u.setObjectName("unit")
        h.addWidget(u)
        h.addStretch(1)
        cl.addRow(self.big_key, row)
        self.cells = {}
        for k in ("Slope", "Downslope", "Easting", "Northing", "Latitude", "Longitude", "Pixel"):
            lab = QtWidgets.QLabel("--")
            lab.setObjectName("mono")
            self.cells[k] = lab
            cl.addRow(self._key(k), lab)
        v.addWidget(cur)

        mg = QtWidgets.QGroupBox("Measured line")
        mv = QtWidgets.QVBoxLayout(mg)
        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Leg", "Horiz m", "dz m", "Grad°", "Brg"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.table.setMinimumHeight(170)
        mv.addWidget(self.table)
        self.totals = {}
        for k in ("Horizontal", "Along seabed", "Straight chord", "Net drop"):
            r = QtWidgets.QWidget()
            hh = QtWidgets.QHBoxLayout(r)
            hh.setContentsMargins(0, 0, 0, 0)
            hh.addWidget(self._key(k))
            hh.addStretch(1)
            lab = QtWidgets.QLabel("--")
            lab.setObjectName("mono")
            self.totals[k] = lab
            hh.addWidget(lab)
            mv.addWidget(r)
        btns = QtWidgets.QWidget()
        bh = QtWidgets.QHBoxLayout(btns)
        bh.setContentsMargins(0, 4, 0, 0)
        for text, fn in (("Undo", self.view.undo_measure),
                         ("Clear", self.view.clear_measure),
                         ("Export CSV", self.export_csv)):
            b = QtWidgets.QPushButton(text)
            b.clicked.connect(fn)
            bh.addWidget(b)
        mv.addWidget(btns)
        v.addWidget(mg)

        lg = QtWidgets.QGroupBox("Live positions")
        lv = QtWidgets.QVBoxLayout(lg)
        self.tgt_table = QtWidgets.QTableWidget(len(ORDER), 7)
        self.tgt_table.setHorizontalHeaderLabels(
            ["Target", "Easting", "Northing", "Depth", "Alt", "Speed", "Age"])
        self.tgt_table.verticalHeader().setVisible(False)
        self.tgt_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tgt_table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeToContents)
        self.tgt_table.setFixedHeight(28 + 24 * len(ORDER))
        for r, nm in enumerate(ORDER):
            item = QtWidgets.QTableWidgetItem(nm)
            item.setForeground(QtGui.QColor(DEFAULT_TARGETS[nm]["color"]))
            self.tgt_table.setItem(r, 0, item)
            for c in range(1, 7):
                cell = QtWidgets.QTableWidgetItem("--")
                cell.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                self.tgt_table.setItem(r, c, cell)
        lv.addWidget(self.tgt_table)
        self.fix_time = QtWidgets.QLabel("No fix received")
        self.fix_time.setObjectName("hint")
        lv.addWidget(self.fix_time)
        v.addWidget(lg)
        v.addStretch(1)

        dock.setWidget(w)
        dock.setMinimumWidth(300)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock)
        self.dock_readout = dock

    # ----------------------------------------------------------------- menus

    def _build_menu(self):
        m = self.menuBar().addMenu("&File")
        act = m.addAction("&Open grid…")
        act.setShortcut(QtGui.QKeySequence.Open)
        act.triggered.connect(self.open_dialog)
        m.addAction("Add shapefile &overlay…").triggered.connect(
            self.add_overlay_dialog)
        self.act_reload = m.addAction("&Reload")
        self.act_reload.triggered.connect(lambda: self.open_path(self._path) if self._path else None)
        m.addSeparator()
        m.addAction("Export measurement &CSV…").triggered.connect(self.export_csv)
        m.addAction("Save &screenshot…").triggered.connect(self.save_screenshot)
        m.addSeparator()
        m.addAction("Forget remembered files").triggered.connect(
            self.forget_session)
        m.addSeparator()
        q = m.addAction("E&xit")
        q.setShortcut(QtGui.QKeySequence.Quit)
        q.triggered.connect(self.close)

        vm = self.menuBar().addMenu("&View")
        vm.addAction("&Reset camera").triggered.connect(self.view.reset_view)
        vm.addAction("&Plan view").triggered.connect(self.view.plan_view)
        vm.addAction("Zoom to &targets").triggered.connect(self.zoom_to_targets)
        vm.addSeparator()
        vm.addAction("Controls panel").triggered.connect(
            lambda: self.dock_controls.setVisible(not self.dock_controls.isVisible()))
        vm.addAction("Readout panel").triggered.connect(
            lambda: self.dock_readout.setVisible(not self.dock_readout.isVisible()))
        vm.addSeparator()
        self.act_demo = vm.addAction("Demo target feed (test)")
        self.act_demo.setCheckable(True)
        self.act_demo.toggled.connect(self.toggle_demo_targets)

    # ------------------------------------------------------------- file open

    def open_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open grid", prefs.last_dir("grid"), OPEN_FILTER)
        if path:
            prefs.set_last_dir("grid", path)
            self.open_path(path)

    def open_path(self, path: str):
        if self._loader is not None and self._loader.isRunning():
            return
        # Overlays are draped on the terrain, so loading a grid drops them.
        # Remember them and put them back once the new surface is up -
        # otherwise every change of Mesh detail costs you your shapefiles.
        # Anything already queued (the startup restore) wins over this.
        if self._restore_overlays is None:
            self._restore_overlays = self._overlay_entries()
        self.prog = QtWidgets.QProgressDialog("Reading grid…", "", 0, 100, self)
        self.prog.setCancelButton(None)
        self.prog.setWindowTitle(os.path.basename(path))
        self.prog.setWindowModality(QtCore.Qt.WindowModal)
        self.prog.setMinimumDuration(0)
        self.prog.setValue(0)
        budget = DETAIL[self.detail_c.currentText()]
        self._loader = Loader(path, budget)
        self._loader.progress.connect(lambda p, m: (self.prog.setValue(p), self.prog.setLabelText(m)))
        self._loader.done.connect(self._loaded)
        self._loader.failed.connect(self._load_failed)
        self._loader.start()

    def _loaded(self, surf):
        self.prog.setValue(100)
        self.prog.close()
        self._path = surf.path
        prefs.set_last_grid(surf.path)
        prefs.set_last_dir("grid", surf.path)
        self.view.set_surface(surf)
        self.ov_list.clear()
        self.setWindowTitle(f"Bathy3D — {os.path.basename(surf.path)}")

        zv = surf.z_disp[~np.isnan(surf.z_disp)]
        lo, hi = (float(zv.min()), float(zv.max())) if zv.size else (0.0, 0.0)
        self.big_key.setText("Depth" if hi <= 0 else "Elevation")
        crs = surf.crs
        name = getattr(crs, "name", None) or str(crs)
        epsg = None
        try:
            epsg = crs.to_epsg()
        except Exception:
            pass
        ew, nh = surf.extent_m
        self.info["File"].setText(os.path.basename(surf.path))
        self.info["CRS"].setText(f"{name}" + (f" (EPSG:{epsg})" if epsg else ""))
        self.info["Size"].setText(f"{surf.width:,} × {surf.height:,} px"
                                  f"  •  {ew / 1000:.1f} × {nh / 1000:.1f} km")
        self.info["Native cell"].setText(f"{surf.native_cell_m:.2f} m")
        self.info["Mesh cell"].setText(
            f"{surf.cell_m:.1f} m  (decimated {surf.step}×)")
        self.info["Range"].setText(f"{fmt(lo, 1)} … {fmt(hi, 1)} m")

        probe_note = "" if surf.probe_step == 1 else (
            f"  •  readout probe decimated {surf.probe_step}× (grid too large for RAM)")
        band_note = "" if surf.band_count == 1 else f"  •  band 1 of {surf.band_count}"
        self.statusBar().showMessage(
            f"{surf.width:,}×{surf.height:,} @ {surf.native_cell_m:.2f} m"
            f"  •  mesh {surf.z_disp.shape[1]:,}×{surf.z_disp.shape[0]:,}"
            f"  •  nodata {surf.nodata}{band_note}{probe_note}"
        )
        self._refresh_measure()
        if self._restore_overlays:
            wanted, self._restore_overlays = self._restore_overlays, None
            lost = []
            for p, colour in wanted:
                if not self.add_overlay_path(p, remember=False, color=colour,
                                             quiet=True):
                    lost.append(os.path.basename(p))
            if lost:
                # A layer that does not reach the new grid is worth a line, not
                # a dialog per file.
                self.statusBar().showMessage(
                    "Overlay not on this grid, dropped: " + ", ".join(lost), 8000)
        self._restore_overlays = None

    def _load_failed(self, msg):
        self.prog.close()
        QtWidgets.QMessageBox.critical(self, "Could not open grid", msg)
        self.statusBar().showMessage("Open failed")

    def _color_by_changed(self, what):
        """Depth and slope get their own ramp menus, and their own last choice."""
        self._ramp_choice[self.view.color_by] = self.ramp_c.currentText()
        names = list(ramps.ramps_for(what))
        chosen = self._ramp_choice.get(what) or ramps.default_ramp(what)
        if chosen not in names:
            chosen = names[0]
        self.ramp_c.blockSignals(True)
        self.ramp_c.clear()
        self.ramp_c.addItems(names)
        self.ramp_c.setCurrentText(chosen)
        self.ramp_c.blockSignals(False)
        self._ramp_choice[what] = chosen
        self.view.set_surface_colours(what, chosen)

    def _ramp_changed(self, name):
        if not name:
            return
        self._ramp_choice[self.view.color_by] = name
        self.view.set_ramp(name)

    def _reload_for_detail(self, _):
        if self._path:
            self.open_path(self._path)

    # ------------------------------------------------------- drag and drop

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if not p:
                continue
            if p.lower().endswith(".shp"):
                self.add_overlay_path(p)
            else:
                self.open_path(p)
            break

    # ------------------------------------------------------------- readout

    def _on_hover(self, p):
        if p is None:
            self.big.setText("--")
            for lab in self.cells.values():
                lab.setText("--")
            return
        depth_mode = self.big_key.text() == "Depth"
        self.big.setText(fmt(-p.z if depth_mode else p.z, 1))
        self.cells["Slope"].setText(f"{p.slope:.2f}°" if math.isfinite(p.slope) else "--")
        self.cells["Downslope"].setText(
            f"{compass(p.aspect)} {p.aspect:.0f}°" if math.isfinite(p.aspect) else "flat")
        geo = self.view.surface.geographic
        self.cells["Easting"].setText(f"{p.x:,.6f}°" if geo else f"{p.x:,.1f}")
        self.cells["Northing"].setText(f"{p.y:,.6f}°" if geo else f"{p.y:,.1f}")
        self.cells["Latitude"].setText(f"{p.lat:.6f}°" if math.isfinite(p.lat) else "--")
        self.cells["Longitude"].setText(f"{p.lon:.6f}°" if math.isfinite(p.lon) else "--")
        self.cells["Pixel"].setText(f"r{p.row:,.0f} c{p.col:,.0f}")

    def _refresh_measure(self):
        line = self.view.line
        legs = line.legs() if line else []
        self.table.setRowCount(len(legs))
        for i, l in enumerate(legs):
            vals = (f"{i + 1}–{i + 2}", fmt(l.horizontal, 0),
                    f"{l.dz:+,.1f}", f"{l.gradient:.2f}", compass(l.bearing))
            for c, text in enumerate(vals):
                item = QtWidgets.QTableWidgetItem(text)
                if c:
                    item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                self.table.setItem(i, c, item)
        t = line.totals() if line else {}
        self.totals["Horizontal"].setText(fmt(t.get("horizontal"), 0) + " m" if t else "--")
        self.totals["Along seabed"].setText(fmt(t.get("slant"), 0) + " m" if t else "--")
        self.totals["Straight chord"].setText(fmt(t.get("chord"), 0) + " m" if t else "--")
        self.totals["Net drop"].setText(
            f"{t.get('drop', 0):+,.1f} m" if t and legs else "--")

    # -------------------------------------------------------------- exports

    def export_csv(self):
        line = self.view.line
        if not line or len(line) < 1:
            QtWidgets.QMessageBox.information(self, "Nothing to export",
                                              "Click at least one station on the seabed first.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export stations", os.path.splitext(self._path or "line")[0] + "_line.csv",
            "CSV (*.csv)")
        if path:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(line.to_csv())
            self.statusBar().showMessage(f"Wrote {path}")

    def save_screenshot(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save screenshot", os.path.splitext(self._path or "view")[0] + "_view.png",
            "PNG (*.png)")
        if path:
            self.view.screenshot(path)
            self.statusBar().showMessage(f"Wrote {path}")

    # ------------------------------------------------------------- overlays

    def add_overlay_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Add shapefile overlay", prefs.last_dir("shp"), SHP_FILTER)
        if path:
            prefs.set_last_dir("shp", path)
            self.add_overlay_path(path)

    def add_overlay_path(self, path, remember=True, color=None, quiet=False):
        """Load a shapefile onto the terrain. True if it landed."""
        if self.view.surface is None:
            if not quiet:
                QtWidgets.QMessageBox.information(
                    self, "Load a grid first",
                    "Overlays are draped on the terrain, so a grid has to be open.")
            return False
        colour = color or vectors.PALETTE[
            len(self.view.overlays) % len(vectors.PALETTE)]
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            layer = vectors.load(path, self.view.surface, color=colour)
        except vectors.VectorError as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            if not quiet:
                QtWidgets.QMessageBox.warning(self, "Could not add overlay",
                                              str(exc))
            return False
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            if not quiet:
                QtWidgets.QMessageBox.critical(
                    self, "Could not add overlay",
                    str(exc) + "\n\n" + traceback.format_exc(limit=3))
            return False
        QtWidgets.QApplication.restoreOverrideCursor()

        self.view.remove_overlay(layer.name)
        for i in range(self.ov_list.count()):
            if self.ov_list.item(i).text() == layer.name:
                self.ov_list.takeItem(i)
                break
        self.view.add_overlay(layer)
        item = QtWidgets.QListWidgetItem(layer.name)
        item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
        item.setCheckState(QtCore.Qt.Checked)
        item.setForeground(QtGui.QColor(layer.color))
        item.setToolTip("\n".join([layer.summary(),
                                   f"CRS: {layer.crs_name}",
                                   layer.path]))
        self.ov_list.addItem(item)
        self.ov_list.setCurrentItem(item)   # so Colour... has something to act on
        if remember:
            prefs.set_last_dir("shp", path)
        self._save_overlay_list()
        self.ov_hint.setText(f"{layer.name}: {layer.summary()}")
        note = "" if layer.dropped == 0 else f"  •  {layer.dropped:,} vertices off grid"
        self.statusBar().showMessage(
            f"Overlay {layer.name}: {layer.summary()}  •  {layer.crs_name}{note}", 8000)
        return True

    def _trail_changed(self, text):
        self.view.targets.set_trail_seconds(TRAILS.get(text, 600))
        self.view.plotter.render()
        secs = TRAILS.get(text, 600)
        if secs:
            self.statusBar().showMessage(
                f"Trails keep the last {text.lower()} of track", 4000)
        else:
            self.statusBar().showMessage("Trails off", 4000)

    def pick_overlay_colour(self, item=None):
        """Recolour one overlay. Reached from the button or a double-click."""
        if not isinstance(item, QtWidgets.QListWidgetItem):
            # A QListWidget selects nothing of its own accord, so straight after
            # adding a layer there is no current item and the button would do
            # nothing at all. With only one layer the intent is not in doubt.
            item = self.ov_list.currentItem()
            if item is None and self.ov_list.count() == 1:
                item = self.ov_list.item(0)
                self.ov_list.setCurrentItem(item)
        if item is None:
            QtWidgets.QMessageBox.information(
                self, "Which overlay?",
                "Select an overlay in the list first, or double-click one to "
                "recolour it.")
            return
        layer = self.view.overlays.get(item.text())
        if layer is None:
            return
        chosen = QtWidgets.QColorDialog.getColor(
            QtGui.QColor(layer.color), self, f"Colour for {layer.name}")
        if not chosen.isValid():
            return
        item.setForeground(chosen)
        self.view.set_overlay_colour(layer.name, chosen.name())
        self._save_overlay_list()

    def _overlay_toggled(self, item):
        self.view.set_overlay_visible(item.text(),
                                      item.checkState() == QtCore.Qt.Checked)

    def remove_overlay(self):
        item = self.ov_list.currentItem()
        if item is None:
            return
        self.view.remove_overlay(item.text())
        self.ov_list.takeItem(self.ov_list.row(item))
        self._save_overlay_list()

    def _overlay_entries(self):
        """(path, colour) for every layer, in the order they are listed."""
        entries = []
        for i in range(self.ov_list.count()):
            layer = self.view.overlays.get(self.ov_list.item(i).text())
            if layer is not None:
                entries.append((layer.path, layer.color))
        return entries

    def _save_overlay_list(self):
        prefs.set_overlays(self._overlay_entries())

    # --------------------------------------------------------- position feed

    def toggle_feed(self, on):
        if not on:
            for attr in ("feed", "dfeed"):
                f = getattr(self, attr, None)
                if f is not None:
                    f.stop()
                    f.wait(2000)
                    setattr(self, attr, None)
            self.listen_b.setText("Start listening")
            self.feed_status.setText("Stopped")
            self.port_s.setEnabled(True)
            self.dport_s.setEnabled(True)
            return
        if self.view.surface is None:
            QtWidgets.QMessageBox.information(
                self, "Load a grid first",
                "Open a bathymetry grid before starting the feed - positions "
                "are placed on the terrain.")
            self.listen_b.setChecked(False)
            return
        self._framed_feed = False
        self.feed = PositionFeed(self.port_s.value())
        self.feed.status.connect(self._feed_status)
        self.feed.fix.connect(self._on_fix)
        self.feed.start()
        self.dfeed = DepthFeed(self.dport_s.value())
        self.dfeed.status.connect(self._feed_status)
        self.dfeed.fix.connect(self._on_fix)
        self.dfeed.start()
        self.listen_b.setText("Stop listening")
        self.port_s.setEnabled(False)
        self.dport_s.setEnabled(False)

    def zoom_to_targets(self):
        if not self.view.zoom_to_targets():
            self.statusBar().showMessage("No target positions yet", 4000)

    def _feed_status(self, msg, ok):
        self.feed_status.setText(msg)
        self.feed_status.setStyleSheet("color: #7f98a1;" if ok else "color: #e8663d;")
        if not ok and self.listen_b.isChecked() and msg.startswith("Cannot bind"):
            self.listen_b.setChecked(False)
        if not self.listen_b.isChecked():
            self.port_s.setEnabled(True)

    def _on_fix(self, fx):
        """A decoded record, on the GUI thread.

        Positions and depths arrive on separate ports at their own rate and
        carry no timestamps, so there is no honest way to time-align them.
        Each vehicle keeps its latest of each, and the scene is rebuilt from
        whichever has just changed.
        """
        if isinstance(fx, DepthFix):
            now = time.monotonic()
            for name, metres in fx.depths.items():
                self._depths[name] = (float(metres), now)
            self._last_depth = now
            self._place_targets()
            return

        s = self.view.surface
        if s is None:
            return
        self._last_fix = time.monotonic()
        self._positions = dict(fx.pos)
        self._place_targets()
        self.fix_time.setText(
            f"{self.feed.records if self.feed else 0} position records / "
            f"{self.dfeed.records if self.dfeed else 0} depth records")

    def _place_targets(self):
        """Put every vehicle where its position and depth say it is."""
        s = self.view.surface
        if s is None or not self._positions:
            return
        now = time.monotonic()
        for r, nm in enumerate(ORDER):
            en = self._positions.get(nm)
            if en is None:
                continue
            e, n = en
            p = s.probe(e, n)
            seabed = None if p is None else -p.z
            surface_vessel = nm == "Vessel" and self.surf_b.isChecked()

            depth, age = self._depths.get(nm, (None, 0.0))
            fresh = depth is not None and (now - age) < DEPTH_STALE_AFTER
            if nm == "Vessel":
                # No depth is sent for the vessel; it is at the surface or,
                # by preference, drawn on the bottom beneath itself.
                z = 0.0 if surface_vessel else (None if p is None else p.z)
                shown, alt = (0.0 if surface_vessel else seabed), None
            elif fresh:
                z, shown = -depth, depth
                alt = None if seabed is None else seabed - depth
            else:
                # Depth missing or stale: rest it on the seabed rather than
                # leave it hanging at a frozen depth.
                z, shown, alt = (None if p is None else p.z), seabed, None

            if z is None:
                self._set_row(r, e, n, None, "off grid")
                continue
            tgt = self.view.targets.update(nm, e, n, z)
            if not fresh and nm in DEPTH_ORDER:
                self.view.targets.mark_stale(nm)
            self._set_row(r, e, n, shown, "0 s", tgt.speed, alt)

        self.view.targets.draw_links(TETHERS)
        self.view.targets.draw_links(UMBILICALS)
        if self._framed_feed is False:
            # At full extent a pixel is ~90 m of seabed, so a vehicle moving at
            # 0.6 m/s looks frozen. Frame them once when the first fix lands.
            self._framed_feed = self.view.zoom_to_targets()
        elif self.follow_b.isChecked():
            self.view.follow_targets()
        self.view.plotter.render()

    def _set_row(self, r, e, n, depth, age, speed=None, alt=None):
        self.tgt_table.item(r, 1).setText(f"{e:,.2f}")
        self.tgt_table.item(r, 2).setText(f"{n:,.2f}")
        self.tgt_table.item(r, 3).setText("--" if depth is None else f"{depth:,.1f}")
        cell = self.tgt_table.item(r, 4)
        if alt is None or not math.isfinite(alt):
            cell.setText("--")
            cell.setForeground(QtGui.QColor("#e3eef1"))
        else:
            cell.setText(f"{alt:,.1f}")
            # Below the seabed is not a flying ROV, it is a datum or sign
            # mismatch between the depth feed and the grid. Say so in red
            # rather than draw the vehicle silently buried.
            cell.setForeground(QtGui.QColor("#e8663d" if alt < 0 else "#e3eef1"))
        self.tgt_table.item(r, 5).setText(
            "--" if speed is None or not math.isfinite(speed) else f"{speed:.2f} m/s")
        self.tgt_table.item(r, 6).setText(age)

    def _tms_toggled(self, on):
        self.view.targets.set_tms_visible(on)
        self.view.targets.draw_links(TETHERS)
        self.view.targets.draw_links(UMBILICALS)
        self.view.plotter.render()

    def _check_stale(self):
        if self.feed is None:
            return
        self._report_feed()
        if self._last_fix is None:
            return
        age = time.monotonic() - self._last_fix
        for r in range(len(ORDER)):
            self.tgt_table.item(r, 6).setText(f"{age:.0f} s")
        if age > STALE_AFTER:
            for nm in ORDER:
                self.view.targets.mark_stale(nm)
            self.view.targets.refresh()
            self.view.plotter.render()

    def _report_feed(self):
        """Say what the socket is actually seeing, fix or no fix.

        Counting only decoded fixes hides the two failures that matter most:
        nothing arriving at all, and datagrams arriving in a shape we cannot
        read. They need different answers, so they need different messages.
        """
        f = self.feed
        d = self.dfeed
        self.feed_stats.setText(
            f"pos {f.packets} pkt / {f.records} rec"
            + (f"   depth {d.packets} pkt / {d.records} rec" if d else ""))
        if f.packets == 0:
            waited = time.monotonic() - f.started_at if f.started_at else 0.0
            self.feed_status.setText(
                f"Listening on UDP {f.port} - no datagrams yet ({waited:.0f}s).\n"
                "Check the sender's destination address and port, and that "
                "Windows Firewall allows inbound UDP for python.exe.")
            self.feed_status.setStyleSheet("color: #e5b15a;")
            return
        quiet = time.monotonic() - f.last_packet_at
        if f.records == 0:
            if f.carry_len:
                # Held bytes mean the stream is being read but no record has
                # completed yet - a different fault from unreadable data.
                self.feed_status.setText(
                    f"{f.packets} datagrams from {f.last_addr}, holding "
                    f"{f.carry_len} bytes, no complete record yet.\n"
                    f"{explain(f.last_pending, f.fields)}")
            else:
                self.feed_status.setText(
                    f"{f.packets} datagrams from {f.last_addr}, none decoded.\n"
                    f"{explain(f.last_pending)}\n"
                    f"Last: {f.last_raw[:110]!r}")
            self.feed_status.setStyleSheet("color: #e8663d;")
        else:
            self.feed_status.setText(
                f"Listening on UDP {f.port} - {f.last_addr}, "
                f"last packet {quiet:.0f}s ago")
            self.feed_status.setStyleSheet("color: #7f98a1;")

    # --------------------------------------------- demo feed for the targets

    def toggle_demo_targets(self, on):
        """Exercises the target layer until the real UDP feed lands (step 2)."""
        if not on:
            if self._demo:
                self._demo.stop()
                self._demo = None
            self.view.targets.clear()
            self.view.plotter.render()
            return
        s = self.view.surface
        if s is None:
            self.act_demo.setChecked(False)
            return
        self._t = 0.0
        w, h = s.extent_m

        def tick():
            self._t += 0.02
            for i, name in enumerate(ORDER):
                a = self._t + i * 2.1
                lx = math.cos(a) * w * 0.28
                ly = math.sin(a * 0.7) * h * 0.28
                x, y = s.crs_from_local(lx, ly)
                p = s.probe(x, y)
                if p is None:
                    continue
                z = 0.0 if (name == "Vessel" and self.surf_b.isChecked()) else p.z
                self.view.targets.update(name, x, y, z)
            self.view.plotter.render()

        self._demo = QtCore.QTimer(self)
        self._demo.timeout.connect(tick)
        self._demo.start(120)

    # ------------------------------------------------------------ settings

    def _save_prefs(self):
        try:
            prefs.set_geometry(self.saveGeometry())
            prefs.set_view("view/ve", self.ve_s.value() / 10.0)
            prefs.set_view("view/sun_az", self.az_s.value())
            prefs.set_view("view/sun_alt", self.al_s.value())
            self._ramp_choice[self.view.color_by] = self.ramp_c.currentText()
            prefs.set_view("view/ramp", self._ramp_choice.get("Depth", "Bathy"))
            prefs.set_view("view/ramp_slope",
                           self._ramp_choice.get("Slope", "Green to red"))
            prefs.set_view("view/color_by", self.by_c.currentText())
            prefs.set_view("view/detail", self.detail_c.currentText())
            prefs.set_view("view/left_action", self.view.left_action)
            prefs.set_view("view/lock_z", self.view.lock_z)
            prefs.set_view("view/trail", self.trail_c.currentText())
            prefs.set_view("feed/port", self.port_s.value())
            prefs.set_view("feed/depth_port", self.dport_s.value())
            prefs.set_view("view/show_tms", self.tms_b.isChecked())
            self._save_overlay_list()
        except Exception:
            pass

    def _apply_saved_view(self):
        """Put the controls where they were left, without firing reloads."""
        for widget, value in (
            (self.ve_s, int(round(prefs.view("view/ve") * 10))),
            (self.az_s, prefs.view("view/sun_az")),
            (self.al_s, prefs.view("view/sun_alt")),
            (self.port_s, prefs.view("feed/port")),
            (self.dport_s, prefs.view("feed/depth_port")),
        ):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
        self._ramp_choice = {"Depth": prefs.view("view/ramp"),
                             "Slope": prefs.view("view/ramp_slope")}
        mode = prefs.view("view/color_by")
        names = list(ramps.ramps_for(mode))
        chosen = self._ramp_choice.get(mode) or ramps.default_ramp(mode)
        if chosen not in names:
            chosen = names[0]
        self.ramp_c.blockSignals(True)
        self.ramp_c.clear()
        self.ramp_c.addItems(names)
        self.ramp_c.setCurrentText(chosen)
        self.ramp_c.blockSignals(False)
        self._ramp_choice[mode] = chosen
        for combo, value in (
            (self.by_c, prefs.view("view/color_by")),
            (self.detail_c, prefs.view("view/detail")),
            (self.trail_c, prefs.view("view/trail")),
            (self.drag_c, "Move map" if prefs.view("view/left_action") == "pan"
                          else "Rotate"),
        ):
            combo.blockSignals(True)
            combo.setCurrentText(value)
            combo.blockSignals(False)
        self.ve_l.setText(f"{prefs.view('view/ve'):.1f}\u00d7")
        self.az_l.setText(f"{prefs.view('view/sun_az')}\u00b0")
        self.al_l.setText(f"{prefs.view('view/sun_alt')}\u00b0")
        self.tms_b.blockSignals(True)
        self.tms_b.setChecked(bool(prefs.view("view/show_tms")))
        self.tms_b.blockSignals(False)
        self.view.targets.tms_visible = bool(prefs.view("view/show_tms"))
        self.lockz_b.blockSignals(True)
        self.lockz_b.setChecked(bool(prefs.view("view/lock_z")))
        self.lockz_b.blockSignals(False)
        # Push them into the view, which has fired no signals of its own.
        self.view.ve = prefs.view("view/ve")
        self.view.sun_az = prefs.view("view/sun_az")
        self.view.sun_alt = prefs.view("view/sun_alt")
        self.view.ramp_name = chosen
        self.view.color_by = prefs.view("view/color_by")
        self.view.lock_z = bool(prefs.view("view/lock_z"))
        self.view.set_left_action(prefs.view("view/left_action"))
        self.view.targets.set_trail_seconds(
            TRAILS.get(prefs.view("view/trail"), DEFAULT_TRAIL_SECONDS))

    def forget_session(self):
        prefs.forget_session()
        self.statusBar().showMessage(
            "Forgotten - the next start will open empty", 5000)

    # ----------------------------------------------------------------- close

    def closeEvent(self, e):
        self._save_prefs()
        if self._demo:
            self._demo.stop()
        for attr in ("feed", "dfeed"):
            f = getattr(self, attr, None)
            if f is not None:
                f.stop()
                f.wait(2000)
        self.view.close()
        super().closeEvent(e)
