"""Live feeds over UDP: positions on one port, depths on another.

Positions (default port 6451), one record, about 1 Hz::

    706148.701,3006428.410,705939.201,3006546.099,706515.275,3006391.404,...
    |__ Vessel E/N ___| |__ ROV1 E/N _____| |__ ROV2 E/N _____| then TMS1, TMS2

Depths (default port 6452), metres below the surface, positive down::

    1656.082,1646.926,1492.150,1508.260
    |  ROV1  |  ROV2  |  TMS1  |  TMS2

Both are comma-separated with three decimals a field, carry no timestamp, and -
the part that matters - have **no separator between one record and the next**.
They are written back to back, so the only mark of a boundary is a field's
decimals running straight into the next field's digits::

    ...,3006363.252706132.235,3006399.181,...
                  ^ record ends here

Eastings and northings are in the **loaded grid's CRS** (UTM 15N for the BOEM
Gulf of Mexico grid), so they need no transform.
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

#: Which vehicle each coordinate pair belongs to, in wire order. Change this
#: tuple - and nothing else - if the sender's field order changes.
ORDER = ("Vessel", "ROV1", "ROV2", "TMS1", "TMS2")

#: Which vehicle each depth belongs to, in wire order. The vessel is at the
#: surface and carries no depth.
DEPTH_ORDER = ("ROV1", "ROV2", "TMS1", "TMS2")

#: Tether management system above, ROV below - used for the tether lines and
#: for pairing the two in the readout.
TETHERS = {"ROV1": "TMS1", "ROV2": "TMS2"}

#: Umbilicals: each TMS hangs off the vessel. Drawn the same way as a tether,
#: so the whole chain from ship to ROV reads as one line.
UMBILICALS = {"TMS1": "Vessel", "TMS2": "Vessel"}

#: The slots are wire positions, not names, so they are generic: what a vessel
#: actually calls its vehicles is a label set in Vehicles > Names and colours.
#: These were originally one vessel's own names, and settings written then -
#: calibration tie-ins especially, which outlive a session - still carry them.
#: Mapped on the way in so a rename of the slots does not orphan them.
LEGACY_SLOTS = {"UHD333": "ROV1", "UHD334": "ROV2",
                "TMS333": "TMS1", "TMS334": "TMS2"}


def slot_for(name: str) -> str:
    """The current slot for a name that may have been written by an older build."""
    return LEGACY_SLOTS.get(str(name), str(name))

#: Bodies a depth calibration tie-in can be taken on: the ones that actually
#: land. A TMS hangs off the umbilical in mid-water and never touches bottom,
#: so it can never witness the seabed and its depth cannot tie to the grid.
#: Derived from the tuples above, so adding a vehicle needs no edit here.
BOTTOM_ORDER = tuple(n for n in DEPTH_ORDER if n not in set(TETHERS.values()))

#: Body -> the ROV whose chain it belongs to. Switching an ROV off in the
#: Targets panel takes its TMS with it, because half a chain on screen leaves
#: a tether running to a body that is not there. The vessel is deliberately
#: absent: it belongs to both chains and to neither, so it always stays.
CHAIN_OF = {}
for _rov, _tms in TETHERS.items():
    CHAIN_OF[_rov] = _rov
    CHAIN_OF[_tms] = _rov

DEFAULT_PORT = 6451
DEFAULT_DEPTH_PORT = 6452

#: Seconds without a datagram before a target is drawn dimmed.
STALE_AFTER = 5.0

#: Seconds before a depth is too old to position a vehicle by.
DEPTH_STALE_AFTER = 15.0

POSITION_FIELDS = 2 * len(ORDER)
DEPTH_FIELDS = len(DEPTH_ORDER)


def active_order(chains, order=ORDER) -> tuple:
    """The slots a sender actually fills when only some chains are deployed.

    The sender emits a field for every body whether or not it is deployed,
    leaving the absent ones empty - and consecutive delimiters collapse, so
    those empties never reach the decoder. A record of ten fields arrives as
    six numbers, and reading six numbers as the first six of ten puts an ROV
    at its TMS's position and a TMS at the vessel's, one second late.

    Telling the decoder which chains are out solves both halves at once: the
    record length becomes what is really on the wire, and the values land on
    the bodies they belong to. The vessel is never part of a chain, so it is
    always kept.
    """
    live = set()
    for rov in chains or ():
        live.add(rov)
        if rov in TETHERS:
            live.add(TETHERS[rov])
    return tuple(n for n in order if n not in CHAIN_OF or n in live)

#: Decimal places every field carries - the only thing that marks where one
#: record ends and the next begins in a delimiter-free stream.
_DECIMALS = 3

_DATE = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?"

#: Inside a record the fields are comma-separated, but consecutive records are
#: written back to back with nothing between them. Put the missing separator
#: back wherever a field's decimals run into the next field's digits.
_GLUE = re.compile(r"(\.\d{%d})(?=[-+]?\d)" % _DECIMALS)

#: A run of digits and dots with no delimiter of any kind.
_GLUED_NUM = re.compile(r"[-+0-9.]+")

#: Field separator. Runs collapse, which is why an absent vehicle's empty
#: field never reaches the decoder and the record arrives shorter than it
#: looks - see active_order.
_SPLIT = re.compile(r"[,;\t\r\n ]+")

#: Field splitter that KEEPS empty fields. The one above collapses runs,
#: which is right for a stream of bare numbers and wrong for a sender that
#: marks an absent vehicle with an empty field - those empties are the only
#: thing saying which bodies are reporting.
_FIELD = re.compile(r"[,;]")

#: Never let a partial-record buffer grow without bound.
_MAX_CARRY = 4096


class FeedError(RuntimeError):
    pass


@dataclass
class Fix:
    """One set of vehicle positions, in the grid's CRS."""

    t: datetime
    pos: dict = field(default_factory=dict)      # name -> (easting, northing)

    def __len__(self) -> int:
        return len(self.pos)


@dataclass
class DepthFix:
    """One set of vehicle depths, metres below the surface, positive down."""

    t: datetime
    depths: dict = field(default_factory=dict)   # name -> metres

    def __len__(self) -> int:
        return len(self.depths)


def unglue(buf: str) -> str:
    return _GLUE.sub(r"\1,", buf)


def _nth_field_end(text: str, n: int) -> int:
    """Index just past the ``n``th *number* in ``text``.

    Counting numbers, not separators. They were the same thing while every
    field carried a value, so this used to count commas - but a sender that
    leaves an undeployed vehicle's field empty writes ",,,", which is three
    separators and no numbers. The buffer was then cut mid-record and every
    record after it came out rotated by a body: the vessel wearing the ROV's
    position, the ROV wearing its TMS's.
    """
    spans = list(_GLUED_NUM.finditer(text))
    if n <= 0:
        return 0
    if n <= len(spans):
        return spans[n - 1].end()
    return len(text)


def parse_fielded(raw: str, full_order: tuple, per_body: int):
    """Decode a datagram that carries whole records, by field position.

    The sender emits a field for every body and leaves an absent one empty, so
    the datagram already says which vehicles are reporting - there is nothing
    to configure and nothing to guess. This is the path that should handle a
    live feed; the numeric resync below exists for a sender that splits or
    batches records, where the field positions cannot be trusted.

    Returns a list of ``{name: values}`` dicts, or None if this datagram is
    not a whole number of records - in which case the caller falls back.
    """
    # Whitespace only. A leading comma is not padding: the depth feed's first
    # field is ROV1's, and it is empty in every record of a one-ROV job, so
    # trimming separators here silently shortened the record by a body.
    text = raw.strip()
    if not text:
        return None
    fields = [f.strip() for f in _FIELD.split(text)]
    width = per_body * len(full_order)
    if width <= 0 or len(fields) % width:
        return None

    out = []
    for k in range(len(fields) // width):
        chunk = fields[k * width:(k + 1) * width]
        one = {}
        for i, name in enumerate(full_order):
            cell = chunk[i * per_body:(i + 1) * per_body]
            filled = [c for c in cell if c]
            if not filled:
                continue                    # this vehicle is not reporting
            if len(filled) != per_body:
                return None                 # half a coordinate is not a fix
            try:
                one[name] = [float(c) for c in cell]
            except ValueError:
                return None
        out.append(one)
    return out


def _values(buf: str, count: int, stream: bool):
    """Whole records' worth of numbers, plus the text not yet consumed.

    ``stream=True`` means more bytes may follow, and it is what makes this safe.
    Nothing in this format says a record has ended until the next one's digits
    arrive, so a record sitting at the end of the buffer may be whole - or one
    the datagram cut in half, in which case its last value is truncated and the
    vehicle lands kilometres away. The trailing record is therefore held back
    until something confirms it: the next datagram, an explicit terminator, or
    ``stream=False`` at the end of the feed. The cost is one record of latency;
    the alternative is publishing a wrong position.
    """
    if count <= 0:
        # No bodies to decode onto: every ROV chain has been deselected, and
        # unlike positions the depth feed has no vessel to fall back on. There
        # is nothing to read the numbers as, so nothing is read - and the
        # buffer is dropped rather than carried, or it would grow for ever.
        #
        # This crashed the listening thread on a vessel: the record length was
        # floored at 1 where the layout was set, but the decoders recompute it
        # from the layout itself, so a zero got through to a // and took the
        # feed down.
        return [], ""
    text = unglue(buf)
    toks = [t for t in re.split(r"[,;\t\r\n ]+", text.strip()) if t]
    vals = []
    for tok in toks:
        try:
            vals.append(float(tok))
        except ValueError:
            return [], buf.lstrip("\r\n \t")[-_MAX_CARRY:]
    if not vals:
        return [], ""

    whole = len(vals) // count
    if stream and not buf.endswith(("\n", "\r")) and whole:
        whole -= 1
    if whole <= 0:
        return [], buf.lstrip("\r\n \t")[-_MAX_CARRY:]

    records = [vals[k * count:(k + 1) * count] for k in range(whole)]
    consumed = _nth_field_end(text, whole * count)
    return records, text[consumed:].lstrip(",\r\n \t")[-_MAX_CARRY:]


def parse_records(buf: str, stream: bool = True,
                  order: tuple = ORDER) -> tuple[list[Fix], str]:
    """Decode position records: one E/N pair per vehicle in ``order``.

    ``order`` is the bodies the sender is actually filling - see
    :func:`active_order`. It defaults to the full fleet.
    """
    records, carry = _values(buf, 2 * len(order), stream)
    now = datetime.now(timezone.utc)
    return ([Fix(now, {nm: (r[2 * i], r[2 * i + 1])
                       for i, nm in enumerate(order)}) for r in records],
            carry)


def parse_depths(buf: str, stream: bool = True,
                 order: tuple = DEPTH_ORDER) -> tuple[list[DepthFix], str]:
    """Decode depth records: one metres-below-surface value per vehicle."""
    records, carry = _values(buf, len(order), stream)
    now = datetime.now(timezone.utc)
    return [DepthFix(now, dict(zip(order, r))) for r in records], carry


def explain(buf: str, count: int = POSITION_FIELDS) -> str:
    """Say why a chunk did not decode, in terms someone can act on.

    "It didn't parse" is useless on a vessel, so name the thing that is wrong:
    an unexpected timestamp, a non-numeric field, or the wrong field count.
    """
    if not buf.strip():
        return "empty datagram"
    m = re.search(_DATE, buf)
    if m:
        return (f"this looks timestamped ({m.group(0)}), but the feed is "
                f"expected to be {count} bare numeric fields per record")
    text = unglue(buf)
    toks = [t for t in re.split(r"[,;\t\r\n ]+", text.strip()) if t]
    bad = [t for t in toks if not _GLUED_NUM.fullmatch(t)]
    if bad:
        return (f"not every field is a number - first odd one is {bad[0]!r}, "
                f"record starts {buf[:40]!r}")
    seps = {c for c in buf[:200] if not (c.isdigit() or c in "+-.eE")}
    seps.discard(" ")
    sep_note = ("separators seen: " + ", ".join(repr(c) for c in sorted(seps)[:6])
                if seps else "no separators at all - the numbers run together")
    return (f"{len(toks)} numeric fields, which is not a whole number of "
            f"{count}-field records. {sep_note}")


class UdpFeed(QtCore.QThread):
    """Listens on a UDP port and emits every record it decodes.

    Runs on its own thread; ``fix`` is delivered to the GUI thread by Qt's
    queued connection, so nothing here touches the scene directly.
    """

    fix = QtCore.Signal(object)          # Fix or DepthFix
    status = QtCore.Signal(str, bool)    # message, healthy

    def __init__(self, port: int, decoder, fields: int, label: str,
                 host: str = "0.0.0.0", parent=None, full_order: tuple = (),
                 per_body: int = 1):
        super().__init__(parent)
        self.port = int(port)
        self.host = host
        self.label = label
        self._decode = decoder
        self.fields = fields
        #: Which bodies the sender is filling, and how many numbers each takes.
        #: Absent vehicles are sent as empty fields and empty fields never
        #: reach the decoder, so the record really is shorter - see
        #: :func:`active_order`.
        self.full_order = tuple(full_order)
        self.per_body = int(per_body)
        self.order = tuple(full_order)
        self.full_fields = fields
        self.mismatch = 0
        self._warned_empty = False
        self._drop_carry = False
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

    def set_layout(self, chains) -> None:
        """Say which ROV chains are deployed, so the record length matches.

        Assigned as one tuple each, which is atomic, so the listening thread
        never sees an order and a field count that disagree.
        """
        order = active_order(chains, self.full_order)
        self.order = order
        self.fields = max(self.per_body * len(order), 1)
        self.mismatch = 0
        self._warned_empty = False
        # Whatever is half-decoded was framed against the old record length,
        # so it cannot be read against the new one: the first records after a
        # change came out shifted by a body, putting the vessel's position on
        # the ROV. Drop it and resync on the next datagram.
        self._drop_carry = True

    def _check_shape(self, raw: str, order: tuple) -> None:
        """Say so when the datagram carries a different number of vehicles.

        Reading a ten-number record as six puts an ROV at its TMS's position,
        which looks entirely plausible and is 132 m wrong. A gap in the display
        is recoverable; a vehicle drawn somewhere it is not, is not. So a
        mismatch is reported rather than decoded.
        """
        n = sum(1 for t in _SPLIT.split(unglue(raw).strip()) if t)
        if not n or self.fields <= 0 or n % self.fields == 0:
            self.mismatch = 0
            return
        self.mismatch += 1
        if self.mismatch not in (3, 30):      # once quickly, then rarely
            return
        want = len(order)
        if self.full_fields and n % self.full_fields == 0:
            guess = (f" That is a full set of {len(self.full_order)} vehicles, "
                     f"so every ROV chain is probably reporting - select both "
                     f"in the Targets panel.")
        else:
            guess = (f" Expecting {self.fields} numbers for {want} "
                     f"vehicle{'s' if want != 1 else ''}.")
        self.status.emit(
            f"{self.label}: {n} numbers per datagram does not divide into "
            f"records.{guess}", False)

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
                f"{self.label}: cannot bind UDP {self.host}:{self.port} - {exc}. "
                "Another program on this machine is probably already receiving "
                "this feed.", False)
            return

        sock.settimeout(0.4)
        self.started_at = time.monotonic()
        self.status.emit(f"{self.label}: listening on UDP {self.port}", True)
        carry = ""
        try:
            while not self._stop.is_set():
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    # Sender has gone quiet: nothing is coming to confirm the
                    # held record, so take it at its word rather than sit on
                    # the last known value for ever.
                    if carry and time.monotonic() - self.last_packet_at > 1.5:
                        flushed, carry = self._decode(carry, stream=False,
                                                      order=self.order)
                        self.carry_len = len(carry)
                        for f in flushed:
                            self.records += 1
                            self.fix.emit(f)
                    continue
                except OSError as exc:
                    self.status.emit(f"{self.label}: socket error - {exc}", False)
                    break
                self.packets += 1
                self.last_packet_at = time.monotonic()
                self.last_addr = f"{addr[0]}:{addr[1]}"
                raw = data.decode("ascii", errors="replace")
                self.last_raw = raw[:220]

                try:
                    carry = self._handle(raw, carry)
                except Exception as exc:                  # noqa: BLE001
                    # Nothing a datagram can do may end the listener. Twice
                    # now a fault in here has killed the thread outright and
                    # the feed simply stopped, with only a traceback on a
                    # console nobody had open. A dropped datagram is cheap; a
                    # dead listener is the whole job.
                    carry = ""
                    self.carry_len = 0
                    self.bad += 1
                    self.status.emit(
                        f"{self.label}: dropped a datagram - "
                        f"{type(exc).__name__}: {exc}", False)
        finally:
            sock.close()
            self.status.emit(f"{self.label}: stopped", False)

    def _handle(self, raw: str, carry: str) -> str:
        """Decode one datagram and publish what it holds. Returns the new carry.

        Split out of the listening loop so a fault in here is caught there and
        costs one datagram rather than the whole feed.
        """
        # Field positions first. A datagram that holds whole records says for
        # itself which bodies are reporting - an absent one is an empty field -
        # so nothing has to be configured and nothing guessed. Only a sender
        # that splits or batches records falls through to the numeric resync,
        # where the field positions cannot be trusted.
        whole = parse_fielded(raw, self.full_order, self.per_body)
        if whole is not None:
            self.carry_len = 0
            self.mismatch = 0
            self._warned_empty = False
            self._drop_carry = False
            now_utc = datetime.now(timezone.utc)
            for one in whole:
                self.records += 1
                if self.per_body == 2:
                    self.fix.emit(Fix(now_utc, {k: (v[0], v[1])
                                                for k, v in one.items()}))
                else:
                    self.fix.emit(DepthFix(now_utc,
                                           {k: v[0] for k, v in one.items()}))
            return ""

        if self._drop_carry:
            # Half-decoded text framed against a previous record length cannot
            # be read against this one. Drop it and resync on this datagram.
            self._drop_carry = False
            carry = ""
        pending = carry + raw
        # Diagnose the pending buffer, not this datagram alone: with no line
        # terminators a datagram routinely starts mid-record, and judging it on
        # its own reports a fault that isn't there.
        self.last_pending = pending[:220]

        order = self.order          # one read; it may change under us
        if not order:
            # Nothing to decode onto. Say so once rather than sitting silent,
            # and keep listening so it recovers when a chain is selected again.
            self.carry_len = 0
            if not self._warned_empty:
                self._warned_empty = True
                self.status.emit(
                    f"{self.label}: no vehicles to decode onto - "
                    "select an ROV chain in the Targets panel.", False)
            return ""
        self._warned_empty = False

        fixes, carry = self._decode(pending, order=order)
        self.carry_len = len(carry)
        if not fixes and not carry:
            self.bad += 1
        self._check_shape(raw, order)
        for f in fixes:
            self.records += 1
            self.fix.emit(f)
        return carry


def PositionFeed(port: int = DEFAULT_PORT, host: str = "0.0.0.0", parent=None):
    """Listener for the position feed."""
    return UdpFeed(port, parse_records, POSITION_FIELDS, "Positions", host,
                   parent, full_order=ORDER, per_body=2)


def DepthFeed(port: int = DEFAULT_DEPTH_PORT, host: str = "0.0.0.0", parent=None):
    """Listener for the depth feed."""
    return UdpFeed(port, parse_depths, DEPTH_FIELDS, "Depths", host,
                   parent, full_order=DEPTH_ORDER, per_body=1)


def _sniff(argv):
    """`python -m bathy3d.feed [port] [fields]` - print datagrams and decodes."""
    port = int(argv[1]) if len(argv) > 1 else DEFAULT_PORT
    fields = int(argv[2]) if len(argv) > 2 else (
        DEPTH_FIELDS if port == DEFAULT_DEPTH_PORT else POSITION_FIELDS)
    decode = parse_depths if fields == DEPTH_FIELDS else parse_records
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
    print(f"listening on UDP 0.0.0.0:{port}, expecting {fields} fields per "
          "record - Ctrl+C to stop")
    n = 0
    carry = ""
    try:
        while True:
            data, addr = sock.recvfrom(65535)
            n += 1
            raw = data.decode("ascii", errors="replace")
            fixes, carry = decode(carry + raw)
            print("")
            print(f"[{n}] {len(data)} bytes from {addr[0]}:{addr[1]}")
            print(f"    raw: {raw[:200]!r}")
            if fixes:
                for f in fixes:
                    if isinstance(f, DepthFix):
                        pretty = "  ".join(f"{k} {v:.3f} m"
                                           for k, v in f.depths.items())
                    else:
                        pretty = "  ".join(f"{k} {v[0]:.3f}E {v[1]:.3f}N"
                                           for k, v in f.pos.items())
                    print(f"    ok : {pretty}")
            else:
                print(f"    !! {explain(raw, fields)}")
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
