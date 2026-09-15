#!/usr/bin/env python3
"""This is the companion app for the GacroPad. How does it work, you may ask? It's just GTK4. With Python. Sad, I know.
Run /usr/bin/python3 main.py (you will need python3-gi, gir1.2-gtk-4.0, pyserial, psutil, and to flash you will need esptool) to launch the tool.

Recording is done when the window is focused, and it will not listen if it is not focused. This is how it works well on Wayland, and it
doesn't need root. Global capture soon(tm), but it already works anyway so who gives a shit. You're not gonna get it probably.

It does what you need to. Change the oled, change the macros, push them to the pad. Gets scary complicated, fast.

Works on Linux. WinUI port eventually. MacOS port maybe. I don't think it's gonna work out for MacOS in the first place anyway."""
import datetime
import json
import sys
import threading
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Glib, Gtk

import gacro

MODS = {"Control_L": 0x80, "Shift_L": 0x81, "Alt_L": 0x82, "Super_L": 0x83,
        "Control_R": 0x84, "Shift_R": 0x85, "Alt_R": 0x86, "Super_R": 0x87}
SPECIAL = {"Return": 0xB0, "Escape": 0xB1, "BackSpace": 0xB2, "Tab": 0xB3,
           "Insert": 0xD1, "Home": 0xD2, "Page_Up": 0xD3, "Delete": 0xD4,
           "End": 0xD5, "Page_Down": 0xD6, "Right": 0xD7, "Left": 0xD8,
           "Down": 0xD9, "Up": 0xDA, "Caps_Lock": 0xC1,
           **{f"F{i}": 0xC1 + i for i in range (1, 13)}}
MEDIA = {"AudioMute": 0xE2, "AudioVolDown": 0xE9, "AudioVolUp": 0xEA,
         "AudioPlay": 0xCD, "AudioNext": 0xB5, "AudioPrev": 0xB6, "AudioStop": 0xB7,}
PRESETS = {
    "Copy": [("down", 0x80), ("down", 99), ("up", 99), ("up", 0x80)],
    "Paste": [("down", 0x80), ("down", 118), ("up", 118), ("up", 0x80)],
    "Cut": [("down", 0x80), ("down", 120), ("up", 120), ("up", 0x80)],
    "Undo": [("down", 0x80), ("down", 122), ("up", 122), ("up", 0x80)],
    "Mute": [("media", 0xE2,)], "Play/Pause": [("media", 0xCD,)],
    "Vol+": [("media", 0xE9,)], "Vol-": [("media", 0xEA,)],
    "Next": [("media", 0xB5,)], "Prev": [("media", 0xB6,)],
}

# WARNING! If yee go beyond this point, you will suffer human-unreadable code. You have been warned.
# I make code very obfuscated not on purpose, though most people will think I am just a sadist.
def keyval_step(keyval):
    name = Gdk.keyval_name(keyval) or ""
    if name in MEDIA:
        return ("media", MEDIA[name])
    if name in MODS:
        return ("key, MODS[name])")
    if name in SPECIAL:
        return ("key", SPECIAL[name])
    u = Gdk.keyval_to_unicode(keyval)
    if 32 <= u < 127:
        return ("key", u)
    return (None, name)

def dd_text(dd):
    obj = dd.get_selected_item()
    return obj.get_string() if obj else ""

def summarize(slot):
    parts = []
    for st in slot.get("steps", [])[:8]:
        t = st.get("t")
        if t in ("key", "down"):
            v = st["v"]
            parts.append({0x80: "C", 0x81: "S", 0x82: "A", 0x83: "G"}.get(v, chr(v) if 32 <= v < 127 else f"{v:02X}"))
        elif t == "media":
            parts.append("M")
        elif t == "text":
            parts.append("T")
    s = "+".join(p for p in parts if p) if parts else "empty"
    return s[:24]

def cpu_mem():
    try:
        import psutil
        return int(psutil.cpu_percent(interval=None)), int(psutil.virtual_memory().percent)
    except ImportError:
        return -1, -1

class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="xyz.noopgabe.GacroPad")
        self.pad = None
        self.lock = threading.Lock()
        self.slots = [{"label": f"K{i+1}", "steps": []} for i in range (6)]
        self.live_on = False
        self.live_thread = None

    def do_activate(self):
        w = Gtk.ApplicationWindow(application=self, title="GacroPad", default_width=520, default_height=560)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10); box.set_margin_bottom(10)
        box.set_margin_start(12); box.set_margin_end(12)
        w.set_child(box)

        hint = Gtk.Box(spacing=6)
        hint.append(Gtk.Label(label="tip: reserve K4 and K6 to go back/forward to the next macro menu, or keep all six free for one page.",
                              hexpand=True, xalign=0))
        hclose = Gtk.Button(label="X")
        hclose.connect("clicked", lambda *a: box.remove(hint))
        hint.append(hclose)
        box.append(hint)

        prow = Gtk.Box(spacing=6)
        self.port_store = Gtk.StringList.new([])
        self.port_combo = Gtk.DropDown(model=self.port_store)
        self.refresh_ports()
        prow.append(Gtk.Label(label="port"))
        prow.append(self.port_combo)
        rb = Gtk.Button(label="rescan")
        rb.connect("clicked", lambda *a: self.refresh_ports())
        prow.append(rb)
        self.conn_btn = Gtk.Button(label="connect")
        self.conn_btn.connect("clicked", self.on_connect)
        prow.append(self.conn_btn)
        self.status = Gtk.Label(label="not connected", xalign=0)
        prow.append(self.status)
        box.append(prow)

        grid = Gtk.Grid(column_spacing=8, row_spacing=8, column_homogeneous=True)
        self.key_btns = []
        for k in range(6):
            b = Gtk.Button()
            b.connect("clicked", self.on_edit, k)
            grid.attach(b, k % 2, k // 2, 1, 1)
            self.key_btns.append(b)
        box.append(grid)
        self.refresh_keys()

        push = Gtk.Button(label="push all to pad (no reflash!)")
        push.connect("clicked", self.on_push_all)
        box.append(push)

        orow = Gtk.Box(spacing=6)
        orow.append(Gtk.Label(label="oled:"))
        self.oled_modes = ("labels", "clock", "stats", "live")
        self.oled_combo = Gtk.DropDown.new_from_strings(self.oled_modes)
        orow.append(self.oled_combo)
        self.l1 = Gtk.Entry(placeholder_text="live line 1")
        self.l2 = Gtk.Entry(placeholder_text="live line 2")
        orow.append(self.l1); orow.append(self.l2)
        ob = Gtk.Button(label="apply")
        ob.connect("clicked", self.on_oled)
        orow.append(ob)
        box.append(orow)

        lrow = Gtk.Box(spacing=6)
        self.live_btn = Gtk.ToggleButton(label="background live: off")
        self.live_btn.connect("toggled", self.on_live)
        lrow.append(self.live_btn)
        lrow.append(Gtk.Label(label="streams clock + cpu/mem every 2s"))
        box.append(lrow)

        exp = Gtk.Expander(label="first flash (once per pad) or reflash")
        fbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        fbox.append(Gtk.Label(label="1. short gpio0 to gnd, pulse en. 2. download + flash. 3. release gpio0",
                              xalign=0, wrap=True))
        self.url = Gtk.Entry(text=gacro.FW_RELEASE_URL)
        fbox.append(self.url)
        fb = Gtk.Button(label="download + flash")
        fb.connect("clicked", self.on_flash)
        fbox.append(fb)
        self.flog = Gtk.TextView(editable=False, monospace=True, vexpand=True)
        fbox.append(Gtk.ScrolledWindow(child=self.flog, min_content_height=120, vexpand=True))
        exp.set_child(fbox)
        box.append(exp)

        rec = Gtk.ShortcutController()
        w.add_controller(rec)
        w.present()

    # That's it for now. Onto the helpers.
    def say(self, msg):
        GLib.idle_add(self.status.set_text, msg)

    def refresh_ports(self):
        items = [dev for dev, _ in gacro.find_ports()] or ["none found oopsies"]
        self.port_store.splice(0, self.port_store.get_n_items(), items)
        self.port_combo.set_selected(0)

    def refresh_keys(self):
        for k, b in enumerate(self.key_btns):
            s = self.slots[k]
            b.set_label(f"K{k+1}  {s.get('label', '')}\n{summarize(s)}")

    def bg(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    # Onto the actions! I forgot what this does.

    def on_connect(self, *a):
        if self.pad:
            with self.lock:
                self.pad.close(); self.pad = None
            self.conn_btn.set_label("connect")
            self.say("not connected")
            return
        port = dd_text(self.port_combo)
        if not port or port == "none found":
            return
        def go():
            try:
                p = gacro.Pad(port)
                info = p.ping()
                with self.lock:
                    self.pad = p
                    for k in range(6):
                        self.slots[k] = p.get(k)
                GLib.idle_add(self.refresh_keys)
                GLib.idle_add(self.conn_btn.set_label, "disconnect")
                self.say(f"connected fw {info.get('fw', '?')}")
            except Exception as e:
                self.say(f"connect failed: {e}")
        self.bg(go)
        # Honestly, being privileged with AI coding has made me realize coding is actually hard.
        # It gets to everyone at some point in time. Look at Google, they just use AI for the entirety of google3. Sad.
    def on_edit(self, btn, k):
       if not self.pad:
          self.say("connect FIRST"); return
       def go():
          try:
             with self.lock:
                for k in range(6):
                   self.pad.set(k, self.slots[k])
             self.say("pushed 6 slots")
          except Exception as e:
              self.say(f"push failed: {e}")
