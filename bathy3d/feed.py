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
    """One set of vehicle positions.

    ``timed`` is False when the sender carried no timestamp we could read and
    ``t`` is therefore the arrival time, not the time of the fix.
    """

    t: datetime
    pos: dict = field(default_factory=dict)  # name -> (easting, northing)
    timed: bool = True

    def __len__(self) -> int:
        return len(self.pos)


def parse_timestamp(s: str) -> datetime:
    """ISO-8601 UTC. .NET's "O" format has 7 fractional digits; datetime takes 6."""
    s = s.strip().rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s = head + "." + (frac + "000000")[:6]
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _all_numeric(buf: str):
    """Every whitespace/comma-separated token as a float, or None if any isn't.

    Strict on purpose. A half-received timestamp like ``2026-09-1`` must not be
    mistaken for coordinates, so one unparseable token rejects the whole buffer.
    """
    toks = [t for t in re.split(r"[,;\t\r\n ]+", buf.strip()) if t]
    out = []
    for t in toks:
        try:
            out.append(float(t))
        except ValueError:
            return None
    return out


def parse_records(buf: str, stream: bool = True) -> tuple[list[Fix], str]:
    """Pull every complete record out of ``buf``.

    Returns the fixes and the trailing text that was not a complete record, so a
    caller reading a stream prepends it to the next chunk.

    ``stream=True`` means more bytes may follow, and it is what makes this safe.
    The feed has no line terminators, so the only thing that proves a record
    ended is the *next* record's timestamp. A record sitting at the end of the
    buffer may be a whole record - or one the datagram cut in half, in which case
    its last coordinate is truncated and the vehicle lands kilometres away. So
    the trailing record is held back until something confirms it: the next
    datagram, an explicit terminator, or ``stream=False`` at end of feed. The
    cost is one record of latency; the alternative is plotting a wrong position.
    """
    fixes: list[Fix] = []
    spans: list[tuple[int, int]] = []
    for m in _REC.finditer(buf):
        try:
            t = parse_timestamp(m.group(1))
        except ValueError:
            continue
        nums = [float(v) for v in m.group(2).split(",") if v.strip()]
        fixes.append(Fix(t, {nm: (nums[2 * i], nums[2 * i + 1])
                             for i, nm in enumerate(ORDER)}))
        spans.append((m.start(), m.end()))

    terminated = buf.endswith(("\n", "\r"))
    if fixes:
        if stream and not terminated and spans[-1][1] == len(buf):
            fixes.pop()  # unconfirmed - may be cut short
            return fixes, buf[spans[-1][0]:][-_MAX_CARRY:]
        return fixes, buf[spans[-1][1]:].lstrip("\r\n \t")[-_MAX_CARRY:]

    # No timestamped record. Some senders omit the timestamp entirely; accept a
    # buffer that is a whole number of coordinate records and nothing else.
    # Anything ragged is held rather than guessed: pairing coordinates off a
    # mid-record slice would put the vehicles somewhere they are not.
    if stream and not terminated:
        return [], buf[-_MAX_CARRY:]
    want = 2 * len(ORDER)
    nums = _all_numeric(buf)
    if nums and len(nums) % want == 0:
        now = datetime.now(timezone.utc)
        for k in range(0, len(nums), want):
            c = nums[k:k + want]
            fixes.append(Fix(now, {nm: (c[2 * i], c[2 * i + 1])
                                   for i, nm in enumerate(ORDER)}, timed=False))
        return fixes, ""
    return [], buf.lstrip("\r\n \t")[-_MAX_CARRY:]


def explain(buf: str) -> str:
    """Say why a chunk did not decode, in terms someone can act on.

    "It didn't parse" is useless on a vessel. The three things that actually
    differ between senders are the timestamp, the field count and the
    separator, so name whichever one is wrong.
    """
    if not buf.strip():
        return "empty datagram"
    m = re.search(_DATE, buf)
    if not m:
        head = buf[:40]
        return (f"no ISO-8601 timestamp found - record starts {head!r}. "
                "Expected something like 2026-09-15T21:39:04.4609743Z")
    after = buf[m.end():]
    nums = re.findall(_NUM, after)
    want = 2 * len(ORDER)
    seps = {c for c in after[:200] if not (c.isdigit() or c in "+-.eE")}
    seps.discard(" ")
    sep_note = ("separators seen: "
                + ", ".join(repr(c) for c in sorted(seps)[:6])) if seps else ""
    if len(nums) < want:
        return (f"timestamp ok, but only {len(nums)} numeric fields follow it - "
                f"need {want} ({len(ORDER)} x E/N for {', '.join(ORDER)}). "
                f"{sep_note}")
    if "," not in after[:200]:
        return (f"timestamp ok and {len(nums)} numbers present, but they are not "
                f"comma-separated. {sep_note}")
    return (f"timestamp ok, {len(nums)} numeric fields present, but the record "
            f"still did not match. {sep_note}")


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
        self.last_pending = ""
        self.carry_len = 0
        self.last_addr = ""
        self.last_packet_at = 0.0
        self.started_at = 0.0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        self._stop.clear()
        self.packets = self.records = self.bad = 0
        self.carry_len = 0
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
                    # Sender has gone quiet: nothing is coming to confirm the
                    # held record, so take it at its word rather than sit on
                    # the last known position for ever.
                    if carry and time.monotonic() - self.last_packet_at > 1.5:
                        flushed, carry = parse_records(carry, stream=False)
                        self.carry_len = len(carry)
                        for f in flushed:
                            self.records += 1
                            self.fix.emit(f)
                    continue
                except OSError as exc:
                    self.status.emit(f"Socket error - {exc}", False)
                    break
                self.packets += 1
                self.last_packet_at = time.monotonic()
                self.last_addr = f"{_addr[0]}:{_addr[1]}"
                raw = data.decode("ascii", errors="replace")
                self.last_raw = raw[:220]
                pending = carry + raw
                # Diagnose the pending buffer, not this datagram alone: with no
                # line terminators a datagram routinely starts mid-record, and
                # judging it on its own reports a fault that isn't there.
                self.last_pending = pending[:220]
                fixes, carry = parse_records(pending)
                self.carry_len = len(carry)
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
                print(f"    !! {explain(raw)}")
                print(f"    hex: {data[:72].hex(' ')}")
                if carry:
                    print(f"    (held {len(carry)} chars for the next datagram)")
    except KeyboardInterrupt:
        print("stopped")
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(_sniff(sys.argv))
