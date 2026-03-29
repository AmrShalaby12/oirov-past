#include <WiFi.h>
#include <Wire.h>
#include <ArduinoJson.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <ESP32Servo.h>

constexpr bool USE_STATION_MODE = false;
const char *STA_SSID = "YOUR_WIFI_NAME";
const char *STA_PASSWORD = "YOUR_WIFI_PASSWORD";

const char *AP_SSID = "OI_ROV_2025_GEN3";
const char *AP_PASSWORD = "rov2025v3";
constexpr uint16_t CONTROL_PORT = 9000;

constexpr uint8_t THRUSTER_COUNT = 6;
constexpr uint8_t THRUSTER_PINS[THRUSTER_COUNT] = {13, 12, 14, 27, 26, 25};
constexpr uint8_t LEAK_SENSOR_PIN = 33;
constexpr uint8_t BATTERY_SENSOR_PIN = 34;
constexpr uint8_t PRESSURE_SENSOR_PIN = 35;

constexpr int PWM_NEUTRAL = 1500;
constexpr int PWM_MIN = 1100;
constexpr int PWM_MAX = 1900;
constexpr unsigned long COMMAND_TIMEOUT_MS = 1200;
constexpr unsigned long TELEMETRY_INTERVAL_MS = 100;

constexpr float ADC_REFERENCE_VOLTAGE = 3.3f;
constexpr float BATTERY_DIVIDER_RATIO = 5.7f;
constexpr float PRESSURE_DIVIDER_RATIO = 1.0f;
constexpr float PRESSURE_SENSOR_MIN_V = 0.5f;
constexpr float PRESSURE_SENSOR_MAX_V = 2.9f;
constexpr float PRESSURE_SENSOR_MIN_KPA = 101.325f;
constexpr float PRESSURE_SENSOR_MAX_KPA = 400.0f;
constexpr float ATMOSPHERIC_PRESSURE_KPA = 101.325f;
constexpr float WATER_DENSITY_KG_M3 = 997.0f;
constexpr float GRAVITY_M_S2 = 9.80665f;
constexpr float RAD_TO_DEGREE = 57.2957795f;

enum ThrusterIndex : uint8_t {
  FRONT_LEFT = 0,
  FRONT_RIGHT,
  REAR_LEFT,
  REAR_RIGHT,
  VERTICAL_LEFT,
  VERTICAL_RIGHT
};

struct MotionCommand {
  float surge = 0.0f;
  float sway = 0.0f;
  float heave = 0.0f;
  float yaw = 0.0f;
  bool holdDepth = false;
  bool holdHeading = false;
  float depthSetpoint = 0.0f;
  float headingSetpoint = 0.0f;
};

struct PIDController {
  float kp = 0.0f;
  float ki = 0.0f;
  float kd = 0.0f;
  float integral = 0.0f;
  float previousError = 0.0f;
  float lastError = 0.0f;
  float lastOutput = 0.0f;
  float outputMin = -1.0f;
  float outputMax = 1.0f;

  void reset() {
    integral = 0.0f;
    previousError = 0.0f;
    lastError = 0.0f;
    lastOutput = 0.0f;
  }

  float compute(float error, float dt) {
    if (dt <= 0.0f) {
      return lastOutput;
    }

    integral += error * dt;
    const float derivative = (error - previousError) / dt;

    float output = (kp * error) + (ki * integral) + (kd * derivative);

    if (output > outputMax) {
      output = outputMax;
      integral -= error * dt * 0.25f;
    } else if (output < outputMin) {
      output = outputMin;
      integral -= error * dt * 0.25f;
    }

    previousError = error;
    lastError = error;
    lastOutput = output;
    return output;
  }
};

WiFiServer server(CONTROL_PORT);
WiFiClient client;
String receiveBuffer;

Adafruit_MPU6050 mpu;
bool imuOnline = false;
Servo thrusters[THRUSTER_COUNT];

MotionCommand command;
PIDController depthPid;
PIDController headingPid;
float thrusterMix[THRUSTER_COUNT] = {0, 0, 0, 0, 0, 0};

float rollDeg = 0.0f;
float pitchDeg = 0.0f;
float yawDeg = 0.0f;
float imuTemperatureC = 0.0f;
float batteryVoltage = 0.0f;
float pressureKpa = ATMOSPHERIC_PRESSURE_KPA;
float depthMeters = 0.0f;
bool leakDetected = false;

unsigned long lastCommandMs = 0;
unsigned long lastTelemetryMs = 0;
unsigned long lastLoopMs = 0;

float clampUnit(float value) {
  if (value > 1.0f) {
    return 1.0f;
  }
  if (value < -1.0f) {
    return -1.0f;
  }
  return value;
}

float wrapAngle180(float angle) {
  while (angle > 180.0f) {
    angle -= 360.0f;
  }
  while (angle < -180.0f) {
    angle += 360.0f;
  }
  return angle;
}

float readBatteryVoltage() {
  const int raw = analogRead(BATTERY_SENSOR_PIN);
  const float sensedVoltage = (static_cast<float>(raw) / 4095.0f) * ADC_REFERENCE_VOLTAGE;
  return sensedVoltage * BATTERY_DIVIDER_RATIO;
}

float readPressureKpa() {
  const int raw = analogRead(PRESSURE_SENSOR_PIN);
  const float adcVoltage = (static_cast<float>(raw) / 4095.0f) * ADC_REFERENCE_VOLTAGE;
  const float sensorVoltage = adcVoltage * PRESSURE_DIVIDER_RATIO;
  const float normalized = constrain(
    (sensorVoltage - PRESSURE_SENSOR_MIN_V) /
      (PRESSURE_SENSOR_MAX_V - PRESSURE_SENSOR_MIN_V),
    0.0f,
    1.0f
  );

  return PRESSURE_SENSOR_MIN_KPA +
         normalized * (PRESSURE_SENSOR_MAX_KPA - PRESSURE_SENSOR_MIN_KPA);
}

float pressureToDepthMeters(float absolutePressureKpa) {
  const float gaugePressureKpa = max(0.0f, absolutePressureKpa - ATMOSPHERIC_PRESSURE_KPA);
  const float gaugePressurePa = gaugePressureKpa * 1000.0f;
  return gaugePressurePa / (WATER_DENSITY_KG_M3 * GRAVITY_M_S2);
}

int pwmFromUnit(float value) {
  return PWM_NEUTRAL + static_cast<int>(clampUnit(value) * static_cast<float>(PWM_MAX - PWM_NEUTRAL));
}

void writeThrusters() {
  for (uint8_t i = 0; i < THRUSTER_COUNT; ++i) {
    thrusters[i].writeMicroseconds(pwmFromUnit(thrusterMix[i]));
  }
}

void zeroManualAxes() {
  command.surge = 0.0f;
  command.sway = 0.0f;
  command.heave = 0.0f;
  command.yaw = 0.0f;
}

void setupWifi() {
  WiFi.mode(WIFI_AP_STA);

  bool connectedToSta = false;
  if (USE_STATION_MODE) {
    WiFi.begin(STA_SSID, STA_PASSWORD);
    const unsigned long startWait = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - startWait < 8000) {
      delay(250);
    }
    connectedToSta = WiFi.status() == WL_CONNECTED;
  }

  if (!connectedToSta) {
    WiFi.softAP(AP_SSID, AP_PASSWORD);
    Serial.print(F("AP IP: "));
    Serial.println(WiFi.softAPIP());
  } else {
    Serial.print(F("STA IP: "));
    Serial.println(WiFi.localIP());
  }

  server.begin();
  server.setNoDelay(true);
}

void setupThrusters() {
  for (uint8_t i = 0; i < THRUSTER_COUNT; ++i) {
    thrusters[i].setPeriodHertz(50);
    thrusters[i].attach(THRUSTER_PINS[i], PWM_MIN, PWM_MAX);
    thrusters[i].writeMicroseconds(PWM_NEUTRAL);
  }
}

void setupImu() {
  imuOnline = mpu.begin();
  if (imuOnline) {
    mpu.setAccelerometerRange(MPU6050_RANGE_8_G);
    mpu.setGyroRange(MPU6050_RANGE_500_DEG);
    mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
  }
}

void setupPidDefaults() {
  depthPid.kp = 1.20f;
  depthPid.ki = 0.08f;
  depthPid.kd = 0.18f;
  depthPid.outputMin = -0.65f;
  depthPid.outputMax = 0.65f;

  headingPid.kp = 0.035f;
  headingPid.ki = 0.000f;
  headingPid.kd = 0.015f;
  headingPid.outputMin = -0.45f;
  headingPid.outputMax = 0.45f;
}

void setup() {
  Serial.begin(115200);
  delay(250);

  Wire.begin();
  analogReadResolution(12);
  pinMode(LEAK_SENSOR_PIN, INPUT_PULLUP);

  setupThrusters();
  setupImu();
  setupPidDefaults();
  setupWifi();

  lastCommandMs = millis();
  lastLoopMs = millis();

  Serial.println(F("OI ROV 2025 Gen 3 ready"));
}

void acceptClient() {
  if (client && client.connected()) {
    return;
  }

  WiFiClient incoming = server.available();
  if (incoming) {
    if (client) {
      client.stop();
    }
    client = incoming;
    client.setNoDelay(true);
    receiveBuffer = "";
    Serial.println(F("Operator connected"));
  }
}

void applyPidUpdate(const String &axis, JsonDocument &document) {
  PIDController *controller = nullptr;

  if (axis == "depth") {
    controller = &depthPid;
  } else if (axis == "heading") {
    controller = &headingPid;
  } else {
    return;
  }

  controller->kp = document["kp"] | controller->kp;
  controller->ki = document["ki"] | controller->ki;
  controller->kd = document["kd"] | controller->kd;
  controller->reset();
}

void processCommandLine(const String &line) {
  StaticJsonDocument<512> document;
  DeserializationError error = deserializeJson(document, line);
  if (error) {
    Serial.println(F("Invalid JSON command"));
    return;
  }

  const char *type = document["type"] | "command";

  if (strcmp(type, "stop") == 0) {
    zeroManualAxes();
    command.holdDepth = false;
    command.holdHeading = false;
    depthPid.reset();
    headingPid.reset();
    lastCommandMs = millis();
    return;
  }

  if (strcmp(type, "pid") == 0) {
    const String axis = document["axis"] | "";
    applyPidUpdate(axis, document);
    return;
  }

  if (strcmp(type, "command") != 0) {
    return;
  }

  JsonObject axes = document["axes"].as<JsonObject>();
  command.surge = clampUnit(axes["surge"] | 0.0f);
  command.sway = clampUnit(axes["sway"] | 0.0f);
  command.heave = clampUnit(axes["heave"] | 0.0f);
  command.yaw = clampUnit(axes["yaw"] | 0.0f);

  command.holdDepth = document["holdDepth"] | command.holdDepth;
  command.holdHeading = document["holdHeading"] | command.holdHeading;
  command.depthSetpoint = document["depthSetpoint"] | command.depthSetpoint;
  command.headingSetpoint = document["headingSetpoint"] | command.headingSetpoint;

  if (document["captureDepthSetpoint"] | false) {
    command.depthSetpoint = depthMeters;
  }
  if (document["captureHeadingSetpoint"] | false) {
    command.headingSetpoint = yawDeg;
  }

  lastCommandMs = millis();
}

void readClientCommands() {
  if (!(client && client.connected())) {
    return;
  }

  while (client.available()) {
    const char incoming = static_cast<char>(client.read());

    if (incoming == '\n') {
      if (receiveBuffer.length() > 0) {
        processCommandLine(receiveBuffer);
        receiveBuffer = "";
      }
    } else if (incoming != '\r') {
      if (receiveBuffer.length() < 320) {
        receiveBuffer += incoming;
      } else {
        receiveBuffer = "";
      }
    }
  }
}

void updateSensors(float dt) {
  leakDetected = digitalRead(LEAK_SENSOR_PIN) == LOW;
  batteryVoltage = readBatteryVoltage();
  pressureKpa = readPressureKpa();
  depthMeters = pressureToDepthMeters(pressureKpa);

  if (!imuOnline) {
    return;
  }

  sensors_event_t accel;
  sensors_event_t gyro;
  sensors_event_t temp;

  mpu.getEvent(&accel, &gyro, &temp);

  rollDeg = atan2(accel.acceleration.y, accel.acceleration.z) * RAD_TO_DEGREE;
  pitchDeg = atan2(-accel.acceleration.x,
                   sqrt(accel.acceleration.y * accel.acceleration.y +
                        accel.acceleration.z * accel.acceleration.z)) * RAD_TO_DEGREE;
  yawDeg = wrapAngle180(yawDeg + gyro.gyro.z * dt * RAD_TO_DEGREE);
  imuTemperatureC = temp.temperature;
}

void updateControlLoop(float dt) {
  if (leakDetected) {
    command.holdDepth = false;
    command.holdHeading = false;
    depthPid.reset();
    headingPid.reset();
  }

  float automaticHeave = 0.0f;
  float automaticYaw = 0.0f;

  if (command.holdDepth) {
    const float depthError = command.depthSetpoint - depthMeters;
    automaticHeave = depthPid.compute(depthError, dt);
  } else {
    depthPid.reset();
  }

  if (command.holdHeading) {
    const float headingError = wrapAngle180(command.headingSetpoint - yawDeg);
    automaticYaw = headingPid.compute(headingError, dt);
  } else {
    headingPid.reset();
  }

  const float heaveCommand = clampUnit(command.heave + automaticHeave);
  const float yawCommand = clampUnit(command.yaw + automaticYaw);

  thrusterMix[FRONT_LEFT] = clampUnit(command.surge + command.sway + yawCommand);
  thrusterMix[FRONT_RIGHT] = clampUnit(command.surge - command.sway - yawCommand);
  thrusterMix[REAR_LEFT] = clampUnit(command.surge - command.sway + yawCommand);
  thrusterMix[REAR_RIGHT] = clampUnit(command.surge + command.sway - yawCommand);
  thrusterMix[VERTICAL_LEFT] = clampUnit(heaveCommand);
  thrusterMix[VERTICAL_RIGHT] = clampUnit(heaveCommand);

  writeThrusters();
}

void sendTelemetry() {
  if (!(client && client.connected())) {
    return;
  }

  if (millis() - lastTelemetryMs < TELEMETRY_INTERVAL_MS) {
    return;
  }
  lastTelemetryMs = millis();

  StaticJsonDocument<1024> document;
  document["type"] = "telemetry";
  document["uptime_ms"] = millis();
  document["imu_online"] = imuOnline;
  document["battery_v"] = batteryVoltage;
  document["leak"] = leakDetected;
  document["pressure_kpa"] = pressureKpa;
  document["depth_m"] = depthMeters;
  document["temperature_c"] = imuTemperatureC;
  document["mode"] = (command.holdDepth || command.holdHeading) ? "assist" : "manual";

  JsonObject imu = document.createNestedObject("imu");
  imu["roll"] = rollDeg;
  imu["pitch"] = pitchDeg;
  imu["yaw"] = yawDeg;

  JsonObject holds = document.createNestedObject("hold");
  holds["depth"] = command.holdDepth;
  holds["heading"] = command.holdHeading;

  JsonObject setpoints = document.createNestedObject("setpoints");
  setpoints["depth_m"] = command.depthSetpoint;
  setpoints["heading_deg"] = command.headingSetpoint;

  JsonObject pid = document.createNestedObject("pid");
  JsonObject depth = pid.createNestedObject("depth");
  depth["kp"] = depthPid.kp;
  depth["ki"] = depthPid.ki;
  depth["kd"] = depthPid.kd;
  depth["error"] = depthPid.lastError;
  depth["output"] = depthPid.lastOutput;

  JsonObject heading = pid.createNestedObject("heading");
  heading["kp"] = headingPid.kp;
  heading["ki"] = headingPid.ki;
  heading["kd"] = headingPid.kd;
  heading["error"] = headingPid.lastError;
  heading["output"] = headingPid.lastOutput;

  JsonArray thrustersArray = document.createNestedArray("thrusters");
  for (uint8_t i = 0; i < THRUSTER_COUNT; ++i) {
    thrustersArray.add(thrusterMix[i]);
  }

  const String ipText = (WiFi.status() == WL_CONNECTED)
    ? WiFi.localIP().toString()
    : WiFi.softAPIP().toString();
  document["ip"] = ipText;

  serializeJson(document, client);
  client.print('\n');
}

void loop() {
  acceptClient();
  readClientCommands();

  const unsigned long now = millis();
  const float dt = max((now - lastLoopMs) / 1000.0f, 0.001f);
  lastLoopMs = now;

  updateSensors(dt);

  if (millis() - lastCommandMs > COMMAND_TIMEOUT_MS) {
    zeroManualAxes();
  }

  updateControlLoop(dt);
  sendTelemetry();
}

