# Smart Bus Monitoring System

A Flask-based monitoring server for a smart bus project that tracks passengers, route movement, fare collection, and live bus status. The application runs on a laptop and exposes a simple web dashboard plus a small HTTP API that a NodeMCU/Arduino device can call to get the current stop or process RFID/card taps.

## Overview

This project simulates a smart public transport system in a classroom-friendly setup:

- Passengers are registered with a card UID, name, age, phone number, and balance.
- Each card tap is processed as either a board or exit event.
- A route contains multiple stops; the server keeps track of the current stop.
- Fare is calculated from the boarding stop to the exit stop.
- The laptop dashboard shows which passengers are on the bus, recent rides, route configuration, and pending unregistered cards.
- USB/serial or Wi-Fi connected hardware can query the server using plain text endpoints.

The main entry point is `smart_bus_server.py`.

## Features

- Passenger management with SQLite persistence
- Card registration and top-up features from the browser
- Automatic fare calculation with minimum balance checks
- Route and stop selection from the dashboard
- Live bus status summary
- CSV export of passengers and rides
- Lightweight HTTP API for hardware integration
- Built-in dashboard for monitoring and control

## Project structure

```text
laptop_server/
├── smart_bus_server.py   # Main Flask server and logic
├── smart_bus.db          # SQLite database created automatically on first run
└── README.md             # Project documentation
```

## Requirements

- Python 3.8+
- Flask
- SQLite (built into Python)

Install the main dependency:

```bash
pip install flask
```

## Configuration

The server defines several important settings at the top of `smart_bus_server.py`:

```python
DB_FILE = "smart_bus.db"
HOST = "0.0.0.0"
PORT = 5000

MIN_BALANCE = 60
FARE_PER_STOP = 5
MIN_FARE = 5
MAX_FARE = 25
```

You can change:

- database file name
- host and port
- minimum boarding balance
- fare per stop and limits
- the route names and stop lists

The route configuration is defined in the `ROUTES` dictionary.

## Running the server

From the project directory:

```bash
python smart_bus_server.py
```

When the server starts, it prints:

- local dashboard URL: `http://localhost:5000`
- the detected laptop IP address to use in the bus hardware setup

Example output:

```text
Smart Bus server is running
Dashboard on this laptop :  http://localhost:5000
Put this in the NodeMCU  :  LAPTOP_IP = "192.168.1.12"
```

The NodeMCU or embedded client should connect to the same Wi‑Fi network as the laptop.

## Web dashboard

Open the following in a browser:

```text
http://localhost:5000
```

The dashboard allows you to:

- select a route
- move to the previous/next stop or jump directly to a stop
- see passengers currently on the bus
- review recent completed rides
- register new passenger cards
- add money to passenger balances
- export passenger and ride data as CSV

## Card/tap logic

The tap flow is managed by `process_tap(uid)`.

Behavior:

- If a card UID is not registered, the ID is marked as unknown and the server responds with `NO,UNKNOWN`.
- If the passenger balance is below the minimum boarding threshold, the tap is rejected with low balance information.
- If the passenger is not on the bus, the system treats the tap as a boarding event.
- If the passenger is already on the bus, the server treats it as an exit and calculates the fare.
- The fare is charged from the passenger balance, and a ride record is stored.

The server uses a SQLite database to store passengers, rides, transactions, and settings.

## HTTP API endpoints

The following endpoints are available for hardware or custom clients.

### Current stop

```http
GET /api/stop
```

Returns the current route and stop in a plain-text format like:

```text
STOP,3,10,Airport Road
```

### Move to next stop

```http
GET /api/next
```

Increments the current stop index and returns the updated stop line.

### Move to previous stop

```http
GET /api/prev
```

Decrements the current stop index and returns the updated stop line.

### Card tap

```http
GET /api/tap?uid=A1B2C3D4
```

Processes the RFID/card tap and returns a simple status string such as:

```text
OK,BOARD,Ali,150
```

or

```text
NO,LOWBAL,Ali,40,60
```

### Bus state

```http
GET /api/state
```

Returns JSON data:

```json
{
  "route": "Route 1 - City Loop",
  "stop": "City Market",
  "stop_number": 3,
  "stop_count": 10,
  "on_bus": 5
}
```

## Database schema

The database is created with SQLite and includes these tables:

- `passengers`
  - `id`
  - `uid` (unique, card identifier)
  - `name`
  - `age`
  - `phone`
  - `balance`
  - `on_bus`
  - `board_route`
  - `board_stop`
  - `board_time`
  - `created_at`

- `rides`
  - passenger id
  - route name
  - board stop
  - exit stop
  - fare
  - board time
  - exit time

- `transactions`
  - passenger id
  - transaction kind (`fare`, `topup`, `initial`)
  - amount
  - balance after transaction
  - timestamp

- `settings`
  - app config values such as active route and stop index

## Fare behavior

Fare logic is implemented in `calc_fare(board_route, board_stop, exit_route, exit_stop)`:

- If the route changes while the passenger is riding, the system applies `MAX_FARE`.
- Fare is based on the distance between boarding and exit stops.
- The fare is clamped to a minimum and maximum value.
- The final charged fare is never greater than the passenger's available balance.

This keeps boarding and exit pricing simple but realistic for a prototype.

## Browser pages

The Flask app exposes a few main web pages:

- `/` — dashboard
- `/passengers` — passenger list and top-up controls
- `/register` — register a new card
- `/export/passengers.csv` — export passengers to CSV
- `/export/rides.csv` — export rides to CSV

## Notes for hardware integration

This laptop server is designed for a NodeMCU-based smart bus prototype. In that setup:

- the embedded device sends a card UID to `/api/tap`
- the server replies with a status message
- the laptop dashboard shows the current stop and route for the bus
- the NodeMCU can use the printed `LAPTOP_IP` value to reach the laptop over the local Wi‑Fi network

The project is intended for education and prototyping. It is not production-grade payment infrastructure and should not be used with real financial or sensitive personal data.

## Example workflow

1. Start the server.
2. Open the dashboard in the browser.
3. Register a passenger card from `/register`.
4. Add initial balance or perform a top-up.
5. Tap a registered card at the bus hardware/client.
6. The server updates the passenger status and fare records.
7. Use the route controls to move through stops and simulate the ride.

## Security and usage notes

- The application uses a hardcoded Flask secret key: `smart-bus-classroom`.
- It is intended for classroom or lab use, not public internet deployment.
- Keep the laptop and NodeMCU on a private/local network.
- Use non-sensitive sample data only.

## License

This project is provided as a classroom/educational prototype. No formal license file is included in the repository.
