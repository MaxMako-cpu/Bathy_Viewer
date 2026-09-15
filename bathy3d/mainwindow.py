"""Main window: control rail, 3D view, readout and measurement panels."""

from __future__ import annotations

import math
import os
import time
import traceback

import numpy as np

from PySide6 import QtCore, QtGui, QtWidgets

from . import raster
from .feed import DEFAULT_PORT, ORDER, PositionFeed, STALE_AFTER, explain
from .measure import compass
from .ramps import DEPTH_RAMPS
from .targets import DEFAULT_TARGETS
from .viewer import TerrainView

OPEN_FILTER = (
    "Raster grids (*.tif *.tiff *.vrt *.img *.bag *.asc *.grd *.nc);;All files (*)"
)

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
        self._last_fix = None
        self._build_controls()
        self._build_readout()
        self._build_menu()
        self.statusBar().showMessage("No grid loaded - File › Open, or drop a GeoTIFF here")

        self._stale_timer = QtCore.QTimer(self)
        self._stale_timer.timeout.connect(self._check_stale)
        self._stale_timer.start(1000)

        if path:
            QtCore.QTimer.singleShot(60, lambda: self.open_path(path))

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
        self.by_c.currentTextChanged.connect(self.view.set_color_by)
        self.ramp_c = QtWidgets.QComboBox()
        self.ramp_c.addItems(list(DEPTH_RAMPS))
        self.ramp_c.currentTextChanged.connect(self.view.set_ramp)
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
        hint = QtWidgets.QLabel(
            "Drag orbits · wheel zooms · middle-drag pans.\n"
            "A click drops a station on the seabed."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        ml.addWidget(hint)
        v.addWidget(meas)

        tg = QtWidgets.QGroupBox("Position feed")
        tl = QtWidgets.QVBoxLayout(tg)
        prow = QtWidgets.QWidget()
        ph = QtWidgets.QHBoxLayout(prow)
        ph.setContentsMargins(0, 0, 0, 0)
        ph.addWidget(self._key("UDP port"))
        self.port_s = QtWidgets.QSpinBox()
        self.port_s.setRange(1, 65535)
        self.port_s.setValue(DEFAULT_PORT)
        self.port_s.setGroupSeparatorShown(False)
        ph.addWidget(self.port_s, 1)
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
        self.tgt_table = QtWidgets.QTableWidget(len(ORDER), 5)
        self.tgt_table.setHorizontalHeaderLabels(["Target", "Easting", "Northing",
                                                  "Seabed m", "Age"])
        self.tgt_table.verticalHeader().setVisible(False)
        self.tgt_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tgt_table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeToContents)
        self.tgt_table.setFixedHeight(28 + 24 * len(ORDER))
        for r, nm in enumerate(ORDER):
            item = QtWidgets.QTableWidgetItem(nm)
            item.setForeground(QtGui.QColor(DEFAULT_TARGETS[nm]["color"]))
            self.tgt_table.setItem(r, 0, item)
            for c in range(1, 5):
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
        self.act_reload = m.addAction("&Reload")
        self.act_reload.triggered.connect(lambda: self.open_path(self._path) if self._path else None)
        m.addSeparator()
        m.addAction("Export measurement &CSV…").triggered.connect(self.export_csv)
        m.addAction("Save &screenshot…").triggered.connect(self.save_screenshot)
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
        start = os.path.dirname(self._path) if self._path else os.path.expanduser("~")
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open grid", start, OPEN_FILTER)
        if path:
            self.open_path(path)

    def open_path(self, path: str):
        if self._loader is not None and self._loader.isRunning():
            return
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
        self.view.set_surface(surf)
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

    def _load_failed(self, msg):
        self.prog.close()
        QtWidgets.QMessageBox.critical(self, "Could not open grid", msg)
        self.statusBar().showMessage("Open failed")

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
            if p:
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

    # --------------------------------------------------------- position feed

    def toggle_feed(self, on):
        if not on:
            if self.feed is not None:
                self.feed.stop()
                self.feed.wait(2000)
                self.feed = None
            self.listen_b.setText("Start listening")
            self.feed_status.setText("Stopped")
            return
        if self.view.surface is None:
            QtWidgets.QMessageBox.information(
                self, "Load a grid first",
                "Open a bathymetry grid before starting the feed - target depth "
                "is read from the terrain.")
            self.listen_b.setChecked(False)
            return
        self.feed = PositionFeed(self.port_s.value())
        self.feed.status.connect(self._feed_status)
        self.feed.fix.connect(self._on_fix)
        self.feed.start()
        self.listen_b.setText("Stop listening")
        self.port_s.setEnabled(False)

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
        """A decoded fix, on the GUI thread. Depth comes from the terrain."""
        s = self.view.surface
        if s is None:
            return
        self._last_fix = time.monotonic()
        for r, nm in enumerate(ORDER):
            en = fx.pos.get(nm)
            if en is None:
                continue
            e, n = en
            p = s.probe(e, n)
            surface_vessel = nm == "Vessel" and self.surf_b.isChecked()
            if p is None and not surface_vessel:
                self._set_row(r, e, n, None, "off grid")
                continue
            z = 0.0 if surface_vessel else p.z
            self.view.targets.update(nm, e, n, z)
            self._set_row(r, e, n, None if p is None else -p.z, "0 s")
        self.view.plotter.render()
        self.fix_time.setText(
            f"Fix {fx.t:%H:%M:%S}Z  •  {self.feed.records if self.feed else 0} records"
            f" / {self.feed.packets if self.feed else 0} packets")
        if self.feed:
            self.feed_stats.setText(
                f"{self.feed.packets} pkt  {self.feed.records} rec  {self.feed.bad} bad")

    def _set_row(self, r, e, n, depth, age):
        self.tgt_table.item(r, 1).setText(f"{e:,.2f}")
        self.tgt_table.item(r, 2).setText(f"{n:,.2f}")
        self.tgt_table.item(r, 3).setText("--" if depth is None else f"{depth:,.1f}")
        self.tgt_table.item(r, 4).setText(age)

    def _check_stale(self):
        if self.feed is None:
            return
        self._report_feed()
        if self._last_fix is None:
            return
        age = time.monotonic() - self._last_fix
        for r in range(len(ORDER)):
            self.tgt_table.item(r, 4).setText(f"{age:.0f} s")
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
        self.feed_stats.setText(
            f"{f.packets} pkt   {f.records} rec   {f.bad} unreadable")
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
            self.feed_status.setText(
                f"{f.packets} datagrams from {f.last_addr}, none decoded.\n"
                f"{explain(f.last_raw)}\n"
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

    # ----------------------------------------------------------------- close

    def closeEvent(self, e):
        if self._demo:
            self._demo.stop()
        if self.feed is not None:
            self.feed.stop()
            self.feed.wait(2000)
        self.view.close()
        super().closeEvent(e)
