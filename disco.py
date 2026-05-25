"""
disco.py — flash all discovered CozyLife lights with random colors.

Usage:
    python disco.py             # discover & start
    python disco.py 192.168.1.x 192.168.1.y ...   # use specific IPs
"""

import random
import sys
import threading
import time

from cozylife import CozyLifeDevice

INTERVAL = 0.05   # seconds between color changes per light


def random_color(device: CozyLifeDevice, stop: threading.Event):
    while not stop.is_set():
        hue = random.uniform(0, 360)
        sat = random.uniform(60, 100)
        device.set_hs(hue, sat)
        stop.wait(INTERVAL)


def main():
    if len(sys.argv) > 1:
        ips = sys.argv[1:]
        print(f"Connecting to {ips}...")
        devices = []
        for ip in ips:
            try:
                devices.append(CozyLifeDevice(ip))
            except OSError as e:
                print(f"  Could not connect to {ip}: {e}")
    else:
        print("Discovering devices...")
        devices = CozyLifeDevice.discover()

    lights = [d for d in devices if True]  # all connected devices
    if not lights:
        print("No devices found.")
        return

    print(f"Found {len(lights)} device(s): {[d.ip for d in lights]}")
    for d in lights:
        d.turn_on()

    stop = threading.Event()
    threads = []
    for d in lights:
        t = threading.Thread(target=random_color, args=(d, stop), daemon=True)
        t.start()
        threads.append(t)

    print("\nDisco mode ON. Press Enter to toggle on/off, Ctrl+C to quit.\n")
    is_on = True
    try:
        while True:
            input()
            is_on = not is_on
            if is_on:
                print("Lights ON — resuming colors.")
                stop.clear()
                for d in lights:
                    d.turn_on()
                threads = []
                for d in lights:
                    t = threading.Thread(target=random_color, args=(d, stop), daemon=True)
                    t.start()
                    threads.append(t)
            else:
                print("Lights OFF.")
                stop.set()
                for t in threads:
                    t.join()
                for d in lights:
                    d.turn_off()

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        stop.set()
        for d in lights:
            try:
                d.turn_off()
                d.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
