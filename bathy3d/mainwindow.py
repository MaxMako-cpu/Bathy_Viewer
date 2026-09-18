"""Main window: control rail, 3D view, readout and measurement panels."""

from __future__ import annotations

import math
import os
import time
import traceback

import numpy as np

from PySide6 import QtCore, QtGui, QtWidgets

from . import calib, nodes, raster
from .feed import (BOTTOM_ORDER, DEFAULT_DEPTH_PORT, DEFAULT_PORT, DEPTH_ORDER,
                   DEPTH_STALE_AFTER, DepthFeed, DepthFix, ORDER,
                   POSITION_FIELDS, PositionFeed, STALE_AFTER, TETHERS,
                   UMBILICALS, explain, slot_for as feed_slot_for)
from .measure import bearing_text, compass
from . import ramps
from .targets import DEFAULT_TRAIL_SECONDS
from . import prefs, vectors, vehicles
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

#: Slope-box sizes offered in the UI, in metres. 0 means pick two corners.
BOX_SIZES = {
    "200 m": 200.0,
    "500 m": 500.0,
    "1 km": 1000.0,
    "2 km": 2000.0,
    "5 km": 5000.0,
    "Two corners": 0.0,
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
/* The measured line is read off the screen and written down, often at arm's
   length from the console, so it gets its own larger type. The live position
   table keeps the compact size - it has seven columns and is glanced at, not
   transcribed. */
QTableWidget#measure { font-size: 15px; }
QTableWidget#measure QHeaderView::section { font-size: 12px; padding: 5px 4px; }
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


class SlideDirectionDialog(QtWidgets.QDialog):
    """Asked once, when a case is opened: did anyone see which way it went?

    Sometimes the pilot watches it go and sometimes the node is simply missing,
    so the honest options are a bearing or "nobody saw". An observed direction
    steers the first step of the trace; more usefully, comparing it with the
    grid's own downslope bearing measures how much the terrain model can be
    trusted here.
    """

    def __init__(self, win, label, probe):
        super().__init__(win)
        self.setWindowTitle(f"Node slid at {label}")
        self.setModal(True)
        v = QtWidgets.QVBoxLayout(self)

        head = QtWidgets.QLabel(
            f"<b>{label}</b> is standing on ground of "
            f"<b>{probe.slope:.1f}°</b>, falling away towards "
            f"<b>{bearing_text(probe.aspect)}° {compass(probe.aspect)}</b>.")
        head.setWordWrap(True)
        v.addWidget(head)

        ask = QtWidgets.QLabel("Did anyone see which way the node went?")
        ask.setWordWrap(True)
        ask.setObjectName("hint")
        v.addWidget(ask)

        self.unknown = QtWidgets.QRadioButton("No - nobody saw it move")
        self.unknown.setChecked(True)
        v.addWidget(self.unknown)

        row = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        self.seen = QtWidgets.QRadioButton("Yes, it went towards")
        h.addWidget(self.seen)
        self.deg = QtWidgets.QDoubleSpinBox()
        self.deg.setRange(0.0, 359.9)
        self.deg.setDecimals(1)
        self.deg.setSuffix(" °")
        self.deg.setValue(probe.aspect if math.isfinite(probe.aspect) else 0.0)
        self.deg.setEnabled(False)
        h.addWidget(self.deg)
        self.card = QtWidgets.QLabel(compass(probe.aspect))
        self.card.setObjectName("mono")
        h.addWidget(self.card)
        h.addStretch(1)
        v.addWidget(row)
        self.seen.toggled.connect(self.deg.setEnabled)
        self.deg.valueChanged.connect(
            lambda d: self.card.setText(compass(d)))

        v.addSpacing(6)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

    def ask(self):
        """The observed bearing, NaN for unknown, or None if cancelled."""
        if self.exec() != QtWidgets.QDialog.Accepted:
            return None
        return self.deg.value() if self.seen.isChecked() else float("nan")


class NodesDialog(QtWidgets.QDialog):
    """Open cases at the top, the learned history underneath."""

    COLS = ["ROV", "Placed E", "Placed N", "Slope°", "Downslope", "Seen",
            "Runout m", "Track°", "Off by°", "When"]

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Node slides - open cases and history")
        self.setModal(False)
        self.resize(900, 520)

        v = QtWidgets.QVBoxLayout(self)
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        self.summary.setObjectName("hint")
        v.addWidget(self.summary)

        self.open_box = QtWidgets.QGroupBox("Open")
        self.open_v = QtWidgets.QVBoxLayout(self.open_box)
        v.addWidget(self.open_box)

        v.addWidget(QtWidgets.QLabel("Recovered cases - what the model learns from"))
        self.table = QtWidgets.QTableWidget(0, len(self.COLS), self)
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeToContents)
        v.addWidget(self.table, 1)

        row = QtWidgets.QHBoxLayout()
        self.forget = QtWidgets.QPushButton("Forget selected case")
        self.forget.setToolTip(
            "Drop a case that was recorded in error - a wrong position taints "
            "the fit the same way a bad calibration tie-in does.")
        self.forget.clicked.connect(self._forget)
        row.addWidget(self.forget)
        row.addStretch(1)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.hide)
        row.addWidget(close)
        v.addLayout(row)
        self._open_rows = []

    def refresh(self):
        win = self.win
        self.summary.setText(win.slide_model.describe())

        for w in self._open_rows:
            w.setParent(None)
        self._open_rows = []
        if not win.slide_open:
            lab = QtWidgets.QLabel("No open cases.")
            lab.setObjectName("hint")
            self.open_v.addWidget(lab)
            self._open_rows.append(lab)
        for slot, case in win.slide_open.items():
            w = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(w)
            h.setContentsMargins(0, 0, 0, 0)
            name = QtWidgets.QLabel(win.fleet.label(slot))
            name.setStyleSheet(f"color: {win.fleet.colour(slot)};")
            h.addWidget(name)
            path, _l, _r, why = win._predict(case)
            run = win._case_runout(path)
            told = (f"corridor runs {run:,.0f} m towards "
                    f"{bearing_text(case.placed_aspect)}°"
                    if len(path) >= 2 else "no corridor")
            h.addWidget(QtWidgets.QLabel(
                f"placed on {case.placed_slope:.1f}°, {told} - {why}"))
            h.addStretch(1)
            found = QtWidgets.QPushButton("Node found here")
            found.setToolTip("Record this ROV's position as where the node "
                             "was recovered, and learn from it.")
            found.clicked.connect(lambda _=False, s=slot: (win.node_found(s),
                                                           self.refresh()))
            h.addWidget(found)
            cancel = QtWidgets.QPushButton("Cancel")
            cancel.setToolTip("Close the case. Nothing is recorded.")
            cancel.clicked.connect(lambda _=False, s=slot: (win.node_cancel(s),
                                                            self.refresh()))
            h.addWidget(cancel)
            self.open_v.addWidget(w)
            self._open_rows.append(w)

        done = [c for c in win.slide_cases if c.confirmed]
        self.table.setRowCount(len(done))
        for r, c in enumerate(done):
            cells = [
                win.fleet.label(c.rov), f"{c.placed_x:,.1f}",
                f"{c.placed_y:,.1f}", f"{c.placed_slope:.1f}",
                bearing_text(c.placed_aspect),
                "--" if not math.isfinite(c.observed_dir)
                else bearing_text(c.observed_dir),
                f"{c.runout:,.1f}", bearing_text(c.track),
                f"{c.track_error:+.0f}",
                time.strftime("%d %b %H:%M", time.localtime(c.found_at))
                if math.isfinite(c.found_at) else "",
            ]
            for col, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                if col:
                    item.setTextAlignment(QtCore.Qt.AlignRight
                                          | QtCore.Qt.AlignVCenter)
                else:
                    item.setForeground(QtGui.QColor(win.fleet.colour(c.rov)))
                # A track that missed the grid's downslope badly is the case
                # that widens every future corridor - worth seeing.
                if col == 8 and math.isfinite(c.track_error) \
                        and abs(c.track_error) > 30:
                    item.setForeground(QtGui.QColor("#e8663d"))
                self.table.setItem(r, col, item)
        self.forget.setEnabled(bool(done))

    def _forget(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        done = [c for c in self.win.slide_cases if c.confirmed]
        for r in rows:
            if 0 <= r < len(done):
                try:
                    self.win.slide_cases.remove(done[r])
                except ValueError:
                    pass
        if rows:
            self.win._refit_slides()
            self.refresh()


class FeedDialog(QtWidgets.QDialog):
    """The two UDP ports and what the listener is currently doing.

    It adopts the window's own port and status widgets rather than making its
    own, so there is one of each: saved preferences restore into them at
    startup, before this dialog has ever been built, and the listener writes
    its status straight to them whether anyone is looking or not.
    """

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Position feed - ports and status")
        self.setModal(False)
        self.resize(430, 220)

        v = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QGridLayout()
        form.addWidget(win._key("Positions"), 0, 0)
        form.addWidget(win.port_s, 0, 1)
        form.addWidget(win._key("Depths"), 1, 0)
        form.addWidget(win.dport_s, 1, 1)
        form.setColumnStretch(1, 1)
        v.addLayout(form)

        hint = QtWidgets.QLabel(
            "A grid must be open first - positions are placed on the terrain. "
            "Eastings and northings are read in the loaded grid's CRS.")
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        v.addWidget(hint)

        v.addWidget(win.feed_status)
        v.addWidget(win.feed_stats)
        v.addStretch(1)

        row = QtWidgets.QHBoxLayout()
        self.listen_btn = QtWidgets.QPushButton()
        self.listen_btn.setCheckable(True)
        self.listen_btn.clicked.connect(
            lambda on: win.listen_b.setChecked(on))
        row.addWidget(self.listen_btn)
        row.addStretch(1)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.hide)
        row.addWidget(close)
        v.addLayout(row)

        # The menu entry is the same switch, so the button has to follow it
        # however it was thrown.
        win.listen_b.toggled.connect(self._sync)

    def _sync(self, on=None):
        checked = self.win.listen_b.isChecked()
        self.listen_btn.blockSignals(True)
        self.listen_btn.setChecked(checked)
        self.listen_btn.blockSignals(False)
        self.listen_btn.setText("Stop listening" if checked else "Start listening")

    def refresh(self):
        self._sync()


class FleetDialog(QtWidgets.QDialog):
    """Rename and recolour the bodies for whichever vessel this is.

    What changes here is only what is drawn. Each row's *slot* - the position
    in the wire record that the feed, the tether pairing and every calibration
    tie-in are keyed on - is shown but cannot be edited, because moving it
    would orphan all three.
    """

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Vehicles - names and colours")
        self.setModal(False)
        self.resize(560, 330)
        self._rows = {}

        v = QtWidgets.QVBoxLayout(self)
        hint = QtWidgets.QLabel(
            "Rename a vehicle to whatever this vessel calls it. The name on "
            "the left is the feed slot and never changes, so calibration "
            "tie-ins and tethers survive a rename.")
        hint.setWordWrap(True)
        hint.setObjectName("hint")
        v.addWidget(hint)

        form = QtWidgets.QGridLayout()
        for col, head in enumerate(("Slot", "Name", "Colour")):
            lab = QtWidgets.QLabel(head)
            lab.setObjectName("hint")
            form.addWidget(lab, 0, col)
        for r, slot in enumerate(ORDER, start=1):
            form.addWidget(QtWidgets.QLabel(slot), r, 0)
            edit = QtWidgets.QLineEdit()
            edit.setMaxLength(24)
            edit.editingFinished.connect(
                lambda s=slot: self._renamed(s))
            form.addWidget(edit, r, 1)
            swatch = QtWidgets.QPushButton()
            swatch.setFixedWidth(120)
            if slot in vehicles.FOLLOWS:
                # A TMS has no colour of its own: it is its ROV's, darkened,
                # which is what keeps the pairing readable when the two bodies
                # are metres apart. Offering a picker here would let them
                # drift apart with nothing to put them back.
                swatch.setEnabled(False)
                swatch.setToolTip(
                    f"Follows {vehicles.FOLLOWS[slot]}, darkened - "
                    "set that vehicle's colour instead.")
            else:
                swatch.clicked.connect(lambda _=False, s=slot: self._pick(s))
            form.addWidget(swatch, r, 2)
            self._rows[slot] = (edit, swatch)
        v.addLayout(form)
        v.addStretch(1)

        row = QtWidgets.QHBoxLayout()
        reset = QtWidgets.QPushButton("Reset to defaults")
        reset.clicked.connect(self.win.reset_fleet)
        row.addWidget(reset)
        row.addStretch(1)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.hide)
        row.addWidget(close)
        v.addLayout(row)

    def refresh(self):
        for slot, (edit, swatch) in self._rows.items():
            label = self.win.fleet.label(slot)
            if edit.text() != label:
                edit.blockSignals(True)
                edit.setText(label)
                edit.blockSignals(False)
            colour = self.win.fleet.colour(slot)
            # Readable either way round: white text on a dark pick, black on
            # a pale one, rather than a swatch whose own label vanishes.
            ink = "#101010" if _is_pale(colour) else "#ffffff"
            swatch.setText(colour)
            swatch.setStyleSheet(
                f"background: {colour}; color: {ink}; border: 1px solid #2b414a;")

    def commit(self):
        """Take whatever is in the boxes and make it the fleet's.

        ``editingFinished`` only fires on Enter or on the box losing focus, so
        a name typed and left sitting there is not yet the fleet's. Committing
        on every exit from this dialog - and again when the window closes - is
        what stops a rename being lost by someone who simply quits the app.
        """
        for slot, (edit, _swatch) in self._rows.items():
            self.win.fleet.set_label(slot, edit.text())
        self.win.apply_fleet()

    def hideEvent(self, e):
        self.commit()
        super().hideEvent(e)

    def _renamed(self, slot):
        self.commit()

    def _pick(self, slot):
        current = QtGui.QColor(self.win.fleet.colour(slot))
        chosen = QtWidgets.QColorDialog.getColor(
            current, self, f"Colour for {self.win.fleet.label(slot)}")
        if chosen.isValid():
            self.win.fleet.set_colour(slot, chosen.name())
            self.win.apply_fleet()


def _is_pale(colour: str) -> bool:
    """Rough perceived brightness, for choosing ink over a swatch."""
    c = QtGui.QColor(colour)
    return (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) > 150


class CalibDialog(QtWidgets.QDialog):
    """The tie-in points, what they add up to, and a way to drop a bad one.

    Modeless on purpose: it is read while the vehicles are moving, and a modal
    box over a live plot during a deployment is the wrong thing entirely.
    """

    COLS = ["Vehicle", "Easting", "Northing", "Grid m", "Feed m", "Out by m",
            "Residual m", "When"]

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Depth calibration - tie-in points")
        self.setModal(False)
        self.resize(760, 380)

        v = QtWidgets.QVBoxLayout(self)
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        self.summary.setObjectName("hint")
        v.addWidget(self.summary)

        self.table = QtWidgets.QTableWidget(0, len(self.COLS), self)
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeToContents)
        v.addWidget(self.table, 1)

        row = QtWidgets.QHBoxLayout()
        self.del_b = QtWidgets.QPushButton("Delete selected")
        self.del_b.setToolTip(
            "Drop a tie-in taken when the vehicle was not really on bottom - "
            "one bad point drags the whole fit with it.")
        self.del_b.clicked.connect(self._delete)
        row.addWidget(self.del_b)
        row.addStretch(1)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.hide)
        row.addWidget(close)
        v.addLayout(row)

    def refresh(self):
        c = self.win.calib
        m = c.model
        state = ("applied to every vehicle depth" if c.active
                 else "not applied - switch it on in the Calibration menu"
                 if m.on else "nothing to apply yet")
        self.summary.setText(f"{m.describe()}\nCurrently {state}.")

        # Residual is per-point and only exists once a model is fitted.
        self.table.setRowCount(len(c.points))
        for r, p in enumerate(c.points):
            resid = (p.grid_depth - m.apply(p.feed_depth)) if m.on else None
            when = time.strftime("%d %b %H:%M", time.localtime(p.when))
            # The point stores the slot, so a body renamed after it was tied
            # in still shows up here - under its new name.
            cells = [self.win.fleet.label(p.vehicle), f"{p.x:,.2f}", f"{p.y:,.2f}",
                     f"{p.grid_depth:,.1f}", f"{p.feed_depth:,.1f}",
                     f"{p.offset:+.1f}",
                     "--" if resid is None else f"{resid:+.1f}", when]
            for col, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                if col:
                    item.setTextAlignment(QtCore.Qt.AlignRight
                                          | QtCore.Qt.AlignVCenter)
                else:
                    item.setForeground(QtGui.QColor(
                        self.win.fleet.colour(p.vehicle)))
                # A point sitting far off its own fit is the one to suspect.
                if col == 6 and resid is not None and abs(resid) > 5.0:
                    item.setForeground(QtGui.QColor("#e8663d"))
                self.table.setItem(r, col, item)
        self.del_b.setEnabled(bool(c.points))

    def _delete(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        if not rows:
            return
        for r in rows:
            self.win.calib.remove(r)
        self.win._after_calib_change()


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
        self.view.patchChanged.connect(self._on_patch)
        self.view.boxProgress.connect(
            lambda msg: self.statusBar().showMessage(msg, 6000))

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
        self._patch = None
        # Both built before the panels and menus, which draw the labels and
        # colours these hold.
        self._closing = False
        self.fleet = vehicles.Fleet()
        self.fleet.load(prefs.fleet())
        self.view.targets.set_styles(self.fleet.styles())
        self._fleet_dialog = None
        self._feed_dialog = None
        # Node slides: the whole history, and the open cases keyed by ROV slot.
        self.slide_db = prefs.slide_db()
        self.slide_cases = nodes.load_cases(self.slide_db)
        self.slide_model = nodes.fit(self.slide_cases)
        self.slide_open: dict = {}
        self._nodes_dialog = None
        self.calib = calib.Calibration(prefs.view("calib/on"))
        self.calib.load(prefs.tiepoints())
        # Tie-ins outlive a session, so points saved before the slots were made
        # generic still name one vessel's own vehicles. calib.py is kept free
        # of the feed, so the mapping happens here, once, on the way in.
        for point in self.calib.points:
            point.vehicle = feed_slot_for(point.vehicle)
        self.calib.refit()
        self._calib_dialog = None
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
        # The readout rail opens wide enough for the measured line's six
        # columns. Done before restoring the saved layout so that a width the
        # operator chose themselves wins over this default.
        self.resizeDocks([self.dock_readout], [430], QtCore.Qt.Horizontal)
        state = prefs.dock_state()
        if state is not None:
            try:
                self.restoreState(state)
            except Exception:
                pass
        # A saved layout from an older build can name docks this one no longer
        # has, which leaves the survivors hidden. Nothing here is closeable by
        # design, so put them back rather than starting with a blank window.
        for d in (self.dock_controls, self.dock_readout):
            d.setVisible(True)

    # ------------------------------------------------------------- left rail

    def _build_controls(self):
        dock = QtWidgets.QDockWidget("Controls", self)
        dock.setObjectName("dock_controls")   # saveState needs a stable name
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
        self.meas_b.toggled.connect(self._measure_toggled)
        ml.addWidget(self.meas_b)
        self.box_b = QtWidgets.QPushButton("Slope box")
        self.box_b.setCheckable(True)
        self.box_b.setToolTip(
            "Click the seabed to recompute slope there at the grid's native "
            "resolution, instead of the decimated display mesh.")
        self.box_b.toggled.connect(self._box_toggled)
        ml.addWidget(self.box_b)
        brow = QtWidgets.QWidget()
        bh = QtWidgets.QHBoxLayout(brow)
        bh.setContentsMargins(0, 0, 0, 0)
        bh.addWidget(self._key("Box size"))
        self.box_c = QtWidgets.QComboBox()
        self.box_c.addItems(list(BOX_SIZES))
        self.box_c.currentTextChanged.connect(
            lambda t: setattr(self.view, "box_size", BOX_SIZES.get(t, 200.0)))
        bh.addWidget(self.box_c, 1)
        ml.addWidget(brow)
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

        # The feed's own settings live in the Feed menu, so these four are not
        # added to any panel layout - the dialog adopts them when it is first
        # opened. They are built here, eagerly, because saved preferences are
        # restored into them at startup and they must exist by then whether or
        # not anyone has opened the dialog.
        self.port_s = QtWidgets.QSpinBox()
        self.port_s.setRange(1, 65535)
        self.port_s.setValue(DEFAULT_PORT)
        self.port_s.setGroupSeparatorShown(False)
        self.dport_s = QtWidgets.QSpinBox()
        self.dport_s.setRange(1, 65535)
        self.dport_s.setValue(DEFAULT_DEPTH_PORT)
        self.dport_s.setGroupSeparatorShown(False)
        self.feed_status = QtWidgets.QLabel("Stopped")
        self.feed_status.setObjectName("hint")
        self.feed_status.setWordWrap(True)
        self.feed_stats = QtWidgets.QLabel("")
        self.feed_stats.setObjectName("mono")

        tg = QtWidgets.QGroupBox("Targets")
        tl = QtWidgets.QVBoxLayout(tg)
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
        dock.setObjectName("dock_readout")    # saveState needs a stable name
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
        self.table = QtWidgets.QTableWidget(0, 6)
        # Degrees and the compass point both: the point says roughly where at a
        # glance, the degrees say exactly, and a survey line usually wants both.
        self.table.setObjectName("measure")
        self.table.setHorizontalHeaderLabels(
            ["Leg", "Horiz m", "dz m", "Grad°", "Brg°", "Brg"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        # Sized to content, not stretched to fill. Six columns of 15px type
        # divided evenly across the rail is about 50px each, which truncates
        # "4,940" to "4,9..." and even chops the header - and a distance you
        # cannot read is worse than a small one.
        _mh = self.table.horizontalHeader()
        _mh.setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
        _mh.setStretchLastSection(False)
        # Spare width goes to the distance, not to the last column. Stretching
        # the last one opened a gap between the degrees and the compass point
        # beside them, which are meant to be read as one thing.
        _mh.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        # Taller than the old five columns needed: the bigger type wants more
        # room per row, so the same few legs still fit without scrolling.
        self.table.setMinimumHeight(210)
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

        sg = QtWidgets.QGroupBox("Slope box")
        sv = QtWidgets.QVBoxLayout(sg)
        self.patch_hint = QtWidgets.QLabel(
            "Turn on Slope box and click the seabed.\n"
            "Slope is recomputed there at native resolution.")
        self.patch_hint.setObjectName("hint")
        self.patch_hint.setWordWrap(True)
        sv.addWidget(self.patch_hint)
        pform = QtWidgets.QWidget()
        pf = QtWidgets.QFormLayout(pform)
        pf.setContentsMargins(0, 0, 0, 0)
        self.patch_cells = {}
        for k in ("Area", "Cells", "Mean slope", "P95 slope", "Max slope",
                  "Relief", "Depth"):
            lab = QtWidgets.QLabel("--")
            lab.setObjectName("mono")
            self.patch_cells[k] = lab
            pf.addRow(self._key(k), lab)
        trow = QtWidgets.QWidget()
        th = QtWidgets.QHBoxLayout(trow)
        th.setContentsMargins(0, 0, 0, 0)
        th.addWidget(self._key("Over"))
        self.thresh_s = QtWidgets.QSpinBox()
        self.thresh_s.setRange(1, 89)
        self.thresh_s.setSuffix("\u00b0")
        self.thresh_s.setValue(15)
        self.thresh_s.valueChanged.connect(lambda _v: self._refresh_patch())
        th.addWidget(self.thresh_s)
        self.patch_over = QtWidgets.QLabel("--")
        self.patch_over.setObjectName("mono")
        th.addWidget(self.patch_over, 1)
        sv.addWidget(pform)
        sv.addWidget(trow)
        clr = QtWidgets.QPushButton("Clear box")
        clr.clicked.connect(self.view.clear_patch)
        sv.addWidget(clr)
        v.addWidget(sg)

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
            item = QtWidgets.QTableWidgetItem(self.fleet.label(nm))
            item.setForeground(QtGui.QColor(self.fleet.colour(nm)))
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

        # Scrolls, like the controls rail. Four groups deep - cursor, measured
        # line, slope box, live positions - is taller than a laptop screen, and
        # without this the bottom of it simply cannot be reached.
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(w)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        # Unlike the controls rail this one keeps its horizontal bar, because
        # the measured line is deliberately wider than the narrowest the rail
        # can be dragged to. Drag it narrow and you scroll across rather than
        # losing the right-hand columns.
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        dock.setWidget(scroll)
        # The measured line wants 430px to show six columns of larger type
        # without truncating a distance, and that is what the rail opens at -
        # but it is a preference, not a floor. Anyone who would rather have the
        # 3D view can drag it down to here and scroll.
        dock.setMinimumWidth(300)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, dock)
        self.dock_readout = dock

    # ---------------------------------------------------------- slope box

    def _measure_toggled(self, on):
        self.view.measuring = on
        if on and self.box_b.isChecked():
            self.box_b.setChecked(False)

    def _box_toggled(self, on):
        """One click at a time belongs to one job, so the two modes exclude."""
        self.view.set_box_mode(on)
        if on and self.meas_b.isChecked():
            self.meas_b.setChecked(False)
        if on:
            size = BOX_SIZES.get(self.box_c.currentText(), 200.0)
            self.view.box_size = size
            self.patch_hint.setText(
                "Click the seabed to place a box."
                if size else "Click two opposite corners on the seabed.")
        elif self.view.patch is None:
            self.patch_hint.setText(
                "Turn on Slope box and click the seabed.\n"
                "Slope is recomputed there at native resolution.")

    def _on_patch(self, patch):
        self._patch = patch
        self._refresh_patch()

    def _refresh_patch(self):
        p = getattr(self, "_patch", None)
        if p is None:
            for lab in self.patch_cells.values():
                lab.setText("--")
            self.patch_over.setText("--")
            return
        st = p.stats()
        if not st:
            return
        self.patch_cells["Area"].setText(
            f"{st['side_x']:,.0f} \u00d7 {st['side_y']:,.0f} m")
        self.patch_cells["Cells"].setText(
            f"{st['valid']:,} @ {p.cell:.2f} m")
        self.patch_cells["Mean slope"].setText(f"{st['mean']:.2f}\u00b0")
        self.patch_cells["P95 slope"].setText(f"{st['p95']:.2f}\u00b0")
        self.patch_cells["Max slope"].setText(f"{st['max']:.2f}\u00b0")
        self.patch_cells["Relief"].setText(f"{st['relief']:,.1f} m")
        self.patch_cells["Depth"].setText(
            f"{st['depth_min']:,.1f} \u2013 {st['depth_max']:,.1f} m")
        frac = p.fraction_over(float(self.thresh_s.value()))
        self.patch_over.setText("--" if frac != frac else f"{frac * 100:.1f}% of it")
        self.patch_hint.setText(
            f"Native {p.cell:.2f} m over {st['side_x']:,.0f} \u00d7 "
            f"{st['side_y']:,.0f} m - the display mesh gives this box about "
            f"{max(st['side_x'] * st['side_y'] / (self.view.surface.cell_m ** 2), 0):.0f}"
            " cells.")

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

        self._build_calib_menu()

    def _build_calib_menu(self):
        """Tie the depth feed to the grid. Off until asked for.

        A vehicle's depth and the grid's depth are separate conversions from
        separate measurements, so they disagree by tens of metres on a
        regional grid. Pressing *Tie in* while a vehicle is on the bottom
        records that disagreement; the correction is fitted from the tie-ins.
        """
        cm = self.menuBar().addMenu("&Calibration")
        self.act_calib = cm.addAction("&Apply depth calibration")
        self.act_calib.setCheckable(True)
        self.act_calib.setChecked(self.calib.enabled)
        self.act_calib.setToolTip(
            "Correct every vehicle depth using the tie-in points. "
            "Off draws the feed exactly as it arrives.")
        self.act_calib.toggled.connect(self._calib_toggled)
        cm.addSeparator()

        # Only the bodies that land. The vessel carries no depth at all, and a
        # TMS hangs in mid-water on the umbilical - it never touches bottom, so
        # it can never witness the seabed a tie-in has to be measured against.
        self.act_tie = {}
        for nm in BOTTOM_ORDER:
            act = cm.addAction("")
            act.triggered.connect(lambda _=False, n=nm: self.tie_in(n))
            self.act_tie[nm] = act
        self._relabel_tie_actions()
        cm.addSeparator()
        cm.addAction("Tie-in &points…").triggered.connect(self.show_calib)
        self.act_calib_clear = cm.addAction("Clear all tie-in points")
        self.act_calib_clear.triggered.connect(self.clear_tiepoints)
        self._sync_calib_menu()

        vm = self.menuBar().addMenu("Ve&hicles")
        vm.addAction("&Names and colours…").triggered.connect(self.show_fleet)
        vm.addSeparator()
        vm.addAction("&Reset to default names").triggered.connect(
            self.reset_fleet)

        # A QAction, not a button, but it answers setChecked/isChecked/setText
        # exactly as the panel button did - so toggle_feed and _feed_status did
        # not have to change when it moved up here.
        fm = self.menuBar().addMenu("&Feed")
        self.listen_b = fm.addAction("Start listening")
        self.listen_b.setCheckable(True)
        self.listen_b.setToolTip(
            "Bind both UDP ports and start placing vehicles on the terrain.")
        self.listen_b.toggled.connect(self.toggle_feed)
        fm.addSeparator()
        fm.addAction("&Ports and status…").triggered.connect(self.show_feed)

        self._build_nodes_menu()

    def _build_nodes_menu(self):
        """Node slides: mark one, get a search corridor, record the recovery.

        Off until switched on, like the depth calibration - a corridor drawn on
        the seabed unasked is a claim nobody made.
        """
        nm = self.menuBar().addMenu("&Nodes")
        self.act_nodes = nm.addAction("&Enable node slide tracking")
        self.act_nodes.setCheckable(True)
        self.act_nodes.setChecked(prefs.view("nodes/on"))
        self.act_nodes.setToolTip(
            "Mark a node as slid and get a search corridor down the fall line "
            "from where it was placed.")
        self.act_nodes.toggled.connect(self._nodes_toggled)
        nm.addSeparator()

        # One per ROV that can place a node. A TMS carries no manipulator.
        self.act_slid = {}
        for slot in BOTTOM_ORDER:
            act = nm.addAction("")
            act.triggered.connect(lambda _=False, s=slot: self.node_slid(s))
            self.act_slid[slot] = act
        nm.addSeparator()
        nm.addAction("Open cases and &history…").triggered.connect(
            self.show_nodes)
        nm.addAction("&Export case database (CSV)…").triggered.connect(
            self.export_slides)
        self._relabel_slide_actions()
        self._sync_nodes_menu()

    def _relabel_slide_actions(self):
        for slot, act in self.act_slid.items():
            label = self.fleet.label(slot)
            act.setText(f"Node slid at {label}…")
            act.setToolTip(
                f"Record that {label} has just lost a node, and predict where "
                "it went from where it is standing now.")

    def _sync_nodes_menu(self):
        on = self.act_nodes.isChecked()
        for act in self.act_slid.values():
            act.setEnabled(on)
        if self._nodes_dialog is not None:
            self._nodes_dialog.refresh()

    def _relabel_tie_actions(self):
        """Menu entries follow a rename, so they name the vehicle you know."""
        for nm, act in self.act_tie.items():
            label = self.fleet.label(nm)
            act.setText(f"Tie in {label} (on bottom now)")
            act.setToolTip(
                f"Record what the grid and the feed each say at {label}'s "
                "position right now. Press only when it is on the bottom.")

    # -------------------------------------------------------------- vehicles

    # ---------------------------------------------------------- node slides

    def _nodes_toggled(self, on):
        prefs.set_view("nodes/on", bool(on))
        self._sync_nodes_menu()
        if not on:
            self.view.clear_slides()
        else:
            for slot in list(self.slide_open):
                self._draw_case(slot)
            self.statusBar().showMessage(self.slide_model.describe(), 12000)

    def _rov_position(self, slot):
        """Where a vehicle is now, with the reason if it cannot be used."""
        s = self.view.surface
        if s is None:
            return None, "Open a grid first - the prediction runs on terrain."
        en = self._positions.get(slot)
        if en is None:
            return None, f"No position has arrived for {self.fleet.label(slot)}."
        p = s.probe(*en)
        if p is None or not math.isfinite(p.z):
            return None, (f"{self.fleet.label(slot)} is off the grid, so there "
                          "is no slope to slide down.")
        return (en[0], en[1], p), None

    def node_slid(self, slot):
        """Open a case at this ROV's position and predict where the node went."""
        if slot in self.slide_open:
            QtWidgets.QMessageBox.information(
                self, "Already open",
                f"{self.fleet.label(slot)} already has an open case. Close it "
                "from Nodes › Open cases before starting another.")
            return
        where, why = self._rov_position(slot)
        if where is None:
            QtWidgets.QMessageBox.information(self, "Cannot predict", why)
            return
        x, y, p = where

        seen = SlideDirectionDialog(self, self.fleet.label(slot), p).ask()
        if seen is None:
            return                      # the pilot thought better of it

        case = nodes.SlideCase(
            rov=slot, placed_x=float(x), placed_y=float(y),
            placed_z=float(p.z), placed_slope=float(p.slope),
            placed_aspect=float(p.aspect), observed_dir=seen,
            grid=self._path or "")
        self.slide_open[slot] = case
        self._sync_nodes_menu()
        self.show_nodes()
        # _draw_case puts the outcome in the status bar, including the case
        # where there is no corridor to draw, so it goes last and has the
        # final word.
        self._draw_case(slot)

    def _predict(self, case):
        """The traced fall line and corridor for one open case."""
        s = self.view.surface
        arrest = nodes.effective_arrest(self.slide_model, case.placed_slope)
        path, why = nodes.trace(s, case.placed_x, case.placed_y, arrest,
                                start_dir=case.observed_dir)
        left, right = nodes.corridor(path, self.slide_model.spread_deg)
        return path, left, right, why

    def _case_runout(self, path) -> float:
        return sum(math.hypot(path[i][0] - path[i - 1][0],
                              path[i][1] - path[i - 1][1])
                   for i in range(1, len(path)))

    def _draw_case(self, slot):
        """Draw the corridor, or say plainly why there is none to draw.

        Drawing nothing and saying nothing was the original fault here: on
        ground below the threshold the trace returned a single point, the draw
        call declined it for being too short, and the operator was left looking
        at an unchanged screen with no idea whether anything had happened.
        """
        case = self.slide_open.get(slot)
        if case is None or not self.act_nodes.isChecked():
            return
        path, left, right, why = self._predict(case)
        if len(path) < 2:
            self.view.clear_slide(slot)
            self.statusBar().showMessage(
                f"{self.fleet.label(slot)}: no corridor drawn - {why}. The "
                "node cannot have gone far from where it was placed; search "
                "close in.", 25000)
            return
        self.view.draw_slide(slot, path, left, right,
                             self.fleet.colour(slot))
        arrest = nodes.effective_arrest(self.slide_model, case.placed_slope)
        self.statusBar().showMessage(
            f"{self.fleet.label(slot)}: corridor runs "
            f"{self._case_runout(path):,.0f} m towards "
            f"{bearing_text(case.placed_aspect)} {compass(case.placed_aspect)}"
            f", arresting below {arrest:.1f} deg - {why}.", 25000)

    def node_found(self, slot):
        """Close a case with the recovery position - this is what teaches it."""
        case = self.slide_open.get(slot)
        if case is None:
            return
        where, why = self._rov_position(slot)
        if where is None:
            QtWidgets.QMessageBox.information(self, "Cannot record", why)
            return
        x, y, p = where
        if math.hypot(x - case.placed_x, y - case.placed_y) < 0.5:
            if QtWidgets.QMessageBox.question(
                    self, "Same position?",
                    f"{self.fleet.label(slot)} is within half a metre of where "
                    "the node was placed. Record this as a recovery anyway?"
                    ) != QtWidgets.QMessageBox.Yes:
                return
        case.found_x, case.found_y = float(x), float(y)
        case.found_z, case.found_slope = float(p.z), float(p.slope)
        case.found_at = time.time()
        self.slide_cases.append(case)
        self.slide_open.pop(slot, None)
        self.view.clear_slide(slot)
        self._refit_slides()
        self.statusBar().showMessage(
            f"Recovered {case.runout:,.0f} m from where it was placed, on a "
            f"track of {bearing_text(case.track)} against a predicted "
            f"{bearing_text(case.placed_aspect)} - out by "
            f"{abs(case.track_error):.0f} deg. " + self.slide_model.describe(),
            25000)

    def node_cancel(self, slot):
        """Give up on a case. Nothing is recorded: a failed search taught us
        nothing we asked to keep."""
        if self.slide_open.pop(slot, None) is None:
            return
        self.view.clear_slide(slot)
        self._sync_nodes_menu()
        self.statusBar().showMessage(
            f"{self.fleet.label(slot)}: case closed, nothing recorded.", 8000)

    def _refit_slides(self):
        self.slide_model = nodes.fit(self.slide_cases)
        try:
            nodes.save_cases(self.slide_db, self.slide_cases)
        except OSError as exc:
            self.statusBar().showMessage(
                f"Could not write the case database: {exc}", 15000)
        for slot in list(self.slide_open):
            self._draw_case(slot)
        self._sync_nodes_menu()

    def show_nodes(self):
        if self._nodes_dialog is None:
            self._nodes_dialog = NodesDialog(self)
        self._nodes_dialog.refresh()
        self._nodes_dialog.show()
        self._nodes_dialog.raise_()
        self._nodes_dialog.activateWindow()

    def export_slides(self):
        if not self.slide_cases:
            QtWidgets.QMessageBox.information(
                self, "Nothing to export",
                "No recovered cases yet. The database fills as nodes are "
                "found.")
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export slide cases", "slide_cases.csv", "CSV (*.csv)")
        if path:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(nodes.to_csv(self.slide_cases))
            self.statusBar().showMessage(
                f"{len(self.slide_cases)} cases written to {path}", 10000)

    def show_feed(self):
        if self._feed_dialog is None:
            self._feed_dialog = FeedDialog(self)
        self._feed_dialog.refresh()
        self._feed_dialog.show()
        self._feed_dialog.raise_()
        self._feed_dialog.activateWindow()

    def show_fleet(self):
        if self._fleet_dialog is None:
            self._fleet_dialog = FleetDialog(self)
        self._fleet_dialog.refresh()
        self._fleet_dialog.show()
        self._fleet_dialog.raise_()
        self._fleet_dialog.activateWindow()

    def reset_fleet(self):
        if not self.fleet.renamed():
            return
        if QtWidgets.QMessageBox.question(
                self, "Reset vehicles",
                "Put every vehicle back to its default name and colour?"
                ) != QtWidgets.QMessageBox.Yes:
            return
        self.fleet.reset()
        self.apply_fleet()

    def apply_fleet(self):
        """Push labels and colours everywhere they are drawn, and remember them.

        Renaming touches no slot, so nothing here has to rebuild the feed, the
        tether pairing or the calibration points - they are all keyed on the
        slot and simply start reading out under a different name.
        """
        if getattr(self, "_closing", False):
            return          # teardown: the widgets below are already gone
        try:
            prefs.set_fleet(self.fleet.encode())
        except Exception:
            pass
        self.view.targets.set_styles(self.fleet.styles())
        for r, nm in enumerate(ORDER):
            item = self.tgt_table.item(r, 0)
            if item is not None:
                item.setText(self.fleet.label(nm))
                item.setForeground(QtGui.QColor(self.fleet.colour(nm)))
        self._relabel_tie_actions()
        self._relabel_slide_actions()
        if self._calib_dialog is not None:
            self._calib_dialog.refresh()
        if self._fleet_dialog is not None:
            self._fleet_dialog.refresh()
        self.view.plotter.render()

    def _sync_calib_menu(self):
        """Grey out what cannot be done yet, and say why in the tooltip."""
        fitted = self.calib.model.on
        self.act_calib.setEnabled(fitted)
        if not fitted:
            self.act_calib.setToolTip(
                "No tie-in points yet - tie in a vehicle on the bottom first.")
            # Remembered as on, but the points that made it are gone: a ticked
            # switch that corrects nothing is worse than an unticked one.
            if self.act_calib.isChecked():
                self.act_calib.setChecked(False)
        self.act_calib_clear.setEnabled(bool(self.calib.points))
        self._mark_depth_column()

    # ----------------------------------------------------------- calibration

    def _calib_toggled(self, on):
        self.calib.enabled = bool(on)
        prefs.set_view("calib/on", self.calib.enabled)
        self._mark_depth_column()
        self.statusBar().showMessage(
            self.calib.model.describe() if self.calib.active
            else "Calibration off - depths drawn exactly as the feed sends them.",
            10000)
        self._place_targets()

    def tie_in(self, name: str):
        """Record grid depth against feed depth for a vehicle on the bottom.

        The raw feed depth is stored, never a corrected one - otherwise
        tie-ins taken with calibration switched on would be measuring the
        correction instead of the error, and each one would fold the previous
        ones in again.
        """
        if name not in BOTTOM_ORDER:
            QtWidgets.QMessageBox.information(
                self, "Cannot tie in",
                f"{name} never sits on the bottom, so its depth has nothing "
                "to tie to. Tie in on a vehicle that lands: "
                + ", ".join(BOTTOM_ORDER) + ".")
            return
        s = self.view.surface
        if s is None:
            QtWidgets.QMessageBox.information(
                self, "No grid", "Open a grid before tying in - the tie-in "
                "records what the grid says at the vehicle's position.")
            return
        en = self._positions.get(name)
        if en is None:
            QtWidgets.QMessageBox.information(
                self, "No position",
                f"No position has arrived for {name} yet.")
            return
        raw = self._depths.get(name)
        if raw is None or (time.monotonic() - raw[1]) >= DEPTH_STALE_AFTER:
            QtWidgets.QMessageBox.information(
                self, "No live depth",
                f"{name} has no depth newer than {DEPTH_STALE_AFTER:.0f} s. "
                "A tie-in has to pair a position and a depth from the same "
                "moment, so the depth feed must be running.")
            return
        e, n = en
        p = s.probe(e, n)
        if p is None or not math.isfinite(p.z):
            QtWidgets.QMessageBox.information(
                self, "Off grid",
                f"{name} is outside the grid, or over a nodata gap, so there "
                "is no grid depth to tie to.")
            return

        pt = calib.TiePoint(vehicle=name, x=float(e), y=float(n),
                            grid_depth=float(-p.z), feed_depth=float(raw[0]),
                            grid=self._path or "")
        self.calib.add(pt)
        self._save_tiepoints()
        self._sync_calib_menu()
        if self._calib_dialog is not None:
            self._calib_dialog.refresh()
        self.statusBar().showMessage(
            f"Tied in {name}: grid {pt.grid_depth:,.1f} m, feed "
            f"{pt.feed_depth:,.1f} m, out by {pt.offset:+.1f} m.  "
            + self.calib.model.describe(), 20000)
        self._place_targets()

    def show_calib(self):
        if self._calib_dialog is None:
            self._calib_dialog = CalibDialog(self)
        self._calib_dialog.refresh()
        self._calib_dialog.show()
        self._calib_dialog.raise_()
        self._calib_dialog.activateWindow()

    def clear_tiepoints(self):
        if not self.calib.points:
            return
        if QtWidgets.QMessageBox.question(
                self, "Clear tie-in points",
                f"Discard all {len(self.calib.points)} tie-in points?"
                ) != QtWidgets.QMessageBox.Yes:
            return
        self.calib.clear()
        self._after_calib_change()

    def _after_calib_change(self):
        """Everything that has to follow a change to the points."""
        self._save_tiepoints()
        if not self.calib.model.on and self.act_calib.isChecked():
            # Nothing left to apply: untick rather than leave a switch that
            # claims to be doing something.
            self.act_calib.setChecked(False)
        self._sync_calib_menu()
        self._mark_depth_column()
        if self._calib_dialog is not None:
            self._calib_dialog.refresh()
        self._place_targets()

    def _save_tiepoints(self):
        try:
            prefs.set_tiepoints(self.calib.encode())
        except Exception:
            pass

    def _mark_depth_column(self):
        """Say in the table header when the depths shown are corrected."""
        head = self.tgt_table.horizontalHeaderItem(3)
        if head is None:
            return
        on = self.calib.active
        head.setText("Depth*" if on else "Depth")
        head.setToolTip(self.calib.model.describe() if on
                        else "Depth exactly as the feed sends it.")

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
                    f"{l.dz:+,.1f}", f"{l.gradient:.2f}",
                    bearing_text(l.bearing), compass(l.bearing))
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
        if not ok and msg.startswith("Cannot bind"):
            # The status itself lives in the dialog, which may well be shut.
            # A port that would not bind has to be said somewhere the operator
            # is actually looking, or the feed just silently never starts.
            self.statusBar().showMessage(msg, 15000)
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

            depth, age = self._depths.get(nm, (None, 0.0))
            fresh = depth is not None and (now - age) < DEPTH_STALE_AFTER
            # Correct before anything is derived from it, so the marker, the
            # drop line, the altitude and the table all agree on one depth.
            far = False
            if fresh and self.calib.active:
                far = self.calib.model.outside(depth)
                depth = self.calib.apply(depth)
            if nm == "Vessel":
                # A vessel floats, so it is drawn at the surface - always. It
                # sends no depth and has none; drawing it on the bottom put it
                # a kilometre and a half below where it was, and ran its
                # umbilicals upward out of the TMS.
                #
                # It still needs to be *on* the grid, though its depth no
                # longer comes from there. A position outside it means the
                # feed and the grid disagree about the zone, and a vessel
                # placed 2700 km away would take the camera with it.
                z, shown, alt = (None if p is None else 0.0), 0.0, None
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
            self._set_row(r, e, n, shown, "0 s", tgt.speed, alt, far)

        self.view.targets.draw_links(TETHERS)
        self.view.targets.draw_links(UMBILICALS)
        if self._framed_feed is False:
            # At full extent a pixel is ~90 m of seabed, so a vehicle moving at
            # 0.6 m/s looks frozen. Frame them once when the first fix lands.
            self._framed_feed = self.view.zoom_to_targets()
        elif self.follow_b.isChecked():
            self.view.follow_targets()
        self.view.plotter.render()

    def _set_row(self, r, e, n, depth, age, speed=None, alt=None, far=False):
        self.tgt_table.item(r, 1).setText(f"{e:,.2f}")
        self.tgt_table.item(r, 2).setText(f"{n:,.2f}")
        dcell = self.tgt_table.item(r, 3)
        dcell.setText("--" if depth is None else f"{depth:,.1f}")
        # Amber where the vehicle has gone outside the depths anything was
        # tied in at: the correction there is an extrapolation, not a fit.
        dcell.setForeground(QtGui.QColor("#e0a33a" if far else "#e3eef1"))
        dcell.setToolTip(
            "Outside the depth range of the tie-in points - the calibration "
            "is extrapolating here." if far else "")
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
                self.view.targets.update(
                    name, x, y, 0.0 if name == "Vessel" else p.z)
            self.view.plotter.render()

        self._demo = QtCore.QTimer(self)
        self._demo.timeout.connect(tick)
        self._demo.start(120)

    # ------------------------------------------------------------ settings

    def _save_prefs(self):
        try:
            prefs.set_geometry(self.saveGeometry())
            prefs.set_dock_state(self.saveState())
            # apply_fleet already writes these whenever anything changes; doing
            # it again on the way out costs nothing and means the names cannot
            # be lost by a path that forgot to call it.
            prefs.set_fleet(self.fleet.encode())
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
        # Close the modeless dialogs first. A line edit losing focus during
        # teardown emits editingFinished, which lands in apply_fleet after the
        # table it wants has already gone - so the flag is set before anything
        # else, and the dialogs are shut while the window is still whole.
        # Anything typed but not yet committed is committed here, while the
        # widgets are still alive and before the teardown guard goes up. This
        # order matters: the guard was silently dropping a name the operator
        # had typed and then quit on, because closing the dialog blurs the box
        # and the resulting editingFinished landed after the guard was set.
        if self._fleet_dialog is not None:
            try:
                self._fleet_dialog.commit()
            except Exception:
                pass
        self._closing = True
        for attr in ("_fleet_dialog", "_calib_dialog", "_feed_dialog",
                     "_nodes_dialog"):
            dlg = getattr(self, attr, None)
            if dlg is not None:
                dlg.close()
                setattr(self, attr, None)
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
