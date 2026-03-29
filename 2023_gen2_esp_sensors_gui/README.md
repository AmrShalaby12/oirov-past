# 2023 Gen 2 - ESP32 + IMU + Leak + PyQt5 GUI

This project is the upgraded second 2023 generation:

- Controller: `ESP32`
- Sensors:
  - `IMU (MPU6050)`
  - `Leak Sensor`
- Operator interface: `PyQt5`
- GUI-to-vehicle communication: `Wi-Fi TCP`

## What Changed From Gen 1

- The system moves from `Serial-only` control to a networked control model.
- Basic telemetry is introduced.
- A dedicated operator screen shows sensor state, thruster status, and simple camera panels.
- The operator gains direct visibility into vehicle condition instead of relying only on a DVR workflow.

## Project Layout

- `firmware/esp32_rov_v2/esp32_rov_v2.ino`
- `gui/main.py`
- `gui/requirements.txt`

## GUI Features

- ESP32 connection over IP and port
- Manual control for `surge / sway / heave / yaw`
- `battery / leak / roll / pitch / yaw` indicators
- Live thruster mix visualization
- Two optional camera panels through OpenCV
- Keyboard shortcuts for rapid control

## Keyboard Map

- `W/S`: surge
- `A/D`: sway
- `R/F`: heave
- `Q/E`: yaw
- `Space`: stop all

## Setup

### Firmware

1. Install the required Arduino IDE libraries:
   - `ArduinoJson`
   - `Adafruit MPU6050`
   - `Adafruit Unified Sensor`
   - `ESP32Servo`
2. Open the firmware file.
3. Adjust network settings and pins if needed.
4. Upload the code to the ESP32.

### GUI

```powershell
cd gui
py -m pip install -r requirements.txt
py main.py
```

## Telemetry

The firmware transmits periodic JSON telemetry containing:

- `battery_v`
- `leak`
- `imu.roll`
- `imu.pitch`
- `imu.yaw`
- `thrusters[]`
