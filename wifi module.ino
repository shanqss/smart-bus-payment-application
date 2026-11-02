/*
  SMART BUS MONITORING SYSTEM - NodeMCU ESP8266 SKETCH  (v3)
  Board: NodeMCU 1.0 (ESP-12E Module)   (install the ESP8266 board package first)
  Libraries: ESP8266WiFi, ESP8266HTTPClient, SoftwareSerial (come with the package)
  Serial Monitor: 115200

  The NodeMCU is a Wi-Fi BRIDGE between the Uno and the laptop server:
    Uno  "TAP,<uid>"  -> GET http://LAPTOP:5000/api/tap?uid=...  -> reply line -> Uno
    Uno  "NEXT"/"PREV"-> GET /api/next or /api/prev              -> STOP line  -> Uno
    every second it asks /api/stop and sends the current stop to the Uno, so the
    LCD follows the buttons on the dashboard.

  Link: NodeMCU RX = D6 (from Uno D3 through the divider), TX = D5 (to Uno D2), GND <-> GND
*/

#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <WiFiClient.h>
#include <SoftwareSerial.h>

// =============== SETTINGS - EDIT THESE ===============
const char* WIFI_SSID     = "Manoj V";
const char* WIFI_PASSWORD = "12345678";

// The laptop's IP address. Start smart_bus_server.py: it prints this address.
const char* LAPTOP_IP   = "10.224.220.137";
const uint16_t LAPTOP_PORT = 5000;

const unsigned long HTTP_TIMEOUT_MS = 2500;
const unsigned long STOP_POLL_MS    = 1000;   // ask the laptop for the stop
const unsigned long STOP_RESEND_MS  = 5000;   // re-send the stop even if unchanged
// =====================================================

SoftwareSerial link(D6, D5);     // RX = D6 (GPIO12), TX = D5 (GPIO14)
WiFiClient wifiClient;

String rxLine = "";
String lastStopLine = "";
unsigned long lastPoll = 0, lastStopSent = 0;
bool wasConnected = false;
bool laptopWasOk = true;

// ---------------- HTTP helper ----------------
bool httpGet(const String& path, String& body) {
  if (WiFi.status() != WL_CONNECTED) return false;
  HTTPClient http;
  String url = String("http://") + LAPTOP_IP + ":" + String(LAPTOP_PORT) + path;
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(wifiClient, url)) return false;
  int code = http.GET();
  bool ok = (code == 200);
  if (ok) {
    body = http.getString();
    body.trim();
    int nl = body.indexOf('\n');
    if (nl >= 0) body = body.substring(0, nl);     // first line only
  } else {
    Serial.print("HTTP error: ");
    Serial.println(code);
  }
  http.end();
  return ok;
}

bool validLine(const String& s) {
  return s.length() > 2 && s.length() < 70 &&
         (s.startsWith("OK,") || s.startsWith("NO,") || s.startsWith("STOP,"));
}

void sendToUno(const String& line) {
  link.println(line);
  Serial.print("-> Uno: ");
  Serial.println(line);
}

// ---------------- Requests from the Uno ----------------
String cleanHex(const String& s) {
  String out = "";
  for (unsigned i = 0; i < s.length() && out.length() < 20; i++) {
    char c = toupper(s[i]);
    if ((c >= '0' && c <= '9') || (c >= 'A' && c <= 'F')) out += c;
  }
  return out;
}

void handleUnoLine(String line) {
  line.trim();
  if (line.length() == 0) return;
  Serial.print("<- Uno: ");
  Serial.println(line);
  String body;

  if (line.startsWith("TAP,")) {
    String uid = cleanHex(line.substring(4));
    if (uid.length() == 0) { sendToUno("NO,ERROR"); return; }
    if (httpGet("/api/tap?uid=" + uid, body) && validLine(body)) sendToUno(body);
    else sendToUno("NO,SERVER");
  } else if (line == "NEXT" || line == "PREV") {
    String path = (line == "NEXT") ? "/api/next" : "/api/prev";
    if (httpGet(path, body) && validLine(body)) {
      lastStopLine = body;
      lastStopSent = millis();
      sendToUno(body);
    } else {
      sendToUno("NO,SERVER");
    }
  }
}

void readUno() {
  while (link.available()) {
    char c = link.read();
    if (c == '\n') {
      handleUnoLine(rxLine);
      rxLine = "";
    } else if (c != '\r' && rxLine.length() < 80) {
      rxLine += c;
    }
  }
}

// ---------------- Keep the Uno LCD in sync ----------------
void pollStop() {
  if (millis() - lastPoll < STOP_POLL_MS) return;
  lastPoll = millis();

  String body;
  bool ok = httpGet("/api/stop", body) && body.startsWith("STOP,");
  if (!ok) {
    if (laptopWasOk) Serial.println("Laptop server not reachable (check IP, Wi-Fi, firewall)");
    laptopWasOk = false;
    return;
  }
  if (!laptopWasOk) Serial.println("Laptop server connected");
  laptopWasOk = true;

  if (body != lastStopLine || millis() - lastStopSent >= STOP_RESEND_MS) {
    lastStopLine = body;
    lastStopSent = millis();
    sendToUno(body);
  }
}

// ---------------- Arduino ----------------
void setup() {
  Serial.begin(115200);
  link.begin(9600);
  delay(200);

  Serial.println();
  Serial.print("Connecting to Wi-Fi: ");
  Serial.println(WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(500);
    Serial.print('.');
  }
  Serial.println();
}

void loop() {
  readUno();

  bool connected = (WiFi.status() == WL_CONNECTED);
  if (connected && !wasConnected) {
    Serial.print("Wi-Fi connected. NodeMCU IP: ");
    Serial.println(WiFi.localIP());
    Serial.print("Laptop server: http://");
    Serial.print(LAPTOP_IP);
    Serial.print(":");
    Serial.println(LAPTOP_PORT);
  }
  wasConnected = connected;

  if (connected) pollStop();
  yield();
}
