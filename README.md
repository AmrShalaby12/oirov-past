# OI ROV Past Generations

This repository contains three separate ROV project generations reconstructed from the requirements you provided:

1. `2023 Gen 1`: a very basic motion-only ROV built around `Arduino Mega`, with surveillance cameras handled externally through a `DVR`.
2. `2023 Gen 2`: the same core concept upgraded to `ESP32`, adding an `IMU`, a `Leak Sensor`, and a `PyQt5` control GUI.
3. `2025 Gen 3`: an advanced `ESP32` generation with `PID control`, a `Pressure Sensor`, a stronger `PyQt5` GUI, and an `Object Detection` pipeline for the camera feeds.

Important note:
This repository is a structured engineering reconstruction based on your current description, not a literal archive dump of historical source files. The code is organized to be practical, readable, and easy to adapt, especially for pin mapping, sensor calibration, and deployment details.

## Structure

```text
oirov-past/
|-- 2023_gen1_mega_motion/
|   |-- README.md
|   |-- docs/system_notes.md
|   `-- firmware/mega_motion_controller/mega_motion_controller.ino
|-- 2023_gen2_esp_sensors_gui/
|   |-- README.md
|   |-- firmware/esp32_rov_v2/esp32_rov_v2.ino
|   `-- gui/
|       |-- main.py
|       `-- requirements.txt
|-- 2025_gen3_esp_pid_vision/
|   |-- README.md
|   |-- firmware/esp32_rov_v3/esp32_rov_v3.ino
|   `-- gui/
|       |-- main.py
|       |-- models/README.md
|       `-- requirements.txt
`-- README.md
```

## Evolution Summary

| Area | 2023 Gen 1 | 2023 Gen 2 | 2025 Gen 3 |
| --- | --- | --- | --- |
| Main controller | Arduino Mega | ESP32 | ESP32 |
| Control scope | Basic motion only | Motion + telemetry + alarms | Motion + closed-loop assist + advanced telemetry |
| Sensors | None in the control stack | IMU + Leak | IMU + Leak + Pressure |
| GUI | No dedicated GUI | PyQt5 operator station | Advanced PyQt5 mission console |
| Camera system | External DVR | GUI camera panels | GUI camera panels + object detection pipeline |
| Stability logic | Open-loop | Open-loop with monitoring | PID for depth and heading hold |
| Communications | Serial commands | Wi-Fi TCP JSON | Wi-Fi TCP JSON with PID tuning messages |
| Intended role | Minimum viable pilot rig | Sensor-aware pilot console | Competition-ready assist/control stack |

## Detailed Differences

### 1. 2023 Gen 1

- Goal: move the ROV with the simplest possible control stack.
- Architecture: `Arduino Mega` receives direct `Serial` commands and mixes them into thruster outputs.
- Cameras are completely outside the control layer and handled through a `DVR`.
- No telemetry, no sensor fusion, and no dedicated GUI.
- Best suited as an initial movement prototype or bench-test platform.

### 2. 2023 Gen 2

- The controller is upgraded to `ESP32` for easier networking and a cleaner operator workflow.
- An `IMU` is added for attitude feedback.
- A `Leak Sensor` is added for direct alarm reporting inside the GUI.
- A `PyQt5 GUI` is introduced for:
  - network connection
  - manual axis control
  - telemetry display
  - thruster status indicators
  - simple camera panels
- Control is still primarily open-loop, but operational awareness is significantly better than Gen 1.

### 3. 2025 Gen 3

- The controller remains in the `ESP32` family, but the control stack is substantially upgraded.
- A `Pressure Sensor` is added to estimate depth.
- `PID` control is enabled for depth hold and heading hold.
- The `PyQt5` GUI is expanded into a mission-style console with:
  - Mission tab
  - Sensors tab
  - PID tuning tab
  - Vision tab
- An `Object Detection` pipeline is added to the camera feeds:
  - `YOLO ONNX` is supported when a model file is present
  - a fallback demo overlay mode is included when no model is available

## Communication Model

### Gen 1

- Control interface: `Serial @ 115200`
- Quick commands include:
  - `F`, `B`, `L`, `R`, `U`, `D`, `Q`, `E`, `S`
  - composite command: `MOVE surge,sway,heave,yaw`

### Gen 2 / Gen 3

- The GUI communicates with the `ESP32` over `TCP sockets` using `JSON lines`
- Every message is newline-terminated
- Example messages:

```json
{"type":"command","mode":"manual","axes":{"surge":0.6,"sway":0.0,"heave":0.0,"yaw":0.15}}
{"type":"pid","axis":"depth","kp":1.2,"ki":0.08,"kd":0.18}
{"type":"stop"}
```

## Recommended Workflow

1. Flash the firmware for the target generation onto the correct board.
2. Adjust pin mapping and Wi-Fi settings if your real hardware layout differs.
3. Launch the `PyQt5` GUI for Gen 2 or Gen 3 from the corresponding `gui` directory.
4. Validate connection and telemetry before driving live ESCs and thrusters.
5. In Gen 3, calibrate the pressure sensor and PID constants before any in-water test.

## Running The GUIs

### 2023 Gen 2 GUI

```powershell
cd 2023_gen2_esp_sensors_gui\gui
py -m pip install -r requirements.txt
py main.py
```

### 2025 Gen 3 GUI

```powershell
cd 2025_gen3_esp_pid_vision\gui
py -m pip install -r requirements.txt
py main.py
```

## Notes For GitHub Delivery

- Each generation is organized as a self-contained project folder.
- The second project is intentionally kept as `2023 Gen 2` to match your current naming request.
- The object detection model itself is not bundled into the repository to avoid unnecessary repository weight; setup instructions are included in `models/README.md`.
