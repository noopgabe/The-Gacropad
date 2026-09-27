#!/usr/bin/env python3
# the GacroPad serial client and the first flash helper. 
import glob
import json
import subprocess
import sys
import urllib.request

PROTO = 1
FW_RELEASE_URL =  "https://github.com/noopgabe/The-Gacropad/releases/latest/download/gacropad.bin"

class PadError(Exception):
    pass

def find_ports():
    try: 
        from serial.tools import list_ports
        pts = []
        for p in list_ports.comports():
            score = 0
            if (p.vid, p.pid) == (0x303A, 0x1001):
                score = 2
            elif p.vid == 0x303A:
                score = 1
            pts.append((score, p.device, p.description or ""))
        pts.sort(reverse=True)
        return [(d, desc) for _, d, desc in pts]
    except ImportError:
        found = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
        return [(d, "") for d in found]


class Pad:
    def __init__(self, port, baud=115200, timeout=2):
        import serial
        self.ser = serial.Serial(port, baud, timeout=timeout)

    def close(self):
        self.ser.close()

    def _cmd(self, obj, timeout=3):
        self.ser.reset_input_buffer()
        self.ser.write((json.dumps(obj) + "\n").encode())
        self.ser.timeout = timeout
        line = self.ser.readline().decode("utf-8", "replace").strip()
        if not line:
            raise PadError("no reply (is the pad connected?)")
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            raise PadError(f"bad reply: {line[:120]}")

    def ping(self):
        r = self._cmd({"cmd": "ping"})
        if not r.get("ok"):
            raise PadError("ping failed")
        return r

    def get(self, key):
        r = self._cmd({"cmd": "get", "key": key})
        if not r.get("ok"):
            raise PadError(r.get("err", "get failed"))
        return r["slot"]

    def set(self, key, slot):
        r = self._cmd({"cmd": "set", "key": key, "slot": slot}, timeout=5)
        if not r.get("ok"):
            raise PadError(r.get("err", "set failed"))

    def oled(self, mode):
        r = self._cmd({"cmd": "oled", "mode": mode})
        if not r.get("ok"):
            raise PadError(r.get("err", "oled failed"))

    def press(self, key):
        r = self._cmd({"cmd": "press", "key": key})
        if not r.get("ok"):
            raise PadError(r.get("err", "press failed"))

    def live (self, clock="", date="", l1="", l2=""):
        self._cmd({"cmd": "live", "clock": clock, "date": date, "l1": l1, "l2": l2})

    def stats(self, cpu, mem):
        self._cmd({"cmd": "stats", "cpu": cpu, "mem": mem})

    def reset(self):
        self._cmd({"cmd": "reset"}, timeout=5)

def download_bin(url, path, log=print):
    log(f"downloading {url}")
    urllib.request.urlretrieve(url, path)
    log(f"saved {path}")

def flash_first(port, bin_path, log=print):
    # Full flash over the ROM bootloader, so this needs a MERGED image (bootloader + partition
    # table + app) written at 0x0. A PlatformIO app-only firmware.bin belongs at 0x10000 and
    # will not boot from 0x0. gpio0 to gnd, pulse en. i will add a button on the pcb for this
    with open(bin_path, "rb") as f:
        if f.read(1) != b"\xe9":
            raise PadError(f"{bin_path} is not an ESP image (bad magic byte)")
    cmd = [sys.executable, "-m", "esptool", "--chip", "esp32s3",
           "-p", port, "-b", "460800", "--before", "default-reset",
           "--after", "hard-reset", "write-flash", "--flash-mode", "dio",
           "--flash-size", "detect", "0x0", bin_path]
    log("$ " + " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    log(p.stdout[-2000:])
    if p.returncode:
        log(p.stderr[-2000:])
        raise PadError("esptool failed, try gpio0 to gnd, pulse en, and run again")
    log("flashed! now you can use the damn thing")


        

