"""
audio_lights.py — react CozyLife lights to live microphone audio.

Frequency → hue:  bass=red, mids=green, treble=blue
Volume    → brightness (auto-gains to the room's loudness level)

Requirements:
    pip install sounddevice numpy

Usage:
    python audio_lights.py                          # auto-discover
    python audio_lights.py 192.168.1.x 192.168.1.y # specific IPs
"""

import math
import sys
import threading
import time

import numpy as np
import sounddevice as sd

from cozylife import CozyLifeDevice

# ------------------------------------------------------------------
# Tuning
# ------------------------------------------------------------------

SAMPLE_RATE   = 44100
CHUNK         = 1024          # samples per audio frame (~23ms)
FREQ_MIN      = 80.0          # Hz — low end of mapped range
FREQ_MAX      = 8000.0        # Hz — high end of mapped range
SMOOTHING     = 0.25          # 0=instant, 1=never changes
GAIN_DECAY    = 0.995         # how fast auto-gain forgets loud peaks
MIN_GAIN      = 0.01          # prevents divide-by-zero in silence
LIGHT_INTERVAL = 0.05         # seconds between TCP sends per light

# ------------------------------------------------------------------
# Shared state (written by audio thread, read by light threads)
# ------------------------------------------------------------------

_lock      = threading.Lock()
_hue       = 0.0
_sat       = 90.0
_bri       = 0        # 0–255
_running   = True
_lights_on = True


def _freq_to_hue(freq: float) -> float:
    """Log-map a frequency in [FREQ_MIN, FREQ_MAX] to hue [0, 360]."""
    freq   = max(FREQ_MIN, min(FREQ_MAX, freq))
    log_lo = math.log(FREQ_MIN)
    log_hi = math.log(FREQ_MAX)
    t      = (math.log(freq) - log_lo) / (log_hi - log_lo)
    return t * 360.0


def audio_callback(indata, frames, time_info, status):
    global _hue, _sat, _bri
    global _peak_rms

    mono = indata[:, 0]

    # RMS volume
    rms = float(np.sqrt(np.mean(mono ** 2)))

    # Auto-gain: track rolling peak with slow decay
    _peak_rms = max(rms, _peak_rms * GAIN_DECAY, MIN_GAIN)
    volume = min(rms / _peak_rms, 1.0)

    # FFT — dominant frequency
    window    = np.hanning(len(mono))
    spectrum  = np.abs(np.fft.rfft(mono * window))
    freqs     = np.fft.rfftfreq(len(mono), 1.0 / SAMPLE_RATE)

    # Only look in the musical range
    mask = (freqs >= FREQ_MIN) & (freqs <= FREQ_MAX)
    if mask.any():
        dominant_freq = float(freqs[mask][np.argmax(spectrum[mask])])
    else:
        dominant_freq = FREQ_MIN

    target_hue = _freq_to_hue(dominant_freq)
    target_bri = int(volume * 255)

    with _lock:
        _hue = _hue + SMOOTHING * (target_hue - _hue)
        _bri = int(_bri + SMOOTHING * (target_bri - _bri))
        _sat = 90.0


# Rolling peak for auto-gain — needs to exist before the callback fires
_peak_rms = MIN_GAIN


def light_worker(device: CozyLifeDevice, stop: threading.Event):
    while not stop.is_set():
        with _lock:
            on  = _lights_on
            h   = _hue
            s   = _sat
            b   = _bri

        if on and b > 5:
            try:
                device._send(3, {
                    "1": 255,
                    "4": round(b * 1000 / 255),  # brightness 0–1000
                    "5": int(h),                  # hue 0–360
                    "6": int(s * 10),             # sat 0–1000
                })
            except OSError:
                pass
        elif not on:
            try:
                device.turn_off()
            except OSError:
                pass

        stop.wait(LIGHT_INTERVAL)


def main():
    global _lights_on

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

    if not devices:
        print("No devices found.")
        return

    print(f"Found {len(devices)} device(s): {[d.ip for d in devices]}")
    for d in devices:
        d.turn_on()

    stop = threading.Event()
    threads = [
        threading.Thread(target=light_worker, args=(d, stop), daemon=True)
        for d in devices
    ]
    for t in threads:
        t.start()

    print("\nListening to microphone...")
    print("Press Enter to toggle lights on/off, Ctrl+C to quit.\n")

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        blocksize=CHUNK,
        channels=1,
        callback=audio_callback,
    ):
        try:
            while True:
                input()
                _lights_on = not _lights_on
                print("Lights", "ON" if _lights_on else "OFF")
        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            stop.set()
            for t in threads:
                t.join()
            for d in devices:
                try:
                    d.turn_off()
                    d.close()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
