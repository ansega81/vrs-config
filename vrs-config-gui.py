#!/usr/bin/env python3
"""
VRS DirectForce Pro - native Linux configurator GUI (Tkinter), styled after
the official VRS config tool (dark 3-column layout, grouped controls).

Protocol: device-verified against recorded official-tool traffic (see PROTOCOL.md).
Key rule: EVERY device write is a FULL-STORE
save burst  00 -> 16 -> 19 -> 03(header) -> 06 x ALL profiles -> 01.
There is no single-field write: profiles/settings "are updated only together".
Profile create = same burst with num+1 and an extra blob (copy of a template,
nested header {profile_number: n, name}); Default is the template and has NO
profile_number; numbered profiles carry sequential numbers 1..N. Never write
0x1a/0x14 (0x1a repacks the store destructively; disproof recorded in SPEC).

Requires: sudo apt install python3-tk ; run with sudo (raw USB unbind):
    xhost +local: ; sudo -E ./vrs-config-gui.py ; xhost -local:
Reads/writes use ./vrs-config.py's Session/open_session/pb_* helpers.
"""
import ctypes
import importlib.util
import json
import math
import os
import queue
import struct
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, 'vrs-gui.log')

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except Exception:
    tk = ttk = filedialog = messagebox = None


def log(msg):
    import datetime
    try:
        with open(LOG_PATH, 'a') as f:
            f.write(f"{datetime.datetime.now():%F %T} {msg}\n")
    except OSError:
        pass


def load_vc():
    spec = importlib.util.spec_from_file_location("vc", os.path.join(HERE, "vrs-config.py"))
    vc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vc)
    return vc


# ---------- proto schema (groups, labels, ranges) ----------
def parse_proto(path):
    fields, enums = {}, {}
    try:
        with open(path) as fh:
            text = fh.read()
    except OSError:
        return fields, enums
    # enums
    import re
    for m in re.finditer(r'enum\s+(\w+)\s*\{(.*?)\}', text, re.S):
        vals = {}
        for em in re.finditer(r'(\w+)\s*=\s*(\d+)\s*;', m.group(2)):
            vals[int(em.group(2))] = em.group(1)
        enums[m.group(1)] = vals
    m = re.search(r'message\s+SettingsProfile\s*\{(.*?)\n\}', text, re.S)
    if not m:
        return fields, enums
    body = m.group(1)
    for fm in re.finditer(r'^\s*(?:(?:optional|repeated|required)\s+)?(\w+)\s+(\w+)\s*=\s*(\d+)\s*(?:\[(.*?)\])?\s*;',
                          body, re.M | re.S):
        typ, name, num, opts = fm.group(1), fm.group(2), int(fm.group(3)), fm.group(4) or ''
        info = {'name': name, 'type': typ, 'min': None, 'max': None,
                'desc': '', 'enum': None}
        dm = re.search(r'description\)\s*=\s*"([^"]*)"', opts)
        if dm:
            info['desc'] = dm.group(1)
        om = re.search(r'int_options\)\s*.\s*min\s*=\s*(-?\d+)', opts)
        ma = re.search(r'int_options\)\s*.\s*max\s*=\s*(-?\d+)', opts)
        if om:
            info['min'] = int(om.group(1))
        if ma:
            info['max'] = int(ma.group(1))
        if typ in enums:
            info['enum'] = typ
        fields[num] = info
    return fields, enums


def nice(s):
    import re
    s = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', s)
    return s[:1].upper() + s[1:]


# label overrides to mirror the original tool's wording
FIELD_LABEL = {
    'rotationDegrees': 'Rotation degrees',
    'ffbForce': 'Max force',
    'staticForceReduction': 'Static force reduction',
    'nonLinearity': 'Non-linearity',
    'endStopMaxForce': 'End-stop max force',
    'endStopStiffness': 'End-stop stiffness',
    'isSoftEndStopEnabled': 'Soft end stop',
    'softEndStopDegrees': 'Soft end-stop degrees',
    'torqueUpperBound': 'Max torque (Nm)',
    'lowPassCutoffFreq': 'Low pass stop frequency (Hz)',
    'LowPassCutoffFreq': 'Low pass stop frequency (Hz)',
    'stepBandStartFreq': 'Step band start frequency (Hz)',
    'stepBandDamping': 'Step band damping (dB)',
    'predictionLevel': 'Prediction level',
    'predictionPeakDamp': 'Prediction peak damping',
    'motorSlewRate': 'Motor slew rate',
    'softwareSlewRate': 'Software slew rate',
    'slewRate': 'Slew rate (legacy)',
    'smoothing': 'Smoothing',
    'smoothingType': 'Smoothing type',
    'filterMode': 'FFB preset',
    'centerOffset': 'Center offset',
}
OVERRIDE_LABEL = {
    'USE_DEVICE_EFFECT_ONLY': 'Device only',
    'USE_GAME_EFFECT_ONLY': 'Game only',
    'USE_DEVICE_AND_GAME_EFFECTS': 'Device + game',
}
SMOOTHING_LABEL = {
    'SMOOTHINGNONE': 'None', 'LEGACY1': 'Legacy 1', 'LEGACY2': 'Legacy 2',
    'LEGACY3': 'Legacy 3', 'LEGACY4': 'Legacy 4',
    'RATEADAPTIVERESPONSIVEPLUS': 'Rate adaptive responsive+',
    'RATEADAPTIVERESPONSIVE': 'Rate adaptive responsive',
    'RATEADAPTIVEOPTIMAL': 'Rate adaptive optimal',
    'RATEADAPTIVESOFT': 'Rate adaptive soft',
    'RATEADAPTIVESOFTPLUS': 'Rate adaptive soft+',
    'RATEADAPTIVESOFTMAX': 'Rate adaptive soft max',
}
SLEW_LABEL = {
    'PERFORMANCE': 'Performance', 'BALANCED': 'Balanced', 'QUIET': 'Quiet',
    'PERFORMANCEPLUS': 'Performance+',
}
FILTER_LABEL = {
    'FILTERMODELEGACY': 'Legacy', 'FILTERMODENGBASIC': 'NextGen FFB - Basic',
    'FILTERMODENGADVANCE': 'NextGen FFB - Advanced',
}
# fallback ranges if the proto lacks int_options for a field
RANGE_FIX = {
    3: (180, 2160), 6: (0, 100), 7: (0, 10), 8: (0, 100), 9: (0, 100),
    10: (0, 100), 11: (0, 100), 17: (0, 70), 18: (0, 10), 20: (0, 100),
    21: (1, 5), 22: (180, 2160), 24: (0, 20), 31: (30, 100), 32: (1, 100),
    33: (0, 100), 34: (0, 100), 35: (5, 1000), 36: (0, 40), 37: (5, 1000),
}

# (title, field numbers) - mirrors the original tool's cards
COL1 = ("Steering & end stops", [3, 20, 21, 23, 22])
COL2 = [("General", [6, 17, 18]),
        ("Effects", [8, 9, 10, 11])]
# value+override pairs updated together
OVERRIDES = {8: 12, 9: 13, 10: 14, 11: 15}
COL3 = ("Force feedback shaping", [30, 31, 32, 33, 34, 37, 35, 36, 7, 16, 19])
LEGACY_ONLY = {7, 16, 19}          # active when filterMode == Legacy
NG_ONLY = {31, 32, 33, 34, 37, 35, 36}   # active on NextGen presets


def pb_put_header(vc, blob, name=None, number=None):
    """Replace/add the nested profile header (field 1 = {1:number, 2:name}).
    number=None leaves an existing number; number=0 REMOVES it (Default)."""
    fields = list(vc.pb_decode(blob))
    hdr = None
    hi = None
    for i, (f, w, v) in enumerate(fields):
        if f == 1 and w == 2:
            hdr = list(vc.pb_decode(v))
            hi = i
            break
    if hdr is None:
        hdr = []
    def _set(hf, val, is_bytes):
        for j, (f, w, v) in enumerate(hdr):
            if f == hf:
                hdr[j] = (hf, 0 if not is_bytes else 2, val)
                return
        hdr.append((hf, 0 if not is_bytes else 2, val))
    if name is not None:
        _set(2, name.encode(), True)
    if number is not None:
        if number == 0:
            hdr = [(f, w, v) for (f, w, v) in hdr if f != 1]
        else:
            _set(1, number, False)
    hdr_bytes = b''.join(
        vc.pb_varint((f << 3) | (0 if w == 0 else 2)) +
        (vc.pb_varint(v) if w == 0 else bytes([len(v)]) + v)
        for (f, w, v) in hdr)
    out = list(fields)
    entry = (1, 2, hdr_bytes)
    if hi is not None:
        out[hi] = entry
    else:
        out.insert(0, entry)
    return b''.join(
        vc.pb_varint((f << 3) | (0 if w == 0 else 2)) +
        (vc.pb_varint(v) if w == 0 else bytes([len(v)]) + v)
        for (f, w, v) in out)


class _LogTee:
    def __init__(self):
        self.buf = []

    def write(self, s):
        if s.strip():
            log(s.rstrip())
        self.buf.append(s)

    def flush(self):
        pass


class AngleReader(threading.Thread):
    """Live steering angle from the base's 1 kHz joystick stream (interrupt EP 0x81).

    Needs the kernel HID driver unbound + the interface claimed. It shares the
    interface with the config sessions: GUI worker tasks set park_ev, the reader
    closes its fd (releasing claim + killing URBs) and sets parked. EBUSY at
    startup (driver still bound) is normal - it self-heals once the first task
    runs as root and unbinds usbhid.
    """
    URB = '<BB2xii4xQiiiiiIQ'          # usbdevfs_urb, 56 bytes
    SUBMIT, REAP, CLAIM = 0x8038550A, 0x4008550D, 0x8004550F
    N_URBS = 4
    _libc = ctypes.CDLL(None, use_errno=True)     # raw ioctl: fcntl rejects REAPURBNDELAY's _IOW encoding

    def __init__(self, vc):
        super().__init__(daemon=True)
        self.vc = vc
        self.x, self.ts = None, 0.0
        self.park_ev, self.parked = threading.Event(), threading.Event()
        self.last_err = None

    def get_x(self):
        if self.x is None or time.monotonic() - self.ts > 0.8:
            return None
        return self.x

    def _unbind(self, udir):
        import glob
        iface = os.path.basename(udir) + ':1.0'
        for drv in glob.glob('/sys/bus/usb/drivers/*/' + iface):
            try:
                with open(os.path.join(os.path.dirname(drv), 'unbind'), 'w') as f:
                    f.write(iface)
                return True
            except OSError:
                pass
        return False

    def _node(self):
        nodes = list(self.vc._usb_nodes())
        if not nodes:
            raise FileNotFoundError('no 0483:a355 on the bus')
        self._unbind(nodes[0][1])       # needs root, like the config sessions
        fd = os.open(nodes[0][0], os.O_RDWR)
        arg = ctypes.c_uint(0)
        if self._libc.ioctl(fd, self.CLAIM, ctypes.byref(arg)) != 0:
            os.close(fd)
            raise OSError(ctypes.get_errno(), 'claim interface')
        return fd

    def run(self):
        while True:
            if self.park_ev.is_set():
                self.parked.set()
                time.sleep(0.03)
                continue
            self.parked.clear()
            fd, urbmap = None, {}
            try:
                fd = self._node()
                bufmap = {}
                for _ in range(self.N_URBS):
                    buf = ctypes.create_string_buffer(64)
                    ub = ctypes.create_string_buffer(56)
                    ctypes.memmove(ub, struct.pack(
                        self.URB, 1, 0x81, 0, 0, ctypes.addressof(buf),
                        64, 0, 0, 0, 0, 0, 0), 56)
                    bufmap[ctypes.addressof(buf)] = buf
                    urbmap[ctypes.addressof(ub)] = ub
                    if self._libc.ioctl(fd, self.SUBMIT, ub) != 0:
                        raise OSError(ctypes.get_errno(), 'submiturb')
                self.last_err = None
                while not self.park_ev.is_set():
                    slot = ctypes.create_string_buffer(8)
                    r = self._libc.ioctl(fd, self.REAP, slot)
                    if r != 0:
                        en = ctypes.get_errno()
                        if en == 11:            # EAGAIN
                            time.sleep(0.004)
                            continue
                        raise OSError(en, 'reapurb')
                    ubaddr = struct.unpack('<Q', bytes(slot[:8]))[0]
                    ub = urbmap.get(ubaddr)
                    if ub is None:
                        continue
                    f = struct.unpack(self.URB, ub.raw)
                    status, addr, alen = f[2], f[4], f[6]
                    if status == 0 and alen >= 4:
                        b = bufmap.get(addr)
                        if b is not None and b.raw[0] == 0x01:
                            self.x = struct.unpack_from('<h', b.raw, 1)[0]
                            self.ts = time.monotonic()
                    ctypes.memmove(ub, struct.pack(
                        self.URB, 1, 0x81, 0, 0, addr, 64,
                        0, 0, 0, 0, 0, 0), 56)
                    if self._libc.ioctl(fd, self.SUBMIT, ub) != 0:
                        raise OSError(ctypes.get_errno(), 'resubmit')
            except Exception as e:                # EBUSY/ENODEV/replug
                self.last_err = e
                self.x = None
                time.sleep(0.4)
            finally:
                if fd is not None:
                    try:
                        os.close(fd)              # releases claim + kills URBs
                    except OSError:
                        pass


class GUI(ttk.Frame if tk else object):
    def __init__(self, root):
        if tk is None:
            sys.exit("tkinter missing: sudo apt install python3-tk")
        super().__init__(root)
        self.vc = load_vc()
        self.pack(fill='both', expand=True)
        self.root = root
        self.q = queue.Queue()
        self.fields_meta, self.enums = parse_proto(os.path.join(HERE, 'SimWheelBase.proto'))
        self.profs = []
        self.hdr = {}
        self.versions = {}
        self.selected = 0
        self.busy_depth = 0
        self.edits = {}       # per-profile {fnum:int}
        self.names = {}       # per-profile name (str)
        self.rows = {}        # fnum -> widgets dict
        self._build()
        self.after(120, self._drain)
        self.async_(self.task_read)

    # ---------- look ----------
    def _style(self):
        s = ttk.Style(self)
        try:
            s.theme_use('clam')
        except tk.TclError:
            pass
        BG, PANEL, EDGE, TXT, MUT = '#1b1b1e', '#26262b', '#3a3a41', '#e8e8ea', '#9a9aa2'
        ACC, WARN = '#ff8c1a', '#d4d43a'
        self.root.configure(bg=BG)  # GUI is a ttk.Frame - bg comes from the TFrame style
        for k, v in (('TFrame', {'background': BG}), ('Panel.TFrame', {'background': PANEL}),
                     ('TLabel', {'background': BG, 'foreground': TXT}),
                     ('Panel.TLabel', {'background': PANEL, 'foreground': TXT}),
                     ('Mut.TLabel', {'background': PANEL, 'foreground': MUT}),
                     ('Title.TLabel', {'background': BG, 'foreground': TXT, 'font': ('Segoe UI', 13, 'bold')}),
                     ('Group.TLabel', {'background': PANEL, 'foreground': ACC, 'font': ('Segoe UI', 11, 'bold')}),
                     ('Accent.TButton', {'foreground': '#0d0d0d', 'background': ACC, 'font': ('Segoe UI', 10, 'bold')}),
                     ('TButton', {'background': '#333338', 'foreground': TXT}),
                     ('TLabelframe', {'background': PANEL, 'foreground': TXT}),
                     ('TLabelframe.Label', {'background': PANEL, 'foreground': ACC, 'font': ('Segoe UI', 10, 'bold')}),
                     ('TCheckbutton', {'background': PANEL, 'foreground': TXT}),
                     ('TNotebook', {'background': BG}),
                     ('Treeview', {'background': PANEL, 'fieldbackground': PANEL,
                                   'foreground': TXT}),
                     ):
            try:
                s.configure(k, **v)
            except tk.TclError:
                pass
        try:
            s.configure('Horizontal.TScale', background=PANEL, troughcolor='#111114',
                        sliderthickness=14)
            s.map('Horizontal.TScale', background=[('active', PANEL)])
            s.configure('TCombobox', fieldbackground='#17171a', background='#333338',
                        foreground=TXT, arrowcolor=TXT, selectbackground='#333338',
                        selectforeground=TXT)
            s.map('TCombobox', fieldbackground=[('readonly', '#17171a')])
            s.configure('TEntry', fieldbackground='#17171a', foreground=TXT,
                        insertcolor=TXT)
        except tk.TclError:
            pass
        self.C = dict(BG=BG, PANEL=PANEL, EDGE=EDGE, TXT=TXT, MUT=MUT, ACC=ACC, WARN=WARN)
        try:
            self.option_add('*TCombobox*Listbox.background', '#26262b')
            self.option_add('*TCombobox*Listbox.foreground', TXT)
            self.option_add('*TCombobox*Listbox.selectBackground', ACC)
            self.option_add('*TCombobox*Listbox.selectForeground', '#0d0d0d')
        except Exception:
            pass

    def _build(self):
        self._style()
        C = self.C
        top = ttk.Frame(self)
        top.pack(fill='x', padx=10, pady=(8, 2))
        ttk.Label(top, text='VRS DirectForce Configuration Tool',
                  style='Title.TLabel').pack(side='left')
        self.dev_lbl = ttk.Label(top, text='device: -', style='Mut.TLabel')
        self.dev_lbl.pack(side='right')
        ttk.Button(top, text='Recover (USB reset)', command=self.on_recovery).pack(side='right', padx=6)
        ttk.Button(top, text='Read base', command=self.on_refresh).pack(side='right', padx=6)

        bar = ttk.Frame(self)
        bar.pack(fill='x', padx=10, pady=(4, 4))
        ttk.Label(bar, text='Profile', style='Mut.TLabel').pack(side='left')
        self.prof_cb = ttk.Combobox(bar, state='readonly', width=24)
        self.prof_cb.pack(side='left', padx=6)
        self.prof_cb.bind('<<ComboboxSelected>>', self.on_select_cb)
        self.slot_lbl = ttk.Label(bar, text='', style='Mut.TLabel')
        self.slot_lbl.pack(side='left', padx=4)
        for text, fn in (('New', self.on_new), ('Duplicate', self.on_dup), ('Delete', self.on_del)):
            ttk.Button(bar, text=text, command=fn).pack(side='left', padx=3)
        ttk.Button(bar, text='Set active', command=self.on_set_current).pack(side='left', padx=3)
        ttk.Button(bar, text='Export profile', command=self.on_export).pack(side='left', padx=3)
        ttk.Button(bar, text='Import over profile', command=self.on_import).pack(side='left', padx=3)
        self.status = tk.Label(self, text='', anchor='w', bg='#111114', fg=C['TXT'],
                               font=('Consolas', 9))
        self.status.pack(fill='x', side='bottom', padx=10, pady=(4, 8))

        # action bar: docked at the bottom, always visible (the toolbar can clip
        # right-packed buttons on narrow windows - that's why Save 'disappeared')
        abar = ttk.Frame(self)
        abar.pack(fill='x', side='bottom', padx=10, pady=(2, 2))
        self.apply_btn = tk.Button(abar, text='SAVE TO DEVICE', bg=C['ACC'],
                                   fg='#111111', activebackground='#ffa040',
                                   relief='flat', font=('Segoe UI', 11, 'bold'),
                                   padx=22, pady=4, command=self.on_apply)
        self.apply_btn.pack(side='right')
        ttk.Button(abar, text='Discard changes', command=self.on_discard).pack(side='right', padx=8)

        body = ttk.Frame(self)
        body.pack(fill='both', expand=True, padx=10, pady=2)
        body.columnconfigure((0, 1, 2), weight=1, uniform='col')
        self.col_frames = []
        for i in range(3):
            f = ttk.Frame(body, style='Panel.TFrame')
            f.grid(row=0, column=i, sticky='nsew', padx=(0 if i == 0 else 6, 0))
            self.col_frames.append(f)

        # live steering-angle gauge, pinned to the top of the left column
        # (skipped by build_form's destroy loop; rebuilt content packs below it)
        self.gauge_host = tk.Frame(self.col_frames[0], bg=C['PANEL'], padx=6, pady=4)
        self.gauge_host.pack(fill='x', pady=(6, 2))
        self.gauge = tk.Canvas(self.gauge_host, width=280, height=170,
                               bg='#111114', highlightthickness=0)
        self.gauge.pack(fill='x')
        self._draw_gauge(None)
        self.reader = AngleReader(self.vc)
        self.reader.start()
        self.after(80, self._tick)

    def _tick(self):
        try:
            if self.gauge.winfo_exists():
                rot = 900
                p = self.profs[self.selected] if self.profs else None
                if p:
                    rot = self.edits.get(self.selected, {}).get(
                        3, p['values'].get(3, 900)) or 900
                self._draw_gauge(self.reader.get_x(), rot)
        except Exception:
            pass
        self.after(80, self._tick)

    def _draw_gauge(self, x, rot=900):
        g, C = self.gauge, self.C
        g.delete('all')
        w = max(240, g.winfo_width())
        cx, cy, r = w // 2, 56, 40
        half = rot / 2.0
        deg = None if x is None else x * rot / 65535.0
        # wheel
        g.create_oval(cx - r, cy - r, cx + r, cy + r, outline='#5a5a64', width=3)
        g.create_oval(cx - 7, cy - 7, cx + 7, cy + 7, fill='#2c2c33', outline='#5a5a64')
        a0 = math.radians(deg if deg is not None else 0)
        for off in (120, 240):                       # three fixed spokes
            a = a0 + math.radians(off)
            g.create_line(cx, cy, cx + (r - 3) * math.sin(a),
                          cy - (r - 3) * math.cos(a), fill='#4a4a55', width=2)
        if deg is not None:                          # marker spoke = live angle
            g.create_line(cx, cy, cx + (r - 3) * math.sin(a0),
                          cy - (r - 3) * math.cos(a0), fill=C['ACC'], width=3)
        # segmented bar -half..+half (labels pinned to canvas edges so they fit)
        x0, x1, y = 58, w - 58, 118
        n = 12
        seg = (x1 - x0) / n
        frac = 0.5 if deg is None else min(1.0, max(0.0, (deg + half) / rot))
        for i in range(n):
            g.create_rectangle(x0 + i * seg + 1, y, x0 + (i + 1) * seg - 1, y + 10,
                               fill='#26262b', outline='#111114')
        g.create_oval(x0 + frac * (x1 - x0) - 5, y + 5 - 5,
                      x0 + frac * (x1 - x0) + 5, y + 5 + 5,
                      fill=(C['ACC'] if deg is not None else '#555'), outline='')
        g.create_text(6, y + 5, text=f'-{half:.0f}\u00b0', anchor='w',
                      fill=C['MUT'], font=('Consolas', 8))
        g.create_text(w - 6, y + 5, text=f'+{half:.0f}\u00b0', anchor='e',
                      fill=C['MUT'], font=('Consolas', 8))
        g.create_text(cx, y + 28,
                      text=('--' if deg is None else f'{deg:.2f}\u00b0'),
                      fill=C['TXT'], font=('Consolas', 13, 'bold'))
        g.create_text(cx, y + 46, text='' if deg is None else f'raw {x}',
                      fill=C['MUT'], font=('Consolas', 8))

    def _busy(self, msg):
        self.status.config(text=msg)

    def _field_card(self, parent, title):
        lf = ttk.LabelFrame(parent, text=title)
        lf.pack(fill='x', padx=8, pady=6)
        return lf

    # ---------- form ----------
    def build_form(self):
        for f in self.col_frames:
            for c in f.winfo_children():
                if c is getattr(self, 'gauge_host', None):
                    continue
                c.destroy()
        self.rows = {}
        p = self.profs[self.selected] if self.profs else None
        vals = (p['values'] if p else {})
        edits = self.edits.get(self.selected, {})

        def getv(num):
            if num in edits:
                return edits[num]
            return vals.get(num, self.fields_meta.get(num, {}).get('default', 0) or 0)

        # col1: steering & end stops (+ preset quick buttons)
        f1 = self.col_frames[0]
        card = self._field_card(f1, COL1[0])
        self._row(card, 3, getv(3), extra_buttons=[360, 540, 720, 900, 1080])
        self._row(card, 20, getv(20))
        self._row(card, 21, getv(21))
        self._row(card, 23, getv(23))
        self._row(card, 22, getv(22))
        # col2: General + Effects (value+override pairs)
        f2 = self.col_frames[1]
        card = self._field_card(f2, COL2[0][0])
        for num in COL2[0][1]:
            self._row(card, num, getv(num))
        self._add_nm_readout(getv(24))
        card = self._field_card(f2, COL2[1][0])
        for num in COL2[1][1]:
            self._row(card, num, getv(num),
                      inline_override=(OVERRIDES[num], getv(OVERRIDES[num]))
                      if num in OVERRIDES else None)
        # col3: FFB shaping
        f3 = self.col_frames[2]
        card = self._field_card(f3, COL3[0])
        for num in COL3[1]:
            self._row(card, num, getv(num))
        self._update_mode_state()

    def _meta(self, num):
        m = self.fields_meta.get(num, {})
        lo, hi = m.get('min'), m.get('max')
        if m.get('type') == 'bool':
            lo, hi = 0, 1
        if lo is None or hi is None:
            fl = RANGE_FIX.get(num)
            lo = fl[0] if fl and lo is None else (lo if lo is not None else 0)
            hi = fl[1] if fl and hi is None else (hi if hi is not None else 100)
        return {'name': m.get('name', f'field{num}'), 'lo': lo, 'hi': hi,
                'desc': m.get('desc', ''), 'enum': m.get('enum')}

    def _label_for(self, num):
        m = self._meta(num)
        return FIELD_LABEL.get(m['name'], nice(m['name']))

    def _add_nm_readout(self, cap):
        """Max torque is DISPLAY-ONLY (like VRS One): Nm = cap x max-force%.

        cap is the stored torqueUpperBound (field 24 - no longer editable);
        at Max force 100% the readout equals the stored value.
        """
        r = self.rows.get(6)
        if not r:
            return
        try:
            cap = int(cap)
        except (TypeError, ValueError):
            cap = 15
        rowf = r['entry'].master
        lbl = tk.Label(rowf, text='', bg=self.C['PANEL'], fg=self.C['MUT'],
                       font=('Consolas', 9, 'bold'))
        lbl.grid(row=0, column=3, sticky='e', padx=(8, 2))

        def upd(*_a, v=r['var']):
            try:
                ffb = max(0, min(100, int(v.get())))
            except (tk.TclError, ValueError):
                return
            lbl.config(text=f'Nm: {cap * ffb / 100.0:.1f}')
        r['var'].trace_add('write', upd)
        upd()

    def _row(self, card, num, cur, sub=False, extra_buttons=None, inline_override=None):
        C = self.C
        m = self._meta(num)
        row = ttk.Frame(card, style='Panel.TFrame')
        row.pack(fill='x', padx=6, pady=(3 if not sub else 0, 2))
        lbl = ttk.Label(row, text=('    └ ' if sub else '') + self._label_for(num) +
                        ('  (override)' if sub else ''), style='Panel.TLabel')
        lbl.grid(row=0, column=0, sticky='w')
        e = tk.Entry(row, width=7, bg='#17171a', fg=C['TXT'], insertbackground=C['TXT'],
                     justify='right', relief='flat', font=('Consolas', 11))
        e.grid(row=0, column=2, sticky='e', padx=(6, 0))
        var = tk.IntVar(value=int(cur) if isinstance(cur, (int, float)) else 0)
        e.config(textvariable=var)

        if m['enum'] and self.enums.get(m['enum']):
            names = self.enums[m['enum']]
            lab = {'smoothingType': SMOOTHING_LABEL, 'slewRate': SLEW_LABEL,
                   'filterMode': FILTER_LABEL}.get(m['name'], OVERRIDE_LABEL if 'Override' in m['enum'] else {})
            pairs = sorted((k, lab.get(names[k], nice(names[k]))) for k in names)
            cb = ttk.Combobox(row, state='readonly', width=16,
                              values=[p[1] for p in pairs])
            try:
                cb.current([k for k, _ in pairs].index(var.get()))
            except (ValueError, tk.TclError):
                pass
            cb.grid(row=0, column=3, sticky='e', padx=(4, 0))
            def cb_sync(*_a, p=pairs, v=var, c=cb):
                i = c.current()
                if 0 <= i < len(p):
                    v.set(p[i][0])
                    self._edit(num, p[i][0])
            cb.bind('<<ComboboxSelected>>', cb_sync)
            entry = e
            def en_sync(*_a, v=var, c=cb, p=pairs):
                try:
                    n = int(v.get())
                except (tk.TclError, ValueError):
                    return
                keys = [k for k, _ in p]
                if n in keys:
                    try:
                        c.current(keys.index(n))
                    except tk.TclError:
                        pass
                self._edit(num, n)
            var.trace_add('write', en_sync)
            e.config(validate='key', validatecommand=(self.register(lambda s: s == '' or s.lstrip('-').isdigit()), '%P'))
            sc = None
        else:
            lo, hi = m['lo'], m['hi']

            def sc_cmd(val, v=var, l=lo, h=hi):
                # ttk.Scale writes raw floats into the variable - snap back to int
                try:
                    n = int(round(float(val)))
                except ValueError:
                    return
                v.set(max(l, min(h, n)))
            sc = ttk.Scale(row, from_=lo, to=hi, orient='horizontal', variable=var,
                           command=sc_cmd, length=130)
            sc.grid(row=0, column=1, sticky='ew', padx=8)
            row.columnconfigure(1, weight=1)
            def sc_sync(*_a, v=var, l=lo, h=hi):
                try:
                    n = int(round(v.get()))
                except (tk.TclError, ValueError):
                    return
                n = max(l, min(h, n))
                self._edit(num, n)
            var.trace_add('write', sc_sync)
            e.bind('<Return>', lambda _e, v=var, l=lo, h=hi: self._commit_entry(num, v, l, h))
            e.bind('<FocusOut>', lambda _e, v=var, l=lo, h=hi: self._commit_entry(num, v, l, h))
            cb = None

        if inline_override:
            ov_num, ov_cur = inline_override
            names = self.enums.get('EffectOverrideEnum', {})
            opairs = sorted((k, OVERRIDE_LABEL.get(names[k], nice(names[k])))
                            for k in names)
            ovar = tk.IntVar(value=int(ov_cur or 0))
            ocb = ttk.Combobox(row, state='readonly', width=14,
                               values=[p[1] for p in opairs])
            try:
                ocb.current([k for k, _ in opairs].index(ovar.get()))
            except (ValueError, tk.TclError):
                pass
            ocb.grid(row=0, column=3, sticky='e', padx=(4, 0))

            def ocb_sync(*_a, p=opairs, v=ovar, c=ocb, n=ov_num):
                i = c.current()
                if 0 <= i < len(p):
                    v.set(p[i][0])
                    self._edit(n, p[i][0])
            ocb.bind('<<ComboboxSelected>>', ocb_sync)
            self.rows[ov_num] = {'var': ovar, 'entry': ocb, 'scale': None,
                                 'cb': ocb, 'num': ov_num}

        if m['desc']:
            hl = tk.Label(row, text='ⓘ', bg=C['PANEL'], fg=C['MUT'],
                          font=('Segoe UI', 9))
            hl.grid(row=0, column=4, padx=(4, 0))
            hl.bind('<Button-1>', lambda _e, d=m['desc'],
                    n=self._label_for(num): self._busy(f"{n}: {d}"))
        r = {'var': var, 'entry': e, 'scale': sc, 'cb': cb, 'num': num}
        self.rows[num] = r

        if extra_buttons:
            bf = ttk.Frame(card, style='Panel.TFrame')
            bf.pack(fill='x', padx=6, pady=(0, 4))
            for v in extra_buttons:
                b = tk.Button(bf, text=f'{v}°', width=5, relief='flat',
                              bg='#333338', fg=C['TXT'], activebackground=C['ACC'],
                              activeforeground='#0d0d0d', font=('Segoe UI', 9),
                              command=lambda vv=v: self._quick_set(num, vv))
                b.pack(side='left', padx=2)

    def _quick_set(self, num, val):
        r = self.rows.get(num)
        if r:
            r['var'].set(val)

    def _commit_entry(self, num, var, lo, hi):
        try:
            n = max(lo, min(hi, int(var.get())))
        except (tk.TclError, ValueError):
            return
        var.set(n)
        self._edit(num, n)

    def _edit(self, num, val):
        if not self.profs:
            return
        self.edits.setdefault(self.selected, {})[num] = int(val)
        if num == 30 or self.fields_meta.get(num, {}).get('name') == 'filterMode':
            self._update_mode_state()
        nchanged = sum(len(v) for v in self.edits.values())
        self.slot_lbl.config(text=f'{len(self.profs)} of {self.hdr.get(3, 100)} slots'
                             + (f'   ·   {nchanged} unsaved change(s)' if nchanged else ''))

    def _update_mode_state(self):
        fm = None
        r = self.rows.get(30)
        if r:
            fm = r['var'].get()
        elif self.profs:
            fm = self.profs[self.selected]['values'].get(30)
        legacy = (fm == 0)
        for num in LEGACY_ONLY | NG_ONLY:
            rr = self.rows.get(num)
            if not rr:
                continue
            on = (num in LEGACY_ONLY and legacy) or (num in NG_ONLY and not legacy) or fm is None
            st = 'normal' if on else 'disabled'
            for w in (rr['entry'], rr['scale'], rr['cb']):
                if w is not None:
                    try:
                        w.config(state=st if w is not rr['scale'] else
                                 (st == 'normal' and 'normal' or 'disabled'))
                    except tk.TclError:
                        pass

    def build_cards(self):
        names = []
        for i, p in enumerate(self.profs):
            star = '★ ' if self.hdr.get(2) == p.get('slot', i + 1) else ''
            nm = self.names.get(i, p['name']) or f'(unnamed {i + 1})'
            names.append(f'{star}{i + 1}. {nm}')
        try:
            self.prof_cb.config(values=names)
            if self.profs:
                self.prof_cb.current(self.selected)
        except tk.TclError:
            pass
        self.slot_lbl.config(text=f'{len(self.profs)} of {self.hdr.get(3, 100)} slots')

    # ---------- workers ----------
    def async_(self, fn, *a):
        self.busy_depth += 1
        t = threading.Thread(target=self._wrap, args=(fn, a), daemon=True)
        t.start()

    def _wrap(self, fn, a):
        tee = _LogTee()
        old = sys.stdout
        sys.stdout = tee
        try:
            # hand the claimed interface over to the config session
            self.reader.park_ev.set()
            self.reader.parked.wait(3.0)
            self.reader.parked.clear()
            r = fn(*a)
            log(f'OK {getattr(fn, "__name__", fn)} {a} -> {str(r)[:400]}')
            self.q.put(('done', r))
        except BaseException as e:
            log(f'FAIL {getattr(fn, "__name__", fn)} {a}\n{traceback.format_exc()}')
            self.q.put(('err', f'{type(e).__name__}: {e}'))
        finally:
            sys.stdout = old
            self.reader.park_ev.clear()   # angle stream resumes on its own

    def _drain(self):
        try:
            while True:
                k, payload = self.q.get_nowait()
                self.busy_depth = max(0, self.busy_depth - 1)
                if k == 'err':
                    self.status.config(text='ERROR: ' + payload)
                    self.apply_btn.config(state='normal')
                else:
                    try:
                        self.on_done(payload)
                    except Exception:
                        traceback.print_exc()
                        self.status.config(text='ERROR (UI): ' + traceback.format_exc(limit=1).strip())
        except queue.Empty:
            pass
        self.after(120, self._drain)

    # ---------- tasks ----------
    def task_read(self):
        dev, s = self.vc.open_session(True, raw=True)
        try:
            s.new_operation()
            pv = s.exchange(0x07)['payload']
            self.versions = self.vc.decode_versions(pv) if pv else {}
            ph = s.exchange(0x02)['payload']
            hdr = {f: v for f, w, v in self.vc.pb_decode(ph)}
            s.new_operation()
            p15 = s.exchange(0x15)['payload']
            p17 = s.exchange(0x17)['payload']
            num = hdr.get(1, 1) or 1
            profs = []
            for i in range(num):
                pp = s.exchange(0x04, self.vc.pb_encode(1, i + 1))['payload']
                vals = {f: v for f, w, v in self.vc.pb_decode(pp) if w == 0}
                nm = ''
                number = None
                for f, w, v in self.vc.pb_decode(pp):
                    if f == 1 and w == 2:
                        for hf, hw, hv in self.vc.pb_decode(v):
                            if hf == 2 and hw == 2:
                                nm = hv.decode('utf-8', 'replace')
                            if hf == 1 and hw == 0:
                                number = hv
                profs.append({'slot': i + 1, 'blob': pp, 'values': vals,
                              'name': nm, 'number': number})
            s.exchange(0x01)
            return {'versions': self.versions, 'hdr': hdr, 'profs': profs,
                    'p15': p15, 'p17': p17}
        finally:
            dev.close()

    def _build_blob(self, idx):
        """live blob + pending edits + name -> final blob bytes"""
        p = self.profs[idx]
        blob = p['blob']
        edits = self.edits.get(idx)
        if edits:
            blob = self.vc.pb_reencode(list(self.vc.pb_decode(p['blob'])), edits)
        nm = self.names.get(idx, p['name'])
        if nm != p['name'] or True:
            number = p['number']
            blob = pb_put_header(self.vc, blob, name=nm,
                                 number=number if number is not None else None)
        return blob

    def _save_burst(self, dev, s, hdr_f02, p15, p17, blobs, new_num=None, new_cur=None):
        f02 = hdr_f02
        upd = {}
        if new_num is not None:
            upd[1] = new_num
        if new_cur is not None:
            upd[2] = new_cur
        if upd:
            f02 = self.vc.pb_reencode(list(self.vc.pb_decode(hdr_f02)), upd)
        s.new_operation()
        s.exchange(0x00)
        for ch, pay in ((0x16, p15), (0x19, p17), (0x03, f02)):
            r = s.exchange(ch, pay)
            if not r['crc_ok']:
                raise RuntimeError(f'bad crc on ch {ch:#04x} reply')
        for b in blobs:
            r = s.exchange(0x06, b)
            if not r['crc_ok']:
                raise RuntimeError('bad crc on profile write reply')
        return True

    def task_save(self):
        dev, s = self.vc.open_session(True, raw=True)
        try:
            s.new_operation()
            ph = s.exchange(0x02)['payload']
            p15 = s.exchange(0x15)['payload']
            p17 = s.exchange(0x17)['payload']
            hdr_f = {f: v for f, w, v in self.vc.pb_decode(ph)}
            num = hdr_f.get(1, len(self.profs))
            fresh = []
            for i in range(num):
                fresh.append(s.exchange(0x04, self.vc.pb_encode(1, i + 1))['payload'])
            s.exchange(0x01)
            blobs = []
            for i in range(len(fresh)):
                if i == self.selected:
                    blobs.append(self._build_blob(i))
                else:
                    blobs.append(fresh[i])
            return self._save_burst(dev, s, ph, p15, p17, blobs)
        finally:
            dev.close()

    def task_manage(self, op, name=None):
        """New / Duplicate / Delete = full-store save burst (device-verified:
        creation is num+1 plus one extra blob with sequential profile_number;
        deletion is the same burst minus the blob, with numbers re-sequenced).
        New/Duplicate copy the currently selected profile's values."""
        dev, s = self.vc.open_session(True, raw=True)
        try:
            s.new_operation()
            ph = s.exchange(0x02)['payload']
            p15 = s.exchange(0x15)['payload']
            p17 = s.exchange(0x17)['payload']
            hdr_f = {f: v for f, w, v in self.vc.pb_decode(ph)}
            num = hdr_f.get(1, 1)
            blobs, nums, names = [], [], []
            for i in range(num):
                b = s.exchange(0x04, self.vc.pb_encode(1, i + 1))['payload']
                blobs.append(b)
                number = None
                nm = ''
                for f, w, v in self.vc.pb_decode(b):
                    if f == 1 and w == 2:
                        for hf, hw, hv in self.vc.pb_decode(v):
                            if hf == 1 and hw == 0:
                                number = hv
                            if hf == 2 and hw == 2:
                                nm = hv.decode('utf-8', 'replace')
                nums.append(number)
                names.append(nm)
            s.exchange(0x01)

            idx = self.selected
            if op in ('new', 'dup'):
                if len(blobs) >= hdr_f.get(3, 100):
                    raise RuntimeError('profile store full')
                template = blobs[idx] if blobs else b''
                newnum = (max([n for n in nums if n] or [0]) + 1)
                nb = pb_put_header(self.vc, template, name=name or 'Profile', number=newnum)
                blobs.append(nb)
                new_count = len(blobs)
            elif op == 'del':
                if len(blobs) <= 1:
                    raise RuntimeError('cannot delete the last profile')
                if idx >= len(blobs):
                    raise RuntimeError('selection out of range')
                del blobs[idx]
                n = 0
                for i, b in enumerate(blobs):
                    if nums[i] is not None and nums[i] == 0:
                        pass
                    if nums[i] is not None:
                        n += 1
                        blobs[i] = pb_put_header(self.vc, blobs[i], name=names[i], number=n)
                    else:
                        blobs[i] = pb_put_header(self.vc, blobs[i], name=names[i])
                new_count = len(blobs)
            else:
                raise RuntimeError(f'unknown op {op}')

            self._save_burst(dev, s, ph, p15, p17, blobs, new_num=new_count)
            return ('manage', op, new_count)
        finally:
            dev.close()

    def task_set_current(self):
        dev, s = self.vc.open_session(True, raw=True)
        try:
            s.new_operation()
            ph = s.exchange(0x02)['payload']
            hdr_f = {f: v for f, w, v in self.vc.pb_decode(ph)}
            num = hdr_f.get(1, 1)
            p15 = s.exchange(0x15)['payload']
            p17 = s.exchange(0x17)['payload']
            blobs = [s.exchange(0x04, self.vc.pb_encode(1, i + 1))['payload'] for i in range(num)]
            s.exchange(0x01)
            self._save_burst(dev, s, ph, p15, p17, blobs, new_cur=self.selected + 1)
            return ('current', self.selected)
        finally:
            dev.close()

    def task_export(self):
        blob = self._build_blob(self.selected)
        path = filedialog.asksaveasfilename(defaultextension='.json',
                                            filetypes=[('JSON', '*.json')]) if filedialog else None
        if not path:
            return ('msg', 'export cancelled')
        vals = {self._label_for(f): v for f, v in
                sorted({f: v for f, w, v in self.vc.pb_decode(blob) if w == 0}.items())}
        json.dump({'name': self.names.get(self.selected, self.profs[self.selected]['name']),
                   'blob_hex': blob.hex(), 'values': vals}, open(path, 'w'), indent=1)
        return ('msg', f'exported to {path}')

    def task_import(self, path):
        d = json.load(open(path))
        blob = bytes.fromhex(d['blob_hex'])
        nm = d.get('name', '')
        idx = self.selected
        new = pb_put_header(self.vc, blob,
                            name=self.names.get(idx, self.profs[idx]['name']) or nm)
        dev, s = self.vc.open_session(True, raw=True)
        try:
            s.new_operation()
            ph = s.exchange(0x02)['payload']
            hdr_f = {f: v for f, w, v in self.vc.pb_decode(ph)}
            num = hdr_f.get(1, 1)
            p15 = s.exchange(0x15)['payload']
            p17 = s.exchange(0x17)['payload']
            blobs = [s.exchange(0x04, self.vc.pb_encode(1, i + 1))['payload'] for i in range(num)]
            s.exchange(0x01)
            blobs[idx] = new
            self._save_burst(dev, s, ph, p15, p17, blobs)
            return ('imported', idx)
        finally:
            dev.close()

    # ---------- results ----------
    def on_done(self, r):
        if isinstance(r, tuple):
            kind = r[0]
            if kind == 'manage':
                self.edits.clear()
                self.names.clear()
                op = r[1]
                nc = r[2]
                if op == 'del' and self.selected >= nc:
                    self.selected = max(0, nc - 1)
                self._busy(f'{r[1]} done - {nc} profiles; re-reading')
                self.async_(self.task_read)
                return
            if kind in ('msg',):
                self.status.config(text=r[1])
                return
            if kind == 'imported':
                self.edits.pop(r[1], None)
                self._busy('imported - re-reading')
                self.async_(self.task_read)
                return
            if kind == 'current':
                self._busy('active profile set')
                self.async_(self.task_read)
                return
        if isinstance(r, dict) and 'profs' in r:
            self.profs = r['profs']
            self.hdr = r['hdr'] or {}
            self.versions = r.get('versions') or {}
            v = self.versions
            self.dev_lbl.config(text=(
                f"device: hw {v.get('hw', '-')} fw {v.get('fw', '-')} "
                f"boot {v.get('bootloader', '-')}  profiles: {len(self.profs)}"))
            if self.selected >= len(self.profs):
                self.selected = max(0, len(self.profs) - 1)
            self.build_cards()
            self.build_form()
            self.status.config(text='read OK - live values from the base')
            return
        self.status.config(text=str(r)[:120])

    # ---------- actions ----------
    def on_select_cb(self, *_a):
        i = self.prof_cb.current()
        if i >= 0:
            self.selected = i
            self.build_cards()
            self.build_form()

    def on_refresh(self):
        self._busy('reading base…')
        self.async_(self.task_read)

    def on_recovery(self):
        import fcntl
        node = None
        for p in self.vc._usb_nodes():
            node = p[0]
            break
        if not node:
            self.status.config(text='no VRS base on USB')
            return
        try:
            fd = os.open(node, os.O_RDWR)
            fcntl.ioctl(fd, 0x00005514, 0)
            os.close(fd)
            self.status.config(text='USB reset done - stale connection cleared')
        except OSError as e:
            self.status.config(text=f'reset failed: {e}')

    def _diff_text(self):
        lines = []
        p = self.profs[self.selected] if self.profs else None
        for num, val in sorted(self.edits.get(self.selected, {}).items()):
            old = p['values'].get(num, '?') if p else '?'
            lines.append(f'  {self._label_for(num)}: {old} -> {val}')
        nm = self.names.get(self.selected)
        if p and nm and nm != p['name']:
            lines.append(f'  name: {p["name"]!r} -> {nm!r}')
        return '\n'.join(lines)

    def on_apply(self):
        if not self.profs or messagebox is None:
            return
        changes = self._diff_text()
        if not changes:
            self.status.config(text='nothing to save')
            return
        if not messagebox.askyesno(
                'Save to Device',
                'The base updates ONLY the whole store together: this writes '
                f'ALL {len(self.profs)} profiles back.\n\n{changes}\n\nProceed?'):
            return
        self.apply_btn.config(state='disabled')
        self._busy('writing full store…')
        self.async_(self.task_save)

    def on_new(self):
        from tkinter import simpledialog
        name = simpledialog.askstring('New profile', 'Profile name:', parent=self,
                                      initialvalue=f'Profile {len(self.profs) + 1}')
        if not name:
            return
        if messagebox.askyesno('New profile',
                               f'Create "{name}" as a copy of the selected profile?\n'
                               'The full store will be rewritten.'):
            self._manage('new', name)

    def on_dup(self):
        from tkinter import simpledialog
        base = self.names.get(self.selected, self.profs[self.selected]['name']) if self.profs else 'Profile'
        name = simpledialog.askstring('Duplicate profile', 'Copy name:', parent=self,
                                      initialvalue=f'{base} copy')
        if not name:
            return
        self._manage('dup', name)

    def on_del(self):
        if not self.profs:
            return
        p = self.profs[self.selected]
        nm = self.names.get(self.selected, p['name'])
        if messagebox.askyesno('Delete profile',
                               f'Delete "{nm}" and rewrite the store with the '
                               'remaining profiles? Cannot be undone.'):
            self._manage('del', None)

    def _manage(self, op, name):
        self.apply_btn.config(state='disabled')
        self._busy(f'{op} …')
        self.async_(self.task_manage, op, name)

    def on_set_current(self):
        if messagebox and not messagebox.askyesno(
                'Set active', 'Switch active profile (rewrites full store with new header)?'):
            return
        self.apply_btn.config(state='disabled')
        self._busy('switching active profile…')
        self.async_(self.task_set_current)

    def on_discard(self):
        self.edits.pop(self.selected, None)
        self.names.pop(self.selected, None)
        if self.profs:
            self.build_form()
            self.build_cards()
        self.status.config(text='edits discarded for this profile')

    def on_export(self):
        if not self.profs:
            return
        self.async_(self.task_export)

    def on_import(self):
        if not self.profs or filedialog is None:
            return
        path = filedialog.askopenfilename(filetypes=[('JSON', '*.json')])
        if path:
            self._busy('importing…')
            self.async_(self.task_import, path)


def main():
    if tk is None:
        sys.exit('tkinter missing: sudo apt install python3-tk '
                 '(or run ../install-packages.sh)')
    log(f'GUI start euid={os.geteuid()}')
    root = tk.Tk()
    root.title('VRS DirectForce')
    root.geometry('1280x800')
    root.minsize(1024, 640)
    GUI(root)
    root.mainloop()


if __name__ == '__main__':
    main()
