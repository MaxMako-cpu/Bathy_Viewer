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
import sys
import threading
import time
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
        # Diagnostics, read by the GUI once a second. Plain ints and strings,
        # so no lock is needed to look at them from the other thread.
        self.last_raw = ""
        self.last_addr = ""
        self.last_packet_at = 0.0
        self.started_at = 0.0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self._stop.clear()
        self.packets = self.records = self.bad = 0
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Deliberately NOT SO_REUSEADDR. On Windows that lets a second socket
        # bind a port another process already holds, and unicast datagrams then
        # go to whichever bound last - so starting the viewer beside a live
        # survey consumer could silently steal its feed. Failing to bind is a
        # message on screen; stealing a production stream is not.
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        try:
            sock.bind((self.host, self.port))
        except OSError as exc:
            sock.close()
            self.status.emit(
                f"Cannot bind UDP {self.host}:{self.port} - {exc}. "
                "Another program on this machine is probably already receiving "
                "this feed.", False)
            return

        sock.settimeout(0.4)
        self.started_at = time.monotonic()
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
                self.last_packet_at = time.monotonic()
                self.last_addr = f"{_addr[0]}:{_addr[1]}"
                raw = data.decode("ascii", errors="replace")
                self.last_raw = raw[:220]
                fixes, carry = parse_records(carry + raw)
                if not fixes and not carry:
                    self.bad += 1
                for f in fixes:
                    self.records += 1
                    self.fix.emit(f)
        finally:
            sock.close()
            self.status.emit("Stopped", False)


def _sniff(argv):
    """`python -m bathy3d.feed [port]` - print raw datagrams and what they decode to."""
    port = int(argv[1]) if len(argv) > 1 else DEFAULT_PORT
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except OSError:
        pass
    try:
        sock.bind(("0.0.0.0", port))
    except OSError as exc:
        print(f"cannot bind UDP {port}: {exc}")
        print("another program on this machine is already receiving this feed -"
              " stop it first, or have the sender repeat to a second port")
        return 1
    print(f"listening on UDP 0.0.0.0:{port} - Ctrl+C to stop")
    n = 0
    carry = ""
    try:
        while True:
            data, addr = sock.recvfrom(65535)
            n += 1
            raw = data.decode("ascii", errors="replace")
            fixes, carry = parse_records(carry + raw)
            print("")
            print(f"[{n}] {len(data)} bytes from {addr[0]}:{addr[1]}")
            print(f"    raw: {raw[:200]!r}")
            if fixes:
                for f in fixes:
                    pretty = "  ".join(f"{k} {v[0]:.3f}E {v[1]:.3f}N"
                                       for k, v in f.pos.items())
                    print(f"    ok : {f.t:%H:%M:%S}Z  {pretty}")
            else:
                print(f"    !! no complete record decoded (carry {len(carry)} chars)")
    except KeyboardInterrupt:
        print("stopped")
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(_sniff(sys.argv))
