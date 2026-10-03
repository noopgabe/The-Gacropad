#!/usr/bin/env python3
"""This is the companion app for the GacroPad. How does it work, you may ask? It's just GTK4. With Python. Sad, I know.
Run /usr/bin/python3 main.py (you will need python3-gi, gir1.2-gtk-4.0, pyserial, psutil, and to flash you will need esptool) to launch the tool.

Recording is done when the window is focused, and it will not listen if it is not focused. This is how it works well on Wayland, and it
doesn't need root. Global capture soon(tm), but it already works anyway so who gives a freak. You're not gonna get it probably.

It does what you need to. Change the oled, change the macros, push them to the pad. Gets scary complicated, fast.

Works on Linux. WinUI port eventually. MacOS port maybe. I don't think it's gonna work out for MacOS in the first place anyway."""
import copy
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

NO_PORT = "none found oopsies"

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
        return ("key", MODS[name])
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
        self.live_l1 = ""
        self.live_l2 = ""

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
        self.l1.connect("changed", self.cache_live_text)
        self.l2.connect("changed", self.cache_live_text)
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

    def cache_live_text(self, *a):
        # GTK is main-thread only, so the live thread reads these plain strings.
        self.live_l1 = self.l1.get_text()
        self.live_l2 = self.l2.get_text()

    def refresh_ports(self):
        items = [dev for dev, _ in gacro.find_ports()] or [NO_PORT]
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
        if not port or port == NO_PORT:
            return
        def go():
            try:
                p = gacro.Pad(port)
                info = p.ping()
                slots = [p.get(k) for k in range(6)]
                with self.lock:
                    self.pad = p
                    self.slots = slots
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
        KeyDialog(self, k)

    def on_push_all(self, *a):
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
        self.bg(go)

    def on_oled(self, *a):
        if not self.pad:
            self.say("connect first"); return
        mode = self.oled_modes[self.oled_combo.get_selected()]
        self.cache_live_text()
        l1, l2 = self.live_l1, self.live_l2
        def go():
            try:
                with self.lock:
                    self.pad.oled(mode)
                    self.pad.live(clock=now_clock(), l1=l1, l2=l2)
                self.say(f"oled set to {mode}")
            except Exception as e:
                self.say(f"oled failed: {e}")
        self.bg(go)
    def on_live(self, btn):
        self.live_on = btn.get_active()
        btn.set_label(f"background live: {'on' if self.live_on else 'off'}")
        if not self.live_on:
            return
        self.cache_live_text()
        if not (self.live_thread and self.live_thread.is_alive()):
            self.live_thread = threading.Thread(target=self.live_loop, daemon=True)
            self.live_thread.start() 

    def live_loop(self):
        while self.live_on:
            try:
                now = datetime.datetime.now()
                cpu, mem = cpu_mem()
                with self.lock:
                    if self.pad:          # Wow, I knew I was bad at dates. But I didn't know I was bad at dates in progamming either.
                        self.pad.live(clock=now.strftime("%H:%M"), date=now.strftime("%Y-%m-%d"), 
                                      l1=self.live_l1 or "GacroPad", l2=self.live_l2)
                        if cpu >= 0:
                            self.pad.stats(cpu, mem)
            except Exception as e:
                self.say(f"live: {e}")
            time.sleep(2)

    def append_log(self, msg):
        self.flog.get_buffer().insert_at_cursor(str(msg) + "\n")

    def on_flash(self, *a):
        port = dd_text(self.port_combo)
        url = self.url.get_text().strip() # nekked
        if not port or port == NO_PORT:
            return
        def log(msg):
            GLib.idle_add(self.append_log, str(msg))
        def go():
            try:
                path = "/tmp/gacropad.bin"
                gacro.download_bin(url, path, log)
                gacro.flash_first(port, path, log)
            except Exception as e:
                log(f"FAILED: {e}")
        self.bg(go)


def now_clock():
    return datetime.datetime.now().strftime("%H:%M")

class KeyDialog(Gtk.Dialog):
    def __init__(self, app, k):
        super().__init__(title=f"key K{k+1}", transient_for=app.get_active_window(), modal=True)
        self.app, self.k = app, k
        self.slot = copy.deepcopy(app.slots[k]) # how it feels to cook and burn down the kitchen
        self.recording = False
        self.last_t = 0
        self.held = {}
        box = self.get_content_area()
        box.set_spacing(8)
        box.set_margin_top(10); box.set_margin_bottom(10)
        box.set_margin_start(12); box.set_margin_end(12)

        self.label_entry = Gtk.Entry(text=self.slot.get("label", ""))
        box.append(Gtk.Label(label="label: (shows on oled)", xalign=0))
        box.append(self.label_entry)

        self.steps_label = Gtk.Label(xalign=0, wrap=True)
        box.append(Gtk.ScrolledWindow(child=self.steps_label, min_content_height=120, vexpand=True))
        self.draw_steps()

        rrow = Gtk.Box(spacing=6)
        self.rec_btn = Gtk.Button(label="record")
        self.rec_btn.connect("clicked", self.on_rec)
        rrow.append(self.rec_btn)
        clr = Gtk.Button(label="clear")
        clr.connect("clicked", lambda *a: (self.slot.__setitem__("steps", []), self.draw_steps))
        rrow.append(clr)
        self.preset = Gtk.DropDown.new_from_strings(list(PRESETS))
        rrow.append(self.preset)
        ins = Gtk.Button(label="insert preset")
        ins.connect("clicked", self.on_preset)
        rrow.append(ins)
        box.append(rrow)
        box.append(Gtk.Label(label="while recording, do your action. esc stops.", xalign=0))

        ctl = Gtk.EventControllerKey()
        ctl.connect("key-pressed", self.on_key, True)
        ctl.connect("key-released", self.on_key, False)
        self.add_controller(ctl)

        self.add_button("test fire", 10)
        self.add_button("cancel", Gtk.ResponseType.CANCEL)
        self.add_button("save to pad", Gtk.ResponseType.OK)
        self.connect("response", self.on_resp)
        self.present()

    def draw_steps(self):
        s = self.slot
        self.steps_label.set_text(f"{len(s['steps'])} steps: {summarize(s)}")

    def on_preset(self, *a):
        name = dd_text(self.preset)
        if not name:
            return
        for kind, v in PRESETS[name]:
            if kind == "media":
                self.slot["steps"].append({"t": "media", "v": v, "hold": 120})
            else:
                self.slot["steps"].extend([{"t": "down", "v": v}, {"t": "up", "v": v}])
        self.draw_steps()

    def on_rec(self, *a):
        self.recording = not self.recording
        self.rec_btn.set_label("stop" if self.recording else "record")
        self.last_t = time.monotonic()
        if not self.recording:
            self.held.clear() # keys still down when recording stopped would never get an "up"

    def on_key(self, ctl, keyval, keycode, state, pressed):
        if not self.recording:
            return False
        if keyval == Gdk.KEY_Escape and pressed:
            self.on_rec()
            return True
        kind, v = keyval_step(keyval)
        if kind is None:
            return True # I don't know what this key is. I'm hungry, so I'll just eat it. 
        now = time.monotonic()
        gap = int((now - self.last_t) * 1000)
        self.last_t = now
        if pressed:
            if v in self.held: 
                return True # Key repeat. So just ignore it. Very particular edge case, but it probably will happen. I can't say the same.
            self.held[v] = now
            if gap > 25:
                self.slot["steps"].append({"t": "delay", "ms": min(gap, 2000)})
            if kind == "media":
                self.slot["steps"].append({"t": "media", "v": v, "hold": 120})
            else:
                self.slot["steps"].append({"t": "down", "v": v})
        else:
            start = self.held.pop(v, None)
            if kind == "media" or start is None:
                pass
            else:
                hold = int((now - start) * 1000)
                # This next line will sure work great
                if hold < 500 and self.slot["steps"] and self.slot["steps"][-1] == {"t": "down", "v": v} \
                        and (len(self.slot["steps"]) < 2 or self.slot["steps"][-2].get("t") != "delay"):
                    self.slot["steps"][-1] = {"t": "key", "v": v, "hold": max(hold, 20)}
                else:
                    self.slot["steps"].append({"t": "up", "v": v})
        if len(self.slot["steps"]) >= 64:
            self.slot["steps"] = self.slot["steps"][:64]
            self.on_rec()
        self.draw_steps() # i dotn know how this works rip
        return True
    def on_resp(self, dlg, resp):
        if resp == 10:
            k = self.k
            self.app.bg(lambda: self.attempt(lambda: self.app.pad.press(k), "fired"))
        elif resp == Gtk.ResponseType.OK:
            self.slot["label"] = self.label_entry.get_text()[:16] or f"K{self.k+1}"
            if len(json.dumps(self.slot)) > 3072:
                self.app.say("too many steps, max 64")
            else:
                slot, k = self.slot, self.k
                def go():
                    try:
                        with self.app.lock:
                            self.app.pad.set(k, slot)
                        self.app.slots[k] = slot
                        GLib.idle_add(self.app.refresh_keys)
                        self.app.say(f"K{k+1} saved to pad")
                    except Exception as e:
                        self.app.say(f"save failed: {e}")
                self.app.bg(go)
        self.destroy() # love

    def attempt(self, fn, ok):
        try:
            with self.app.lock:
                fn()
            self.app.say(ok)
        except Exception as e:
            self.app.say(str(e))

if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
    