// GacroPad firmware v1 with ESP32-S3-WROOM-1U, Arduino + Tiny USB. 
// I have nothing funny to say anymore

#include <Arduino.h>
#include <Preferences.h>
#include <Wire.h>
#include <U8g2lib.h>
#include "USB.h"
#include "USBHIDKeyboard.h"
#include "USBHIDConsumerControl.h"

#define FW_VERSION "1.0.0"
#define NKEYS 6
#define PIN_OLED_SDA 46
#define PIN_OLED_SCL 45

static const uint8_t KEY_PINS[NKEYS] = {10, 9, 8, 7, 6, 40};
static const uint8_t DEFAULT_RAW[NKEYS] = {0x68, 0x69, 0x6A, 0x6B, 0x6C, 0x6D}; // F13-F18
#define MAX_STEPS 64
#define MAX_JSON 3072
#define DEBOUNCE_MS 25

USBHIDKeyboard Keyboard;
USBHIDConsumerControl Consumer;
Preferences prefs;
U8G2_SH1106_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE, PIN_OLED_SCL, PIN_OLED_SDA);

struct Live {
    String clock = "--:--"; // This is my first time in CPP so I don't know what I'm doing
    String date = "";
    String l1 = "GacroPad";
    String l2 = "";
    int cpu = -1, mem = -1;
} live;
String oledMode = "labels";
int lastKey = -1;
uint32_t lastKeyAt = 0;
bool playing = false;
uint8_t oledAddr = 0x3C;

static bool keyValid(int k) { return k >= 0 && k < NKEYS; }
static String slotName(int k) { return "m" + String(k); } // flowers bloom in your heart 

static String defaultSlot(int k) {
    char b[128];
    snprintf(b, sizeof b, 
             "{\"label\":"K%d\",\"steps\":[{\"t\":\"raw\",\"v\":%d,\"hold\":40}]}"
             k + 1, DEFAULT_RAW[k]);
    return String(b);
}

static String loadSlot(int k) {
    String s = prefs.getString(slotName(K).c_str(), "");
    if (!s.length()) { s = defaultSlot(k); prefs.putString(slotName(k).c_str(), s); }
    return s;
}

static String slotLabel(const String &slotJson) {
    JsonDocument d;
    if (deserializeJson(d, slotJson)) return "?";
    const char *l = d["label"] | "?";
    return String(l).substring(0, 10);
}

// Macro playback. The core functionality, how nice. 
static void playSteps(JsonArry steps) {
    int n = 0; 
    for (JsonObject st : steps) {
        if (++n > MAX_STEPS) break;
        const char *t = st["t"] | "";
        if (!strcmp(t, "delay")) {
            delay(st["ms" | 50]);
        } else if (!strcmp(t, "text")) {
            const char *s = st["s"] | "";
            Keyboard.print(s);
            delay(15);
        } else if (!strcmp(t, "media")) {
            uint16_t v = st["v" | 0];
            if (v) { Consumer.press(v); delay(st["hold"] | 120); Consumer.release(); }
            delay(15);
        } else if (!strcmp(t, "raw")) {
            uint8_t v = st["v"] | 0;
            if (v) { Keyboard.pressRaw(v); delay(st["hold"] | 40); Keyboard.releaseRaw(v); }
            delay(15);
        } else if (!strcmp(t, "key")) {
            uint8_t v = st["v"] | 0;
            if (v) { Keyboard.press(v); delay(st["hold"] | 40); Keyboard.release(); }
            delay(15);
        } else if (!strcmp(t, "down")) {
            uint8_t v = st["v"] | 0;
            if (v) Keyboard.press(v); 
        } else if (!strcmp(t, "up")) {
            uint8_t v = st["v"] | 0;
            if (v) Keyboard.releaseRaw(v);
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
    Serial.printf("{\"ev\":\"play\",\"key\"%d}\n", k);
    JsonDocument d;
    if (!deserializeJson(d, loadSlot(k)) && d["steps"].is<JsonArray>())
        playSteps(d["steps"].as<JsonArry>());
    playing = false;
}

// Serial stuff
static char lineBuf[MAX_JSON + 64];
static size_t lineLen = 0;

static void reply(JsonDocument &d) {
    serializeJson(d, Serial);
    Serial.println();
}

static void handleLine(const String &line) {
    JsonDocument d;
    JsonDocument out;
}