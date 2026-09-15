"""Live position feed over UDP.

Wire format, one record per datagram at about 1 Hz::

    2026-09-15T21:39:04.4609743Z,707364.210,3009048.400,707036.708,3009161.042,707634.048,3009049.775
    |__ ISO-8601 UTC, .NET "O" __| |__ Vessel E/N __| |__ UHD333 E/N __| |__ UHD334 E/N __|

Eastings and northings are in the **loaded grid's CRS** (UTM 15N for the BOEM
Gulf of Mexico grid), so they need no transform. No depth and no heading are
carried - depth comes from the terrain.

The parser is deliberately forgiving about framing: records may arrive one per
datagram, newline- or CRLF-separated, or run together with no separator at all.
"""

from __future__ import annotations

import re
import socket
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from PySide6 import QtCore

#: Which vehicle each coordinate pair belongs to, in wire order.
#: Change this tuple - and nothing else - if the sender's field order changes.
ORDER = ("Vessel", "UHD333", "UHD334")

DEFAULT_PORT = 6451
#: Seconds without a datagram before a target is drawn dimmed.
STALE_AFTER = 5.0

_DATE = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?"
_NUM = r"[-+]?\d+(?:\.\d+)?"
# The trailing lookahead is what stops the last coordinate from swallowing the
# next record's year when datagrams arrive glued together with no separator:
# "...3009049.775" + "2026-09-15T..." reads as one 15-digit number without it.
_REC = re.compile(
    r"(" + _DATE + r")((?:\s*,\s*" + _NUM + r"){%d})(?=\s*(?:%s|$))"
    % (2 * len(ORDER), _DATE)
)

#: Never let a partial-record buffer grow without bound.
_MAX_CARRY = 4096


class FeedError(RuntimeError):
    pass


@dataclass
class Fix:
    """One timestamped set of vehicle positions."""

    t: datetime
    pos: dict = field(default_factory=dict)  # name -> (easting, northing)

    def __len__(self) -> int:
        return len(self.pos)


def parse_timestamp(s: str) -> datetime:
    """ISO-8601 UTC. .NET's "O" format has 7 fractional digits; datetime takes 6."""
    s = s.strip().rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s = head + "." + (frac + "000000")[:6]
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def parse_records(buf: str) -> tuple[list[Fix], str]:
    """Pull every complete record out of ``buf``.

    Returns the fixes and whatever trailing text was not a complete record, so a
    caller reading a stream can prepend it to the next chunk.
    """
    fixes: list[Fix] = []
    end = 0
    for m in _REC.finditer(buf):
        try:
            t = parse_timestamp(m.group(1))
        except ValueError:
            continue
        nums = [float(v) for v in m.group(2).split(",") if v.strip()]
        fixes.append(Fix(t, {nm: (nums[2 * i], nums[2 * i + 1])
                             for i, nm in enumerate(ORDER)}))
        end = m.end()
    tail = buf[end:].lstrip("\r\n \t")
    return fixes, tail[-_MAX_CARRY:]


class PositionFeed(QtCore.QThread):
    """Listens on a UDP port and emits every fix it decodes.

    Runs on its own thread; ``fix`` is delivered to the GUI thread by Qt's
    queued connection, so nothing here touches the scene directly.
    """

    fix = QtCore.Signal(object)  # Fix
    status = QtCore.Signal(str, bool)  # message, healthy

    def __init__(self, port: int = DEFAULT_PORT, host: str = "0.0.0.0", parent=None):
        super().__init__(parent)
        self.port = int(port)
        self.host = host
        self._stop = threading.Event()
        self.packets = 0
        self.records = 0
        self.bad = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self._stop.clear()
        self.packets = self.records = self.bad = 0
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        try:
            sock.bind((self.host, self.port))
        except OSError as exc:
            sock.close()
            self.status.emit(f"Cannot bind UDP {self.host}:{self.port} - {exc}", False)
            return

        sock.settimeout(0.4)
        self.status.emit(f"Listening on UDP {self.port}", True)
        carry = ""
        try:
            while not self._stop.is_set():
                try:
                    data, _addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError as exc:
                    self.status.emit(f"Socket error - {exc}", False)
                    break
                self.packets += 1
                try:
                    text = carry + data.decode("ascii", errors="replace")
                except Exception:
                    self.bad += 1
                    continue
                fixes, carry = parse_records(text)
                if not fixes and not carry:
                    self.bad += 1
                for f in fixes:
                    self.records += 1
                    self.fix.emit(f)
        finally:
            sock.close()
            self.status.emit("Stopped", False)
