#!/usr/bin/env python
"""Launch Bathy3D without installing the package.

    python run.py                      # empty, then File > Open
    python run.py path\\to\\grid.tif     # open a grid straight away
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bathy3d.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
