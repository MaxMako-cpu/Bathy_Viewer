"""Live position feed over UDP.

The wire format, as sent by the survey PC (one record, about 1 Hz)::

    706148.701,3006428.410,705939.201,3006546.099,706515.275,3006391.404
    |__ Vessel E/N ____| |__ UHD333 E/N ____| |__ UHD334 E/N ____|

Six comma-separated fields, three decimals each. No timestamp. And - the part
that matters - **no separator between one record and the next**: they are
written back to back, so the only mark of a boundary is a field's decimals
running straight into the next field's digits::

    ...,3006363.252706132.235,3006399.181,...
                  ^ record ends here

Eastings and northings are in the **loaded grid's CRS** (UTM 15N for the BOEM
Gulf of Mexico grid), so they need no transform. No depth is carried - depth
comes from the terrain under each position.

An earlier sample also carried an ISO-8601 timestamp per record; that form is
still decoded, and the timestamp is used as the record boundary when present.
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

#: Decimal places every field carries - the only thing that marks where
#: one record ends and the next begins in a delimiter-free stream.
_DECIMALS = 3

#: A run of digits and dots with no delimiter of any kind.
_GLUED_NUM = re.compile(r"[-+0-9.]+")

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


#: Inside a record the fields are comma-separated, but consecutive records are
#: written back to back with nothing between them, so the only mark of a record
#: boundary is a field's decimals running straight into the next field's digits:
#: ``...3006363.252706132.235...``. Put the missing separator back.
_GLUE = re.compile(r"(\.\d{%d})(?=[-+]?\d)" % _DECIMALS)


def _unglue(buf: str) -> str:
    return _GLUE.sub(r"\1,", buf)


def _glued_numeric(buf: str):
    """Numbers written end to end with no delimiter, e.g. ``657.3006487.764``.

    Only safe because every field carries the same number of decimals; the
    split is accepted only if the pieces reassemble into exactly the input.
    """
    s = buf.strip()
    if not s or not _GLUED_NUM.fullmatch(s):
        return None
    for dp in (3, 2, 4, 1):
        parts = re.findall(r"[-+]?\d+\.\d{%d}" % dp, s)
        if parts and "".join(parts) == s:
            return [float(p) for p in parts]
    return None


def parse_records(buf: str, stream: bool = True,
                  allow_bare: bool = True) -> tuple[list[Fix], str]:
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

    # No timestamped record. If this feed has never carried a timestamp, then
    # there is no in-band record boundary at all and the datagram itself is the
    # only framing there is - so decode it whole rather than holding bytes that
    # nothing will ever come along to terminate.
    if not allow_bare:
        return [], buf[-_MAX_CARRY:]

    want = 2 * len(ORDER)
    toks = [t for t in re.split(r"[,;\t\r\n ]+", _unglue(buf).strip()) if t]
    vals = []
    for t in toks:
        try:
            vals.append(float(t))
        except ValueError:
            return [], buf.lstrip("\r\n \t")[-_MAX_CARRY:]
    if not vals:
        return [], ""

    whole = len(vals) // want
    # The final record is unconfirmed for the same reason as the timestamped
    # case: if the datagram stopped mid-field its last value is truncated, and
    # nothing in this format says otherwise until the next digits arrive.
    if stream and not terminated and whole:
        whole -= 1
    if whole <= 0:
        return [], buf.lstrip("\r\n \t")[-_MAX_CARRY:]

    now = datetime.now(timezone.utc)
    for k in range(0, whole * want, want):
        c = vals[k:k + want]
        fixes.append(Fix(now, {nm: (c[2 * i], c[2 * i + 1])
                               for i, nm in enumerate(ORDER)}, timed=False))
    consumed = _nth_field_end(_unglue(buf), whole * want)
    return fixes, _unglue(buf)[consumed:].lstrip(",\r\n \t")[-_MAX_CARRY:]


def _nth_field_end(text: str, n: int) -> int:
    """Index just past the ``n``th comma-separated field in ``text``."""
    seen = 0
    for i, ch in enumerate(text):
        if ch in ",;\t\r\n ":
            seen += 1
            if seen == n:
                return i
    return len(text)


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
        # Digits and dots only: the sender is writing numbers with no delimiter
        # between them. Show the fixed-decimals split so the real field layout
        # can be read off the message instead of guessed at.
        if _GLUED_NUM.fullmatch(buf.strip()):
            for dp in (3, 2, 4):
                parts = re.findall(r"[-+]?\d+\.\d{%d}" % dp, buf)
                if parts and "".join(parts) == buf.strip():
                    return (f"no separators at all - the numbers run together. "
                            f"Split at {dp} decimal places gives {len(parts)} "
                            f"values: {', '.join(parts[:8])}"
                            + (" ..." if len(parts) > 8 else ""))
            return (f"no separators and no consistent decimal width - "
                    f"starts {head!r}")
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
        #: Once a timestamped record decodes, never fall back to
        #: delimiter-free parsing - a mid-record slice of a timestamped
        #: stream is all digits too, and would decode to nonsense.
        self.saw_timestamp = False

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
                        flushed, carry = parse_records(
                            carry, stream=False,
                            allow_bare=not self.saw_timestamp)
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
                fixes, carry = parse_records(
                    pending, allow_bare=not self.saw_timestamp)
                self.carry_len = len(carry)
                if not fixes and not carry:
                    self.bad += 1
                for f in fixes:
                    if f.timed:
                        self.saw_timestamp = True
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
