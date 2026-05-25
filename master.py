"""
master.py — interactive TUI master control for CozyLife lights.

Keys:
  1 / 2 / 3 / 4   switch mode: OFF / DISCO / AUDIO / MANUAL
  ↑ / ↓            navigate settings
  ← / →            decrease / increase selected setting value
  r                rediscover devices
  q                quit

Usage:
  python master.py                          # auto-discover
  python master.py 192.168.1.x 192.168.1.y # specific IPs
"""

import curses
import math
import random
import socket
import sys
import threading
import time
from typing import Optional

try:
    import fcntl as _fcntl
    import struct as _struct
    import termios as _termios
    _FIONREAD = getattr(_termios, "FIONREAD", 0x4004667F)
    _HAS_FIONREAD = True
except ImportError:
    _HAS_FIONREAD = False

from cozylife import CozyLifeDevice

try:
    import numpy as np
    import sounddevice as sd
    AUDIO_AVAILABLE = True
except ImportError:
    AUDIO_AVAILABLE = False


# ──────────────────────────────────────────────────────────────────────────────
# Settings metadata — defines every tunable knob per mode
# ──────────────────────────────────────────────────────────────────────────────

SETTINGS_META: dict[str, list[dict]] = {
    "off": [],
    "disco": [
        {"key": "interval",  "label": "Interval",  "min": 0.01, "max": 2.0,    "step": 0.01, "fmt": "{:.2f}s"},
        {"key": "sat_min",   "label": "Sat Min",   "min": 0.0,  "max": 100.0,  "step": 5.0,  "fmt": "{:.0f}%"},
        {"key": "sat_max",   "label": "Sat Max",   "min": 0.0,  "max": 100.0,  "step": 5.0,  "fmt": "{:.0f}%"},
        {"key": "bri",       "label": "Brightness","min": 0.0,  "max": 255.0,  "step": 10.0, "fmt": "{:.0f}"},
    ],
    "audio": [
        {"key": "smoothing",      "label": "Smoothing",   "min": 0.01, "max": 0.99,   "step": 0.05,  "fmt": "{:.2f}"},
        {"key": "gain_decay",     "label": "Gain Decay",  "min": 0.90, "max": 0.999,  "step": 0.001, "fmt": "{:.3f}"},
        {"key": "freq_min",       "label": "Freq Min",    "min": 20.0, "max": 500.0,  "step": 10.0,  "fmt": "{:.0f} Hz"},
        {"key": "freq_max",       "label": "Freq Max",    "min": 1000.,"max": 20000., "step": 500.0, "fmt": "{:.0f} Hz"},
        {"key": "light_interval", "label": "Update Rate", "min": 0.02, "max": 0.5,    "step": 0.01,  "fmt": "{:.2f}s"},
        {"key": "hue_spread",     "label": "Hue Spread",  "min": 0.0,  "max": 180.0,  "step": 5.0,   "fmt": "{:.0f}°"},
    ],
    "manual": [
        {"key": "hue",        "label": "Hue",        "min": 0.0,   "max": 360.0, "step": 2.0,  "fmt": "{:.0f}°"},
        {"key": "saturation", "label": "Saturation", "min": 0.0,   "max": 100.0, "step": 2.0,  "fmt": "{:.0f}%"},
        {"key": "brightness", "label": "Brightness", "min": 0.0,   "max": 255.0, "step": 5.0,  "fmt": "{:.0f}"},
        {"key": "color_temp", "label": "Color Temp", "min": 2000.0, "max": 6500.0, "step": 100.0, "fmt": "{:.0f} K"},
        {"key": "use_temp",   "label": "Color Mode", "min": 0.0,   "max": 1.0,   "step": 1.0,
         "fmt_fn": lambda v: "Hue / Sat" if v < 0.5 else "Color Temp"},
    ],
}

DEFAULT_SETTINGS: dict[str, dict] = {
    "off":    {},
    "disco":  {"interval": 0.05, "sat_min": 60.0, "sat_max": 100.0, "bri": 255.0},
    "audio":  {"smoothing": 0.25, "gain_decay": 0.995,
               "freq_min": 80.0, "freq_max": 8000.0, "light_interval": 0.05, "hue_spread": 0.0},
    "manual": {"hue": 0.0, "saturation": 90.0, "brightness": 200.0,
               "color_temp": 3500.0, "use_temp": 0.0},
}

BAR_W = 14

_DBG_EMPTY = {
    "recv_pending": -1,
    "recv_buf":     -1,
    "send_buf":     -1,
    "cmd_rate":      0.0,
    "total_cmds":    0,
}


def _sock_stats(sock) -> dict:
    """Read kernel-level TCP buffer metrics for a connected socket."""
    out = {"recv_pending": -1, "recv_buf": -1, "send_buf": -1}
    if sock is None:
        return out
    try:
        out["recv_buf"] = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        out["send_buf"] = sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
    except OSError:
        pass
    if _HAS_FIONREAD:
        try:
            raw = _fcntl.ioctl(sock.fileno(), _FIONREAD, _struct.pack("I", 0))
            out["recv_pending"] = _struct.unpack("I", raw)[0]
        except OSError:
            pass
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Controller
# ──────────────────────────────────────────────────────────────────────────────

class Controller:
    def __init__(self, devices: list[CozyLifeDevice]):
        self.devices  = devices
        self.mode     = "off"
        self.settings = {m: dict(v) for m, v in DEFAULT_SETTINGS.items()}
        self.sel      = 0
        self.status   = f"{len(devices)} device(s) connected"
        self.lock     = threading.Lock()

        # audio reactive state (written by audio callback, read by workers)
        self._a_hue   = 0.0
        self._a_bri   = 0
        self._a_sat   = 90.0
        self._a_peak  = 0.01
        self._a_stream: Optional[object] = None

        self._stop    = threading.Event()
        self._workers: list[threading.Thread] = []

        self.debug = False
        self._dbg: list[dict] = [dict(_DBG_EMPTY) for _ in devices]

    # ── mode switching ────────────────────────────────────────────────────────

    def set_mode(self, mode: str) -> None:
        if mode == "audio" and not AUDIO_AVAILABLE:
            self.status = "audio unavailable — pip install sounddevice numpy"
            return

        self._stop_workers()
        self.mode = mode
        self.sel  = 0

        if mode == "off":
            for d in self.devices:
                try:
                    d.turn_off()
                except OSError:
                    pass

        elif mode == "manual":
            for d in self.devices:
                try:
                    d.turn_on()
                except OSError:
                    pass
            self._push_manual()

        else:  # disco / audio
            self._stop.clear()
            for idx, d in enumerate(self.devices):
                t = threading.Thread(target=self._worker, args=(d, idx), daemon=True)
                t.start()
                self._workers.append(t)
            if mode == "audio":
                self._start_audio()

        self.status = f"Mode: {mode.upper()}"

    def _stop_workers(self) -> None:
        if self._a_stream:
            try:
                self._a_stream.stop()
                self._a_stream.close()
            except Exception:
                pass
            self._a_stream = None
        self._stop.set()
        for t in self._workers:
            t.join(timeout=6.0)  # must exceed socket send timeout (5s)
        self._workers.clear()

    # ── per-device workers ────────────────────────────────────────────────────

    def _worker(self, device: CozyLifeDevice, idx: int = 0) -> None:
        cmd_count  = 0
        total_cmds = 0
        rate_t     = time.monotonic()

        while not self._stop.is_set():
            mode = self.mode
            s    = self.settings.get(mode, {})
            sent = False
            try:
                if mode == "disco":
                    hue = random.uniform(0, 360)
                    sat = random.uniform(s.get("sat_min", 60), s.get("sat_max", 100))
                    bri = round(int(s.get("bri", 255)) * 1000 / 255)
                    device._send(3, {"1": 255, "4": bri,
                                     "5": int(hue), "6": int(sat * 10)})
                    device._drain()
                    sent = True
                    self._stop.wait(s.get("interval", 0.05))

                elif mode == "audio":
                    with self.lock:
                        h, b, sat = self._a_hue, self._a_bri, self._a_sat
                    h = (h + idx * s.get("hue_spread", 0.0)) % 360
                    if b > 5:
                        device._send(3, {"1": 255, "4": round(b * 1000 / 255),
                                         "5": int(h), "6": int(sat * 10)})
                        device._drain()
                        sent = True
                    self._stop.wait(s.get("light_interval", 0.05))

            except OSError:
                if self._stop.is_set():
                    return
                self.status = f"Lost {device.ip} — reconnecting..."
                try:
                    device.reconnect()
                    self.status = f"Reconnected {device.ip}"
                except OSError:
                    self._stop.wait(5.0)

            if sent:
                cmd_count  += 1
                total_cmds += 1

            # Refresh debug stats every 0.5 s regardless of send rate
            now = time.monotonic()
            elapsed = now - rate_t
            if elapsed >= 0.5:
                rate = cmd_count / elapsed
                cmd_count = 0
                rate_t    = now
                stats = _sock_stats(device._sock)
                stats["cmd_rate"]   = rate
                stats["total_cmds"] = total_cmds
                if idx < len(self._dbg):
                    self._dbg[idx] = stats

    # ── audio ─────────────────────────────────────────────────────────────────

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        # Exceptions raised here are caught by sounddevice and cause it to abort
        # the stream, after which stream.stop() blocks indefinitely — freezing the
        # terminal. Swallow all errors so the stream stays alive.
        try:
            mono = indata[:, 0]
            s    = self.settings["audio"]
            rms  = float(np.sqrt(np.mean(mono ** 2)))
            self._a_peak = max(rms, self._a_peak * s["gain_decay"], 0.01)
            vol  = min(rms / self._a_peak, 1.0)

            win   = np.hanning(len(mono))
            spec  = np.abs(np.fft.rfft(mono * win))
            freqs = np.fft.rfftfreq(len(mono), 1.0 / 44100)
            fmin, fmax = s["freq_min"], s["freq_max"]
            mask  = (freqs >= fmin) & (freqs <= fmax)
            dom   = float(freqs[mask][np.argmax(spec[mask])]) if mask.any() else fmin
            t     = (math.log(max(dom, fmin)) - math.log(fmin)) / (math.log(fmax) - math.log(fmin))
            sm    = s["smoothing"]

            with self.lock:
                self._a_hue = self._a_hue + sm * (t * 360.0 - self._a_hue)
                self._a_bri = int(self._a_bri + sm * (vol * 255 - self._a_bri))
        except Exception:
            pass

    def _start_audio(self) -> None:
        self._a_peak = 0.01
        stream = sd.InputStream(samplerate=44100, blocksize=1024,
                                channels=1, callback=self._audio_callback)
        stream.start()
        self._a_stream = stream

    # ── settings ──────────────────────────────────────────────────────────────

    def nav(self, direction: int) -> None:
        meta = SETTINGS_META.get(self.mode, [])
        if meta:
            self.sel = (self.sel + direction) % len(meta)

    def adjust(self, direction: int) -> None:
        meta = SETTINGS_META.get(self.mode, [])
        if not meta or self.sel >= len(meta):
            return
        m   = meta[self.sel]
        key = m["key"]
        cur = self.settings[self.mode].get(key, m["min"])
        new = max(m["min"], min(m["max"], cur + direction * m["step"]))
        dec = len(str(m["step"]).split(".")[-1]) if "." in str(m["step"]) else 0
        self.settings[self.mode][key] = round(new, dec)
        if self.mode == "manual":
            self._push_manual()

    def _push_manual(self) -> None:
        s   = self.settings["manual"]
        bri = round(int(s.get("brightness", 200)) * 1000 / 255)
        if s.get("use_temp", 0) >= 0.5:
            k = s.get("color_temp", 3500)
            payload: dict = {"1": 255, "4": bri,
                             "3": round((k - 2000) / (6500 - 2000) * 1000)}
        else:
            payload = {"1": 255, "4": bri,
                       "5": int(s.get("hue", 0)),
                       "6": int(s.get("saturation", 90) * 10)}
        for d in self.devices:
            try:
                d._send(3, payload)
            except OSError:
                pass

    def shutdown(self) -> None:
        self._stop_workers()
        # Close all devices in parallel — close() waits up to 1 s for device
        # FIN so serial shutdown of N devices would take N seconds.
        def _close_one(d: CozyLifeDevice) -> None:
            try:
                d.turn_off()
                d.close()
            except OSError:
                pass
        threads = [
            threading.Thread(target=_close_one, args=(d,), daemon=True)
            for d in self.devices
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)


# ──────────────────────────────────────────────────────────────────────────────
# TUI drawing
# ──────────────────────────────────────────────────────────────────────────────

def _bar(val: float, lo: float, hi: float, width: int = BAR_W) -> str:
    frac   = (val - lo) / (hi - lo) if hi != lo else 0.0
    filled = min(int(frac * width), width)
    return "█" * filled + "░" * (width - filled)


def draw(stdscr, ctrl: Controller) -> None:
    stdscr.erase()
    rows, cols = stdscr.getmaxyx()

    # colour pairs
    C_TITLE = curses.color_pair(1) | curses.A_BOLD
    C_ACTIVE = curses.color_pair(2) | curses.A_BOLD
    C_IDLE   = curses.color_pair(3)
    C_SEL    = curses.color_pair(4)
    C_VAL    = curses.color_pair(5)
    C_DEV    = curses.color_pair(6)
    C_DIM    = curses.color_pair(7) | curses.A_DIM

    def put(row: int, col: int, text: str, attr: int = curses.A_NORMAL) -> int:
        if row >= rows - 1 or col >= cols - 1:
            return col
        avail = cols - col - 1
        try:
            stdscr.addstr(row, col, text[:avail], attr)
        except curses.error:
            pass
        return col + len(text)

    r = 0

    # title bar
    title = "  CozyLife Master Control  "
    put(r, max(0, (cols - len(title)) // 2), title, C_TITLE)
    r += 2

    # mode strip
    mode_defs = [
        ("off",    "1", " OFF    "),
        ("disco",  "2", " DISCO  "),
        ("audio",  "3", " AUDIO  "),
        ("manual", "4", " MANUAL "),
    ]
    x = 2
    for mkey, num, label in mode_defs:
        unavail = mkey == "audio" and not AUDIO_AVAILABLE
        if unavail:
            attr = C_DIM
        elif mkey == ctrl.mode:
            attr = C_ACTIVE
        else:
            attr = C_IDLE
        x = put(r, x, f"[{num}]{label}", attr)
        x += 2
    r += 2

    # separator
    put(r, 0, "─" * (cols - 1), C_DIM)
    r += 1

    # settings panel
    meta = SETTINGS_META.get(ctrl.mode, [])
    if meta:
        put(r, 2, f"Settings — {ctrl.mode.upper()}", curses.A_BOLD)
        r += 1
        s = ctrl.settings.get(ctrl.mode, {})
        for i, m in enumerate(meta):
            val     = s.get(m["key"], m["min"])
            val_str = m["fmt_fn"](val) if "fmt_fn" in m else m["fmt"].format(val)
            bar_str = _bar(val, m["min"], m["max"]) if "fmt_fn" not in m else None
            is_sel  = i == ctrl.sel
            prefix  = "  ▶ " if is_sel else "    "
            label_a = C_SEL if is_sel else curses.A_NORMAL
            val_a   = C_VAL | (curses.A_BOLD if is_sel else 0)

            cx = put(r, 0, f"{prefix}{m['label']:<14}", label_a)
            if bar_str:
                cx = put(r, cx, f" [{bar_str}]", C_DIM)
            cx = put(r, cx, f"  {val_str}", val_a)
            if is_sel:
                put(r, cx, "   ← →", C_DIM)
            r += 1
    else:
        put(r, 2, "All lights off." if ctrl.mode == "off" else "No settings.", C_DIM)
        r += 1

    # audio live meters
    if ctrl.mode == "audio" and AUDIO_AVAILABLE:
        r += 1
        put(r, 2, "Live audio:", curses.A_BOLD)
        r += 1
        with ctrl.lock:
            ah, ab = ctrl._a_hue, ctrl._a_bri
        cx = put(r, 4, "Hue ", C_DIM)
        cx = put(r, cx, f"[{_bar(ah, 0, 360, 20)}]", C_VAL)
        put(r, cx, f"  {ah:5.0f}°", C_DIM)
        r += 1
        cx = put(r, 4, "Bri ", C_DIM)
        cx = put(r, cx, f"[{_bar(ab, 0, 255, 20)}]", C_VAL)
        put(r, cx, f"  {ab:3d}", C_DIM)
        r += 1

    r += 1
    put(r, 0, "─" * (cols - 1), C_DIM)
    r += 1

    # device list
    put(r, 2, f"Devices ({len(ctrl.devices)} connected)", curses.A_BOLD)
    r += 1
    for d in ctrl.devices:
        put(r, 4, f"● {d.ip}", C_DEV)
        r += 1

    # debug panel
    if ctrl.debug:
        r += 1
        put(r, 2, "Debug — TCP Buffer Stats  [d to hide]", curses.A_BOLD)
        r += 1
        fionread_note = "" if _HAS_FIONREAD else "  (FIONREAD unavailable — recv_pending always -1)"
        if fionread_note:
            put(r, 4, fionread_note, C_DIM)
            r += 1
        for i, (d, dbg) in enumerate(zip(ctrl.devices, ctrl._dbg)):
            rp = dbg.get("recv_pending", -1)
            rb = dbg.get("recv_buf",     -1)
            sb = dbg.get("send_buf",     -1)
            cr = dbg.get("cmd_rate",    0.0)
            tc = dbg.get("total_cmds",    0)

            put(r, 4, f"● {d.ip}", C_DEV)
            r += 1

            if rp >= 0 and rb > 0:
                fill_bar = _bar(rp, 0, rb, 16)
                pct      = rp / rb * 100
                recv_str = f"recv pending: {rp:>6} / {rb} B  [{fill_bar}] {pct:4.1f}%"
            elif rp >= 0:
                recv_str = f"recv pending: {rp} B  (buf size unknown)"
            else:
                recv_str = "recv pending: n/a"
            put(r, 6, recv_str, C_DIM)
            r += 1

            send_str = f"send buf cap: {sb} B" if sb >= 0 else "send buf cap: n/a"
            put(r, 6, f"{send_str}   rate: {cr:5.1f} cmd/s   total: {tc}", C_DIM)
            r += 1

    # status
    if ctrl.status:
        r += 1
        put(r, 2, ctrl.status, C_DIM)

    # help strip pinned to bottom
    help_row = rows - 2
    if help_row > r:
        put(help_row, 0, "─" * (cols - 1), C_DIM)
        put(help_row + 1, 0,
            "  ↑/↓ navigate   ←/→ adjust   1-4 mode   r rediscover   d debug   q/Esc quit",
            C_DIM)

    stdscr.refresh()


# ──────────────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────────────

def _run(stdscr, devices: list[CozyLifeDevice]) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)   # title
    curses.init_pair(2, curses.COLOR_GREEN, -1)                   # active mode
    curses.init_pair(3, curses.COLOR_WHITE, -1)                   # idle mode
    curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_WHITE)  # selected row
    curses.init_pair(5, curses.COLOR_CYAN,  -1)                   # value
    curses.init_pair(6, curses.COLOR_GREEN, -1)                   # device bullet
    curses.init_pair(7, curses.COLOR_WHITE, -1)                   # dim text

    ctrl = Controller(devices)

    try:
        while True:
            draw(stdscr, ctrl)
            key = stdscr.getch()

            if key in (ord("q"), 27):   # q or Escape
                break
            elif key == ord("1"):
                ctrl.set_mode("off")
            elif key == ord("2"):
                ctrl.set_mode("disco")
            elif key == ord("3"):
                ctrl.set_mode("audio")
            elif key == ord("4"):
                ctrl.set_mode("manual")
            elif key == curses.KEY_UP:
                ctrl.nav(-1)
            elif key == curses.KEY_DOWN:
                ctrl.nav(1)
            elif key == curses.KEY_LEFT:
                ctrl.adjust(-1)
            elif key == curses.KEY_RIGHT:
                ctrl.adjust(1)
            elif key == ord("d"):
                ctrl.debug = not ctrl.debug
            elif key == ord("r"):
                ctrl.status = "Rediscovering..."
                draw(stdscr, ctrl)
                ctrl.shutdown()
                new_devices = CozyLifeDevice.discover()
                ctrl = Controller(new_devices)
                ctrl.status = f"Found {len(new_devices)} device(s)"
            elif key == curses.KEY_RESIZE:
                pass  # redraws automatically next tick

            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        ctrl.shutdown()


def main() -> None:
    if len(sys.argv) > 1:
        ips = sys.argv[1:]
        print(f"Connecting to {ips}...")
        devices = []
        for ip in ips:
            try:
                devices.append(CozyLifeDevice(ip))
            except OSError as e:
                print(f"  {ip}: {e}")
    else:
        print("Discovering devices...")
        devices = CozyLifeDevice.discover()

    if not devices:
        print("No devices found. Try: python master.py 192.168.1.x")
        return

    print(f"Connected to {len(devices)} device(s). Launching TUI...")
    time.sleep(0.3)
    curses.wrapper(_run, devices)


if __name__ == "__main__":
    main()
