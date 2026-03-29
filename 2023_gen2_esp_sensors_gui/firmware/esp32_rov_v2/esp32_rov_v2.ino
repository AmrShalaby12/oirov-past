#include <WiFi.h>
#include <Wire.h>
#include <ArduinoJson.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <ESP32Servo.h>

constexpr bool USE_STATION_MODE = false;
const char *STA_SSID = "YOUR_WIFI_NAME";
const char *STA_PASSWORD = "YOUR_WIFI_PASSWORD";

const char *AP_SSID = "OI_ROV_2023_GEN2";
const char *AP_PASSWORD = "rov2023v2";
constexpr uint16_t CONTROL_PORT = 9000;

constexpr uint8_t THRUSTER_COUNT = 6;
constexpr uint8_t THRUSTER_PINS[THRUSTER_COUNT] = {13, 12, 14, 27, 26, 25};
constexpr uint8_t LEAK_SENSOR_PIN = 33;
constexpr uint8_t BATTERY_SENSOR_PIN = 34;

constexpr int PWM_NEUTRAL = 1500;
constexpr int PWM_MIN = 1100;
constexpr int PWM_MAX = 1900;
constexpr unsigned long COMMAND_TIMEOUT_MS = 1000;
constexpr unsigned long TELEMETRY_INTERVAL_MS = 100;

constexpr float ADC_REFERENCE_VOLTAGE = 3.3f;
constexpr float BATTERY_DIVIDER_RATIO = 5.7f;
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
};

WiFiServer server(CONTROL_PORT);
WiFiClient client;
String receiveBuffer;

Adafruit_MPU6050 mpu;
bool imuOnline = false;
Servo thrusters[THRUSTER_COUNT];

MotionCommand command;
float thrusterMix[THRUSTER_COUNT] = {0, 0, 0, 0, 0, 0};

float rollDeg = 0.0f;
float pitchDeg = 0.0f;
float yawDeg = 0.0f;
float imuTemperatureC = 0.0f;
float batteryVoltage = 0.0f;
bool leakDetected = false;

unsigned long lastCommandMs = 0;
unsigned long lastTelemetryMs = 0;
unsigned long lastSensorMs = 0;

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

int pwmFromUnit(float value) {
  return PWM_NEUTRAL + static_cast<int>(clampUnit(value) * static_cast<float>(PWM_MAX - PWM_NEUTRAL));
}

void writeThrusters() {
  for (uint8_t i = 0; i < THRUSTER_COUNT; ++i) {
    thrusters[i].writeMicroseconds(pwmFromUnit(thrusterMix[i]));
  }
}

void zeroCommand() {
  command.surge = 0.0f;
  command.sway = 0.0f;
  command.heave = 0.0f;
  command.yaw = 0.0f;
}

void mixThrusters() {
  thrusterMix[FRONT_LEFT] = clampUnit(command.surge + command.sway + command.yaw);
  thrusterMix[FRONT_RIGHT] = clampUnit(command.surge - command.sway - command.yaw);
  thrusterMix[REAR_LEFT] = clampUnit(command.surge - command.sway + command.yaw);
  thrusterMix[REAR_RIGHT] = clampUnit(command.surge + command.sway - command.yaw);
  thrusterMix[VERTICAL_LEFT] = clampUnit(command.heave);
  thrusterMix[VERTICAL_RIGHT] = clampUnit(command.heave);
  writeThrusters();
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

void setupImu() {
  imuOnline = mpu.begin();
  if (imuOnline) {
    mpu.setAccelerometerRange(MPU6050_RANGE_8_G);
    mpu.setGyroRange(MPU6050_RANGE_500_DEG);
    mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
  }
}

void setupThrusters() {
  for (uint8_t i = 0; i < THRUSTER_COUNT; ++i) {
    thrusters[i].setPeriodHertz(50);
    thrusters[i].attach(THRUSTER_PINS[i], PWM_MIN, PWM_MAX);
    thrusters[i].writeMicroseconds(PWM_NEUTRAL);
  }
}

void setup() {
  Serial.begin(115200);
  delay(250);

  Wire.begin();
  analogReadResolution(12);
  pinMode(LEAK_SENSOR_PIN, INPUT_PULLUP);

  setupThrusters();
  setupImu();
  setupWifi();

  lastCommandMs = millis();
  lastSensorMs = millis();

  Serial.println(F("OI ROV 2023 Gen 2 ready"));
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

void processCommandLine(const String &line) {
  StaticJsonDocument<384> document;
  DeserializationError error = deserializeJson(document, line);
  if (error) {
    Serial.println(F("Invalid JSON command"));
    return;
  }

  const char *type = document["type"] | "command";

  if (strcmp(type, "stop") == 0) {
    zeroCommand();
    lastCommandMs = millis();
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
      if (receiveBuffer.length() < 255) {
        receiveBuffer += incoming;
      } else {
        receiveBuffer = "";
      }
    }
  }
}

void updateSensors() {
  const unsigned long now = millis();
  const float dt = max((now - lastSensorMs) / 1000.0f, 0.001f);
  lastSensorMs = now;

  leakDetected = digitalRead(LEAK_SENSOR_PIN) == LOW;
  batteryVoltage = readBatteryVoltage();

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

void sendTelemetry() {
  if (!(client && client.connected())) {
    return;
  }

  if (millis() - lastTelemetryMs < TELEMETRY_INTERVAL_MS) {
    return;
  }
  lastTelemetryMs = millis();

  StaticJsonDocument<512> document;
  document["type"] = "telemetry";
  document["uptime_ms"] = millis();
  document["imu_online"] = imuOnline;
  document["battery_v"] = batteryVoltage;
  document["leak"] = leakDetected;
  document["temperature_c"] = imuTemperatureC;

  JsonObject imu = document.createNestedObject("imu");
  imu["roll"] = rollDeg;
  imu["pitch"] = pitchDeg;
  imu["yaw"] = yawDeg;

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
  updateSensors();

  if (millis() - lastCommandMs > COMMAND_TIMEOUT_MS) {
    zeroCommand();
  }

  mixThrusters();
  sendTelemetry();
}

