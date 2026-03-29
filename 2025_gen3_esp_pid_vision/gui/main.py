import json
import os
import socket
import sys
import threading
from collections import deque
from datetime import datetime

import cv2
import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def frame_to_qimage(frame, size=None):
    if size is not None:
        frame = cv2.resize(frame, size)
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    height, width, channels = rgb.shape
    bytes_per_line = channels * width
    return QtGui.QImage(
        rgb.data, width, height, bytes_per_line, QtGui.QImage.Format_RGB888
    ).copy()


class RovClient(QtCore.QObject):
    telemetry_received = QtCore.pyqtSignal(dict)
    connection_changed = QtCore.pyqtSignal(bool, str)
    log_message = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._socket = None
        self._lock = threading.Lock()
        self._reader_thread = None
        self._running = False

    def is_connected(self):
        with self._lock:
            return self._socket is not None and self._running

    def connect_to_host(self, host, port):
        self.disconnect_from_host(silent=True)

        try:
            sock = socket.create_connection((host, port), timeout=4)
            sock.settimeout(0.5)
        except OSError as exc:
            message = f"Connection failed: {exc}"
            self.connection_changed.emit(False, message)
            self.log_message.emit(message)
            return

        with self._lock:
            self._socket = sock
            self._running = True

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        message = f"Connected to {host}:{port}"
        self.connection_changed.emit(True, message)
        self.log_message.emit(message)

    def disconnect_from_host(self, silent=False):
        with self._lock:
            sock = self._socket
            was_running = self._running
            self._socket = None
            self._running = False

        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

        if was_running and not silent:
            self.connection_changed.emit(False, "Disconnected")
            self.log_message.emit("Disconnected")

    def send_json(self, payload):
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            sock = self._socket
            running = self._running

        if sock is None or not running:
            return

        try:
            sock.sendall(encoded)
        except OSError as exc:
            self.log_message.emit(f"Send failed: {exc}")
            self.disconnect_from_host()

    def _reader_loop(self):
        buffer = ""
        disconnect_message = "Connection closed"

        while True:
            with self._lock:
                sock = self._socket
                running = self._running

            if sock is None or not running:
                return

            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                disconnect_message = f"Socket error: {exc}"
                break

            if not data:
                disconnect_message = "Remote side closed the connection"
                break

            buffer += data.decode("utf-8", errors="ignore")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue

                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    self.log_message.emit(f"Discarded invalid telemetry: {line}")
                    continue

                self.telemetry_received.emit(payload)

        with self._lock:
            should_notify = self._running
            self._running = False
            self._socket = None

        try:
            sock.close()
        except OSError:
            pass

        if should_notify:
            self.connection_changed.emit(False, disconnect_message)
            self.log_message.emit(disconnect_message)


class ObjectDetector:
    DEFAULT_CLASSES = [
        "person",
        "bicycle",
        "car",
        "motorcycle",
        "airplane",
        "bus",
        "train",
        "truck",
        "boat",
        "target",
    ]

    def __init__(self, model_path="", conf_threshold=0.35, nms_threshold=0.45):
        self.conf_threshold = conf_threshold
        self.nms_threshold = nms_threshold
        self.model_path = model_path
        self.net = None
        self.mode = "fallback"

        if model_path and os.path.exists(model_path):
            try:
                self.net = cv2.dnn.readNetFromONNX(model_path)
                self.mode = "onnx"
            except cv2.error:
                self.net = None
                self.mode = "fallback"

    def detect(self, frame):
        if self.net is not None:
            try:
                return self._detect_onnx(frame)
            except cv2.error:
                self.mode = "fallback"
                self.net = None
        return self._detect_fallback(frame)

    def _detect_fallback(self, frame):
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        orange_mask = cv2.inRange(hsv, (5, 120, 80), (25, 255, 255))
        yellow_mask = cv2.inRange(hsv, (20, 80, 80), (40, 255, 255))
        mask = cv2.bitwise_or(orange_mask, yellow_mask)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        detections = []
        frame_area = float(frame.shape[0] * frame.shape[1])

        for contour in contours:
            area = cv2.contourArea(contour)
            if area < 1200:
                continue

            x, y, w, h = cv2.boundingRect(contour)
            confidence = clamp(area / frame_area * 10.0, 0.35, 0.92)
            detections.append(
                {
                    "label": "target",
                    "confidence": float(confidence),
                    "box": (x, y, w, h),
                }
            )

        return detections

    def _detect_onnx(self, frame):
        height, width = frame.shape[:2]
        blob = cv2.dnn.blobFromImage(
            frame, 1.0 / 255.0, (640, 640), swapRB=True, crop=False
        )
        self.net.setInput(blob)
        output = np.squeeze(self.net.forward())

        if output.ndim == 3:
            output = output[0]
        if output.ndim == 2 and output.shape[0] in (84, 85) and output.shape[0] < output.shape[1]:
            output = output.T
        if output.ndim != 2:
            return []

        boxes = []
        confidences = []
        class_ids = []
        x_scale = width / 640.0
        y_scale = height / 640.0

        for row in output:
            row = row.flatten()
            if row.size < 6:
                continue

            scores = row[4:]
            class_id = int(np.argmax(scores))
            confidence = float(scores[class_id])
            if confidence < self.conf_threshold:
                continue

            cx, cy, w, h = row[:4]
            left = int((cx - w / 2.0) * x_scale)
            top = int((cy - h / 2.0) * y_scale)
            box_width = int(w * x_scale)
            box_height = int(h * y_scale)

            boxes.append([left, top, box_width, box_height])
            confidences.append(confidence)
            class_ids.append(class_id)

        if not boxes:
            return []

        indices = cv2.dnn.NMSBoxes(boxes, confidences, self.conf_threshold, self.nms_threshold)
        if len(indices) == 0:
            return []

        detections = []
        for index in np.array(indices).flatten():
            label_index = class_ids[index] % len(self.DEFAULT_CLASSES)
            detections.append(
                {
                    "label": self.DEFAULT_CLASSES[label_index],
                    "confidence": float(confidences[index]),
                    "box": tuple(boxes[index]),
                }
            )
        return detections


def draw_detections(frame, detections):
    for detection in detections:
        x, y, w, h = detection["box"]
        label = detection["label"]
        confidence = detection["confidence"]
        color = (22, 182, 163) if label != "target" else (37, 175, 255)
        cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
        cv2.rectangle(frame, (x, y - 24), (x + max(120, w // 2), y), color, -1)
        cv2.putText(
            frame,
            f"{label} {confidence:.2f}",
            (x + 6, max(18, y - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (12, 16, 20),
            1,
            cv2.LINE_AA,
        )
    return frame


class VisionWorker(QtCore.QThread):
    frame_ready = QtCore.pyqtSignal(QtGui.QImage)
    metrics_ready = QtCore.pyqtSignal(dict)
    status_changed = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = False
        self._source = ""
        self._model_path = ""
        self._lock = threading.Lock()

    def configure(self, source, model_path):
        with self._lock:
            self._source = source.strip()
            self._model_path = model_path.strip()

    def stop(self):
        self._running = False
        self.wait(1500)

    def run(self):
        self._running = True
        current_source = None
        current_model_path = None
        capture = None
        detector = None

        while self._running:
            with self._lock:
                source = self._source
                model_path = self._model_path

            if not source:
                if capture is not None:
                    capture.release()
                    capture = None
                current_source = None
                self.status_changed.emit("No source configured")
                self.msleep(250)
                continue

            if capture is None or source != current_source:
                if capture is not None:
                    capture.release()

                capture_source = int(source) if source.isdigit() else source
                capture = cv2.VideoCapture(capture_source)
                current_source = source

                if not capture.isOpened():
                    self.status_changed.emit(f"Unable to open source: {source}")
                    capture.release()
                    capture = None
                    self.msleep(900)
                    continue

                self.status_changed.emit(f"Streaming: {source}")

            if detector is None or model_path != current_model_path:
                detector = ObjectDetector(model_path)
                current_model_path = model_path
                self.status_changed.emit(f"Detector mode: {detector.mode}")

            ok, frame = capture.read()
            if not ok:
                self.status_changed.emit("Frame read failed, reconnecting...")
                capture.release()
                capture = None
                self.msleep(400)
                continue

            detections = detector.detect(frame)
            overlay = draw_detections(frame.copy(), detections)
            image = frame_to_qimage(overlay, (520, 292))

            self.frame_ready.emit(image)
            self.metrics_ready.emit(
                {
                    "count": len(detections),
                    "mode": detector.mode,
                    "labels": ", ".join(d["label"] for d in detections) or "None",
                }
            )
            self.msleep(45)

        if capture is not None:
            capture.release()


class VisionPanel(QtWidgets.QGroupBox):
    log_message = QtCore.pyqtSignal(str)

    def __init__(self, title, default_model_path, parent=None):
        super().__init__(title, parent)
        self.worker = VisionWorker(self)

        self.preview = QtWidgets.QLabel("Vision offline")
        self.preview.setAlignment(QtCore.Qt.AlignCenter)
        self.preview.setMinimumSize(520, 292)
        self.preview.setStyleSheet(
            "background:#060808; border:1px solid #374045; color:#cfd5d9;"
        )

        self.source_edit = QtWidgets.QLineEdit()
        self.source_edit.setPlaceholderText("Camera index or stream URL")
        self.model_edit = QtWidgets.QLineEdit(default_model_path)
        self.toggle_button = QtWidgets.QPushButton("Start Vision")
        self.toggle_button.clicked.connect(self.toggle_stream)

        self.status_label = QtWidgets.QLabel("Idle")
        self.mode_label = QtWidgets.QLabel("Mode: fallback")
        self.count_label = QtWidgets.QLabel("Detections: 0")
        self.labels_label = QtWidgets.QLabel("Labels: None")
        self.labels_label.setWordWrap(True)

        self.worker.frame_ready.connect(self._set_frame)
        self.worker.status_changed.connect(self.status_label.setText)
        self.worker.metrics_ready.connect(self._handle_metrics)

        top_row = QtWidgets.QHBoxLayout()
        top_row.addWidget(self.source_edit, 3)
        top_row.addWidget(self.model_edit, 2)
        top_row.addWidget(self.toggle_button)

        info_row = QtWidgets.QVBoxLayout()
        info_row.addWidget(self.mode_label)
        info_row.addWidget(self.count_label)
        info_row.addWidget(self.labels_label)
        info_row.addWidget(self.status_label)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.preview)
        layout.addLayout(top_row)
        layout.addLayout(info_row)

    def toggle_stream(self):
        if self.worker.isRunning():
            self.stop_stream()
            return

        source = self.source_edit.text().strip()
        if not source:
            self.status_label.setText("Provide a camera source first")
            return

        self.worker.configure(source, self.model_edit.text().strip())
        self.worker.start()
        self.toggle_button.setText("Stop Vision")

    def stop_stream(self):
        if self.worker.isRunning():
            self.worker.stop()
        self.preview.setText("Vision offline")
        self.toggle_button.setText("Start Vision")
        self.status_label.setText("Stopped")
        self.count_label.setText("Detections: 0")
        self.labels_label.setText("Labels: None")

    def shutdown(self):
        self.stop_stream()

    def _set_frame(self, image):
        self.preview.setPixmap(QtGui.QPixmap.fromImage(image))

    def _handle_metrics(self, metrics):
        count = int(metrics.get("count", 0))
        mode = metrics.get("mode", "fallback")
        labels = metrics.get("labels", "None")
        self.mode_label.setText(f"Mode: {mode}")
        self.count_label.setText(f"Detections: {count}")
        self.labels_label.setText(f"Labels: {labels}")

        if count > 0:
            self.log_message.emit(f"{self.title()} detected {count} object(s): {labels}")


class PidAxisWidget(QtWidgets.QGroupBox):
    apply_requested = QtCore.pyqtSignal(str, float, float, float)

    def __init__(self, axis_name, defaults, parent=None):
        super().__init__(f"{axis_name.title()} PID", parent)
        self.axis_name = axis_name

        self.kp_spin = self._create_spin(defaults[0], 4.0)
        self.ki_spin = self._create_spin(defaults[1], 2.0)
        self.kd_spin = self._create_spin(defaults[2], 2.0)
        self.error_label = QtWidgets.QLabel("Error: 0.000")
        self.output_label = QtWidgets.QLabel("Output: 0.000")
        self.live_label = QtWidgets.QLabel("Live gains follow firmware telemetry")

        apply_button = QtWidgets.QPushButton("Apply Gains")
        apply_button.clicked.connect(self._emit_apply)

        form = QtWidgets.QFormLayout(self)
        form.addRow("Kp", self.kp_spin)
        form.addRow("Ki", self.ki_spin)
        form.addRow("Kd", self.kd_spin)
        form.addRow(self.error_label)
        form.addRow(self.output_label)
        form.addRow(self.live_label)
        form.addRow(apply_button)

    def _create_spin(self, value, maximum):
        spin = QtWidgets.QDoubleSpinBox()
        spin.setDecimals(4)
        spin.setRange(0.0, maximum)
        spin.setSingleStep(0.01)
        spin.setValue(value)
        return spin

    def _emit_apply(self):
        self.apply_requested.emit(
            self.axis_name,
            self.kp_spin.value(),
            self.ki_spin.value(),
            self.kd_spin.value(),
        )

    def set_live_data(self, gains, error, output):
        if gains:
            for spin, key in (
                (self.kp_spin, "kp"),
                (self.ki_spin, "ki"),
                (self.kd_spin, "kd"),
            ):
                if not spin.hasFocus():
                    spin.setValue(float(gains.get(key, spin.value())))

        self.error_label.setText(f"Error: {float(error):.3f}")
        self.output_label.setText(f"Output: {float(output):.3f}")


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ABYSS COMMAND | OI ROV 2025 Gen 3")
        self.resize(1720, 980)

        pg.setConfigOptions(antialias=True)

        self.client = RovClient(self)
        self.client.telemetry_received.connect(self.handle_telemetry)
        self.client.connection_changed.connect(self.handle_connection_changed)
        self.client.log_message.connect(self.append_log)

        self.active_keys = set()
        self.keyboard_axes = {"surge": 0.0, "sway": 0.0, "heave": 0.0, "yaw": 0.0}
        self.axis_sliders = {}
        self.axis_value_labels = {}
        self.thruster_bars = []
        self.last_telemetry = {}

        self.history = {
            "depth": deque(maxlen=180),
            "pressure": deque(maxlen=180),
            "battery": deque(maxlen=180),
            "heading": deque(maxlen=180),
            "depth_error": deque(maxlen=180),
            "heading_error": deque(maxlen=180),
        }
        self.alert_state = {"leak": False, "battery_low": False, "assist": False}

        self._build_ui()
        self._apply_style()

        self.command_timer = QtCore.QTimer(self)
        self.command_timer.setInterval(100)
        self.command_timer.timeout.connect(self.send_motion_frame)
        self.command_timer.start()

        self.plot_timer = QtCore.QTimer(self)
        self.plot_timer.setInterval(300)
        self.plot_timer.timeout.connect(self.refresh_plots)
        self.plot_timer.start()

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        layout.addWidget(self._build_top_bar())

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_mission_tab(), "Mission")
        self.tabs.addTab(self._build_sensors_tab(), "Sensors")
        self.tabs.addTab(self._build_pid_tab(), "PID Lab")
        self.tabs.addTab(self._build_vision_tab(), "Vision")
        layout.addWidget(self.tabs, 1)

    def _build_top_bar(self):
        frame = QtWidgets.QFrame()
        bar = QtWidgets.QHBoxLayout(frame)
        bar.setContentsMargins(12, 12, 12, 12)
        bar.setSpacing(10)

        title = QtWidgets.QLabel("ABYSS COMMAND")
        title.setObjectName("heroTitle")
        subtitle = QtWidgets.QLabel("2025 GEN 3 MISSION CONSOLE")
        subtitle.setObjectName("heroSubtitle")

        title_box = QtWidgets.QVBoxLayout()
        title_box.addWidget(title)
        title_box.addWidget(subtitle)

        self.host_edit = QtWidgets.QLineEdit("192.168.4.1")
        self.port_spin = QtWidgets.QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(9000)
        self.connect_button = QtWidgets.QPushButton("Connect")
        self.connect_button.clicked.connect(self.toggle_connection)
        self.connection_label = QtWidgets.QLabel("OFFLINE")
        self.connection_label.setObjectName("dangerChip")

        bar.addLayout(title_box)
        bar.addStretch(1)
        bar.addWidget(QtWidgets.QLabel("Host"))
        bar.addWidget(self.host_edit)
        bar.addWidget(QtWidgets.QLabel("Port"))
        bar.addWidget(self.port_spin)
        bar.addWidget(self.connect_button)
        bar.addWidget(self.connection_label)
        return frame

    def _build_mission_tab(self):
        widget = QtWidgets.QWidget()
        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_mission_overview())
        splitter.addWidget(self._build_mission_controls())
        splitter.addWidget(self._build_mission_alerts())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 1)

        layout = QtWidgets.QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        return widget

    def _build_mission_overview(self):
        panel = QtWidgets.QFrame()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        title = QtWidgets.QLabel("Vehicle Overview")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        self.mode_value = QtWidgets.QLabel("MANUAL")
        self.mode_value.setObjectName("goldChip")
        self.depth_value = QtWidgets.QLabel("0.00 m")
        self.pressure_value = QtWidgets.QLabel("101.33 kPa")
        self.battery_value = QtWidgets.QLabel("0.00 V")
        self.leak_value = QtWidgets.QLabel("DRY")
        self.leak_value.setObjectName("tealChip")
        self.roll_value = QtWidgets.QLabel("0.0 deg")
        self.pitch_value = QtWidgets.QLabel("0.0 deg")
        self.yaw_value = QtWidgets.QLabel("0.0 deg")
        self.depth_hold_state = QtWidgets.QLabel("Depth Hold: OFF")
        self.heading_hold_state = QtWidgets.QLabel("Heading Hold: OFF")

        summary_grid = QtWidgets.QGridLayout()
        summary_grid.addWidget(QtWidgets.QLabel("Mode"), 0, 0)
        summary_grid.addWidget(self.mode_value, 0, 1)
        summary_grid.addWidget(QtWidgets.QLabel("Depth"), 1, 0)
        summary_grid.addWidget(self.depth_value, 1, 1)
        summary_grid.addWidget(QtWidgets.QLabel("Pressure"), 2, 0)
        summary_grid.addWidget(self.pressure_value, 2, 1)
        summary_grid.addWidget(QtWidgets.QLabel("Battery"), 3, 0)
        summary_grid.addWidget(self.battery_value, 3, 1)
        summary_grid.addWidget(QtWidgets.QLabel("Leak"), 4, 0)
        summary_grid.addWidget(self.leak_value, 4, 1)
        summary_grid.addWidget(QtWidgets.QLabel("Roll"), 5, 0)
        summary_grid.addWidget(self.roll_value, 5, 1)
        summary_grid.addWidget(QtWidgets.QLabel("Pitch"), 6, 0)
        summary_grid.addWidget(self.pitch_value, 6, 1)
        summary_grid.addWidget(QtWidgets.QLabel("Yaw"), 7, 0)
        summary_grid.addWidget(self.yaw_value, 7, 1)

        summary_group = QtWidgets.QGroupBox("Command State")
        summary_group.setLayout(summary_grid)

        assist_group = QtWidgets.QGroupBox("Assist State")
        assist_layout = QtWidgets.QVBoxLayout(assist_group)
        assist_layout.addWidget(self.depth_hold_state)
        assist_layout.addWidget(self.heading_hold_state)

        thruster_group = QtWidgets.QGroupBox("Thruster Outputs")
        thruster_layout = QtWidgets.QVBoxLayout(thruster_group)
        for name in (
            "Front Left",
            "Front Right",
            "Rear Left",
            "Rear Right",
            "Vertical Left",
            "Vertical Right",
        ):
            thruster_layout.addWidget(QtWidgets.QLabel(name))
            bar = QtWidgets.QProgressBar()
            bar.setRange(-100, 100)
            bar.setValue(0)
            bar.setFormat("%v")
            self.thruster_bars.append(bar)
            thruster_layout.addWidget(bar)

        layout.addWidget(summary_group)
        layout.addWidget(assist_group)
        layout.addWidget(thruster_group, 1)
        return panel

    def _build_mission_controls(self):
        panel = QtWidgets.QFrame()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        title = QtWidgets.QLabel("Motion Control Mapping")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        slider_group = QtWidgets.QGroupBox("Manual Axes")
        slider_layout = QtWidgets.QGridLayout(slider_group)
        for row, (axis, label) in enumerate(
            (
                ("surge", "Surge"),
                ("sway", "Sway"),
                ("heave", "Heave"),
                ("yaw", "Yaw"),
            )
        ):
            slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            slider.setRange(-100, 100)
            slider.setValue(0)
            value_label = QtWidgets.QLabel("0%")
            slider.valueChanged.connect(
                lambda value, key=axis: self.axis_value_labels[key].setText(f"{value}%")
            )
            self.axis_sliders[axis] = slider
            self.axis_value_labels[axis] = value_label
            slider_layout.addWidget(QtWidgets.QLabel(label), row, 0)
            slider_layout.addWidget(slider, row, 1)
            slider_layout.addWidget(value_label, row, 2)

        hold_group = QtWidgets.QGroupBox("Assist Modes")
        hold_layout = QtWidgets.QGridLayout(hold_group)
        self.depth_hold_checkbox = QtWidgets.QCheckBox("Depth Hold")
        self.heading_hold_checkbox = QtWidgets.QCheckBox("Heading Hold")
        self.depth_setpoint_spin = QtWidgets.QDoubleSpinBox()
        self.depth_setpoint_spin.setDecimals(2)
        self.depth_setpoint_spin.setRange(0.0, 100.0)
        self.depth_setpoint_spin.setSuffix(" m")
        self.heading_setpoint_spin = QtWidgets.QDoubleSpinBox()
        self.heading_setpoint_spin.setDecimals(1)
        self.heading_setpoint_spin.setRange(-180.0, 180.0)
        self.heading_setpoint_spin.setSuffix(" deg")

        capture_depth = QtWidgets.QPushButton("Capture Current Depth")
        capture_depth.clicked.connect(self.capture_depth_setpoint)
        capture_heading = QtWidgets.QPushButton("Capture Current Heading")
        capture_heading.clicked.connect(self.capture_heading_setpoint)

        hold_layout.addWidget(self.depth_hold_checkbox, 0, 0)
        hold_layout.addWidget(self.depth_setpoint_spin, 0, 1)
        hold_layout.addWidget(capture_depth, 0, 2)
        hold_layout.addWidget(self.heading_hold_checkbox, 1, 0)
        hold_layout.addWidget(self.heading_setpoint_spin, 1, 1)
        hold_layout.addWidget(capture_heading, 1, 2)

        quick_group = QtWidgets.QGroupBox("Quick Commands")
        quick_layout = QtWidgets.QGridLayout(quick_group)
        actions = [
            ("Forward", lambda: self.set_axis_snapshot(surge=70)),
            ("Backward", lambda: self.set_axis_snapshot(surge=-70)),
            ("Strafe Left", lambda: self.set_axis_snapshot(sway=65)),
            ("Strafe Right", lambda: self.set_axis_snapshot(sway=-65)),
            ("Ascend", lambda: self.set_axis_snapshot(heave=65)),
            ("Descend", lambda: self.set_axis_snapshot(heave=-65)),
            ("Yaw Left", lambda: self.set_axis_snapshot(yaw=45)),
            ("Yaw Right", lambda: self.set_axis_snapshot(yaw=-45)),
            ("Zero Axes", self.zero_axes),
            ("Kill Thrust", self.emergency_stop),
        ]
        for index, (label, callback) in enumerate(actions):
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(callback)
            quick_layout.addWidget(button, index // 2, index % 2)

        keymap_group = QtWidgets.QGroupBox("Keyboard Mapping")
        keymap_layout = QtWidgets.QVBoxLayout(keymap_group)
        keymap_layout.addWidget(QtWidgets.QLabel("W/S  : surge"))
        keymap_layout.addWidget(QtWidgets.QLabel("A/D  : sway"))
        keymap_layout.addWidget(QtWidgets.QLabel("R/F  : heave"))
        keymap_layout.addWidget(QtWidgets.QLabel("Q/E  : yaw"))
        keymap_layout.addWidget(QtWidgets.QLabel("Space: stop"))

        layout.addWidget(slider_group)
        layout.addWidget(hold_group)
        layout.addWidget(quick_group)
        layout.addWidget(keymap_group)
        layout.addStretch(1)
        return panel

    def _build_mission_alerts(self):
        panel = QtWidgets.QFrame()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        title = QtWidgets.QLabel("Alerts And Diagnostics")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        self.alerts_list = QtWidgets.QListWidget()
        self.alerts_list.setAlternatingRowColors(True)
        self.event_log = QtWidgets.QPlainTextEdit()
        self.event_log.setReadOnly(True)
        self.event_log.document().setMaximumBlockCount(400)

        layout.addWidget(QtWidgets.QLabel("Active Alert Stack"))
        layout.addWidget(self.alerts_list, 1)
        layout.addWidget(QtWidgets.QLabel("Mission Event Timeline"))
        layout.addWidget(self.event_log, 1)
        return panel

    def _build_sensors_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        cards = QtWidgets.QGridLayout()
        self.sensor_cards = {}
        sensor_names = [
            ("Depth", "0.00 m"),
            ("Pressure", "101.33 kPa"),
            ("Battery", "0.00 V"),
            ("Heading", "0.0 deg"),
            ("Roll", "0.0 deg"),
            ("Pitch", "0.0 deg"),
        ]
        for index, (name, value) in enumerate(sensor_names):
            frame = QtWidgets.QFrame()
            inner = QtWidgets.QVBoxLayout(frame)
            name_label = QtWidgets.QLabel(name.upper())
            name_label.setObjectName("metricName")
            value_label = QtWidgets.QLabel(value)
            value_label.setObjectName("metricValue")
            inner.addWidget(name_label)
            inner.addWidget(value_label)
            self.sensor_cards[name.lower()] = value_label
            cards.addWidget(frame, index // 3, index % 3)
        layout.addLayout(cards)

        plot_grid = QtWidgets.QGridLayout()
        self.depth_plot, self.depth_curve = self._create_plot("Depth Trend", "#D4AF37", "Depth")
        self.pressure_plot, self.pressure_curve = self._create_plot("Pressure Trend", "#18B6A3", "kPa")
        self.battery_plot, self.battery_curve = self._create_plot("Battery Trend", "#F3D98B", "V")
        self.heading_plot, self.heading_curve = self._create_plot("Heading Trend", "#D63C33", "deg")
        plot_grid.addWidget(self.depth_plot, 0, 0)
        plot_grid.addWidget(self.pressure_plot, 0, 1)
        plot_grid.addWidget(self.battery_plot, 1, 0)
        plot_grid.addWidget(self.heading_plot, 1, 1)
        layout.addLayout(plot_grid, 1)
        return widget

    def _build_pid_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        top_row = QtWidgets.QHBoxLayout()
        self.depth_pid_widget = PidAxisWidget("depth", (1.20, 0.08, 0.18))
        self.heading_pid_widget = PidAxisWidget("heading", (0.035, 0.0, 0.015))
        self.depth_pid_widget.apply_requested.connect(self.apply_pid)
        self.heading_pid_widget.apply_requested.connect(self.apply_pid)
        top_row.addWidget(self.depth_pid_widget)
        top_row.addWidget(self.heading_pid_widget)

        setpoint_group = QtWidgets.QGroupBox("Setpoints And Safety")
        setpoint_form = QtWidgets.QFormLayout(setpoint_group)
        self.pid_depth_target_label = QtWidgets.QLabel("0.00 m")
        self.pid_heading_target_label = QtWidgets.QLabel("0.0 deg")
        self.pid_mode_label = QtWidgets.QLabel("Manual")
        notes = QtWidgets.QLabel(
            "Apply gains carefully. Validate pressure and heading telemetry before enabling assist modes."
        )
        notes.setWordWrap(True)
        setpoint_form.addRow("Depth setpoint", self.pid_depth_target_label)
        setpoint_form.addRow("Heading setpoint", self.pid_heading_target_label)
        setpoint_form.addRow("Current mode", self.pid_mode_label)
        setpoint_form.addRow(notes)

        plots_row = QtWidgets.QHBoxLayout()
        self.depth_error_plot, self.depth_error_curve = self._create_plot("Depth PID Error", "#18B6A3", "error")
        self.heading_error_plot, self.heading_error_curve = self._create_plot("Heading PID Error", "#D63C33", "error")
        plots_row.addWidget(self.depth_error_plot)
        plots_row.addWidget(self.heading_error_plot)

        layout.addLayout(top_row)
        layout.addWidget(setpoint_group)
        layout.addLayout(plots_row, 1)
        return widget

    def _build_vision_tab(self):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        default_model = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "models", "yolov8n.onnx"
        )
        vision_grid = QtWidgets.QGridLayout()

        self.front_camera_panel = VisionPanel("Primary Front Camera", default_model)
        self.tool_camera_panel = VisionPanel("Manipulator Camera", default_model)
        self.rear_camera_panel = VisionPanel("Rear Thruster Camera", default_model)

        for panel in (
            self.front_camera_panel,
            self.tool_camera_panel,
            self.rear_camera_panel,
        ):
            panel.log_message.connect(self.append_log)

        vision_grid.addWidget(self.front_camera_panel, 0, 0, 1, 2)
        vision_grid.addWidget(self.tool_camera_panel, 1, 0)
        vision_grid.addWidget(self.rear_camera_panel, 1, 1)

        self.vision_log = QtWidgets.QPlainTextEdit()
        self.vision_log.setReadOnly(True)
        self.vision_log.document().setMaximumBlockCount(250)

        layout.addLayout(vision_grid)
        layout.addWidget(QtWidgets.QLabel("Vision Activity"))
        layout.addWidget(self.vision_log, 1)
        return widget

    def _create_plot(self, title, color, left_label):
        plot = pg.PlotWidget()
        plot.setBackground((0, 0, 0, 0))
        plot.showGrid(x=True, y=True, alpha=0.2)
        plot.setMenuEnabled(False)
        plot.setLabel("left", left_label)
        plot.setLabel("bottom", "samples")
        plot.setTitle(title, color="#F3D98B", size="11pt")
        plot.getAxis("left").setTextPen("#d3dbe5")
        plot.getAxis("bottom").setTextPen("#d3dbe5")
        curve = plot.plot(pen=pg.mkPen(color, width=2))
        return plot, curve

    def _apply_style(self):
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #080808;
                color: #edf1f5;
                font-family: "Segoe UI";
                font-size: 10.5pt;
            }
            QFrame, QGroupBox, QListWidget, QTabWidget::pane {
                background: #15171b;
                border: 1px solid #3a3424;
                border-radius: 12px;
            }
            QGroupBox {
                margin-top: 10px;
                padding-top: 14px;
                font-weight: 700;
                color: #f3d98b;
            }
            QTabBar::tab {
                background: #111214;
                border: 1px solid #3a3424;
                padding: 10px 16px;
                margin-right: 4px;
                color: #d7dde5;
                border-top-left-radius: 10px;
                border-top-right-radius: 10px;
            }
            QTabBar::tab:selected {
                background: #1c1a15;
                color: #f3d98b;
            }
            QLineEdit, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QListWidget {
                background: #101214;
                border: 1px solid #4e4733;
                border-radius: 8px;
                padding: 6px;
                color: #f0f4f7;
            }
            QPushButton {
                background: #1d1b15;
                border: 1px solid #d4af37;
                border-radius: 8px;
                padding: 8px 12px;
                color: #f3d98b;
                font-weight: 700;
            }
            QPushButton:hover {
                background: #272319;
            }
            QSlider::groove:horizontal {
                background: #0c0e10;
                height: 8px;
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                background: #d4af37;
                width: 18px;
                margin: -6px 0;
                border-radius: 9px;
            }
            QProgressBar {
                background: #0b0d10;
                border: 1px solid #4e4733;
                border-radius: 7px;
                text-align: center;
                min-height: 20px;
            }
            QProgressBar::chunk {
                background: #18b6a3;
                border-radius: 6px;
            }
            QLabel#heroTitle {
                font-size: 18pt;
                font-weight: 800;
                color: #f3d98b;
                letter-spacing: 1px;
            }
            QLabel#heroSubtitle {
                font-size: 10pt;
                font-weight: 700;
                color: #9ca3ab;
                letter-spacing: 1px;
            }
            QLabel#sectionTitle {
                font-size: 14pt;
                font-weight: 800;
                color: #f3d98b;
            }
            QLabel#metricName {
                font-size: 9pt;
                font-weight: 700;
                color: #9ca3ab;
            }
            QLabel#metricValue {
                font-size: 16pt;
                font-weight: 800;
                color: #f3d98b;
            }
            QLabel#goldChip {
                background: #2a2313;
                border: 1px solid #d4af37;
                border-radius: 14px;
                padding: 6px 12px;
                color: #f3d98b;
                font-weight: 800;
            }
            QLabel#dangerChip {
                background: #351617;
                border: 1px solid #d63c33;
                border-radius: 14px;
                padding: 6px 12px;
                color: #ffd7d4;
                font-weight: 800;
            }
            QLabel#tealChip {
                background: #103430;
                border: 1px solid #18b6a3;
                border-radius: 14px;
                padding: 6px 12px;
                color: #cffff8;
                font-weight: 800;
            }
            """
        )

    def append_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}"
        self.event_log.appendPlainText(line)
        self.vision_log.appendPlainText(line)

    def toggle_connection(self):
        if self.client.is_connected():
            self.client.disconnect_from_host()
            return
        self.client.connect_to_host(self.host_edit.text().strip(), self.port_spin.value())

    def handle_connection_changed(self, connected, message):
        self.connection_label.setText("ONLINE" if connected else "OFFLINE")
        self.connection_label.setStyleSheet(
            "background:#103430; border:1px solid #18b6a3; border-radius:14px; padding:6px 12px; color:#cffff8; font-weight:800;"
            if connected
            else "background:#351617; border:1px solid #d63c33; border-radius:14px; padding:6px 12px; color:#ffd7d4; font-weight:800;"
        )
        self.connect_button.setText("Disconnect" if connected else "Connect")
        self.append_log(message)

    def set_axis_snapshot(self, surge=0, sway=0, heave=0, yaw=0):
        self.active_keys.clear()
        self.axis_sliders["surge"].setValue(surge)
        self.axis_sliders["sway"].setValue(sway)
        self.axis_sliders["heave"].setValue(heave)
        self.axis_sliders["yaw"].setValue(yaw)

    def zero_axes(self):
        for slider in self.axis_sliders.values():
            slider.setValue(0)

    def emergency_stop(self):
        self.active_keys.clear()
        self.keyboard_axes = {"surge": 0.0, "sway": 0.0, "heave": 0.0, "yaw": 0.0}
        self.zero_axes()
        self.depth_hold_checkbox.setChecked(False)
        self.heading_hold_checkbox.setChecked(False)
        self.client.send_json({"type": "stop"})
        self.append_log("Emergency stop issued")

    def current_axes(self):
        if self.active_keys:
            return dict(self.keyboard_axes)
        return {axis: slider.value() / 100.0 for axis, slider in self.axis_sliders.items()}

    def send_motion_frame(self):
        if not self.client.is_connected():
            return

        self.client.send_json(
            {
                "type": "command",
                "mode": "assist"
                if self.depth_hold_checkbox.isChecked() or self.heading_hold_checkbox.isChecked()
                else "manual",
                "axes": self.current_axes(),
                "holdDepth": self.depth_hold_checkbox.isChecked(),
                "holdHeading": self.heading_hold_checkbox.isChecked(),
                "depthSetpoint": self.depth_setpoint_spin.value(),
                "headingSetpoint": self.heading_setpoint_spin.value(),
            }
        )

    def capture_depth_setpoint(self):
        self.depth_setpoint_spin.setValue(float(self.last_telemetry.get("depth_m", 0.0)))
        self.client.send_json({"type": "command", "captureDepthSetpoint": True})
        self.append_log("Captured current depth as setpoint")

    def capture_heading_setpoint(self):
        yaw = float(self.last_telemetry.get("imu", {}).get("yaw", 0.0))
        self.heading_setpoint_spin.setValue(yaw)
        self.client.send_json({"type": "command", "captureHeadingSetpoint": True})
        self.append_log("Captured current heading as setpoint")

    def apply_pid(self, axis, kp, ki, kd):
        self.client.send_json({"type": "pid", "axis": axis, "kp": kp, "ki": ki, "kd": kd})
        self.append_log(f"Applied {axis} PID gains: kp={kp:.4f}, ki={ki:.4f}, kd={kd:.4f}")

    def handle_telemetry(self, payload):
        self.last_telemetry = payload
        imu = payload.get("imu", {})
        hold = payload.get("hold", {})
        setpoints = payload.get("setpoints", {})
        pid = payload.get("pid", {})

        depth = float(payload.get("depth_m", 0.0))
        pressure = float(payload.get("pressure_kpa", 0.0))
        battery = float(payload.get("battery_v", 0.0))
        roll = float(imu.get("roll", 0.0))
        pitch = float(imu.get("pitch", 0.0))
        yaw = float(imu.get("yaw", 0.0))
        leak = bool(payload.get("leak", False))
        mode = payload.get("mode", "manual").upper()

        self.mode_value.setText(mode)
        self.depth_value.setText(f"{depth:.2f} m")
        self.pressure_value.setText(f"{pressure:.2f} kPa")
        self.battery_value.setText(f"{battery:.2f} V")
        self.roll_value.setText(f"{roll:.1f} deg")
        self.pitch_value.setText(f"{pitch:.1f} deg")
        self.yaw_value.setText(f"{yaw:.1f} deg")

        self.leak_value.setText("LEAK" if leak else "DRY")
        self.leak_value.setStyleSheet(
            "background:#351617; border:1px solid #d63c33; border-radius:14px; padding:6px 12px; color:#ffd7d4; font-weight:800;"
            if leak
            else "background:#103430; border:1px solid #18b6a3; border-radius:14px; padding:6px 12px; color:#cffff8; font-weight:800;"
        )

        self.depth_hold_state.setText(f"Depth Hold: {'ON' if hold.get('depth', False) else 'OFF'}")
        self.heading_hold_state.setText(f"Heading Hold: {'ON' if hold.get('heading', False) else 'OFF'}")

        self.sensor_cards["depth"].setText(f"{depth:.2f} m")
        self.sensor_cards["pressure"].setText(f"{pressure:.2f} kPa")
        self.sensor_cards["battery"].setText(f"{battery:.2f} V")
        self.sensor_cards["heading"].setText(f"{yaw:.1f} deg")
        self.sensor_cards["roll"].setText(f"{roll:.1f} deg")
        self.sensor_cards["pitch"].setText(f"{pitch:.1f} deg")

        self.pid_depth_target_label.setText(f"{float(setpoints.get('depth_m', 0.0)):.2f} m")
        self.pid_heading_target_label.setText(f"{float(setpoints.get('heading_deg', 0.0)):.1f} deg")
        self.pid_mode_label.setText(mode)

        depth_pid = pid.get("depth", {})
        heading_pid = pid.get("heading", {})
        self.depth_pid_widget.set_live_data(depth_pid, depth_pid.get("error", 0.0), depth_pid.get("output", 0.0))
        self.heading_pid_widget.set_live_data(heading_pid, heading_pid.get("error", 0.0), heading_pid.get("output", 0.0))

        for bar, value in zip(self.thruster_bars, payload.get("thrusters", [])):
            bar.setValue(int(float(value) * 100.0))

        self.history["depth"].append(depth)
        self.history["pressure"].append(pressure)
        self.history["battery"].append(battery)
        self.history["heading"].append(yaw)
        self.history["depth_error"].append(float(depth_pid.get("error", 0.0)))
        self.history["heading_error"].append(float(heading_pid.get("error", 0.0)))

        assist_active = bool(hold.get("depth", False) or hold.get("heading", False))
        self._update_alert("leak", leak, "CRITICAL | Leak detected")
        self._update_alert("battery_low", battery < 12.5, "HIGH | Battery reserve below threshold")
        self._update_alert("assist", assist_active, "INFO | Assist profile active")

    def _update_alert(self, key, active, message):
        previous = self.alert_state.get(key, False)
        if active == previous:
            return
        self.alert_state[key] = active
        if active:
            self.alerts_list.insertItem(0, message)
            self.append_log(message)
        else:
            self.append_log(f"RESOLVED | {message}")

    def refresh_plots(self):
        if not self.history["depth"]:
            return
        x = list(range(len(self.history["depth"])))
        self.depth_curve.setData(x, list(self.history["depth"]))
        self.pressure_curve.setData(x, list(self.history["pressure"]))
        self.battery_curve.setData(x, list(self.history["battery"]))
        self.heading_curve.setData(x, list(self.history["heading"]))
        self.depth_error_curve.setData(x, list(self.history["depth_error"]))
        self.heading_error_curve.setData(x, list(self.history["heading_error"]))

    def keyPressEvent(self, event):
        if event.isAutoRepeat():
            return
        key = event.key()
        if key == QtCore.Qt.Key_Space:
            self.emergency_stop()
            return
        mapped = {
            QtCore.Qt.Key_W,
            QtCore.Qt.Key_S,
            QtCore.Qt.Key_A,
            QtCore.Qt.Key_D,
            QtCore.Qt.Key_R,
            QtCore.Qt.Key_F,
            QtCore.Qt.Key_Q,
            QtCore.Qt.Key_E,
        }
        if key not in mapped:
            super().keyPressEvent(event)
            return
        self.active_keys.add(key)
        self._refresh_keyboard_axes()

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            return
        key = event.key()
        if key in self.active_keys:
            self.active_keys.remove(key)
            self._refresh_keyboard_axes()
            return
        super().keyReleaseEvent(event)

    def _refresh_keyboard_axes(self):
        axes = {"surge": 0.0, "sway": 0.0, "heave": 0.0, "yaw": 0.0}
        if QtCore.Qt.Key_W in self.active_keys:
            axes["surge"] += 0.75
        if QtCore.Qt.Key_S in self.active_keys:
            axes["surge"] -= 0.75
        if QtCore.Qt.Key_A in self.active_keys:
            axes["sway"] += 0.65
        if QtCore.Qt.Key_D in self.active_keys:
            axes["sway"] -= 0.65
        if QtCore.Qt.Key_R in self.active_keys:
            axes["heave"] += 0.65
        if QtCore.Qt.Key_F in self.active_keys:
            axes["heave"] -= 0.65
        if QtCore.Qt.Key_Q in self.active_keys:
            axes["yaw"] += 0.45
        if QtCore.Qt.Key_E in self.active_keys:
            axes["yaw"] -= 0.45
        self.keyboard_axes = axes

    def closeEvent(self, event):
        for panel in (self.front_camera_panel, self.tool_camera_panel, self.rear_camera_panel):
            panel.shutdown()
        self.client.disconnect_from_host(silent=True)
        super().closeEvent(event)


def main():
    app = QtWidgets.QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
