"""
Standalone CozyLife device controller — no Home Assistant required.

Usage:
    # Auto-discover devices on your LAN
    devices = CozyLifeDevice.discover()
    for d in devices:
        print(d)

    # Connect directly by IP
    d = CozyLifeDevice("192.168.1.100")
    print(d.query())
    d.turn_on()
    d.set_brightness(128)
    d.set_color_temp(3500)  # Kelvin (2000=warm – 6500=cool)
    d.set_hs(240, 80)       # hue 0–360, saturation 0–100
    d.turn_off()
    d.close()
"""

import concurrent.futures
import json
import re
import socket
import subprocess
import time
from typing import Optional


_PORT = 5555
_DISCOVERY_PORT = 6095
_DISCOVERY_ADDR = "255.255.255.255"


def _sn() -> str:
    return str(int(time.time() * 1000))


class CozyLifeDevice:
    """TCP client for a single CozyLife smart device."""

    def __init__(self, ip: str):
        self.ip = ip
        self._sock: Optional[socket.socket] = None
        self._connect()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        # Keepalives: OS probes after 10 s idle, every 3 s, gives up after 3 misses.
        # Without this, a silently-dropped connection isn't detected for minutes and
        # send() eventually blocks waiting for ACKs, freezing the caller.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        _ka = getattr(socket, "TCP_KEEPALIVE", getattr(socket, "TCP_KEEPIDLE", None))
        if _ka:
            s.setsockopt(socket.IPPROTO_TCP, _ka, 10)
        if hasattr(socket, "TCP_KEEPINTVL"):
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)
        if hasattr(socket, "TCP_KEEPCNT"):
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        s.connect((self.ip, _PORT))
        self._sock = s

    def reconnect(self) -> None:
        """Close and reopen the TCP connection."""
        self.close()
        self._connect()

    def close(self) -> None:
        """Close the TCP connection with a full 4-way FIN handshake.

        Sequence:
          1. shutdown(SHUT_WR)  — sends our FIN; TCP guarantees any buffered
             data (e.g. turn_off) is delivered before the FIN.
          2. recv loop (1 s timeout) — drains remaining device responses and
             waits for the device's own FIN (recv returns b"").  This ACKs
             the device's FIN and lets it reach CLOSED before we leave.
          3. close() — releases the fd.

        Skipping step 2 leaves the device in LAST_ACK waiting for our final
        ACK indefinitely, which blocks it from accepting new connections.
        """
        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            try:
                self._sock.settimeout(1.0)
                while self._sock.recv(4096):
                    pass
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> "CozyLifeDevice":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"CozyLifeDevice(ip={self.ip})"

    # ------------------------------------------------------------------
    # Low-level protocol
    # ------------------------------------------------------------------

    def _build(self, command: int, payload: dict) -> bytes:
        sn = _sn()
        if command == 3:    # SET
            msg = {"pv": 0, "cmd": command, "sn": sn,
                   "msg": {"attr": [int(k) for k in payload], "data": payload}}
        elif command == 2:  # QUERY
            msg = {"pv": 0, "cmd": command, "sn": sn, "msg": {"attr": [0]}}
        elif command == 0:  # INFO
            msg = {"pv": 0, "cmd": command, "sn": sn, "msg": {}}
        else:
            raise ValueError(f"Unknown command: {command}")
        return (json.dumps(msg, separators=(",", ":")) + "\r\n").encode()

    def _drain(self) -> None:
        """Discard queued device responses to prevent recv-buffer overflow.

        The device ACKs every SET command with a small JSON packet.  If the
        caller never reads these, the kernel recv buffer fills over several
        minutes, TCP flow-control backs up the device's send side, and
        eventually send() blocks until the connection times out.  Call this
        after any fire-and-forget _send() to keep the pipe clear.
        """
        if self._sock is None:
            return
        try:
            self._sock.setblocking(False)
            while self._sock.recv(4096):
                pass
        except (BlockingIOError, OSError):
            pass
        finally:
            self._sock.settimeout(5)

    def _send(self, command: int, payload: Optional[dict] = None) -> None:
        if self._sock is None:
            raise OSError("not connected")
        self._sock.send(self._build(command, payload or {}))

    def _send_recv(self, command: int, payload: Optional[dict] = None) -> dict:
        packet = self._build(command, payload or {})
        sn = json.loads(packet.strip())["sn"]
        self._sock.send(packet)

        for _ in range(10):
            try:
                raw = self._sock.recv(1024)
            except socket.timeout:
                break
            if sn.encode() in raw:
                resp = json.loads(raw.strip())
                return (resp.get("msg") or {}).get("data") or {}
        return {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def info(self) -> dict:
        """Return device identity (device ID, PID, MAC, firmware version, etc.)."""
        self._send(0)
        raw = self._sock.recv(1024)
        return json.loads(raw.strip()).get("msg", {})

    def query(self) -> dict:
        """
        Return current device state as a raw dpid→value dict, e.g.
        {'1': 255, '2': 0, '3': 500, '4': 600, '5': 0, '6': 0}
        Keys:
            '1' switch (0=off, 255=on)
            '2' work mode
            '3' color temp (0–1000, higher=warmer)
            '4' brightness (0–1000)
            '5' hue (0–360)
            '6' saturation (0–1000)
        """
        return self._send_recv(2)

    def turn_on(self) -> None:
        """Turn the device on."""
        self._send(3, {"1": 255})

    def turn_off(self) -> None:
        """Turn the device off."""
        self._send(3, {"1": 0})

    def set_brightness(self, brightness: int) -> None:
        """brightness: 0–255, converted internally to 0–1000."""
        value = round(max(0, min(255, brightness)) * 1000 / 255)
        self._send(3, {"1": 255, "4": value})

    def set_color_temp(self, kelvin: int) -> None:
        """kelvin: 2000 (warm) – 6500 (cool)."""
        k = max(2000, min(6500, kelvin))
        value = round((k - 2000) / (6500 - 2000) * 1000)
        self._send(3, {"1": 255, "3": value})

    def set_hs(self, hue: float, saturation: float) -> None:
        """hue: 0–360, saturation: 0–100."""
        self._send(3, {"1": 255, "5": int(hue), "6": int(saturation * 10)})

    def set_rgb(self, r: int, g: int, b: int) -> None:
        """Convenience: convert RGB (0–255 each) to hue/saturation and apply."""
        h, s = _rgb_to_hs(r, g, b)
        self.set_hs(h, s)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @staticmethod
    def discover(timeout: float = 2.0, scan_fallback: bool = True) -> list["CozyLifeDevice"]:
        """
        Discover devices in two stages:
        1. UDP broadcast on port 6095 — fast, requires AP isolation to be OFF.
        2. TCP subnet scan on port 5555 — slower fallback when UDP finds nothing.

        If your router has AP/client isolation enabled, UDP will always fail.
        Disable it in your router's wireless settings, or pass IPs directly.
        """
        found_ips: list[str] = []

        # --- Stage 1: UDP broadcast, bound to each local interface ---
        local_ips = _get_local_ips()
        probe = ('{"cmd":0,"pv":0,"sn":"' + _sn() + '","msg":{}}').encode()

        socks = []
        for local_ip in local_ips:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((local_ip, 0))
                s.settimeout(0.1)
                socks.append(s)
            except OSError:
                pass

        # Re-broadcast every 0.4 s so devices that were briefly busy on the
        # first burst still get a probe during the listen window.
        deadline    = time.time() + timeout
        next_probe  = 0.0
        while time.time() < deadline:
            if time.time() >= next_probe:
                for s in socks:
                    try:
                        s.sendto(probe, (_DISCOVERY_ADDR, _DISCOVERY_PORT))
                    except OSError:
                        pass
                next_probe = time.time() + 0.4
            for s in socks:
                try:
                    _, addr = s.recvfrom(1024)
                    if addr[0] not in found_ips:
                        found_ips.append(addr[0])
                except socket.timeout:
                    pass
        for s in socks:
            s.close()

        # --- Stage 2: TCP subnet scan if UDP found nothing ---
        if not found_ips and scan_fallback:
            print(
                "UDP discovery found nothing — "
                "falling back to TCP subnet scan (this takes a few seconds)..."
            )
            found_ips = _tcp_scan(local_ips)

        devices = []
        for device_ip in found_ips:
            try:
                devices.append(CozyLifeDevice(device_ip))
            except OSError:
                pass
        return devices


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _get_local_ips() -> list[str]:
    """Return all non-loopback IPv4 addresses on this machine."""
    try:
        out = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, check=False
        ).stdout
        addrs = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        return [a for a in addrs if not a.startswith("127.")]
    except OSError:
        return []


def _tcp_scan(local_ips: list[str]) -> list[str]:
    """
    Scan each /24 subnet of the given local IPs for open port 5555.
    Uses 50 parallel threads; typically completes in 2-4 seconds.
    """
    targets: list[str] = []
    for local_ip in local_ips:
        prefix = ".".join(local_ip.split(".")[:3])
        for i in range(1, 255):
            candidate = f"{prefix}.{i}"
            if candidate not in targets and candidate != local_ip:
                targets.append(candidate)

    def probe(target: str) -> Optional[str]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect((target, _PORT))
            s.close()
            return target
        except OSError:
            return None

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as ex:
        for result in ex.map(probe, targets):
            if result:
                results.append(result)
    return results


def _rgb_to_hs(r: int, g: int, b: int) -> tuple[float, float]:
    r_, g_, b_ = r / 255, g / 255, b / 255
    cmax, cmin = max(r_, g_, b_), min(r_, g_, b_)
    delta = cmax - cmin

    if delta == 0:
        h = 0.0
    elif cmax == r_:
        h = 60 * (((g_ - b_) / delta) % 6)
    elif cmax == g_:
        h = 60 * ((b_ - r_) / delta + 2)
    else:
        h = 60 * ((r_ - g_) / delta + 4)

    s = 0.0 if cmax == 0 else (delta / cmax) * 100
    return h, s


# ------------------------------------------------------------------
# Quick CLI
# ------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Discovering devices...")
        discovered = CozyLifeDevice.discover()
        if not discovered:
            print("No devices found.")
        for d in discovered:
            print(d, d.query())
        sys.exit(0)

    target_ip = sys.argv[1]
    target_cmd = sys.argv[2] if len(sys.argv) > 2 else "query"

    with CozyLifeDevice(target_ip) as d:
        if target_cmd == "on":
            d.turn_on()
        elif target_cmd == "off":
            d.turn_off()
        elif target_cmd == "info":
            print(d.info())
        elif target_cmd == "query":
            print(d.query())
        elif target_cmd == "brightness":
            d.set_brightness(int(sys.argv[3]))
        elif target_cmd == "temp":
            d.set_color_temp(int(sys.argv[3]))   # Kelvin, e.g. 3500
        elif target_cmd == "hs":
            d.set_hs(float(sys.argv[3]), float(sys.argv[4]))
        elif target_cmd == "rgb":
            d.set_rgb(int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]))
        else:
            print(f"Unknown command: {target_cmd}")
