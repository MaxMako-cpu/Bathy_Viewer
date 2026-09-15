"""Entry point: ``python -m bathy3d [grid.tif]``."""

from __future__ import annotations

import argparse
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bathy3d", description=__doc__)
    ap.add_argument("grid", nargs="?", help="GeoTIFF (or any GDAL grid) to open on start")
    args = ap.parse_args(argv)

    from PySide6 import QtWidgets
    from .mainwindow import MainWindow

    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("Bathy3D")
    win = MainWindow(args.grid)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
