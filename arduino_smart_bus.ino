/*
  SMART BUS MONITORING SYSTEM - ARDUINO UNO SKETCH  (v3)
  Board: Arduino Uno          Libraries: MFRC522 (miguelbalboa), LiquidCrystal_I2C

  The Uno runs the hardware only. All money, passengers and stops are kept on the
  LAPTOP server (smart_bus_server.py). The NodeMCU is the Wi-Fi bridge.

    RFID tap  -> Uno sends  TAP,<uid>      -> NodeMCU -> laptop -> reply -> LCD + LEDs
    Button    -> Uno sends  NEXT / PREV    -> laptop changes the stop
    Laptop    -> NodeMCU -> Uno  STOP,<number>,<count>,<name>   (shown on the LCD)

  Driver button: short press = next stop, hold 2 seconds = previous stop.
  The dashboard on the laptop can also change the stop; the LCD follows it.

  Pins (same as the Picture Guide)
    LCD I2C  SDA=A4 SCL=A5       RC522  SS=D10 RST=D9 SCK=D13 MOSI=D11 MISO=D12
    Button   D4 (to GND)         Green LED D5      Red LED D6
    Buzzer   D7 (optional, off by default)
    Link     Uno RX=D2 (from NodeMCU D5), Uno TX=D3 (to NodeMCU D6 via 1k/2.2k divider)
  USB Serial (9600) is only for debugging.
*/

#include <SPI.h>
#include <MFRC522.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <SoftwareSerial.h>

// =============== SETTINGS ===============
const uint8_t PIN_BUTTON   = 4;
const uint8_t PIN_GREEN    = 5;
const uint8_t PIN_RED      = 6;
const uint8_t PIN_BUZZER   = 7;
const uint8_t PIN_RFID_SS  = 10;
const uint8_t PIN_RFID_RST = 9;
const uint8_t LINK_RX      = 2;
const uint8_t LINK_TX      = 3;

const uint8_t LCD_ADDRESS = 0x27;          // try 0x3F if the screen stays blank

// Buzzer is NOT in the Picture Guide. Set true only if you wired one on D7.
const bool USE_BUZZER        = false;
const bool BUZZER_IS_PASSIVE = false;      // false: LOW = beep (active module)
const unsigned int TONE_HZ   = 2000;       // used only for a passive buzzer

const unsigned long LONG_PRESS_MS    = 2000;   // hold = previous stop
const unsigned long DEBOUNCE_MS      = 30;
const unsigned long SAME_CARD_MS     = 1500;   // ignore the same card this long
const unsigned long REPLY_TIMEOUT_MS = 5000;   // wait for the laptop answer
const unsigned long RESULT_MS        = 2000;   // result stays on the LCD
const unsigned long BLINK_MS         = 200;    // LED on time = off time
const uint8_t GREEN_BLINKS = 2;                // approved card: green blinks 2 times
const uint8_t RED_BLINKS   = 5;                // wrong card / low balance: red blinks 5 times
const unsigned long BEEP_MS          = 150;
// ========================================

LiquidCrystal_I2C lcd(LCD_ADDRESS, 16, 2);
MFRC522 rfid(PIN_RFID_SS, PIN_RFID_RST);
SoftwareSerial link(LINK_RX, LINK_TX);

// stop information (sent by the laptop)
char stopName[17] = "";
uint8_t stopNum = 0, stopCount = 0;
bool haveStop = false;

bool waitingReply = false;
unsigned long replyDeadline = 0;
unsigned long resultUntil = 0;             // 0 = no result on screen

uint8_t blinkPin = 0;                      // LED that is blinking now
uint8_t blinkToggles = 0;                  // changes still to do
unsigned long blinkNext = 0;

uint8_t beepsLeft = 0;
bool beepIsOn = false;
unsigned long beepNext = 0;

char lastUid[21] = "";
unsigned long lastReadAt = 0;

// ---------------- LCD helpers ----------------
void printLine(uint8_t row, const char* text) {
  lcd.setCursor(0, row);
  uint8_t i = 0;
  while (text[i] && i < 16) { lcd.write(text[i]); i++; }
  while (i < 16) { lcd.write(' '); i++; }
}

void printLineF(uint8_t row, const __FlashStringHelper* text) {
  char buf[17];
  strncpy_P(buf, (const char*)text, 16);
  buf[16] = 0;
  printLine(row, buf);
}

void showHome() {
  if (!haveStop) {
    printLineF(0, F("Connecting..."));
    printLineF(1, F("Waiting laptop"));
    return;
  }
  char line[20];
  printLine(0, stopName);                                  // bus stop name
  snprintf(line, sizeof(line), "%u/%u Tap card", stopNum, stopCount);
  printLine(1, line);
}

// ---------------- LEDs (non-blocking blink) ----------------
void ledsOff() {
  digitalWrite(PIN_GREEN, LOW);
  digitalWrite(PIN_RED, LOW);
  blinkToggles = 0;
}

void startBlink(uint8_t pin, uint8_t blinks) {
  ledsOff();
  blinkPin = pin;
  digitalWrite(pin, HIGH);                 // first ON
  blinkToggles = blinks * 2 - 1;           // OFF, ON, OFF ... ends OFF
  blinkNext = millis() + BLINK_MS;
}

void updateBlink() {
  if (blinkToggles == 0 || (long)(millis() - blinkNext) < 0) return;
  digitalWrite(blinkPin, !digitalRead(blinkPin));
  blinkToggles--;
  blinkNext += BLINK_MS;
}

// ---------------- Buzzer (optional, non-blocking) ----------------
void buzzerOn() {
  if (!USE_BUZZER) return;
  if (BUZZER_IS_PASSIVE) tone(PIN_BUZZER, TONE_HZ);
  else digitalWrite(PIN_BUZZER, LOW);
}

void buzzerOff() {
  if (!USE_BUZZER) return;
  if (BUZZER_IS_PASSIVE) { noTone(PIN_BUZZER); digitalWrite(PIN_BUZZER, LOW); }
  else digitalWrite(PIN_BUZZER, HIGH);
}

void startBeeps(uint8_t count) {
  if (!USE_BUZZER) return;
  beepsLeft = count;
  beepIsOn = true;
  buzzerOn();
  beepNext = millis() + BEEP_MS;
}

void updateBeeps() {
  if (beepsLeft == 0) return;
  if ((long)(millis() - beepNext) < 0) return;
  if (beepIsOn) { buzzerOff(); beepIsOn = false; beepsLeft--; beepNext = millis() + BEEP_MS; }
  else          { buzzerOn();  beepIsOn = true;               beepNext = millis() + BEEP_MS; }
}

// ---------------- Result screens ----------------
// ok = true : green LED blinks 2 times (+ 1 beep if a buzzer is used)
// ok = false: red LED blinks 5 times   (+ 2 beeps if a buzzer is used)
void showResult(bool ok, const char* l1, const char* l2) {
  printLine(0, l1);
  printLine(1, l2);
  resultUntil = millis() + RESULT_MS;
  if (ok) { startBlink(PIN_GREEN, GREEN_BLINKS); startBeeps(1); }
  else    { startBlink(PIN_RED, RED_BLINKS);     startBeeps(2); }
}

// ---------------- Messages from the NodeMCU ----------------
void parseStop(char* line) {                 // STOP,3,10,Market
  strtok(line, ",");                         // "STOP"
  char* n = strtok(NULL, ",");
  char* c = strtok(NULL, ",");
  char* name = strtok(NULL, "\n");
  if (!n || !c || !name) return;
  stopNum = atoi(n);
  stopCount = atoi(c);
  strncpy(stopName, name, sizeof(stopName) - 1);
  stopName[sizeof(stopName) - 1] = 0;
  haveStop = true;
  if (resultUntil == 0 && !waitingReply) showHome();   // update the stop on the LCD
}

void handleReply(char* reply) {
  // OK,BOARD,name,balance
  // OK,EXIT,name,fare,balance
  // NO,UNKNOWN
  // NO,LOWBAL,name,balance,minimum
  // NO,SERVER   (laptop not reachable)
  bool serverError = (strcmp(reply, "NO,SERVER") == 0);
  if (!waitingReply && !serverError) return;   // late answer, ignore
  waitingReply = false;

  char* status = strtok(reply, ",");
  char* kind   = strtok(NULL, ",");
  char* f1     = strtok(NULL, ",");
  char* f2     = strtok(NULL, ",");
  char* f3     = strtok(NULL, ",");
  char l2[20];
  if (!status || !kind) { showResult(false, "Bad reply", "Try again"); return; }

  if (!strcmp(status, "OK") && !strcmp(kind, "BOARD") && f1 && f2) {
    snprintf(l2, sizeof(l2), "Bal: Rs %s", f2);
    showResult(true, f1, l2);
  } else if (!strcmp(status, "OK") && !strcmp(kind, "EXIT") && f1 && f2 && f3) {
    snprintf(l2, sizeof(l2), "Fare:%s Bal:%s", f2, f3);
    showResult(true, f1, l2);
  } else if (!strcmp(kind, "UNKNOWN")) {
    showResult(false, "Unknown card", "Register on PC");
  } else if (!strcmp(kind, "LOWBAL")) {
    snprintf(l2, sizeof(l2), "Bal:%s Min:%s", f2 ? f2 : "0", f3 ? f3 : "60");
    showResult(false, "Low balance", l2);
  } else if (!strcmp(kind, "SERVER")) {
    showResult(false, "Laptop offline", "Check server");
  } else {
    showResult(false, "Declined", "Try again");
  }
}

void handleLine(char* line) {
  Serial.print(F("RX: "));
  Serial.println(line);
  if (!strncmp(line, "STOP,", 5)) parseStop(line);
  else if (!strncmp(line, "OK,", 3) || !strncmp(line, "NO,", 3)) handleReply(line);
}

void readLink() {
  static char buf[72];
  static uint8_t n = 0;
  while (link.available()) {
    char c = link.read();
    if (c == '\n') {
      buf[n] = 0;
      if (n > 0) handleLine(buf);
      n = 0;
    } else if (c != '\r' && n < sizeof(buf) - 1) {
      buf[n++] = c;
    }
  }
}

// ---------------- Button ----------------
void handleButton() {
  static bool lastRaw = HIGH, stable = HIGH, longDone = false;
  static unsigned long lastChange = 0, pressStart = 0;

  bool raw = digitalRead(PIN_BUTTON);
  unsigned long now = millis();
  if (raw != lastRaw) { lastChange = now; lastRaw = raw; }

  if (now - lastChange > DEBOUNCE_MS && raw != stable) {
    stable = raw;
    if (stable == LOW) {                       // pressed
      pressStart = now;
      longDone = false;
    } else if (!longDone) {                    // short press released
      link.println(F("NEXT"));
    }
  }
  if (stable == LOW && !longDone && now - pressStart >= LONG_PRESS_MS) {
    longDone = true;                           // held 2 s
    link.println(F("PREV"));
  }
}

// ---------------- RFID ----------------
bool readCardUid(char* out) {                  // out needs 21 characters
  if (!rfid.PICC_IsNewCardPresent()) return false;
  if (!rfid.PICC_ReadCardSerial())   return false;
  uint8_t p = 0;
  for (uint8_t i = 0; i < rfid.uid.size && i < 10; i++) {
    out[p++] = "0123456789ABCDEF"[rfid.uid.uidByte[i] >> 4];
    out[p++] = "0123456789ABCDEF"[rfid.uid.uidByte[i] & 0x0F];
  }
  out[p] = 0;
  rfid.PICC_HaltA();                           // always halt the card after reading
  rfid.PCD_StopCrypto1();
  return true;
}

void sendTap(const char* uid) {
  printLineF(0, F("Checking card..."));
  printLineF(1, F("Please wait"));
  while (link.available()) link.read();        // clear old data
  link.print(F("TAP,"));
  link.println(uid);
  waitingReply = true;
  replyDeadline = millis() + REPLY_TIMEOUT_MS;
}

// ---------------- Arduino ----------------
void setup() {
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  pinMode(PIN_GREEN, OUTPUT);
  pinMode(PIN_RED, OUTPUT);
  if (USE_BUZZER) { pinMode(PIN_BUZZER, OUTPUT); buzzerOff(); }
  ledsOff();

  Serial.begin(9600);
  link.begin(9600);

  lcd.init();
  lcd.backlight();
  printLineF(0, F("  SMART BUS"));
  printLineF(1, F("  Starting..."));

  SPI.begin();
  rfid.PCD_Init();
  delay(50);

  byte version = rfid.PCD_ReadRegister(MFRC522::VersionReg);
  if (version == 0x00 || version == 0xFF) {
    printLineF(0, F("RFID not found!"));
    printLineF(1, F("Check 3.3V/wires"));
    digitalWrite(PIN_RED, HIGH);
    delay(2000);
    digitalWrite(PIN_RED, LOW);
  }
  showHome();
}

void loop() {
  updateBeeps();
  updateBlink();
  readLink();
  handleButton();

  if (waitingReply && (long)(millis() - replyDeadline) >= 0) {
    waitingReply = false;
    showResult(false, "Server busy", "Try again");
  }

  if (resultUntil != 0) {                      // a result is on screen
    if ((long)(millis() - resultUntil) >= 0) {
      resultUntil = 0;
      showHome();
    }
    return;
  }
  if (waitingReply) return;

  char uid[21];
  if (readCardUid(uid)) {
    Serial.print(F("Card UID: "));
    Serial.println(uid);
    bool sameCard = (strcmp(uid, lastUid) == 0) && (millis() - lastReadAt < SAME_CARD_MS);
    if (!sameCard) {
      strcpy(lastUid, uid);
      lastReadAt = millis();
      sendTap(uid);
    }
  }
}
