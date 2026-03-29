# 2025 Gen 3 - ESP32 + PID + Pressure + Vision

This is the strongest generation in the series:

- Controller: `ESP32`
- Sensors:
  - `IMU`
  - `Leak Sensor`
  - `Pressure Sensor`
- Control features:
  - `Manual mode`
  - `Depth hold`
  - `Heading hold`
  - `PID tuning`
- Vision features:
  - camera streaming panels
  - object detection pipeline
- GUI:
  - a larger `PyQt5` interface with richer status panels and live plots

## What Changed From Gen 2

- A `Pressure Sensor` is added for depth estimation.
- `PID` control is introduced for depth and heading.
- A dedicated PID tuning panel is added directly into the GUI.
- A vision pipeline is added for detection overlays on the camera streams.
- The interface is split into dedicated mission, sensor, PID, and vision tabs.

## Project Layout

- `firmware/esp32_rov_v3/esp32_rov_v3.ino`
- `gui/main.py`
- `gui/requirements.txt`
- `gui/models/README.md`

## GUI Highlights

- A mission-control-inspired visual theme
- Mission panel with:
  - manual axes
  - assist mode toggles
  - thruster outputs
  - active alerts
- Sensors panel with:
  - live values
  - depth / pressure / battery / heading plots
- PID panel with:
  - depth and heading gains
  - setpoints
  - apply buttons
- Vision panel with:
  - 3 camera panels
  - detection counts
  - inference status

## Setup

### Firmware

Install the following Arduino IDE libraries:

- `ArduinoJson`
- `Adafruit MPU6050`
- `Adafruit Unified Sensor`
- `ESP32Servo`

Then open the firmware file and review `Wi-Fi`, `pins`, and `sensor calibration constants`.

### GUI

```powershell
cd gui
py -m pip install -r requirements.txt
py main.py
```

## Object Detection

- If you place an `ONNX` model inside `gui/models/`, the system will switch into inference mode.
- If no model is present, the GUI will continue to run in `demo / fallback overlay mode`.
- More details are provided in `gui/models/README.md`.

## Safety Notes

- Do not test PID on a real vehicle without proper output limits and calibration.
- Make sure the pressure reading is valid before relying on depth hold.
- Any leak sensor fault should disable assist mode or force the system into a safe state.
