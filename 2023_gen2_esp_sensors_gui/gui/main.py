import json
import socket
import sys
import threading
from datetime import datetime

import cv2
from PyQt5 import QtCore, QtGui, QtWidgets


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def frame_to_qimage(frame, size):
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


class VideoWorker(QtCore.QThread):
    frame_ready = QtCore.pyqtSignal(QtGui.QImage)
    status_changed = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = False
        self._source = ""
        self._lock = threading.Lock()

    def set_source(self, source):
        with self._lock:
            self._source = source.strip()

    def stop(self):
        self._running = False
        self.wait(1500)

    def run(self):
        self._running = True
        current_source = None
        capture = None

        while self._running:
            with self._lock:
                source = self._source

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

                stream_source = int(source) if source.isdigit() else source
                capture = cv2.VideoCapture(stream_source)
                current_source = source

                if not capture.isOpened():
                    self.status_changed.emit(f"Unable to open source: {source}")
                    capture.release()
                    capture = None
                    self.msleep(1000)
                    continue

                self.status_changed.emit(f"Streaming: {source}")

            ok, frame = capture.read()
            if not ok:
                self.status_changed.emit("Frame read failed, retrying...")
                capture.release()
                capture = None
                self.msleep(400)
                continue

            image = frame_to_qimage(frame, (480, 270))
            self.frame_ready.emit(image)
            self.msleep(33)

        if capture is not None:
            capture.release()


class VideoPanel(QtWidgets.QGroupBox):
    def __init__(self, title, parent=None):
        super().__init__(title, parent)
        self.worker = VideoWorker(self)

        self.preview = QtWidgets.QLabel("Camera offline")
        self.preview.setAlignment(QtCore.Qt.AlignCenter)
        self.preview.setMinimumSize(480, 270)
        self.preview.setStyleSheet(
            "background:#111723; border:1px solid #2f3c52; color:#a9b7d1;"
        )

        self.source_edit = QtWidgets.QLineEdit()
        self.source_edit.setPlaceholderText("Camera index or stream URL")
        self.toggle_button = QtWidgets.QPushButton("Start")
        self.toggle_button.clicked.connect(self.toggle_stream)
        self.status_label = QtWidgets.QLabel("Idle")
        self.status_label.setWordWrap(True)

        self.worker.frame_ready.connect(self._set_frame)
        self.worker.status_changed.connect(self.status_label.setText)

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(self.source_edit, 1)
        controls.addWidget(self.toggle_button)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.preview)
        layout.addLayout(controls)
        layout.addWidget(self.status_label)

    def toggle_stream(self):
        if self.worker.isRunning():
            self.stop_stream()
            return

        source = self.source_edit.text().strip()
        if not source:
            self.status_label.setText("Provide a camera source first")
            return

        self.worker.set_source(source)
        self.worker.start()
        self.toggle_button.setText("Stop")

    def stop_stream(self):
        if self.worker.isRunning():
            self.worker.stop()
        self.preview.clear()
        self.preview.setText("Camera offline")
        self.status_label.setText("Stopped")
        self.toggle_button.setText("Start")

    def shutdown(self):
        self.stop_stream()

    def _set_frame(self, image):
        pixmap = QtGui.QPixmap.fromImage(image)
        self.preview.setPixmap(pixmap)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("OI ROV 2023 Gen 2 Control Station")
        self.resize(1560, 920)

        self.client = RovClient(self)
        self.client.telemetry_received.connect(self.handle_telemetry)
        self.client.connection_changed.connect(self.handle_connection_changed)
        self.client.log_message.connect(self.append_log)

        self.keyboard_axes = {"surge": 0.0, "sway": 0.0, "heave": 0.0, "yaw": 0.0}
        self.active_keys = set()
        self.last_telemetry = {}

        self.axis_sliders = {}
        self.axis_value_labels = {}
        self.thruster_bars = []

        self._build_ui()
        self._apply_style()

        self.command_timer = QtCore.QTimer(self)
        self.command_timer.setInterval(120)
        self.command_timer.timeout.connect(self.send_motion_frame)
        self.command_timer.start()

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        main_layout = QtWidgets.QVBoxLayout(central)
        main_layout.setContentsMargins(14, 14, 14, 14)
        main_layout.setSpacing(12)

        main_layout.addWidget(self._build_connection_bar())

        content_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        content_splitter.setChildrenCollapsible(False)
        content_splitter.addWidget(self._build_status_panel())
        content_splitter.addWidget(self._build_control_panel())
        content_splitter.addWidget(self._build_visual_panel())
        content_splitter.setStretchFactor(0, 1)
        content_splitter.setStretchFactor(1, 1)
        content_splitter.setStretchFactor(2, 2)

        main_layout.addWidget(content_splitter, 1)

    def _build_connection_bar(self):
        frame = QtWidgets.QFrame()
        layout = QtWidgets.QHBoxLayout(frame)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QtWidgets.QLabel("ROV OPERATOR LINK")
        title.setObjectName("headerTitle")

        self.host_edit = QtWidgets.QLineEdit("192.168.4.1")
        self.host_edit.setPlaceholderText("ESP32 host")
        self.port_spin = QtWidgets.QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(9000)
        self.connect_button = QtWidgets.QPushButton("Connect")
        self.connect_button.clicked.connect(self.toggle_connection)
        self.connection_label = QtWidgets.QLabel("Offline")
        self.connection_label.setObjectName("statusChip")

        layout.addWidget(title)
        layout.addStretch(1)
        layout.addWidget(QtWidgets.QLabel("Host"))
        layout.addWidget(self.host_edit)
        layout.addWidget(QtWidgets.QLabel("Port"))
        layout.addWidget(self.port_spin)
        layout.addWidget(self.connect_button)
        layout.addWidget(self.connection_label)
        return frame

    def _build_status_panel(self):
        panel = QtWidgets.QFrame()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        title = QtWidgets.QLabel("Vehicle Status")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        self.battery_bar = QtWidgets.QProgressBar()
        self.battery_bar.setRange(0, 100)
        self.battery_bar.setFormat("%p%")
        self.battery_value = QtWidgets.QLabel("0.0 V")

        battery_layout = QtWidgets.QVBoxLayout()
        battery_layout.addWidget(QtWidgets.QLabel("Battery reserve"))
        battery_layout.addWidget(self.battery_bar)
        battery_layout.addWidget(self.battery_value)

        battery_card = QtWidgets.QFrame()
        battery_card.setLayout(battery_layout)

        self.leak_label = QtWidgets.QLabel("DRY")
        self.leak_label.setAlignment(QtCore.Qt.AlignCenter)
        self.leak_label.setMinimumHeight(38)
        self.leak_label.setObjectName("safeChip")

        imu_group = QtWidgets.QGroupBox("IMU Feedback")
        imu_form = QtWidgets.QFormLayout(imu_group)
        self.roll_value = QtWidgets.QLabel("0.0 deg")
        self.pitch_value = QtWidgets.QLabel("0.0 deg")
        self.yaw_value = QtWidgets.QLabel("0.0 deg")
        self.temp_value = QtWidgets.QLabel("0.0 C")
        imu_form.addRow("Roll", self.roll_value)
        imu_form.addRow("Pitch", self.pitch_value)
        imu_form.addRow("Yaw", self.yaw_value)
        imu_form.addRow("IMU Temp", self.temp_value)

        thruster_group = QtWidgets.QGroupBox("Thruster Mix")
        thruster_layout = QtWidgets.QVBoxLayout(thruster_group)
        thruster_names = [
            "Front Left",
            "Front Right",
            "Rear Left",
            "Rear Right",
            "Vertical Left",
            "Vertical Right",
        ]
        for name in thruster_names:
            label = QtWidgets.QLabel(name)
            bar = QtWidgets.QProgressBar()
            bar.setRange(-100, 100)
            bar.setValue(0)
            bar.setFormat("%v")
            self.thruster_bars.append(bar)
            thruster_layout.addWidget(label)
            thruster_layout.addWidget(bar)

        layout.addWidget(battery_card)
        layout.addWidget(QtWidgets.QLabel("Leak State"))
        layout.addWidget(self.leak_label)
        layout.addWidget(imu_group)
        layout.addWidget(thruster_group, 1)
        return panel

    def _build_control_panel(self):
        panel = QtWidgets.QFrame()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        title = QtWidgets.QLabel("Motion Control")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        slider_group = QtWidgets.QGroupBox("Manual Axes")
        slider_layout = QtWidgets.QGridLayout(slider_group)

        axes = [
            ("surge", "Surge"),
            ("sway", "Sway"),
            ("heave", "Heave"),
            ("yaw", "Yaw"),
        ]

        for row, (key, label) in enumerate(axes):
            slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
            slider.setRange(-100, 100)
            slider.setValue(0)
            slider.valueChanged.connect(
                lambda value, axis=key: self.axis_value_labels[axis].setText(f"{value}%")
            )

            value_label = QtWidgets.QLabel("0%")
            self.axis_sliders[key] = slider
            self.axis_value_labels[key] = value_label

            slider_layout.addWidget(QtWidgets.QLabel(label), row, 0)
            slider_layout.addWidget(slider, row, 1)
            slider_layout.addWidget(value_label, row, 2)

        quick_group = QtWidgets.QGroupBox("Quick Actions")
        quick_layout = QtWidgets.QGridLayout(quick_group)

        buttons = [
            ("Forward", lambda: self.set_axis_snapshot(surge=70)),
            ("Backward", lambda: self.set_axis_snapshot(surge=-70)),
            ("Strafe Left", lambda: self.set_axis_snapshot(sway=70)),
            ("Strafe Right", lambda: self.set_axis_snapshot(sway=-70)),
            ("Up", lambda: self.set_axis_snapshot(heave=70)),
            ("Down", lambda: self.set_axis_snapshot(heave=-70)),
            ("Yaw Left", lambda: self.set_axis_snapshot(yaw=55)),
            ("Yaw Right", lambda: self.set_axis_snapshot(yaw=-55)),
            ("Zero Axes", self.zero_axes),
            ("Emergency Stop", self.emergency_stop),
        ]

        for index, (label, callback) in enumerate(buttons):
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(callback)
            quick_layout.addWidget(button, index // 2, index % 2)

        keymap_group = QtWidgets.QGroupBox("Keyboard")
        keymap_layout = QtWidgets.QVBoxLayout(keymap_group)
        keymap_layout.addWidget(QtWidgets.QLabel("W/S  : surge"))
        keymap_layout.addWidget(QtWidgets.QLabel("A/D  : sway"))
        keymap_layout.addWidget(QtWidgets.QLabel("R/F  : heave"))
        keymap_layout.addWidget(QtWidgets.QLabel("Q/E  : yaw"))
        keymap_layout.addWidget(QtWidgets.QLabel("Space: stop"))

        layout.addWidget(slider_group)
        layout.addWidget(quick_group)
        layout.addWidget(keymap_group)
        layout.addStretch(1)
        return panel

    def _build_visual_panel(self):
        panel = QtWidgets.QFrame()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        title = QtWidgets.QLabel("Telemetry And Cameras")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        self.camera_one = VideoPanel("Primary Camera")
        self.camera_two = VideoPanel("Tool Camera")
        self.log_output = QtWidgets.QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumBlockCount(300)

        layout.addWidget(self.camera_one)
        layout.addWidget(self.camera_two)
        layout.addWidget(QtWidgets.QLabel("Event Log"))
        layout.addWidget(self.log_output, 1)
        return panel

    def _apply_style(self):
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #0c1118;
                color: #d7e3f3;
                font-family: "Segoe UI";
                font-size: 11pt;
            }
            QFrame, QGroupBox {
                background: #131b26;
                border: 1px solid #253245;
                border-radius: 10px;
            }
            QGroupBox {
                margin-top: 10px;
                padding-top: 14px;
                font-weight: 600;
            }
            QLineEdit, QSpinBox, QPlainTextEdit {
                background: #091018;
                border: 1px solid #31415a;
                border-radius: 8px;
                padding: 6px;
                color: #d7e3f3;
            }
            QPushButton {
                background: #1e2a3a;
                border: 1px solid #36506e;
                border-radius: 8px;
                padding: 8px 12px;
                color: #e8f0ff;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #26364c;
            }
            QProgressBar {
                background: #091018;
                border: 1px solid #31415a;
                border-radius: 7px;
                text-align: center;
                min-height: 20px;
            }
            QProgressBar::chunk {
                background: #28c4a1;
                border-radius: 6px;
            }
            QSlider::groove:horizontal {
                background: #0a1017;
                height: 8px;
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                background: #5fd2ff;
                width: 18px;
                margin: -6px 0;
                border-radius: 9px;
            }
            QLabel#headerTitle {
                font-size: 16pt;
                font-weight: 700;
                color: #eef6ff;
            }
            QLabel#sectionTitle {
                font-size: 14pt;
                font-weight: 700;
                color: #eef6ff;
            }
            QLabel#statusChip {
                background: #16212f;
                border: 1px solid #36506e;
                border-radius: 14px;
                padding: 6px 12px;
                font-weight: 700;
            }
            QLabel#safeChip {
                background: #123428;
                border: 1px solid #22a878;
                border-radius: 14px;
                color: #c9ffe9;
                font-weight: 700;
            }
            """
        )

    def append_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_output.appendPlainText(f"[{timestamp}] {message}")

    def toggle_connection(self):
        if self.client.is_connected():
            self.client.disconnect_from_host()
            return

        self.client.connect_to_host(self.host_edit.text().strip(), self.port_spin.value())

    def handle_connection_changed(self, connected, message):
        self.connection_label.setText("Online" if connected else "Offline")
        self.connection_label.setStyleSheet(
            "background:#143527; border:1px solid #22a878; border-radius:14px; padding:6px 12px; font-weight:700;"
            if connected
            else "background:#3c1616; border:1px solid #d55353; border-radius:14px; padding:6px 12px; font-weight:700;"
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
        self.client.send_json({"type": "stop"})
        self.append_log("Emergency stop issued")

    def current_axes(self):
        if self.active_keys:
            return dict(self.keyboard_axes)

        return {
            axis: slider.value() / 100.0
            for axis, slider in self.axis_sliders.items()
        }

    def send_motion_frame(self):
        if not self.client.is_connected():
            return

        axes = self.current_axes()
        self.client.send_json(
            {
                "type": "command",
                "mode": "manual",
                "axes": axes,
            }
        )

    def handle_telemetry(self, payload):
        self.last_telemetry = payload

        battery_v = float(payload.get("battery_v", 0.0))
        battery_pct = int(clamp(((battery_v - 11.0) / 5.8) * 100.0, 0.0, 100.0))
        self.battery_bar.setValue(battery_pct)
        self.battery_value.setText(f"{battery_v:.2f} V")

        leak = bool(payload.get("leak", False))
        if leak:
            self.leak_label.setText("LEAK DETECTED")
            self.leak_label.setStyleSheet(
                "background:#4a1717; border:1px solid #db5454; border-radius:14px; color:#ffd9d9; font-weight:700;"
            )
        else:
            self.leak_label.setText("DRY")
            self.leak_label.setStyleSheet(
                "background:#123428; border:1px solid #22a878; border-radius:14px; color:#c9ffe9; font-weight:700;"
            )

        imu = payload.get("imu", {})
        self.roll_value.setText(f"{float(imu.get('roll', 0.0)):.1f} deg")
        self.pitch_value.setText(f"{float(imu.get('pitch', 0.0)):.1f} deg")
        self.yaw_value.setText(f"{float(imu.get('yaw', 0.0)):.1f} deg")
        self.temp_value.setText(f"{float(payload.get('temperature_c', 0.0)):.1f} C")

        thrusters = payload.get("thrusters", [])
        for bar, value in zip(self.thruster_bars, thrusters):
            bar.setValue(int(float(value) * 100.0))

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
        self.camera_one.shutdown()
        self.camera_two.shutdown()
        self.client.disconnect_from_host(silent=True)
        super().closeEvent(event)


def main():
    app = QtWidgets.QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
