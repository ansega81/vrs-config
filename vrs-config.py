#!/usr/bin/env python3
"""
vrs-config - native Linux configurator for VRS DirectForce wheel bases
(DFP15 / DirectForce Pro, USB VID 0x0483 PID 0xA355) over /dev/hidraw.

The wire protocol was reverse-engineered from VRS's own `direct_force_sdk`
(disassembled under Wine + struct-probed with an embedded-python harness) and
confirmed byte-for-byte against recorded traffic of the official VRS One
configuration tool reading and saving settings on a healthy DFP15.

Transport (device-verified, identical to the official tool):
    SET_FEATURE(0x66, 512-byte request frame)          # incl. report-id byte
    GET_FEATURE(0x66, 512-byte reply frame)
over EP0 HID GET/SET_REPORT - no interrupt traffic for config.

Frame layout (little-endian, both directions), confirmed byte-for-byte:

    off  size  field
      0    1   report id (0x66)
      1    1   channel   (protocol selector, see CHANNELS)
      2    4   client_id    random u32, CONSTANT per connection
      6    2   payload_size  (requests: 0 for pure reads; replies: bytes at 32)
      8    2   offset        (chunked reads, e.g. 0/476/952/1428 on channel 0x0e)
     10    4   sequence_id   random u32 per logical OPERATION (constant in a burst)
     14    4   request_id    increments 0,1,2... per connection
     18    1   calculate_crc flag (always 1)
     19   13   zero var-header (always zeros in practice)
     32    N   PROTOBUF payload
    ...        zero padding
    508    4   crc32 = zlib.crc32(frame[0:508])   (CONFIRMED on 22+ device replies)

CHANNELS (observed on the device; payloads are protobuf, schema SimWheelBase.proto
shipped by the device itself on channel 0x0e as a 1604-byte gzip):

    READ  0x00 handshake echo (sz 0)              SAFE (proven on the wire)
    READ  0x02 settings header -> SimSettings.SettingsHeader
    READ  0x04 profile by index: req pb{1:n} -> reply profile blob
    READ  0x07 device versions -> DeviceVersions (hw/fw/boot/build date)
    READ  0x0e schema, chunked 476B gzip -> SimWheelBase.proto
    READ  0x15 / 0x17 small status/profile-list reads
    WRITE 0x01 commit (sz 0, last frame of a save burst)
    WRITE 0x03 settings header, 0x16 header variant (pb{1,3})
    WRITE 0x19 profile header 16B, 0x06 profile blob  (variants 0x18/0x05 seen)
    FORBIDDEN 0x08 0x09  firmware/bootloader region - NEVER exercised.

A save burst is: 0x00, 0x16, 0x19, 0x03, 0x06 x N(profiles), 0x01(commit);
all write replies are sz-0 ACKs.

STATUS: fully live-validated against a DFP15 (fw 1.15) - reads, writes and
profile management all work natively on Linux.

Safety: this tool never builds firmware/bootloader frames. Writes require
--allow-write AND an explicit channel; the high-level commands are read-only.
"""

import argparse
import ctypes
import ctypes.util
import fcntl
import glob
import gzip
import os
import random
import struct
import sys
import time
import zlib

VRS_VID = 0x0483
VRS_BASE_PIDS = {0xA355}          # DFP15 / DirectForce Pro wheel base
FEATURE_ID = 0x66
FEAT_SIZE = 512
FEAT64_SIZE = 64
CRC_FLAG = 18
VARSZ = 13                        # zero header between flag and payload
PAYLOAD_OFF = HDR = 19 + VARSZ    # payloads live at [32:32+sz] (both ways)

READ_CHANNELS  = [0x00, 0x02, 0x04, 0x07, 0x0e, 0x15, 0x17]
WRITE_CHANNELS = [0x01, 0x03, 0x05, 0x06, 0x16, 0x18, 0x19, 0x1a, 0x14]
FORBIDDEN      = [0x08, 0x09]     # fw/bootloader - never send

libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def _ioc(direction, iotype, nr, size):
    return ((direction << 30) | (size << 16) | (iotype << 8) | nr) & 0xFFFFFFFF


HIDIOCGFEATURE = lambda n: _ioc(2, ord("H"), 3, n)
HIDIOCSFEATURE = lambda n: _ioc(1, ord("H"), 3, n)


class HidDevice:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def _ioctl(self, code, buf):
        r = libc.ioctl(self.fd, ctypes.c_ulong(code), buf)
        if r < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"ioctl 0x{code:08x} failed: {os.strerror(err)}")
        return r

    def get_feature(self, report_id, size):
        buf = ctypes.create_string_buffer(size)
        buf[0] = bytes([report_id])
        self._ioctl(HIDIOCGFEATURE(size), buf)
        return bytearray(buf.raw[:size])

    def set_feature(self, data):
        size = len(data)
        buf = ctypes.create_string_buffer(bytes(data), size)
        self._ioctl(HIDIOCSFEATURE(size), buf)


def find_bases():
    for rd in sorted(glob.glob("/dev/hidraw*")):
        try:
            minor = os.minor(os.stat(rd).st_rdev)
            ue = open(f"/sys/class/hidraw/hidraw{minor}/device/uevent").read()
        except OSError:
            continue
        kv = dict(l.split("=", 1) for l in ue.splitlines() if "=" in l)
        hid = kv.get("HID_ID", "")
        parts = hid.split(":")
        if len(parts) == 3:
            try:
                vid, pid = int(parts[1], 16), int(parts[2], 16)
            except ValueError:
                continue
            if vid == VRS_VID and pid in VRS_BASE_PIDS:
                yield rd, vid, pid, kv.get("HID_NAME", "?")


# ------------------------------------------------------------------ protobuf
def pb_varint(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def pb_read_varint(buf, i):
    val = shift = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def pb_encode(field, value):
    """Encode one field: int -> varint, bytes -> length-delimited,
    str -> length-delimited utf8. Tag is a varint (fields >= 16 need
    two tag bytes - device blobs use e.g. a0 01 for field 20)."""
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        return pb_varint(field << 3) + pb_varint(value)
    if isinstance(value, str):
        value = value.encode()
    return pb_varint((field << 3) | 2) + pb_varint(len(value)) + value


def pb_decode(buf):
    """Return list of (field, wire, value) - value int/bytes for wire 0/2."""
    out, i = [], 0
    buf = bytes(buf)
    while i < len(buf):
        try:
            key, i = pb_read_varint(buf, i)
        except IndexError:
            break
        field, wire = key >> 3, key & 7
        if wire == 0:
            v, i = pb_read_varint(buf, i)
        elif wire == 2:
            ln, i = pb_read_varint(buf, i)
            v = buf[i:i + ln]
            i += ln
        elif wire == 5:
            v = buf[i:i + 4]
            i += 4
        elif wire == 1:
            v = buf[i:i + 8]
            i += 8
        else:
            break
        out.append((field, wire, v))
    return out


def pb_get(fields, num, default=None):
    for f, w, v in fields:
        if f == num:
            return v
    return default


# profile fields from SimWheelBase.proto (device-shipped schema, see schema cmd)
PROFILE_FIELDS = {
    1: "profile_header", 3: "rotationDegrees", 6: "ffbForce", 7: "smoothing",
    8: "damping", 9: "friction", 10: "inertia", 11: "spring",
    12: "dampingOverride", 13: "frictionOverride", 14: "inertiaOverride",
    15: "springOverride", 16: "smoothingType", 17: "staticForceReduction",
    18: "nonLinearity", 19: "slewRate", 20: "endStopMaxForce",
    21: "endStopStiffness", 22: "softEndStopDegrees", 23: "isSoftEndStopEnabled",
    24: "torqueUpperBound", 30: "filterMode", 31: "motorSlewRate",
    32: "softwareSlewRate", 33: "predictionLevel", 34: "predictionPeakDamp",
    35: "stepBandStartFreq", 36: "stepBandDamping", 37: "lowPassCutoffFreq",
}
HEADER_FIELDS = {1: "num_profiles", 2: "current_profile", 3: "max_num_profiles",
                 4: "settings_version", 5: "config_byte_a"}


def pb_reencode(fields, updates):
    """Rebuild a proto3 message from decoded (field, wire, value) triples,
    replacing/adding varint fields from {field: int}. Wire order preserved,
    new fields appended in field-number order."""
    out = b""
    used = set()
    for f, w, v in fields:
        if f in updates:
            out += pb_encode(f, updates[f])
            used.add(f)
        elif w in (0, 2):
            out += pb_encode(f, v)
        elif w == 5:
            out += pb_varint((f << 3) | 5) + v
        elif w == 1:
            out += pb_varint((f << 3) | 1) + v
        else:
            raise ValueError("bad wire type %d for field %d" % (w, f))
    for f in sorted(set(updates) - used):
        out += pb_encode(f, updates[f])
    return out


def decode_versions(payload):
    """DeviceVersions{1 hw Version,2 fw Version,3 boot Version,4 build_info}"""
    f = pb_decode(payload)

    def ver(n):
        v = pb_get(f, n)
        if v is None:
            return "?"
        inner = pb_decode(v)
        return "%d.%d" % (pb_get(inner, 1, 0), pb_get(inner, 2, 0))

    build = pb_get(f, 4)
    date, hash_ = "", ""
    if build:
        bf = pb_decode(build)
        hash_ = "%08x" % (pb_get(bf, 1, 0) & 0xFFFFFFFF)
        date = (pb_get(bf, 2, b"") or b"").decode("ascii", "replace")
    return dict(hw=ver(1), fw=ver(2), bootloader=ver(3), hash=hash_, date=date)


def decode_profile(payload):
    """SimWheelBase.SettingsProfile -> ordered dict incl. name."""
    f = pb_decode(payload)
    out = {}
    hdr = pb_get(f, 1)
    if hdr:
        hf = pb_decode(hdr)
        name = pb_get(hf, 2, b"")
        out["name"] = (name or b"").decode("utf-8", "replace")
        out["profile_number"] = pb_get(hf, 1, "?")
    for n, key in PROFILE_FIELDS.items():
        if n == 1:
            continue
        v = pb_get(f, n)
        if v is None:
            continue
        if isinstance(v, bytes) and len(v) == 4:
            out[key] = round(struct.unpack("<f", v)[0], 4)
        else:
            out[key] = v
    return out


# ------------------------------------------------------------------ frames
def build_frame(channel, payload=b"", size=FEAT_SIZE, client_id=0,
                offset=0, seq=0, req=0, crc_at=None):
    if crc_at is None:
        crc_at = size - 4                       # 508 / 60 (both device-verified)
    if len(payload) > crc_at - PAYLOAD_OFF:
        raise ValueError(f"payload {len(payload)} > room {crc_at - PAYLOAD_OFF}")
    f = bytearray(size)
    f[0] = FEATURE_ID
    f[1] = channel & 0xFF
    struct.pack_into("<I", f, 2, client_id)
    struct.pack_into("<H", f, 6, len(payload))
    struct.pack_into("<H", f, 8, offset)
    struct.pack_into("<I", f, 10, seq)
    struct.pack_into("<I", f, 14, req)
    f[CRC_FLAG] = 1
    f[PAYLOAD_OFF:PAYLOAD_OFF + len(payload)] = payload
    crc = zlib.crc32(bytes(f[:crc_at])) & 0xFFFFFFFF
    struct.pack_into("<I", f, crc_at, crc)
    return f


def parse_frame(buf):
    d = dict(
        report_id=buf[0], channel=buf[1],
        client_id=struct.unpack_from("<I", buf, 2)[0],
        payload_size=struct.unpack_from("<H", buf, 6)[0],
        offset=struct.unpack_from("<H", buf, 8)[0],
        sequence_id=struct.unpack_from("<I", buf, 10)[0],
        request_id=struct.unpack_from("<I", buf, 14)[0],
        crc_flag=buf[CRC_FLAG],
    )
    n = len(buf)
    d["crc_tail"] = struct.unpack_from("<I", buf, n - 4)[0]
    d["crc_calc"] = zlib.crc32(bytes(buf[:n - 4])) & 0xFFFFFFFF
    d["crc_ok"] = d["crc_tail"] == d["crc_calc"]
    ps = d["payload_size"]
    d["payload"] = bytes(buf[PAYLOAD_OFF:PAYLOAD_OFF + ps]) if ps else b""
    return d


def hexdump(b, limit=64):
    out = []
    for i in range(0, min(len(b), limit), 16):
        row = b[i:i + 16]
        out.append("%03x  %s  %s" % (i, " ".join("%02x" % c for c in row),
                                     "".join(chr(c) if 32 <= c < 127 else "." for c in row)))
    return "\n".join(out)


class Session:
    """One logical connection: fixed random client_id, incrementing req,
    fresh random seq nonce per operation (exactly like VRS One on Windows)."""

    def __init__(self, dev, verbose=False):
        self.dev = dev
        self.verbose = verbose
        self.client_id = random.getrandbits(32)
        self.req = -1
        self.seq = random.getrandbits(32)

    def new_operation(self):
        self.seq = random.getrandbits(32)

    def exchange(self, channel, payload=b"", size=FEAT_SIZE, crc_at=None,
                 offset=0):
        self.req += 1
        frame = build_frame(channel, payload, size=size, client_id=self.client_id,
                            seq=self.seq, req=self.req, crc_at=crc_at, offset=offset)
        if self.verbose:
            print("--> ch=0x%02x req=%d seq=%08x" % (channel, self.req, self.seq))
            print(hexdump(frame, PAYLOAD_OFF + min(len(payload), 32)))
        self.dev.set_feature(frame)
        time.sleep(0.05)
        reply = self.dev.get_feature(FEATURE_ID, size)
        p = parse_frame(reply)
        if self.verbose:
            print("<-- ch=0x%02x size=%d seq=%08x req=%d crc@508 ok=%s"
                  % (p["channel"], p["payload_size"], p["sequence_id"],
                     p["request_id"], p["crc_ok"]))
        return p


def open_session(verbose=False, raw=False):
    if raw:
        dev = RawDevice()
        sess = Session(dev, verbose=verbose)
        # firmware gates all reads per logical connection: the ch-0x00
        # handshake must be request #0 with THIS client_id (official-tool burst
        # behaviour; without it every GET_REPORT stalls EPIPE), and every
        # burst ENDS with a ch-0x01 commit - without it the device keeps
        # the connection open and stalls the NEXT handshake too.
        sess.exchange(0x00)
        orig_close = dev.close
        def _close(orig=orig_close, ss=sess):
            try:
                ss.exchange(0x01)
            except OSError:
                pass
            orig()
        dev.close = _close
        return dev, sess
    path = next(iter(find_bases()), None)
    if not path:
        sys.exit("no VRS wheel base (0483:A355) on /dev/hidraw* - "
                 "see README (wedge status) and run ./vrs-config.py status")
    try:
        dev = HidDevice(path[0])
    except PermissionError:
        sys.exit(f"{path[0]}: EACCES - fix with: sudo udevadm trigger "
                 "--subsystem-match=hidraw --action=change")
    return dev, Session(dev, verbose=verbose)


class RawDevice:
    """RAW usbdevfs transport - the one proven to answer on Linux:
    iface-recipient SET/GET_REPORT 0x66, exact Windows frames, device
    CRC-valid replies. Requirements:
      - the /dev/bus/usb node is openable (0666 via the 0483 udev rule), and
      - no kernel HID driver holds the interface: usbhid (or pidff) is
        unbound automatically here via sysfs when possible (needs root);
        otherwise run with sudo (pass --raw to any command).
    Set feature report includes the leading 0x66 id byte, wIndex = interface.
    """

    IFACE = 0

    def __init__(self):
        nodes = list(_usb_nodes())
        if not nodes:
            raise SystemExit("no 0483:a355 under /sys/bus/usb/devices "
                             "(is the base plugged in?)")
        node, udir = nodes[0]
        self.udir = udir
        self.busdir = node
        self.path = node
        self._unbind_driver()
        self.fd = os.open(node, os.O_RDWR)
        fcntl.ioctl(self.fd, 0x8004550F, struct.pack("@I", self.IFACE))
        # success = no exception; _IOR returns the u32 echo buffer

    def _unbind_driver(self):
        import glob as _g
        for ifd in _g.glob(self.udir + ":*"):
            drv = _g.glob(ifd + "/driver")
            if drv:
                dname = os.path.basename(os.readlink(drv[0]))
                try:
                    with open(f"/sys/bus/usb/drivers/{dname}/unbind", "w") as f:
                        f.write(os.path.basename(ifd))
                    print(f"  (unbound {os.path.basename(ifd)} from {dname}; "
                          "joystick/FFB return on replug or after this tool "
                          "rebinds)")
                except OSError as e:
                    raise SystemExit(f"interface held by {dname} and unbind "
                                     f"denied: {e}\nrun with sudo")

    def _xfer(self, reqtype, request, value, length=0, data=None):
        ok, r, payload = _ep0_ctrl(self.fd, reqtype, request, value,
                                   self.IFACE, length=length, data=data)
        if not ok:
            raise OSError(0, f"USB control transfer failed: {r}")
        return payload if data is None else b""

    def set_feature(self, frame):
        self._xfer(0x21, 0x09, 0x0366, data=bytes(frame))

    def get_feature(self, rid, size):
        d = self._xfer(0xA1, 0x01, 0x0300 | rid, length=size)
        if len(d) < size:
            d = d + bytes(size - len(d))
        return d

    def close(self):
        try:
            fcntl.ioctl(self.fd, 0x80045510, struct.pack("@I", self.IFACE))
        except OSError:
            pass
        try:
            self.fd.close()
        except Exception:
            pass


# ------------------------------------------------------------------ commands
def cmd_status(args):
    if getattr(args, "raw", False):
        try:
            dev, s = open_session(False, raw=True)
        except (OSError, SystemExit) as e:
            print("raw transport unavailable:", e)
            return 2
        try:
            p = s.exchange(0x00)
            print(f"raw EP0 transport OK: handshake echoed ch=0x{p['channel']:02x}"
                  f" crc@508={'valid' if p['crc_ok'] else 'BAD'} (device-computed)")
            rc = 0 if p["crc_ok"] else 4
        except OSError as e:
            print("raw exchange failed:", e)
            rc = 2
        dev.close()
        return rc
    found = list(find_bases())
    if not found:
        print("no VRS wheel base (0483:A355) on /dev/hidraw*")
        return 1
    rc = 0
    for path, vid, pid, name in found:
        try:
            d = HidDevice(path)
        except PermissionError:
            print(f"{path}  vid=0x{vid:04x} pid=0x{pid:04x}  {name}")
            print("  EACCES - udev missed the node's add event; fix with:")
            print("  sudo udevadm trigger --subsystem-match=hidraw --action=change")
            rc = 3
            continue
        print(f"{path}  vid=0x{vid:04x} pid=0x{pid:04x}  {name}")
        try:
            b = d.get_feature(0x10, 2)  # exact: 1-byte feature report + id byte
            print("  GET 0x10:", bytes(b).hex(), "- control OK")
        except OSError as e:
            print(f"  GET 0x10 failed: {e}")
            print("  NOTE: this is EXPECTED on healthy bases - the firmware only")
            print("  services the handshake-gated protocol. Liveness check is:")
            print("      sudo ./vrs-config.py --raw status")
            rc = 2
        d.close()
    return rc


def cmd_versions(args):
    dev, s = open_session(args.verbose, raw=args.raw)
    p = s.exchange(0x07)
    if not p["crc_ok"]:
        print("reply CRC mismatch - not a valid VRS reply:", hexdump(bytes(p["payload"][:0]), 0) or p["crc_ok"])
    v = decode_versions(p["payload"])
    print(f"hardware rev:   {v['hw']}")
    print(f"firmware rev:   {v['fw']}   (build {v['hash']} {v['date']})")
    print(f"bootloader rev: {v['bootloader']}")
    dev.close()
    return 0


def cmd_header(args):
    dev, s = open_session(args.verbose, raw=args.raw)
    p = s.exchange(0x02)
    for f, w, v in pb_decode(p["payload"]):
        name = HEADER_FIELDS.get(f, f"field{f}")
        print(f"{name} = {v}")
    dev.close()
    return 0


def cmd_profiles(args):
    dev, s = open_session(args.verbose, raw=args.raw)
    p = s.exchange(0x02)
    hdr = pb_decode(p["payload"])
    n = pb_get(hdr, 1, 1)
    for idx in range(n):
        s.new_operation()
        r = s.exchange(0x04, pb_encode(1, idx + 1))
        prof = decode_profile(r["payload"])
        name = prof.pop("name", "?")
        print(f"--- profile {idx}: {name}")
        for k, val in prof.items():
            print(f"    {k:22s} = {val}")
    dev.close()
    return 0


def cmd_schema(args):
    dev, s = open_session(args.verbose, raw=args.raw)
    s.new_operation()
    chunks, off = {}, 0
    while True:
        r = s.exchange(0x0e, offset=off)
        sz = r["payload_size"]
        chunks[off] = r["payload"][:sz]
        if sz < 476 or off + sz > 8192:
            break
        off += sz
    blob = b"".join(chunks[k] for k in sorted(chunks))
    print(f"collected {len(blob)} bytes (gzip magic: {blob[:2].hex()})")
    try:
        text = gzip.decompress(blob)
    except OSError:
        print("payload is not gzip - raw dump follows:")
        print(hexdump(blob, 128))
        dev.close()
        return 2
    out = args.out or "SimWheelBase.proto"
    open(out, "wb").write(text)
    print(f"wrote {len(text)} bytes -> {out}")
    dev.close()
    return 0


def cmd_raw(args):
    ch = int(args.channel, 0)
    if ch in FORBIDDEN and not args.force:
        print(f"channel 0x{ch:02x} is forbidden (fw/bootloader region) - use --force if insane")
        return 1
    if ch in WRITE_CHANNELS and not args.allow_write:
        print(f"channel 0x{ch:02x} writes to the base; add --allow-write")
        return 1
    payload = bytes.fromhex(args.payload.replace(" ", "")) if args.payload else b""
    dev, s = open_session(True, raw=args.raw)
    p = s.exchange(ch, payload, size=args.size, crc_at=args.crc_at)
    print("reply payload:", p["payload"].hex())
    if payload[:2] == b"\x0a" or p["payload_size"] > 0:
        try:
            print("decoded:", pb_decode(p["payload"]))
        except Exception:
            pass
    dev.close()
    return 0


def cmd_sweep(args):
    dev, s = open_session(args.verbose, raw=args.raw)
    channels = READ_CHANNELS if not args.channels else \
        [int(x, 0) for x in args.channels.split(",")]
    print(f"sweeping channels {[hex(c) for c in channels]}")
    for ch in channels:
        if ch in FORBIDDEN or (ch in WRITE_CHANNELS and not args.allow_write):
            continue
        s.new_operation()
        try:
            p = s.exchange(ch)
        except OSError as e:
            print(f"ch 0x{ch:02x}: SET/GET failed: {e}")
            continue
        print(f"ch=0x{ch:02x}: size={p['payload_size']} crc_ok={p['crc_ok']}")
        if p["payload_size"]:
            print(hexdump(p["payload"], min(p["payload_size"], 96)))
    dev.close()
    return 0


def cmd_set_profile(args):
    NAME2NUM = {v: k for k, v in PROFILE_FIELDS.items()
                if k != 1 and isinstance(v, str)}
    upd = {}
    for kv in args.set or []:
        k, _, v = kv.partition("=")
        if k not in NAME2NUM:
            print(f"unknown field '{k}'; valid: {', '.join(sorted(NAME2NUM))}")
            return 1
        upd[NAME2NUM[k]] = int(v, 0)
    if not upd:
        print("nothing to set; use --set NAME=INT "
              "(names/ranges: SimWheelBase.proto, see README)")
        return 1
    dev, s = open_session(True, raw=args.raw)
    try:
        p02 = s.exchange(0x02)["payload"]
        num = pb_get(pb_decode(p02), 1, 1)
        if not (0 <= args.profile < num):
            print(f"--profile must be 0..{num - 1}")
            return 1
        s.new_operation()
        p15 = s.exchange(0x15)["payload"]
        s.new_operation()
        p17 = s.exchange(0x17)["payload"]
        blobs = []
        for i in range(num):
            s.new_operation()
            blobs.append(s.exchange(0x04, pb_encode(1, i + 1))["payload"])
        old = blobs[args.profile]
        fields = pb_decode(old)
        wires = {f: w for f, w, v in fields}
        for fnum in upd:
            if fnum in wires and wires[fnum] != 0:
                print(f"{PROFILE_FIELDS[fnum]} is not a plain int field on this "
                      f"fw (wire {wires[fnum]}); refusing")
                return 1
        print(f"profile {args.profile} ({decode_profile(old).get('name', '?')}):")
        for fnum, val in upd.items():
            print(f"    {PROFILE_FIELDS[fnum]}: {pb_get(fields, fnum)} -> {val}")
        new = pb_reencode(fields, upd)
        print(f"  blob {len(old)}B -> {len(new)}B")
        if args.dry_run:
            print("dry-run: nothing written. new blob hex:")
            print(new.hex())
            return 0
        if not args.allow_write:
            print("this WRITES to the base - re-run with --allow-write "
                  "(or --dry-run to preview)")
            return 1
        # official-tool save pattern: close the read burst (ch 0x01), then a NEW
        # burst on the same connection: handshake, capability/header frames,
        # the full profile set, and the commit comes from dev.close().
        s.exchange(0x01)
        s.new_operation()
        s.exchange(0x00)
        s.exchange(0x16, p15)
        s.exchange(0x19, p17)
        s.exchange(0x03, p02)
        blobs[args.profile] = new
        ack_ok = True
        for b in blobs:
            r = s.exchange(0x06, b)
            ack_ok &= bool(r["crc_ok"])
        print("save burst sent (16/19/03/06x%d); replies crc-ok: %s"
              % (len(blobs), ack_ok))
        print("verify with: ./vrs-config.py --raw profiles")
        return 0
    finally:
        dev.close()


def cmd_selftest(args):
    """Replay an official-tool frame dump offline (decoder self-check)."""
    path = args.frames
    reps, reqs = [], []
    for line in open(path):
        m = line.strip().startswith("REPLY full:")
        if m:
            h = line.strip().split("REPLY full:")[1].strip()
            if len(h) == 1024:
                reps.append(bytes.fromhex(h))
    reqpath = os.path.join(os.path.dirname(path), "reqs.txt")
    if os.path.exists(reqpath):
        reqs = [bytes.fromhex(l.strip()) for l in open(reqpath) if len(l.strip()) == 1024]
    print(f"frame dump: {len(reps)} replies, {len(reqs)} requests")
    fails = 0
    for f in reps + reqs:
        p = parse_frame(f)
        if not p["crc_ok"]:
            fails += 1
            print(f"CRC FAIL ch=0x{p['channel']:02x} req={p['request_id']}")
    print(f"CRC32@508 over [:508]: {'ALL OK' if fails == 0 else str(fails) + ' FAILURES'}")
    seen = {}
    for f in reps:
        p = parse_frame(f)
        seen.setdefault(p["channel"], p)
    if 0x07 in seen:
        v = decode_versions(seen[0x07]["payload"])
        print(f"versions: hw {v['hw']} fw {v['fw']} boot {v['bootloader']} build {v['hash']} {v['date']}")
        assert v["fw"] == "1.15", v
    if 0x02 in seen:
        hd = {f: v for f, w, v in pb_decode(seen[0x02]["payload"])}
        print(f"header: num_profiles={hd.get(1)} current={hd.get(2)} "
              f"max={hd.get(3)} field5={hd.get(5)}")
        assert hd.get(1) == 2 and hd.get(2) == 1 and hd.get(3) == 100
    profs = [p for p in (parse_frame(f) for f in reps) if p["channel"] == 0x04 and p["payload_size"]]
    for p in profs[:2]:
        pr = decode_profile(p["payload"])
        print(f"profile: name={pr.get('name')} rotation={pr.get('rotationDegrees')} "
              f"ffbForce={pr.get('ffbForce')} torqueUpperBound={pr.get('torqueUpperBound')}")
    chunks = {p["offset"]: p["payload"] for p in (parse_frame(f) for f in reps)
              if p["channel"] == 0x0e and p["payload_size"]}
    if chunks:
        blob = b"".join(chunks[o][:int(476 if o < max(chunks) else 176)]
                        for o in sorted(chunks))
        try:
            text = gzip.decompress(b"".join(chunks[o] for o in sorted(chunks)))
            print(f"schema: gunzipped {len(text)} B, package line: "
                  + [l for l in text.splitlines() if l.startswith(b"package")][0].decode())
        except Exception as e:
            print("schema chunks present but decode failed:", e)
            fails += 1
    print("selftest:", "PASS" if fails == 0 else f"{fails} FAIL")
    return 0 if fails == 0 else 1


def _usb_nodes():
    for d in sorted(glob.glob("/sys/bus/usb/devices/*")):
        try:
            vid = open(d + "/idVendor").read().strip()
            pid = open(d + "/idProduct").read().strip()
        except OSError:
            continue
        if vid == "0483" and pid.lower() in ("a355", "cb52"):
            bus = open(d + "/busnum").read().strip()
            dev = open(d + "/devnum").read().strip()
            yield f"/dev/bus/usb/{int(bus):03d}/{int(dev):03d}", d


def _ep0_ctrl(fd, reqtype, request, value, index, length=0, data=None, timeout=2000):
    inb = data is None
    buf = ctypes.create_string_buffer(data if not inb else max(length, 1))
    hdr = bytearray(struct.pack("<BBHHHI4xQ", reqtype, request, value, index,
                                length if inb else len(data), timeout,
                                ctypes.addressof(buf)))
    try:
        n = fcntl.ioctl(fd, 0xC0185500, hdr)
        return True, n, buf.raw[:n] if inb else b""
    except OSError as e:
        return False, e.strerror or str(e), b""


def cmd_ep0(args):
    """Raw EP0 probe: standard descriptors vs HID-class GET/SET_REPORT."""
    found = list(_usb_nodes())
    if not found:
        print("no 0483:a355 node under /sys/bus/usb/devices")
        return 1
    node, sysdir = found[0]
    itf = next((p.split(":")[1].split(".")[1] for p in os.listdir(sysdir)
                if ":" in p and p.endswith(".0")), "0")
    try:
        fd = os.open(node, os.O_RDWR)
    except OSError as e:
        print(f"{node}: cannot open: {e}")
        return 2
    print(f"probing {node} (interface {itf})")
    ok, r, data = _ep0_ctrl(fd, 0x80, 6, 0x0100, 0, 18)
    print(f"GET_DESCRIPTOR(device): {'ok ' + str(r) + 'B' if ok else r}")
    if ok:
        print("  idVendor=0x%04x idProduct=0x%04x bcdDevice=0x%04x"
              % struct.unpack("<HHH", data[8:14]))
    ok, r, data = _ep0_ctrl(fd, 0x80, 6, 0x0402, 0x0409, 255)
    if ok and r >= 4:
        print(f"GET_DESCRIPTOR(string:product): '{data[2:r].decode('utf-16-le', 'ignore')}'")
    else:
        print("GET_DESCRIPTOR(string:product):", r if not ok else "short")
    for typ, rid, ln, name in ((3, 0x10, 2, "GET_REPORT feature 0x10"),
                               (3, 0x12, 3, "GET_REPORT feature 0x12"),
                               (3, 0x66, 512, "GET_REPORT feature 0x66"),
                               (1, 0x01, 65, "GET_REPORT input 0x01")):
        ok, r, data = _ep0_ctrl(fd, 0xC1, 1, (typ << 8) | rid, int(itf), ln)
        print(f"{name}: " + (f"ok {r}B {data[:16].hex()}" if ok else r))
    if args.try_set:
        frame = bytes(build_frame(0x02, b"", size=FEAT_SIZE))
        ok, r, _ = _ep0_ctrl(fd, 0x41, 9, (3 << 8) | 0x66, int(itf),
                             length=len(frame) - 1, data=frame[1:])
        print("SET_REPORT feature 0x66 (header-read frame): " + (f"ok {r}B" if ok else r))
        if ok:
            ok, r, data = _ep0_ctrl(fd, 0xC1, 1, (3 << 8) | 0x66, int(itf), 512)
            print("GET_REPORT feature 0x66 after set: " +
                  (f"ok {r}B {data[:24].hex()}" if ok else r))
    os.close(fd)
    return 0 if ok else 3


def main():
    ap = argparse.ArgumentParser(prog="vrs-config",
                                 description="VRS DFP15 configurator "
                                             "(protocol confirmed against official-tool traffic)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--raw", action="store_true",
                    help="use the RAW usbdevfs transport (iface-recipient "
                         "SET/GET_REPORT 0x66 - proven to answer even while "
                         "the kernel's hidraw path is dead; unbinds the HID "
                         "driver, may need sudo)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="find base + control-endpoint sanity")
    sub.add_parser("versions", help="device hardware/firmware/bootloader versions")
    sub.add_parser("header", help="settings header (profiles count, current, version)")
    sub.add_parser("profiles", help="dump all profiles, decoded")
    sc = sub.add_parser("schema", help="download device's SimWheelBase.proto schema")
    sc.add_argument("--out", default=None)
    pr = sub.add_parser("raw", help="send one frame, dump reply")
    pr.add_argument("--channel", required=True)
    pr.add_argument("--payload", default="", help="hex protobuf payload (goes at [32:])")
    pr.add_argument("--size", type=int, default=FEAT_SIZE, choices=[FEAT64_SIZE, FEAT_SIZE])
    pr.add_argument("--crc-at", type=int, default=None)
    pr.add_argument("--allow-write", action="store_true")
    pr.add_argument("--force", action="store_true")
    ps = sub.add_parser("sweep", help="sweep read channels")
    ps.add_argument("--channels", default="")
    ps.add_argument("--allow-write", action="store_true")
    pv = sub.add_parser("set-profile",
                        help="edit profile fields and run the confirmed "
                             "save burst (writes! default dry-run)")
    pv.add_argument("--profile", type=int, required=True,
                    help="profile index as shown by `profiles` (0-based)")
    pv.add_argument("--set", action="append", metavar="NAME=INT",
                    help="field=value; repeatable. Names from SimWheelBase.proto"
                         " (rotationDegrees, ffbForce, damping, ... ranges in the proto)")
    pv.add_argument("--dry-run", action="store_true",
                    help="read + show the new blob, write nothing")
    pv.add_argument("--allow-write", action="store_true",
                    help="actually send the save burst")
    pw = sub.add_parser("selftest", help="decode an official-tool frame dump offline")
    pw.add_argument("frames", help="frame dump: lines 'REPLY full: <1024 hex>'; optional sibling reqs.txt with bare hex lines")
    pe = sub.add_parser("ep0", help="raw USB control-transfer probe (diagnostic)")
    pe.add_argument("--try-set", action="store_true",
                    help="also attempt SET_REPORT feature 0x66 (needs the "
                         "hid/pidff driver unbound, see README)")
    args = ap.parse_args()
    return {"status": cmd_status, "versions": cmd_versions, "header": cmd_header,
            "profiles": cmd_profiles, "schema": cmd_schema, "raw": cmd_raw,
            "sweep": cmd_sweep, "set-profile": cmd_set_profile,
            "selftest": cmd_selftest, "ep0": cmd_ep0}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
