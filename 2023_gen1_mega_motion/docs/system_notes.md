# System Notes

## Hardware Assumptions

- `Arduino Mega 2560`
- `6 x ESC`
- `4 x horizontal thrusters`
- `2 x vertical thrusters`
- External cameras recorded or monitored through a `DVR`

## Control Philosophy

This generation does not include any closed-loop control.
The operator sends direct movement commands, and the Mega applies a simple thruster mix.

## Why This Version Matters

- Very easy to maintain
- Good for early prototyping and bench testing
- Lower programming and wiring complexity
- Suitable when the primary goal is simply to prove basic vehicle motion

## Limitations

- No true vehicle feedback
- No stabilization
- No sensor alarms
- No operator GUI
- Any monitoring depends on systems that are external to the controller
