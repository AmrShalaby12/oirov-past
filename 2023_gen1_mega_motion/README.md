# 2023 Gen 1 - Arduino Mega Basic Motion

This project represents the first, simplest ROV generation:

- Main controller: `Arduino Mega`
- Function: motion only
- Cameras: independent `DVR` system
- No dedicated GUI
- No advanced sensors or telemetry

## Operating Concept

The topside computer or operator controller sends simple `Serial` commands.
The `Arduino Mega` converts those commands directly into PWM output for the ESCs.

## Files

- `firmware/mega_motion_controller/mega_motion_controller.ino`
- `docs/system_notes.md`

## Serial Protocol

- `F`: forward
- `B`: backward
- `L`: strafe left
- `R`: strafe right
- `U`: heave up
- `D`: heave down
- `Q`: yaw left
- `E`: yaw right
- `S`: stop all
- `P60`: change default power to `60%`
- `MOVE 40,0,0,15`: `surge,sway,heave,yaw`

## Upload

1. Open the `.ino` file in Arduino IDE.
2. Select `Arduino Mega 2560`.
3. Review the pin assignment before uploading.
4. Upload the firmware.

## Notes

- The firmware enables a short command timeout fail-safe.
- Any different wiring arrangement only requires updating the `THRUSTER_PINS` array.
- Cameras are not part of this firmware because they are handled independently through the `DVR` path.
