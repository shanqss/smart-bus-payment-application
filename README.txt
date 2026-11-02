SMART BUS MONITORING SYSTEM - FILES AND SETUP

1) laptop_server/smart_bus_server.py
   pip install flask
   python smart_bus_server.py
   Dashboard: http://localhost:5000  (it prints the laptop IP for the NodeMCU)
   Allow Python through Windows Firewall (private network) if asked.

2) nodemcu_smart_bus/nodemcu_smart_bus.ino
   Board: NodeMCU 1.0 (ESP-12E Module)  (install the ESP8266 board package)
   Edit: WIFI_SSID, WIFI_PASSWORD, LAPTOP_IP

3) arduino_smart_bus/arduino_smart_bus.ino
   Board: Arduino Uno
   Libraries: MFRC522 (miguelbalboa), LiquidCrystal_I2C

Rules: minimum balance Rs 60, Rs 5 per stop (min Rs 5, max Rs 25), 3 routes x 10 stops.
LEDs: correct card = green blinks 2 times, wrong card / low balance = red blinks 5 times.
Button: short press = next stop, hold 2 s = previous stop.
Classroom project: never use real bank or debit card data.
