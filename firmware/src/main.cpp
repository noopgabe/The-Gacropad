// GacroPad firmware v1 with ESP32-S3-WROOM-1U, Arduino + Tiny USB. 
// I have nothing funny to say anymore

// Companion app talks newline-JSON over serial and mooches this thing on the cheek whenever it needs to change stuff.  
// The OLED is a 1.3" SH1106 128x64 I2C display.

#include <Arduino.h>
#include <Preferences.h>
#include <Wire.h>
#include <U8g2lib.h>
#include <ArduinoJson.h>
#include "USB.h"
#include "USBHIDKeyboard.h"
#include "USBHIDConsumerControl.h"

#define FW_VERSION "1.0.0"
#define NKEYS 6
#define PIN_OLED_SDA 46
#define PIN_OLED_SCL 45

static const uint8_t KEY_PINS[NKEYS] = {10, 9, 8, 7, 6, 40};
static const uint8_t DEFAULT_RAW[NKEYS] = {0x68, 0x69, 0x6A, 0x6B, 0x6C, 0x6D}; // F13-F18
static const char *SLOT_KEYS[NKEYS] = {"m0", "m1", "m2", "m3", "m4", "m5"};
#define MAX_STEPS 64
#define MAX_JSON 3072
#define DEBOUNCE_MS 25
#define LABEL_MAX 10

USBHIDKeyboard Keyboard;
USBHIDConsumerControl Consumer;
Preferences prefs;
U8G2_SH1106_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE, PIN_OLED_SCL, PIN_OLED_SDA);

struct Live {
    String clock = "--:--"; 
    String date = "";
    String l1 = "GacroPad";
    String l2 = "";
    int cpu = -1, mem = -1;
} live;
String oledMode = "labels";
int lastKey = -1;
uint32_t lastKeyAt = 0;
bool playing = false;

static bool keyValid(int k) { return k >= 0 && k < NKEYS; }

static String defaultSlot(int k) {
    char b[128];
    snprintf(b, sizeof b,
             "{\"label\":\"K%d\",\"steps\":[{\"t\":\"raw\",\"v\":%d,\"hold\":40}]}",
             k + 1, DEFAULT_RAW[k]);
    return String(b);
}

// flowers bloom in your heart
static String slotLabel(const char *json) {
    JsonDocument d;
    if (deserializeJson(d, json)) return "?";
    const char *l = d["label"] | "?";
    return String(l).substring(0, LABEL_MAX);
}

// Labels are cached: reading them from NVS every OLED frame is 6 flash lookups per 200ms.
static String slotLabels[NKEYS];

// getString's String overload has no length cap; the char* one silently drops slots too big to read.
static String loadSlot(int k) {
    String s = prefs.getString(SLOT_KEYS[k], "");
    if (!s.length()) { s = defaultSlot(k); prefs.putString(SLOT_KEYS[k], s); }
    return s;
}

// The one place a slot changes: NVS and the label cache move together, or they drift.
static void setSlot(int k, const char *json) {
    prefs.putString(SLOT_KEYS[k], json);
    slotLabels[k] = slotLabel(json);
}

static void loadLabels() {
    for (int k = 0; k < NKEYS; k++) {
        String s = loadSlot(k);
        slotLabels[k] = slotLabel(s.c_str());
    }
}

// Macro playback. The core functionality, how nice. 
static void playSteps(JsonArray steps) {
    int n = 0; 
    for (JsonObject st : steps) {
        if (++n > MAX_STEPS) break;
        const char *t = st["t"] | "";
        if (!strcmp(t, "delay")) {
            delay(st["ms"] | 50);
        } else if (!strcmp(t, "text")) {
            const char *s = st["s"] | "";
            Keyboard.print(s);
            delay(15);
        } else if (!strcmp(t, "media")) {
            uint16_t v = st["v"] | 0;
            if (v) { Consumer.press(v); delay(st["hold"] | 120); Consumer.release(); }
            delay(15);
        } else if (!strcmp(t, "raw")) {
            uint8_t v = st["v"] | 0;
            if (v) { Keyboard.pressRaw(v); delay(st["hold"] | 40); Keyboard.releaseRaw(v); }
            delay(15);
        } else if (!strcmp(t, "key")) {
            uint8_t v = st["v"] | 0;
            if (v) { Keyboard.press(v); delay(st["hold"] | 40); Keyboard.release(v); }
            delay(15);
        } else if (!strcmp(t, "down")) {
            uint8_t v = st["v"] | 0;
            if (v) Keyboard.press(v); 
        } else if (!strcmp(t, "up")) {
            uint8_t v = st["v"] | 0;
            if (v) Keyboard.release(v); // release(), not releaseRaw(): an "up" step pairs with a press()
            delay(10);
      }
    }
    Keyboard.releaseAll();
    Consumer.release();
}

static void playKey(int k) {
    if (!keyValid(k) || playing) return;
    playing = true;
    lastKey = k;
    lastKeyAt = millis();
    Serial.printf("{\"ev\":\"play\",\"key\":%d}\n", k);
    String slot = loadSlot(k);
    JsonDocument d;
    if (!deserializeJson(d, slot) && d["steps"].is<JsonArray>())
        playSteps(d["steps"].as<JsonArray>());
    playing = false;
}

// Serial stuff
static char lineBuf[MAX_JSON + 64];
static size_t lineLen = 0;

static void reply(JsonDocument &d) {
    serializeJson(d, Serial);
    Serial.println();
}

static void handleLine(const char *line) {
    JsonDocument d;
    JsonDocument out;
    if (deserializeJson(d, line)) { out["ok"] = false; out["err"] = "json"; reply(out); return; }
    const char *cmd = d["cmd"] | "";
    if (!strcmp(cmd, "ping")) {
        out ["ok"] = true; out ["proto"] = 1; out["fw"] = FW_VERSION; out["keys"] = NKEYS;
    } else if (!strcmp(cmd, "get")) {
        int k = d["key"] | -1;
        if (!keyValid(k)) { out["ok"] = false; out["err"] = "key"; }
        else {
            JsonDocument s;
            deserializeJson(s, loadSlot(k));
            out["ok"] = true; out["slot"] = s.as<JsonObjectConst>();
        }
    } else if (!strcmp(cmd, "set")) {
        int k = d["key"] | -1;
        if (!keyValid(k) || !d["slot"].is<JsonObject>()) { out["ok"] = false; out["err"] = "arg"; }
        else {
            String s;
            serializeJson(d["slot"], s);
            if (s.length() > MAX_JSON) { out["ok"] = false; out["err"] = "too big"; }
            else { setSlot(k, s.c_str()); out["ok"] = true; }
        }
    } else if (!strcmp(cmd, "press")) {
        int k = d["key"] | -1;
        if (!keyValid(k)) { out["ok"] = false; out["err"] = "key"; }
        else { out["ok"] = true; reply(out); playKey(k); return; }
    } else if (!strcmp(cmd, "oled")) {
        const char *m = d["mode"] | "";
        if (strcmp(m, "labels") && strcmp(m, "clock") && strcmp(m, "stats") && strcmp(m, "live")) {
            out["ok"] = false; out["err"] = "mode";
        } else { oledMode = m; prefs.putString("oledmode", m); out["ok"] = true; }
    } else if (!strcmp(cmd, "live")) {
        // Clipped to what the 6x10 font fits, here once, instead of per frame in every draw function.
        if (d["clock"].is<const char *>()) live.clock = String((const char *)d["clock"]).substring(0, 21);
        if (d["date"].is<const char *>()) live.date = String((const char *)d["date"]).substring(0, 20);
        if (d["l1"].is<const char *>()) live.l1 = String((const char *)d["l1"]).substring(0, 21);
        if (d["l2"].is<const char *>()) live.l2 = String((const char *)d["l2"]).substring(0, 21);
        out["ok"] = true;
    } else if (!strcmp(cmd, "stats")) {
        // Only the stats screen reads cpu/mem; writing them into live.l2 would clobber live mode's line 2.
        if (d["cpu"].is<int>()) live.cpu = d["cpu"];
        if (d["mem"].is<int>()) live.mem = d["mem"];
        out["ok"] = true;
    } else if (!strcmp(cmd, "reset")) {
        for (int k = 0; k < NKEYS; k++) { String s = defaultSlot(k); setSlot(k, s.c_str()); }
        oledMode = "labels";
        prefs.putString("oledmode", oledMode);
        out["ok"] = true;
    } else {
        out["ok"] = false; out["err"] = "cmd";
    }
    reply(out);
}

static void pollSerial() {
    while (Serial.available()) {
        char c = (char)Serial.read();
        if (c == '\n') {
            lineBuf[lineLen] = 0;
            if (lineLen) handleLine(lineBuf);
            lineLen = 0;
        } else if (c != '\r' && lineLen < sizeof(lineBuf) - 1) {
            lineBuf[lineLen++] = c;
        }
    }
}

// The buttons! 
static uint8_t stableState[NKEYS] = {HIGH, HIGH, HIGH, HIGH, HIGH, HIGH}; 
static uint8_t candState[NKEYS] = {HIGH, HIGH, HIGH, HIGH, HIGH, HIGH};
static uint32_t candSince[NKEYS] = {0};

static void pollKeys() {
    uint32_t now = millis();
    for (int k = 0; k < NKEYS; k++) {
        uint8_t r = digitalRead(KEY_PINS[k]);
        if (r == stableState[k]) { candState[k] = r; continue; }                        // bounced back, drop the candidate
        if (r != candState[k]) { candState[k] = r; candSince[k] = now; continue; }      // new candidate, restart the timer
        if (now - candSince[k] < DEBOUNCE_MS) continue;                                // candidate hasn't settled yet
        stableState[k] = r;
        if (r == LOW) {
            Serial.printf("{\"ev\":\"press\",\"key\":%d}\n", k);
            playKey(k);
        }
    }
}

// The OLED! 
static void drawLabels() {
    u8g2.setFont(u8g2_font_6x10_tf);
    u8g2.drawStr(0, 9, "GacroPad");
    u8g2.drawStr(128 - 6 * (int)live.clock.length(), 9, live.clock.c_str());
    for (int k = 0; k < NKEYS; k++) {
        int col = k % 2, row = k /2;
        int x = col * 64, y = 12 + row * 14;
        char l[LABEL_MAX + 4]; // key digits, ':', label, terminator
        snprintf(l, sizeof l, "%d:%s", k + 1, slotLabels[k].c_str());
        if (k == lastKey && millis() - lastKeyAt < 800) {
            u8g2.drawBox(x, y - 10, 63, 12);
            u8g2.setDrawColor(0);
            u8g2.drawStr(x + 2, y - 1, l);
            u8g2.setDrawColor(1);
        } else {
            u8g2.drawStr(x + 2, y - 1, l);
        }
    }
    u8g2.drawStr(0, 63, live.l2.c_str());
}

static void drawClock() {
    u8g2.setFont(u8g2_font_logisoso16_tr);
    u8g2.drawStr(22, 32, live.clock.c_str());
    u8g2.setFont(u8g2_font_6x10_tf);
    u8g2.drawStr(24, 46, live.date.c_str());
    u8g2.drawStr(0, 63, live.l1.c_str());
}

static void drawStats() {
    char b[32];
    u8g2.setFont(u8g2_font_6x10_tr);
    u8g2.drawStr(0, 12, "GacroPad stats");
    if (live.cpu < 0) strcpy(b, "CPU: idk (no sync)");
    else snprintf(b, sizeof b, "CPU: %d%% MEM: %d%%", live.cpu, live.mem);
    u8g2.drawStr(0, 28, b);
    snprintf(b, sizeof b, "up %lus", (unsigned long)(millis() / 1000));
    u8g2.drawStr(0, 42, b);
    u8g2.drawStr(0, 63, live.clock.c_str());
}

static void drawLive() {
    u8g2.setFont(u8g2_font_6x10_tr);
    u8g2.drawStr(0, 24, live.l1.c_str());
    u8g2.drawStr(0, 44, live.l2.c_str());
    u8g2.drawStr(0, 63, live.clock.c_str());
}

static void drawOled() {
    u8g2.clearBuffer();
    if (oledMode == "clock") drawClock();
    else if (oledMode == "stats") drawStats();
    else if (oledMode == "live") drawLive();
    else drawLabels();
    u8g2.sendBuffer();
}

// Setup AND loop.

static uint8_t i2cProbe() {
    Wire.begin(PIN_OLED_SDA, PIN_OLED_SCL);
    for (uint8_t a : {0x3Cu, 0x3Du}) {
        Wire.beginTransmission(a);
        if (!Wire.endTransmission()) return a;
    }
    return 0x3C;
}

void setup() {
    for (int k = 0; k < NKEYS; k++) pinMode(KEY_PINS[k], INPUT_PULLUP);
    Serial.begin(115200); // man i don't need this much serial in my bowl, too many calories
    prefs.begin("gacro", false); // that wasnt a funny joke i apologize sincerely
    oledMode = prefs.getString("oledmode", "labels");
    loadLabels();
    const uint8_t oledAddr = i2cProbe();
    u8g2.setI2CAddress(oledAddr * 2);
    u8g2.begin();
    USB.productName("The GacroPad");
    USB.manufacturerName("noopgabe");
    USB.serialNumber("42069"); // customize this to be whatever you want. default is 42069 because i am a funny
    Keyboard.begin();
    Consumer.begin();
    USB.begin();
    drawOled();
    Serial.println("{\"ev\":\"boot\",\"fw\":\"" FW_VERSION "\"}");
}

void loop() {
    pollSerial();
    pollKeys();
    static uint32_t lastDraw = 0;
    const uint32_t now = millis();
    if (now - lastDraw > 200) { lastDraw = now; drawOled(); }
    delay(2);
}

// I have served my penance. I am free. 