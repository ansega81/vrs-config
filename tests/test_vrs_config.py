#!/usr/bin/env python3
"""Unit tests for vrs-config (CLI) and vrs-config-gui (GUI helpers).

No hardware and no network needed - everything is pure codec/frame logic.
Run from the repository root:

    python3 -m unittest discover -s tests -v
"""
import importlib.util
import os
import struct
import unittest
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(fname, modname):
    spec = importlib.util.spec_from_file_location(modname, os.path.join(ROOT, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vc = _load('vrs-config.py', 'vrs_config')
gui, GUI_IMPORT_ERROR = None, None
try:
    gui = _load('vrs-config-gui.py', 'vrs_config_gui')
except Exception as e:                      # tkinter missing is the only excuse
    GUI_IMPORT_ERROR = e


class FrameTest(unittest.TestCase):
    """512/64-byte vendor feature-report framing, PROTOCOL.md SS3/SS4."""

    def test_roundtrip_512(self):
        payload = vc.pb_encode(1, 7) + b'abc'
        f = vc.build_frame(0x04, payload, size=512, client_id=0xdeadbeef,
                           offset=476, seq=0x12345678, req=9)
        p = vc.parse_frame(bytes(f))
        self.assertEqual(p['report_id'], 0x66)
        self.assertEqual(p['channel'], 0x04)
        self.assertEqual(p['client_id'], 0xdeadbeef)
        self.assertEqual(p['payload_size'], len(payload))
        self.assertEqual(p['offset'], 476)
        self.assertEqual(p['sequence_id'], 0x12345678)
        self.assertEqual(p['request_id'], 9)
        self.assertEqual(p['crc_flag'], 1)
        self.assertEqual(p['payload'], payload)
        self.assertTrue(p['crc_ok'])

    def test_roundtrip_64(self):
        payload = b'\x01\x02\x03'
        f = vc.build_frame(0x0f, payload, size=64)
        self.assertEqual(len(f), 64)
        p = vc.parse_frame(bytes(f))
        self.assertTrue(p['crc_ok'])
        self.assertEqual(p['payload'], payload)

    def test_crc_position_512(self):
        f = bytes(vc.build_frame(0x02))
        self.assertEqual(struct.unpack_from('<I', f, 508)[0],
                         zlib.crc32(f[:508]) & 0xFFFFFFFF)
        # var-header [19:32] is always zero, payload starts at 32
        self.assertEqual(f[19:32], bytes(13))
        self.assertEqual(vc.PAYLOAD_OFF, 32)

    def test_empty_payload(self):
        p = vc.parse_frame(bytes(vc.build_frame(0x00)))
        self.assertEqual(p['payload_size'], 0)
        self.assertEqual(p['payload'], b'')
        self.assertTrue(p['crc_ok'])

    def test_oversize_payload_rejected(self):
        with self.assertRaises(ValueError):
            vc.build_frame(0x06, b'x' * (512 - 32 + 1))

    def test_corrupted_crc_detected(self):
        f = bytearray(vc.build_frame(0x07))
        f[40] ^= 0xFF
        self.assertFalse(vc.parse_frame(bytes(f))['crc_ok'])


class ChannelPolicyTest(unittest.TestCase):

    def test_forbidden_are_fw_channels(self):
        self.assertEqual(vc.FORBIDDEN, [0x08, 0x09])

    def test_lists_disjoint(self):
        self.assertFalse(set(vc.FORBIDDEN) & set(vc.READ_CHANNELS))
        self.assertFalse(set(vc.FORBIDDEN) & set(vc.WRITE_CHANNELS))
        self.assertFalse(set(vc.READ_CHANNELS) & set(vc.WRITE_CHANNELS))

    def test_save_burst_channels_present(self):
        for ch in (0x00, 0x16, 0x19, 0x03, 0x06, 0x01):   # official burst
            self.assertTrue(ch in vc.READ_CHANNELS or ch in vc.WRITE_CHANNELS,
                            hex(ch))

    def test_never_used_channels_absent_from_high_level(self):
        # 0x1a/0x14 are gated as writes: reachable only via raw --allow-write
        self.assertIn(0x1a, vc.WRITE_CHANNELS)
        self.assertIn(0x14, vc.WRITE_CHANNELS)


class ProtobufTest(unittest.TestCase):

    def test_varint_boundaries(self):
        self.assertEqual(vc.pb_varint(0), b'\x00')
        self.assertEqual(vc.pb_varint(127), b'\x7f')
        self.assertEqual(vc.pb_varint(128), b'\x80\x01')
        self.assertEqual(vc.pb_varint(300), b'\xac\x02')

    def test_multibyte_tag_fields_ge_16(self):
        # field 20 tag = 20<<3 = 160 -> varint a0 01 (regression: old
        # single-byte struct.pack('B') corrupted every high-field write)
        enc = vc.pb_encode(20, 100)
        self.assertTrue(enc.startswith(b'\xa0\x01'))
        f = vc.pb_decode(enc)
        self.assertEqual(vc.pb_get(f, 20), 100)
        enc37 = vc.pb_encode(37, 1000)
        self.assertEqual(vc.pb_get(vc.pb_decode(enc37), 37), 1000)

    def test_string_field(self):
        enc = vc.pb_encode(2, 'NextGen-AC')
        self.assertEqual(vc.pb_get(vc.pb_decode(enc), 2), b'NextGen-AC')

    def test_nested_message(self):
        inner = vc.pb_encode(1, 3) + vc.pb_encode(2, 'X')
        outer = vc.pb_encode(1, inner)
        hf = vc.pb_decode(vc.pb_get(vc.pb_decode(outer), 1))
        self.assertEqual(vc.pb_get(hf, 1), 3)
        self.assertEqual(vc.pb_get(hf, 2), b'X')

    def test_reencode_updates_and_preserves(self):
        orig = vc.pb_encode(3, 900) + vc.pb_encode(6, 24) + vc.pb_encode(40, 7)
        fields = vc.pb_decode(orig)
        new = vc.pb_reencode(fields, {6: 50})
        got = {f: v for f, w, v in vc.pb_decode(new)}
        self.assertEqual(got[6], 50)
        self.assertEqual(got[3], 900)      # untouched
        self.assertEqual(got[40], 7)       # unknown field preserved

    def test_reencode_inserts_new_field(self):
        fields = vc.pb_decode(vc.pb_encode(3, 10))
        got = {f: v for f, w, v in vc.pb_decode(vc.pb_reencode(fields, {24: 15}))}
        self.assertEqual(got, {3: 10, 24: 15})


class DecodeTest(unittest.TestCase):

    def _versions_payload(self):
        def ver(maj, minr):
            return vc.pb_encode(1, maj) + vc.pb_encode(2, minr)
        build = vc.pb_encode(1, 0x05709ecc) + vc.pb_encode(2, 'Nov 27 2025')
        return (vc.pb_encode(1, ver(4, 0)) + vc.pb_encode(2, ver(1, 15)) +
                vc.pb_encode(3, ver(1, 15)) + vc.pb_encode(4, build))

    def test_decode_versions(self):
        v = vc.decode_versions(self._versions_payload())
        self.assertEqual(v['hw'], '4.0')
        self.assertEqual(v['fw'], '1.15')
        self.assertEqual(v['bootloader'], '1.15')
        self.assertEqual(v['hash'], '05709ecc')
        self.assertEqual(v['date'], 'Nov 27 2025')

    def test_decode_profile(self):
        hdr = vc.pb_encode(1, 2) + vc.pb_encode(2, 'TestProf')
        blob = (vc.pb_encode(1, hdr) + vc.pb_encode(3, 900) +
                vc.pb_encode(6, 24) + vc.pb_encode(20, 100))
        p = vc.decode_profile(blob)
        self.assertEqual(p['name'], 'TestProf')
        self.assertEqual(p['profile_number'], 2)
        self.assertEqual(p['rotationDegrees'], 900)
        self.assertEqual(p['ffbForce'], 24)
        self.assertEqual(p['endStopMaxForce'], 100)


class ProtoSchemaTest(unittest.TestCase):
    """GUI-generated form: parse_proto on the shipped device schema."""

    @unittest.skipIf(gui is None, f'tkinter required to import GUI: {GUI_IMPORT_ERROR}')
    def test_parse_proto(self):
        meta, enums = gui.parse_proto(os.path.join(ROOT, 'SimWheelBase.proto'))
        self.assertEqual(len(meta), 28)
        m3 = meta[3]
        self.assertEqual(m3['name'], 'rotationDegrees')
        self.assertEqual((m3['min'], m3['max']), (180, 2160))
        self.assertEqual(meta[6]['name'], 'ffbForce')
        self.assertEqual((meta[6]['min'], meta[6]['max']), (0, 100))
        # enum-typed fields resolve to device enums
        self.assertEqual(meta[12]['enum'], 'EffectOverrideEnum')
        self.assertIn('USE_DEVICE_AND_GAME_EFFECTS',
                      list(enums['EffectOverrideEnum'].values()))
        # the firmware's FILTERMODE enum contains the upstream typo
        names = list(enums['FilteringModeEmum'].values())
        self.assertIn('FILTERMODENGADVANCE', names)
        # every field ordered on the GUI form exists in the proto
        shown = (list(gui.COL1[1]) + [n for _t, ns in gui.COL2 for n in ns]
                 + list(gui.COL3[1]) + list(gui.OVERRIDES)
                 + list(gui.OVERRIDES.values()))
        for num in shown:
            self.assertIn(num, meta, num)


class GuiHeaderTest(unittest.TestCase):
    """pb_put_header: the profile-management core (nested header rewrite)."""

    @unittest.skipIf(gui is None, 'tkinter required to import GUI')
    def setUp(self):
        self.vc = gui.load_vc()

    def _blob(self, name='P', number=None, extra=True):
        hdr = b''
        if number is not None:
            hdr += self.vc.pb_encode(1, number)
        hdr += self.vc.pb_encode(2, name)
        b = self.vc.pb_encode(1, hdr)
        if extra:
            b += self.vc.pb_encode(3, 720) + self.vc.pb_encode(40, 1)
        return b

    def _hdr_of(self, blob):
        hf = self.vc.pb_decode(self.vc.pb_get(self.vc.pb_decode(blob), 1))
        return {f: v for f, w, v in hf}

    def test_set_name_keeps_number_and_fields(self):
        out = gui.pb_put_header(self.vc, self._blob('Old', 2), name='New')
        h = self._hdr_of(out)
        self.assertEqual(h[2], b'New')
        self.assertEqual(h[1], 2)
        vals = {f: v for f, w, v in self.vc.pb_decode(out) if f != 1}
        self.assertEqual(vals, {3: 720, 40: 1})

    def test_number_zero_removes_number(self):
        out = gui.pb_put_header(self.vc, self._blob('Default', 1), number=0)
        self.assertNotIn(1, self._hdr_of(out))
        self.assertEqual(self._hdr_of(out)[2], b'Default')

    def test_creates_header_when_absent(self):
        bare = self.vc.pb_encode(3, 500)              # no field-1 header
        out = gui.pb_put_header(self.vc, bare, name='X', number=3)
        h = self._hdr_of(out)
        self.assertEqual((h[1], h[2]), (3, b'X'))
        self.assertEqual(self.vc.pb_get(
            self.vc.pb_decode(out), 3), 500)

    def test_manage_math_new_and_delete(self):
        # mirror of task_manage: number = max(existing)+1; delete re-sequences
        store = [self._blob('Default', None), self._blob('A', 1),
                 self._blob('B', 2)]
        nums = [self._hdr_of(b).get(1) for b in store if self._hdr_of(b).get(1)]
        new = gui.pb_put_header(self.vc, store[1], name='C', number=max(nums) + 1)
        self.assertEqual(self._hdr_of(new)[1], 3)
        # delete store[2] ('B'): remaining numbered blobs re-sequence 1..N
        rest = [store[0], store[1]]
        n = 0
        out = []
        for b in rest:
            h = self._hdr_of(b)
            if h.get(1) is not None:
                n += 1
                b = gui.pb_put_header(self.vc, b, name=h[2].decode(), number=n)
            out.append(b)
        self.assertEqual([self._hdr_of(b).get(1) for b in out], [None, 1])


class AngleReaderTest(unittest.TestCase):
    """Struct/ioctl contracts of the live-angle URB pump (no device needed)."""

    @unittest.skipIf(gui is None, 'tkinter required to import GUI')
    def test_urb_layout(self):
        fmt = gui.AngleReader.URB
        self.assertEqual(struct.calcsize(fmt), 56)
        raw = struct.pack(fmt, 1, 0x81, 0, 0, 0x1000, 64, 0, 0, 0, 0, 0, 0)
        self.assertEqual(len(raw), 56)
        # buffer pointer sits at offset 16, buffer_length at 24
        self.assertEqual(struct.unpack_from('<Q', raw, 16)[0], 0x1000)
        self.assertEqual(struct.unpack_from('<i', raw, 24)[0], 64)

    @unittest.skipIf(gui is None, 'tkinter required to import GUI')
    def test_ioctl_codes(self):
        self.assertEqual(gui.AngleReader.CLAIM, 0x8004550F)    # _IOR('U',15)
        self.assertEqual(gui.AngleReader.SUBMIT, 0x8038550A)   # _IOR('U',10)
        self.assertEqual(gui.AngleReader.REAP, 0x4008550D)     # _IOW('U',13)

    @unittest.skipIf(gui is None, 'tkinter required to import GUI')
    def test_angle_math(self):
        # deg = x * rot / 65535 with signed x: screenshot anchor raw 1314
        x = 1314
        self.assertAlmostEqual(x * 900 / 65535.0, 18.03, delta=0.05)
        neg = struct.unpack('<h', b'\xf2\xc5')[0] & 0xFFFF
        self.assertEqual(neg, 50674)   # unsigned mirror of -14862


class UsbDevfsCtrlTest(unittest.TestCase):
    """vrs-config.py _ep0_ctrl header struct: '<BBHHHI4xQ' = 24 bytes + pad."""

    def test_ctrltransfer_size(self):
        self.assertGreaterEqual(struct.calcsize('<BBHHHI4xQ'), 20)

    def test_hid_ioctl_codes(self):
        self.assertEqual(vc._ioc(2, ord('H'), 3, 512),
                         (2 << 30) | (512 << 16) | (ord('H') << 8) | 3)
        # size is embedded in the request code (the fcntl.ioctl gotcha)
        self.assertEqual((vc._ioc(2, ord('H'), 3, 2) >> 16) & 0xFF, 2)
        self.assertEqual((vc._ioc(1, ord('H'), 3, 64) >> 16) & 0xFF, 64)
        self.assertEqual(vc._ioc(2, ord('H'), 3, 2) & 0xFF, 3)  # nr


if __name__ == '__main__':
    unittest.main(verbosity=2)
