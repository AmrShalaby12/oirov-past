#include <Servo.h>

constexpr uint8_t THRUSTER_COUNT = 6;
constexpr uint8_t THRUSTER_PINS[THRUSTER_COUNT] = {2, 3, 4, 5, 6, 7};

enum ThrusterIndex : uint8_t {
  FRONT_LEFT = 0,
  FRONT_RIGHT,
  REAR_LEFT,
  REAR_RIGHT,
  VERTICAL_LEFT,
  VERTICAL_RIGHT
};

constexpr int PWM_NEUTRAL = 1500;
constexpr int PWM_MIN = 1100;
constexpr int PWM_MAX = 1900;
constexpr unsigned long COMMAND_TIMEOUT_MS = 700;

Servo thrusters[THRUSTER_COUNT];
int currentPwm[THRUSTER_COUNT] = {
  PWM_NEUTRAL, PWM_NEUTRAL, PWM_NEUTRAL,
  PWM_NEUTRAL, PWM_NEUTRAL, PWM_NEUTRAL
};

struct Axes {
  int surge = 0;
  int sway = 0;
  int heave = 0;
  int yaw = 0;
};

Axes axes;
int defaultPowerPct = 55;
unsigned long lastCommandMs = 0;

void setup() {
  Serial.begin(115200);

  for (uint8_t i = 0; i < THRUSTER_COUNT; ++i) {
    thrusters[i].attach(THRUSTER_PINS[i], PWM_MIN, PWM_MAX);
    thrusters[i].writeMicroseconds(PWM_NEUTRAL);
  }

  lastCommandMs = millis();
  printBanner();
  printHelp();
}

void loop() {
  readSerialCommands();

  if (millis() - lastCommandMs > COMMAND_TIMEOUT_MS) {
    zeroAxes();
  }

  updateThrusters();
}

void readSerialCommands() {
  static String line;

  while (Serial.available() > 0) {
    const char incoming = static_cast<char>(Serial.read());

    if (incoming == '\n' || incoming == '\r') {
      if (line.length() > 0) {
        processCommand(line);
        line = "";
      }
      continue;
    }

    if (line.length() < 64) {
      line += incoming;
    } else {
      line = "";
      Serial.println(F("ERR command too long"));
    }
  }
}

void processCommand(String command) {
  command.trim();
  command.toUpperCase();

  if (command == "F") {
    setAxes(defaultPowerPct, 0, 0, 0);
  } else if (command == "B") {
    setAxes(-defaultPowerPct, 0, 0, 0);
  } else if (command == "L") {
    setAxes(0, defaultPowerPct, 0, 0);
  } else if (command == "R") {
    setAxes(0, -defaultPowerPct, 0, 0);
  } else if (command == "U") {
    setAxes(0, 0, defaultPowerPct, 0);
  } else if (command == "D") {
    setAxes(0, 0, -defaultPowerPct, 0);
  } else if (command == "Q") {
    setAxes(0, 0, 0, defaultPowerPct);
  } else if (command == "E") {
    setAxes(0, 0, 0, -defaultPowerPct);
  } else if (command == "S") {
    stopAll();
  } else if (command == "HELP") {
    printHelp();
  } else if (command.startsWith("P")) {
    setDefaultPower(command.substring(1));
  } else if (command.startsWith("MOVE")) {
    parseMoveCommand(command);
  } else {
    Serial.print(F("ERR unknown command: "));
    Serial.println(command);
  }
}

void setDefaultPower(const String &payload) {
  const int requested = payload.toInt();
  defaultPowerPct = constrain(requested, 10, 100);
  Serial.print(F("OK default power "));
  Serial.print(defaultPowerPct);
  Serial.println(F("%"));
  lastCommandMs = millis();
}

void parseMoveCommand(const String &command) {
  const int separatorIndex = command.indexOf(' ');
  if (separatorIndex < 0) {
    Serial.println(F("ERR MOVE format is: MOVE surge,sway,heave,yaw"));
    return;
  }

  const String payload = command.substring(separatorIndex + 1);
  int values[4] = {0, 0, 0, 0};

  if (!parseCsvIntegers(payload, values, 4)) {
    Serial.println(F("ERR invalid MOVE payload"));
    return;
  }

  setAxes(values[0], values[1], values[2], values[3]);
}

bool parseCsvIntegers(String payload, int *values, uint8_t expectedCount) {
  payload.trim();

  int startIndex = 0;
  for (uint8_t i = 0; i < expectedCount; ++i) {
    const int endIndex = (i == expectedCount - 1) ? -1 : payload.indexOf(',', startIndex);

    if (endIndex == -1 && i != expectedCount - 1) {
      return false;
    }

    const String token = (endIndex == -1)
      ? payload.substring(startIndex)
      : payload.substring(startIndex, endIndex);

    if (token.length() == 0) {
      return false;
    }

    values[i] = token.toInt();
    startIndex = endIndex + 1;
  }

  return true;
}

void setAxes(int surge, int sway, int heave, int yaw) {
  axes.surge = constrain(surge, -100, 100);
  axes.sway = constrain(sway, -100, 100);
  axes.heave = constrain(heave, -100, 100);
  axes.yaw = constrain(yaw, -100, 100);
  lastCommandMs = millis();

  Serial.print(F("OK axes "));
  Serial.print(axes.surge);
  Serial.print(',');
  Serial.print(axes.sway);
  Serial.print(',');
  Serial.print(axes.heave);
  Serial.print(',');
  Serial.println(axes.yaw);
}

void zeroAxes() {
  axes.surge = 0;
  axes.sway = 0;
  axes.heave = 0;
  axes.yaw = 0;
}

void stopAll() {
  zeroAxes();
  lastCommandMs = millis();
  Serial.println(F("OK stop"));
}

float clampUnit(float value) {
  if (value > 1.0f) {
    return 1.0f;
  }
  if (value < -1.0f) {
    return -1.0f;
  }
  return value;
}

int mapUnitToPwm(float value) {
  const float limited = clampUnit(value);
  return PWM_NEUTRAL + static_cast<int>(limited * static_cast<float>(PWM_MAX - PWM_NEUTRAL));
}

void updateThrusters() {
  const float surge = axes.surge / 100.0f;
  const float sway = axes.sway / 100.0f;
  const float heave = axes.heave / 100.0f;
  const float yaw = axes.yaw / 100.0f;

  float mix[THRUSTER_COUNT];
  mix[FRONT_LEFT] = surge + sway + yaw;
  mix[FRONT_RIGHT] = surge - sway - yaw;
  mix[REAR_LEFT] = surge - sway + yaw;
  mix[REAR_RIGHT] = surge + sway - yaw;
  mix[VERTICAL_LEFT] = heave;
  mix[VERTICAL_RIGHT] = heave;

  for (uint8_t i = 0; i < THRUSTER_COUNT; ++i) {
    currentPwm[i] = mapUnitToPwm(mix[i]);
    thrusters[i].writeMicroseconds(currentPwm[i]);
  }
}

void printBanner() {
  Serial.println();
  Serial.println(F("OI ROV 2023 Gen 1"));
  Serial.println(F("Arduino Mega basic motion controller"));
}

void printHelp() {
  Serial.println(F("Commands:"));
  Serial.println(F("  F B L R U D Q E S"));
  Serial.println(F("  P60"));
  Serial.println(F("  MOVE surge,sway,heave,yaw"));
  Serial.println(F("  HELP"));
}

