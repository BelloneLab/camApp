"""
Arduino TTL communication worker for camApp.

This module handles all board communication used by the GUI:
1) Serial port discovery and connection
2) Start/stop commands for test and recording modes
3) Live TTL/behavior state parsing
4) Rising-edge counting and event history bookkeeping

The worker is designed to support both:
- command/response sketches (`GET_STATES`, `GET_PINS`)
- passive/event-text sketches (`CUE_ON`, `Lever was pressed`, etc.)
"""
import time
from typing import Dict, List, Optional

import serial
import serial.tools.list_ports
from PySide6.QtCore import QMutex, QMutexLocker, QSettings, QThread, Signal


class ArduinoOutputWorker(QThread):
    """
    Arduino TTL output generator.
    Syncs with camera frames for precise TTL state logging.
    """

    SIGNAL_KEYS = ("gate", "sync", "barcode0", "barcode1", "lever", "cue", "reward", "iti")
    COUNT_KEY_MAP = {
        "gate": "gate_count",
        "sync": "sync_count",
        "barcode0": "barcode_count",
        "barcode1": "barcode_count",
        "lever": "lever_count",
        "cue": "cue_count",
        "reward": "reward_count",
        "iti": "iti_count",
    }
    EDGE_KEY_MAP = {
        "gate": "gate_edge_ms",
        "sync": "sync_edge_ms",
        "barcode0": "barcode_edge_ms",
        "barcode1": "barcode_edge_ms",
        "lever": "lever_edge_ms",
        "cue": "cue_edge_ms",
        "reward": "reward_edge_ms",
        "iti": "iti_edge_ms",
    }
    DEFAULT_PIN_CONFIG = {
        "gate": [3],
        "sync": [9],
        "barcode": [18],
        "lever": [14],
        "cue": [45],
        "reward": [21],
        "iti": [46],
    }

    # Signals
    port_list_updated = Signal(list)
    connection_status = Signal(bool, str)
    ttl_states_updated = Signal(dict)
    pin_config_received = Signal(dict)
    error_occurred = Signal(str)

    def __init__(self):
        super().__init__()

        self.serial_port: Optional[serial.Serial] = None
        self.running = False
        self.mutex = QMutex()
        self.is_generating = False

        # Configuration
        self.port_name = ""
        self.baud_rate = 9600
        self.pin_config = {key: pins.copy() for key, pins in self.DEFAULT_PIN_CONFIG.items()}
        self.passive_monitor_mode = False
        self.serial_pulse_width_s = 0.12
        self.transient_high_until = {key: 0.0 for key in self.SIGNAL_KEYS}
        self.last_baud_candidates = []
        self.last_state_query_time = 0.0
        self.state_query_interval_s = 0.25
        self.passive_state_query_interval_s = 1.0
        self.last_serial_error_message = ""
        self.last_serial_error_time = 0.0
        self.serial_error_cooldown_s = 1.0
        self.protocol_verified = False

        # Current states
        self.current_states = {key: False for key in self.SIGNAL_KEYS}
        self.last_state_packet = self.current_states.copy()

        # TTL history for frame-synced logging
        self.ttl_history: List[Dict] = []
        self.last_state_emit = 0.0
        self.ttl_event_history: List[Dict] = []
        self.ttl_pulse_counts = {key: 0 for key in self.SIGNAL_KEYS}
        self.last_event_state = self.current_states.copy()
        self.live_state_history: List[Dict] = []
        self.max_live_state_history = 50000

        # Maintain old public attributes used by UI code
        self.gate_pins = self.pin_config["gate"].copy()
        self.sync_pins = self.pin_config["sync"].copy()
        self.barcode_pins = self.pin_config["barcode"].copy()

        self.settings = QSettings("BaslerCam", "CameraApp")
        self.load_settings()

    # ===== Settings / Config =====

    def load_settings(self):
        """Load saved Arduino settings."""
        self.port_name = self.settings.value("arduino_port", "")
        self.baud_rate = int(self.settings.value("arduino_baud_rate", 9600))
        for key, default_pins in self.DEFAULT_PIN_CONFIG.items():
            raw_value = self.settings.value(f"behavior_pin_{key}", None)
            pins = self._parse_pin_setting_value(raw_value)
            if pins:
                self.pin_config[key] = pins
            else:
                self.pin_config[key] = default_pins.copy()
        self._refresh_legacy_pin_attributes()

    def save_settings(self):
        """Save Arduino settings."""
        self.settings.setValue("arduino_port", self.port_name)
        self.settings.setValue("arduino_baud_rate", int(self.baud_rate))
        for key, pins in self.pin_config.items():
            self.settings.setValue(f"behavior_pin_{key}", ",".join(str(int(pin)) for pin in pins))

    def set_manual_pin_config(self, pin_config: Dict[str, List[int]]):
        """Apply manual pin mapping from GUI configuration."""
        if not isinstance(pin_config, dict):
            return

        with QMutexLocker(self.mutex):
            updated = self.pin_config.copy()
            for raw_key, raw_pins in pin_config.items():
                key = self._normalize_pin_key(str(raw_key))
                if not key:
                    continue
                pins = self._normalize_pin_list(raw_pins)
                if pins:
                    updated[key] = pins

            self.pin_config = updated
            self._refresh_legacy_pin_attributes()
            self.save_settings()

        self.pin_config_received.emit(self.pin_config.copy())

    def _refresh_legacy_pin_attributes(self):
        self.gate_pins = self.pin_config.get("gate", []).copy()
        self.sync_pins = self.pin_config.get("sync", []).copy()
        self.barcode_pins = self.pin_config.get("barcode", []).copy()

    # ===== Serial Port =====

    def scan_ports(self) -> List[str]:
        """Scan for available serial ports."""
        ports = serial.tools.list_ports.comports()
        port_list = []

        for port in ports:
            if "Arduino" in port.description or "CH340" in port.description or "USB" in port.description:
                port_list.append(f"{port.device} - {port.description}")

        if not port_list:
            port_list = [f"{port.device} - {port.description}" for port in ports]

        self.port_list_updated.emit(port_list)
        return port_list

    def connect_to_port(self, port_name: str) -> bool:
        """
        Open serial connection and auto-detect a compatible baud/protocol.

        Design notes for beginners:
        - Many boards reset when a serial port is opened.
        - Different sketches use different baud rates (commonly 9600/115200).
        - We probe each candidate baud and score it based on:
          1) valid pin config response
          2) valid state packet response
          3) READY text
          4) any readable text
        - The best-scoring baud is kept as the active session baud.
        """
        with QMutexLocker(self.mutex):
            try:
                if " - " in port_name:
                    port_name = port_name.split(" - ")[0]

                self.port_name = port_name
                # Prefer persisted baud first, then 9600 (common behavior sketches),
                # then 115200 (common TTL generator sketches).
                baud_candidates = []
                for candidate in (int(self.baud_rate), 9600, 115200):
                    if candidate not in baud_candidates:
                        baud_candidates.append(candidate)
                self.last_baud_candidates = baud_candidates.copy()

                best_serial = None
                best_baud = int(self.baud_rate)
                best_score = -1
                best_pin_config = {}
                best_has_protocol = False

                for baud in baud_candidates:
                    test_serial = None
                    try:
                        test_serial = serial.Serial(
                            port=port_name,
                            baudrate=baud,
                            timeout=0.1,
                        )
                        time.sleep(1.6)
                        saw_ready = False
                        saw_any_text = False
                        saw_state_packet = False
                        saw_passive_event = False
                        parsed_pin_config = {}

                        # Probe window is intentionally longer for behavior sketches
                        # with blocking loops (cue/servo/ITI) where serial commands
                        # may only be serviced intermittently.
                        probe_window_s = 12.0 if baud <= 9600 else 4.0
                        probe_deadline = time.time() + probe_window_s
                        next_get_pins = 0.0
                        next_get_states = 0.0
                        while time.time() < probe_deadline:
                            now_probe = time.time()
                            if now_probe >= next_get_pins:
                                try:
                                    test_serial.write(b"GET_PINS\n")
                                except Exception:
                                    pass
                                next_get_pins = now_probe + 0.45
                            if now_probe >= next_get_states:
                                try:
                                    test_serial.write(b"GET_STATES\n")
                                except Exception:
                                    pass
                                next_get_states = now_probe + 0.45

                            if test_serial.in_waiting <= 0:
                                time.sleep(0.02)
                                continue
                            line = test_serial.readline().decode(errors="ignore").strip()
                            if not line:
                                continue
                            saw_any_text = True
                            if line == "READY":
                                saw_ready = True
                            candidate = self._parse_pin_config_response(line)
                            if self._is_plausible_pin_config(candidate):
                                parsed_pin_config = candidate
                            state_candidate = self._parse_state_response(line)
                            if state_candidate:
                                saw_state_packet = True
                            if self._apply_passive_line(line):
                                saw_passive_event = True

                        has_protocol = bool(parsed_pin_config or saw_state_packet or saw_passive_event or saw_ready)
                        score = 0
                        if parsed_pin_config:
                            score += 4
                        if saw_state_packet:
                            score += 3
                        if saw_passive_event:
                            score += 2
                        if saw_ready:
                            score += 2
                        if saw_any_text and has_protocol:
                            score += 1

                        prefer_candidate = False
                        if score > best_score:
                            prefer_candidate = True
                        elif score == best_score and score <= 3 and baud == 9600 and best_baud != 9600:
                            # If no strong handshake evidence, favor 9600 for behavior sketches.
                            prefer_candidate = True

                        if prefer_candidate:
                            if best_serial and best_serial.is_open:
                                best_serial.close()
                            best_serial = test_serial
                            best_baud = baud
                            best_score = score
                            best_has_protocol = has_protocol
                            best_pin_config = parsed_pin_config
                            test_serial = None
                        else:
                            if test_serial and test_serial.is_open:
                                test_serial.close()

                    except Exception:
                        if test_serial and test_serial.is_open:
                            test_serial.close()

                if best_serial is None:
                    raise RuntimeError("Unable to open serial port")

                self.serial_port = best_serial
                self.baud_rate = int(best_baud)
                self.protocol_verified = bool(best_has_protocol)
                self.passive_monitor_mode = False
                self.last_state_query_time = 0.0
                if self.baud_rate <= 9600:
                    # 9600 baud cannot sustain dense GET_STATES polling with long packets.
                    self.state_query_interval_s = 0.8
                    self.passive_state_query_interval_s = 1.5
                else:
                    self.state_query_interval_s = 0.05
                    self.passive_state_query_interval_s = 0.25
                self._reset_ttl_event_tracking()

                if best_pin_config:
                    self._apply_pin_config_dict(best_pin_config)
                self.pin_config_received.emit(self.pin_config.copy())

                self.save_settings()
                status_text = f"Connected to {port_name} @ {self.baud_rate}"
                if not self.protocol_verified:
                    status_text += " (protocol not verified yet)"
                    self.error_occurred.emit(
                        "Warning: Connected, but no immediate protocol response detected. "
                        "Using passive monitor fallback; press lever/start behavior to generate events."
                    )
                self.connection_status.emit(True, status_text)
                return True

            except Exception as e:
                self.error_occurred.emit(f"Arduino connection error: {str(e)}")
                self.connection_status.emit(False, str(e))
                return False

    def _apply_pin_config_dict(self, parsed_config: Dict[str, List[int]]):
        """Apply parsed pin configuration dictionary."""
        for key, pins in parsed_config.items():
            if key in self.pin_config:
                self.pin_config[key] = [int(pin) for pin in pins]
        self._refresh_legacy_pin_attributes()

    def disconnect_port(self):
        """Disconnect from Arduino."""
        with QMutexLocker(self.mutex):
            if self.serial_port and self.serial_port.is_open:
                try:
                    self.serial_port.write(b"STOP_RECORDING\n")
                    time.sleep(0.1)
                except Exception:
                    pass

                self.serial_port.close()
                self.serial_port = None
                self.is_generating = False
                self.passive_monitor_mode = False
                self.current_states = {key: False for key in self.current_states}
                self.transient_high_until = {key: 0.0 for key in self.transient_high_until}
                self.connection_status.emit(False, "Disconnected")

    # ===== Control Commands =====

    def start_recording(self):
        """
        Request board-side TTL start for camera recording mode.

        If firmware does not acknowledge `OK_RECORDING`, we still switch to a
        passive monitor fallback so behavior markers can be visualized.
        """
        with QMutexLocker(self.mutex):
            if self.serial_port and self.serial_port.is_open:
                for _ in range(3):
                    try:
                        self.serial_port.reset_input_buffer()
                        self.serial_port.write(b"START_RECORDING\n")
                        time.sleep(0.1)

                        start_wait = time.time()
                        while time.time() - start_wait < 1.0:
                            if self.serial_port.in_waiting > 0:
                                response = self.serial_port.readline().decode(errors="ignore").strip()
                                if response == "OK_RECORDING":
                                    self.is_generating = True
                                    self.ttl_history.clear()
                                    self._reset_ttl_event_tracking()
                                    return True
                            time.sleep(0.01)
                    except Exception as e:
                        if self._is_serial_io_error(e):
                            self._handle_serial_io_failure(e, context="Start recording error")
                            return False
                        time.sleep(0.2)

                # Fallback only if passive events are actually observed.
                # Some behavior sketches may take several seconds before servicing
                # serial while inside cue/servo windows.
                passive_seen = False
                fallback_deadline = time.time() + 10.0
                next_retry = 0.0
                while time.time() < fallback_deadline:
                    now_fallback = time.time()
                    if now_fallback >= next_retry:
                        try:
                            self.serial_port.write(b"START_RECORDING\n")
                        except Exception:
                            pass
                        next_retry = now_fallback + 0.5
                    if self.serial_port.in_waiting <= 0:
                        time.sleep(0.01)
                        continue
                    response = self.serial_port.readline().decode(errors="ignore").strip()
                    if response == "OK_RECORDING":
                        self.is_generating = True
                        self.passive_monitor_mode = False
                        self.ttl_history.clear()
                        self._reset_ttl_event_tracking()
                        return True
                    if self._apply_passive_line(response):
                        passive_seen = True
                if passive_seen:
                    self.is_generating = True
                    self.passive_monitor_mode = True
                    self.ttl_history.clear()
                    self._reset_ttl_event_tracking()
                    return True

                # Last fallback: keep recording mode armed and monitor passive
                # events if they appear later (some sketches only print on
                # specific behavioral events).
                self.is_generating = True
                self.passive_monitor_mode = True
                self.ttl_history.clear()
                self._reset_ttl_event_tracking()
                self.error_occurred.emit(
                    "START_RECORDING not acknowledged yet; armed passive monitoring mode."
                )
                return True
        return False

    def stop_recording(self):
        """Request board-side TTL stop for recording mode."""
        with QMutexLocker(self.mutex):
            if self.serial_port and self.serial_port.is_open:
                try:
                    self.serial_port.write(b"STOP_RECORDING\n")
                    ok = self._await_response("OK_STOPPED", timeout=0.5)
                    self.is_generating = False
                    self.passive_monitor_mode = False
                    self.current_states = {key: False for key in self.current_states}
                    self._reset_ttl_event_tracking()
                    return ok
                except Exception as e:
                    if self._is_serial_io_error(e):
                        self._handle_serial_io_failure(e, context="Stop recording error")
                    else:
                        self.error_occurred.emit(f"Stop recording error: {str(e)}")
        return False

    def start_test(self):
        """
        Start TTL/behavior monitoring test mode.

        The command protocol is intentionally simple text so it works across
        Arduino-like boards, ESP32-S3, and custom sketches.
        """
        with QMutexLocker(self.mutex):
            if self.serial_port and self.serial_port.is_open:
                try:
                    try:
                        self.serial_port.reset_input_buffer()
                    except Exception:
                        pass
                    self.serial_port.write(b"START_TEST\n")
                    if self._await_response("OK_TEST", timeout=1.0):
                        self.is_generating = True
                        self.passive_monitor_mode = False
                        self._reset_ttl_event_tracking()
                        return True

                    # Fallback only if passive events are actually observed.
                    # Some behavior sketches may take several seconds before
                    # servicing serial while inside long task loops.
                    passive_seen = False
                    fallback_deadline = time.time() + 10.0
                    next_retry = 0.0
                    while time.time() < fallback_deadline:
                        now_fallback = time.time()
                        if now_fallback >= next_retry:
                            try:
                                self.serial_port.write(b"START_TEST\n")
                            except Exception:
                                pass
                            next_retry = now_fallback + 0.5
                        if self.serial_port.in_waiting <= 0:
                            time.sleep(0.01)
                            continue
                        response = self.serial_port.readline().decode(errors="ignore").strip()
                        if response == "OK_TEST":
                            self.is_generating = True
                            self.passive_monitor_mode = False
                            self._reset_ttl_event_tracking()
                            return True
                        if self._apply_passive_line(response):
                            passive_seen = True
                    if passive_seen:
                        self.is_generating = True
                        self.passive_monitor_mode = True
                        self._reset_ttl_event_tracking()
                        return True

                    # Last fallback: keep test mode armed and monitor passive
                    # events if they appear later.
                    self.is_generating = True
                    self.passive_monitor_mode = True
                    self._reset_ttl_event_tracking()
                    self.error_occurred.emit(
                        "START_TEST not acknowledged yet; armed passive monitoring mode."
                    )
                    return True
                except Exception as e:
                    if self._is_serial_io_error(e):
                        self._handle_serial_io_failure(e, context="Start test error")
                    else:
                        self.error_occurred.emit(f"Start test error: {str(e)}")
        return False

    def stop_test(self):
        """Stop TTL test/monitoring mode."""
        with QMutexLocker(self.mutex):
            if self.serial_port and self.serial_port.is_open:
                try:
                    try:
                        self.serial_port.write(b"STOP_TEST\n")
                    except Exception:
                        pass
                    ok = self._await_response("OK_STOPPED", timeout=0.5)
                    if self.passive_monitor_mode and not ok:
                        ok = True
                    self.is_generating = False
                    self.passive_monitor_mode = False
                    self.current_states = {key: False for key in self.current_states}
                    self._reset_ttl_event_tracking()
                    return ok
                except Exception as e:
                    if self._is_serial_io_error(e):
                        self._handle_serial_io_failure(e, context="Stop test error")
                    else:
                        self.error_occurred.emit(f"Stop test error: {str(e)}")
        return False

    # ===== Data Sampling =====

    def sample_ttl_state(self, frame_metadata: Dict):
        """
        Sample TTL state synchronized with camera frame.
        Called when each camera frame is recorded.
        """
        with QMutexLocker(self.mutex):
            states = self.current_states.copy()
            counts = self.ttl_pulse_counts.copy()

        if not states:
            return

        ttl_data = {
            "frame_id": frame_metadata.get("frame_id", 0),
            "timestamp_camera": frame_metadata.get("timestamp_ticks", None),
            "timestamp_software": frame_metadata.get("timestamp_software", None),
            "exposure_time_us": frame_metadata.get("exposure_time_us", None),
            "line1_status": frame_metadata.get("line1_status", None),
            "line2_status": frame_metadata.get("line2_status", None),
            "line3_status": frame_metadata.get("line3_status", None),
            "line4_status": frame_metadata.get("line4_status", None),
            "gate_ttl": int(states["gate"]),
            "sync_1hz_ttl": int(states["sync"]),
            "sync_10hz_ttl": int(states["sync"]),
            "barcode_pin0_ttl": int(states["barcode0"]),
            "barcode_pin1_ttl": int(states["barcode1"]),
            "lever_ttl": int(states["lever"]),
            "cue_ttl": int(states["cue"]),
            "reward_ttl": int(states["reward"]),
            "iti_ttl": int(states["iti"]),
            "gate_count": counts["gate"],
            "sync_count": counts["sync"],
            "barcode_count": max(counts["barcode0"], counts["barcode1"]),
            "lever_count": counts["lever"],
            "cue_count": counts["cue"],
            "reward_count": counts["reward"],
            "iti_count": counts["iti"],
        }

        self.ttl_history.append(ttl_data)

    def get_ttl_states(self) -> Optional[Dict]:
        """
        Active polling path: ask board for one `GET_STATES` packet.

        This path is used when firmware supports command/response packets.
        Unsolicited passive lines are still parsed in the same window so no
        behavior marker is dropped.
        """
        try:
            with QMutexLocker(self.mutex):
                if not (self.serial_port and self.serial_port.is_open):
                    return None

                # Keep existing command style
                self.serial_port.write(b"GET_STATES\n")

                parsed = {}
                passive_changed = False
                # Allow noisy serial logs from behavior code and keep command protocol unchanged.
                response_timeout = max(0.12, min(0.35, self.state_query_interval_s + 0.1))
                deadline = time.time() + response_timeout
                while time.time() < deadline:
                    if self.serial_port.in_waiting <= 0:
                        time.sleep(0.002)
                        continue
                    response = self.serial_port.readline().decode(errors="ignore").strip()
                    if not response:
                        continue
                    candidate = self._parse_state_response(response)
                    if candidate:
                        parsed = candidate
                        # Keep draining lines in this window so passive behavior markers
                        # (e.g., CUE_ON/OFF, Lever was pressed) are not discarded.
                        continue
                    if self._apply_passive_line(response):
                        passive_changed = True
                passive_changed = self._apply_transient_state_decay() or passive_changed
                if passive_changed:
                    self.passive_monitor_mode = True
                    if parsed:
                        self._sync_counts_from_packet(parsed)
                        return self._build_state_packet(passive=True, parsed=parsed)
                    return self._build_state_packet(passive=True)
                if not parsed:
                    return None

                self.passive_monitor_mode = False
                for key in self.SIGNAL_KEYS:
                    if key in parsed:
                        self.current_states[key] = bool(parsed[key])
                        self.transient_high_until[key] = 0.0

                self._sync_counts_from_packet(parsed)
                return self._build_state_packet(passive=False, parsed=parsed)
        except Exception as e:
            if self._is_serial_io_error(e):
                self._handle_serial_io_failure(e, context="Arduino read error")
            else:
                self.error_occurred.emit(f"Arduino parse error: {str(e)}")

        return None

    def run(self):
        """
        Worker loop that feeds the GUI with live board states.

        Poll strategy:
        - First consume passive serial lines (event text markers).
        - Then issue `GET_STATES` at a controlled interval.
        - Emit periodic packets even without changes so plots stay alive.
        """
        self.running = True

        while self.running:
            try:
                if self.serial_port and self.serial_port.is_open:
                    previous_packet = self.last_state_packet.copy()
                    now_loop = time.time()
                    # Always parse any passive event lines first to avoid missing behavior markers.
                    states = self._poll_passive_serial_states()
                    query_interval = (
                        self.passive_state_query_interval_s
                        if self.passive_monitor_mode
                        else self.state_query_interval_s
                    )
                    if states is None and (now_loop - self.last_state_query_time) >= query_interval:
                        self.last_state_query_time = now_loop
                        states = self.get_ttl_states()
                    if states is None and self.is_generating and (now_loop - self.last_state_emit) >= 0.2:
                        # Keep plots/status alive even when states are momentarily unchanged.
                        states = self._build_state_packet(passive=self.passive_monitor_mode)

                    if states:
                        state_changed = any(
                            bool(states.get(key, False)) != bool(previous_packet.get(key, False))
                            for key in self.SIGNAL_KEYS
                        )
                        count_changed = self._counts_changed(states, previous_packet)
                        if state_changed:
                            self._record_ttl_event(states, previous_packet)
                        now = time.time()
                        if state_changed or count_changed or (now - self.last_state_emit) >= 0.2:
                            states["pulse_counts"] = self.ttl_pulse_counts.copy()
                            self.ttl_states_updated.emit(states)
                            self._record_live_state_sample(states, now)
                            self.last_state_emit = now
                        self.last_state_packet = states.copy()

                time.sleep(0.01)  # ~100Hz polling to capture TTL edges reliably

            except Exception as e:
                if self._is_serial_io_error(e):
                    self._handle_serial_io_failure(e, context="Arduino read error")
                else:
                    self.error_occurred.emit(f"Arduino read error: {str(e)}")
                time.sleep(0.1)

    def _build_state_packet(self, passive: bool = False, parsed: Optional[Dict] = None) -> Dict:
        """
        Build one normalized packet consumed by GUI plots/counters.

        Packet format is the single source of truth between worker and UI.
        """
        packet = self.current_states.copy()
        packet["gate_count"] = int(self.ttl_pulse_counts["gate"])
        packet["sync_count"] = int(self.ttl_pulse_counts["sync"])
        packet["barcode_count"] = int(max(self.ttl_pulse_counts["barcode0"], self.ttl_pulse_counts["barcode1"]))
        packet["lever_count"] = int(self.ttl_pulse_counts["lever"])
        packet["cue_count"] = int(self.ttl_pulse_counts["cue"])
        packet["reward_count"] = int(self.ttl_pulse_counts["reward"])
        packet["iti_count"] = int(self.ttl_pulse_counts["iti"])

        if parsed:
            for key in (
                "gate_count",
                "sync_count",
                "barcode_count",
                "lever_count",
                "cue_count",
                "reward_count",
                "iti_count",
                "gate_edge_ms",
                "sync_edge_ms",
                "barcode_edge_ms",
                "lever_edge_ms",
                "cue_edge_ms",
                "reward_edge_ms",
                "iti_edge_ms",
            ):
                if key in parsed:
                    packet[key] = int(parsed[key])

        packet["pulse_counts"] = self.ttl_pulse_counts.copy()
        packet["passive_mode"] = bool(passive or self.passive_monitor_mode)
        return packet

    def _poll_passive_serial_states(self) -> Optional[Dict]:
        """
        Drain unsolicited serial lines and map them to signal states.

        This allows camApp to monitor firmware that mainly prints events rather
        than replying to every `GET_STATES` query.
        """
        with QMutexLocker(self.mutex):
            if not (self.serial_port and self.serial_port.is_open):
                return None

            changed = False
            pending = self.serial_port.in_waiting
            if pending > 0:
                deadline = time.time() + 0.08
                while time.time() < deadline:
                    if self.serial_port.in_waiting <= 0:
                        break
                    line = self.serial_port.readline().decode(errors="ignore").strip()
                    if self._apply_passive_line(line):
                        changed = True

            if self._apply_transient_state_decay():
                changed = True

            if changed:
                self.passive_monitor_mode = True
                return self._build_state_packet(passive=True)
        return None

    def _apply_passive_line(self, line: str) -> bool:
        """
        Apply one unsolicited serial line (passive monitor mode).

        Why this exists:
        Some sketches do not answer `GET_STATES` quickly while running behavior
        delays. In that case we still recover signal states by parsing human-
        readable markers printed by firmware (e.g. `CUE_ON`, `Lever was pressed`,
        `SYNC_OFF`, ...).
        """
        text = (line or "").strip()
        if not text:
            return False

        # Accept full state packets as-is when they appear.
        parsed = self._parse_state_response(text)
        if parsed:
            for key in self.SIGNAL_KEYS:
                if key in parsed:
                    self.current_states[key] = bool(parsed[key])
                    self.transient_high_until[key] = 0.0
            self._sync_counts_from_packet(parsed)
            return True

        lower = text.lower()
        if lower in ("ready", "ok_recording", "ok_test", "ok_stopped", "boot_ok", "loop_started"):
            return True
        if lower.startswith("fw:"):
            return True
        normalized = (
            lower
            .replace(" ", "")
            .replace("-", "_")
            .replace(":", "_")
            .replace("=", "_")
        )

        def _set(keys: List[str], value: bool) -> bool:
            changed_local = False
            for key in keys:
                current = bool(self.current_states.get(key, False))
                if current != bool(value):
                    changed_local = True
                self.current_states[key] = bool(value)
                # Explicit ON/OFF events are level states, not transient pulses.
                self.transient_high_until[key] = 0.0
            return changed_local

        def _pulse(keys: List[str]) -> bool:
            now = time.time()
            changed_local = False
            for key in keys:
                if not self.current_states.get(key, False):
                    changed_local = True
                self.current_states[key] = True
                self.transient_high_until[key] = max(
                    self.transient_high_until.get(key, 0.0),
                    now + self.serial_pulse_width_s,
                )
            return changed_local

        # Explicit ON/OFF level events from merged firmware.
        if ("gate_on" in normalized) or ("gateon" == normalized):
            return _set(["gate"], True)
        if ("gate_off" in normalized) or ("gateoff" == normalized):
            return _set(["gate"], False)
        if ("sync_on" in normalized) or ("syncon" == normalized):
            return _set(["sync"], True)
        if ("sync_off" in normalized) or ("syncoff" == normalized):
            return _set(["sync"], False)
        if ("barcode_on" in normalized) or ("barcodeon" == normalized):
            return _set(["barcode0", "barcode1"], True)
        if ("barcode_off" in normalized) or ("barcodeoff" == normalized):
            return _set(["barcode0", "barcode1"], False)
        if ("cue_on" in normalized) or ("cueon" == normalized):
            return _set(["cue"], True)
        if ("cue_off" in normalized) or ("cueoff" == normalized):
            return _set(["cue"], False)
        if ("reward_on" in normalized) or ("rewardon" == normalized):
            return _set(["reward"], True)
        if ("reward_off" in normalized) or ("rewardoff" == normalized):
            return _set(["reward"], False)
        if ("iti_on" in normalized) or ("ition" == normalized):
            return _set(["iti"], True)
        if ("iti_off" in normalized) or ("itioff" == normalized):
            return _set(["iti"], False)
        if ("lever_on" in normalized) or ("leveron" == normalized):
            return _set(["lever"], True)
        if ("lever_off" in normalized) or ("leveroff" == normalized):
            return _set(["lever"], False)

        # Keep behavior sketch text style intact (e.g., "Lever was pressed").
        if "lever" in lower and ("press" in lower or "pressed" in lower):
            # In the provided operant sketch, reward LED is delivered on lever press.
            return _pulse(["lever", "reward"])
        if "cue" in lower or "green" in lower:
            return _pulse(["cue"])
        if "reward" in lower or "blue" in lower:
            return _pulse(["reward"])
        if "iti" in lower or "inter trial" in lower or "red" in lower:
            return _pulse(["iti"])
        if "barcode" in lower:
            return _pulse(["barcode0", "barcode1"])
        if "sync" in lower or "1hz" in lower:
            return _pulse(["sync"])
        if "gate" in lower:
            return _pulse(["gate"])

        return False

    def _apply_transient_state_decay(self) -> bool:
        """Auto-return passive pulse states to LOW after a short hold."""
        now = time.time()
        changed = False
        for key in self.SIGNAL_KEYS:
            until = self.transient_high_until.get(key, 0.0)
            if until <= 0.0:
                continue
            if now >= until and self.current_states.get(key, False):
                self.current_states[key] = False
                self.transient_high_until[key] = 0.0
                changed = True
        return changed

    # ===== Internal Parsers =====

    def _parse_pin_config(self, response: str):
        """Parse pin configuration from Arduino."""
        try:
            parsed = self._parse_pin_config_response(response)
            if not self._is_plausible_pin_config(parsed):
                return

            self._apply_pin_config_dict(parsed)
            self.pin_config_received.emit(self.pin_config.copy())
        except Exception as e:
            self.error_occurred.emit(f"Pin config parse error: {str(e)}")

    def _parse_pin_config_response(self, response: str) -> Dict[str, List[int]]:
        """
        Parse comma-separated pin config.
        Supports single or multi-pin values, for example:
        GATE:3,SYNC:9,BARCODE:18,... or GATE:6,7,SYNC:8,9,...
        """
        if not response:
            return {}

        parts = response.split(",")
        config: Dict[str, List[int]] = {}
        current_key = None

        for raw_part in parts:
            part = raw_part.strip()
            if not part:
                continue
            if ":" in part:
                key_part, value_part = part.split(":", 1)
                normalized_key = self._normalize_pin_key(key_part)
                pin_value = self._safe_int(value_part)
                if normalized_key is None or pin_value is None:
                    current_key = None
                    continue
                config[normalized_key] = [pin_value]
                current_key = normalized_key
            else:
                if current_key is None:
                    continue
                pin_value = self._safe_int(part)
                if pin_value is not None:
                    config[current_key].append(pin_value)

        return config

    def _is_plausible_pin_config(self, config: Optional[Dict[str, List[int]]]) -> bool:
        """
        Validate pin config shape so state packets like gate:1,sync:0 are not
        mistaken for configuration responses.
        """
        if not isinstance(config, dict) or not config:
            return False

        allowed = {"gate", "sync", "barcode", "lever", "cue", "reward", "iti"}
        if not any(key in config for key in ("gate", "sync", "barcode")):
            return False

        saw_non_boolean_pin = False
        for key, pins in config.items():
            if key not in allowed:
                return False
            if not isinstance(pins, list) or not pins:
                return False
            for pin in pins:
                pin_value = self._safe_int(pin, default=None)
                if pin_value is None or pin_value < 0 or pin_value > 99:
                    return False
                if pin_value > 1:
                    saw_non_boolean_pin = True

        return saw_non_boolean_pin

    def _parse_state_response(self, response: str) -> Dict:
        """
        Parse GET_STATES response.
        Supports:
        - Legacy positional: gate,sync,barcode0,barcode1,...
        - Extended positional: gate,sync,barcode0,barcode1,lever,cue,reward,iti,...
        - Keyed pairs: gate:1,sync:0,... or gate=1,sync=0,...
        """
        if not response:
            return {}

        has_keyed_format = False
        for token in response.replace(";", ",").split(","):
            if ":" in token or "=" in token:
                has_keyed_format = True
                break
        if has_keyed_format:
            return self._parse_keyed_state_response(response)

        parts = [part.strip() for part in response.split(",") if part.strip() != ""]
        if len(parts) < 4:
            return {}

        parsed: Dict[str, int] = {}

        # Legacy required fields (strict numeric parse to avoid false positives).
        required_values = [self._safe_int(parts[i], default=None) for i in range(4)]
        if any(value is None for value in required_values):
            return {}
        parsed["gate"] = 1 if int(required_values[0]) != 0 else 0
        parsed["sync"] = 1 if int(required_values[1]) != 0 else 0
        parsed["barcode0"] = 1 if int(required_values[2]) != 0 else 0
        parsed["barcode1"] = 1 if int(required_values[3]) != 0 else 0

        # Legacy + counts: gate,sync,barcode0,barcode1,gate_edge,sync_edge,barcode_edge,gate_count,sync_count,barcode_count
        if 10 <= len(parts) < 15:
            gate_edge = self._safe_int(parts[4], default=None)
            sync_edge = self._safe_int(parts[5], default=None)
            barcode_edge = self._safe_int(parts[6], default=None)
            gate_count = self._safe_int(parts[7], default=None)
            sync_count = self._safe_int(parts[8], default=None)
            barcode_count = self._safe_int(parts[9], default=None)
            if None in (gate_edge, sync_edge, barcode_edge, gate_count, sync_count, barcode_count):
                return {}
            parsed["gate_edge_ms"] = int(gate_edge)
            parsed["sync_edge_ms"] = int(sync_edge)
            parsed["barcode_edge_ms"] = int(barcode_edge)
            parsed["gate_count"] = int(gate_count)
            parsed["sync_count"] = int(sync_count)
            parsed["barcode_count"] = int(barcode_count)
            return parsed

        # Extended states (behavior signals)
        if len(parts) >= 8:
            behavior_values = [self._safe_int(parts[i], default=None) for i in range(4, 8)]
            if any(value is None for value in behavior_values):
                return {}
            parsed["lever"] = 1 if int(behavior_values[0]) != 0 else 0
            parsed["cue"] = 1 if int(behavior_values[1]) != 0 else 0
            parsed["reward"] = 1 if int(behavior_values[2]) != 0 else 0
            parsed["iti"] = 1 if int(behavior_values[3]) != 0 else 0

            tail = parts[8:]
            edge_keys = [
                "gate_edge_ms",
                "sync_edge_ms",
                "barcode_edge_ms",
                "lever_edge_ms",
                "cue_edge_ms",
                "reward_edge_ms",
                "iti_edge_ms",
            ]
            count_keys = [
                "gate_count",
                "sync_count",
                "barcode_count",
                "lever_count",
                "cue_count",
                "reward_count",
                "iti_count",
            ]

            for idx, key in enumerate(edge_keys):
                if idx >= len(tail):
                    break
                value = self._safe_int(tail[idx])
                if value is not None:
                    parsed[key] = value

            count_start = len(edge_keys)
            for idx, key in enumerate(count_keys):
                pos = count_start + idx
                if pos >= len(tail):
                    break
                value = self._safe_int(tail[pos])
                if value is not None:
                    parsed[key] = value

        return parsed

    def _parse_keyed_state_response(self, response: str) -> Dict:
        """Parse keyed state format like gate:1,sync:0 or gate=1,sync=0."""
        parsed: Dict[str, int] = {}
        saw_signal_key = False
        saw_state_meta_key = False
        invalid_signal_value = False
        tokens = response.replace(";", ",").split(",")
        for raw_token in tokens:
            token = raw_token.strip()
            if not token:
                continue
            if ":" in token:
                raw_key, raw_value = token.split(":", 1)
            elif "=" in token:
                raw_key, raw_value = token.split("=", 1)
            else:
                continue

            key = self._normalize_state_key(raw_key)
            if key is None:
                continue

            if key in self.SIGNAL_KEYS:
                value = self._safe_int(raw_value, default=None)
                if value is None:
                    continue
                saw_signal_key = True
                if value in (0, 1):
                    parsed[key] = int(value)
                else:
                    # Likely a pin configuration line (e.g., gate:3,sync:9,...),
                    # not a real state packet.
                    invalid_signal_value = True
            else:
                value = self._safe_int(raw_value)
                if value is not None:
                    parsed[key] = value
                    saw_state_meta_key = True

        if saw_signal_key and invalid_signal_value and not saw_state_meta_key:
            return {}
        return parsed

    def _normalize_pin_key(self, raw_key: str) -> Optional[str]:
        key = str(raw_key).strip().lower().replace(" ", "").replace("-", "").replace("_", "")
        mapping = {
            "gate": "gate",
            "sync": "sync",
            "sync1hz": "sync",
            "barcode": "barcode",
            "barcode0": "barcode",
            "barcode1": "barcode",
            "lever": "lever",
            "cue": "cue",
            "cueled": "cue",
            "ledgreen": "cue",
            "reward": "reward",
            "rewardled": "reward",
            "ledblue": "reward",
            "iti": "iti",
            "itled": "iti",
            "ledred": "iti",
        }
        return mapping.get(key, None)

    def _normalize_state_key(self, raw_key: str) -> Optional[str]:
        key = str(raw_key).strip().lower().replace(" ", "").replace("-", "").replace("_", "")
        mapping = {
            "gate": "gate",
            "sync": "sync",
            "sync1hz": "sync",
            "barcode": "barcode0",
            "barcode0": "barcode0",
            "barcode1": "barcode1",
            "lever": "lever",
            "cue": "cue",
            "reward": "reward",
            "iti": "iti",
            "gateedgems": "gate_edge_ms",
            "syncedgems": "sync_edge_ms",
            "barcodeedgems": "barcode_edge_ms",
            "leveredgems": "lever_edge_ms",
            "cueedgems": "cue_edge_ms",
            "rewardedgems": "reward_edge_ms",
            "itiedgems": "iti_edge_ms",
            "gatecount": "gate_count",
            "synccount": "sync_count",
            "barcodecount": "barcode_count",
            "levercount": "lever_count",
            "cuecount": "cue_count",
            "rewardcount": "reward_count",
            "iticount": "iti_count",
        }
        return mapping.get(key, None)

    # ===== Event Tracking =====

    def _reset_ttl_event_tracking(self):
        """Reset TTL edge tracking and counters."""
        self.ttl_event_history.clear()
        self.live_state_history.clear()
        self.ttl_pulse_counts = {key: 0 for key in self.ttl_pulse_counts}
        self.last_event_state = self.current_states.copy()
        self.last_state_packet = self.current_states.copy()

    def _record_live_state_sample(self, states: Dict, timestamp: Optional[float] = None):
        """Record periodic live TTL/behavior state samples for CSV export."""
        ts = float(timestamp if timestamp is not None else time.time())
        sample = {
            "timestamp_software": ts,
            "passive_mode": int(bool(states.get("passive_mode", self.passive_monitor_mode))),
        }
        for key in self.SIGNAL_KEYS:
            sample[key] = int(bool(states.get(key, False)))

        sample["gate_count"] = int(self.ttl_pulse_counts.get("gate", 0))
        sample["sync_count"] = int(self.ttl_pulse_counts.get("sync", 0))
        sample["barcode_count"] = int(max(self.ttl_pulse_counts.get("barcode0", 0), self.ttl_pulse_counts.get("barcode1", 0)))
        sample["lever_count"] = int(self.ttl_pulse_counts.get("lever", 0))
        sample["cue_count"] = int(self.ttl_pulse_counts.get("cue", 0))
        sample["reward_count"] = int(self.ttl_pulse_counts.get("reward", 0))
        sample["iti_count"] = int(self.ttl_pulse_counts.get("iti", 0))

        with QMutexLocker(self.mutex):
            self.live_state_history.append(sample)
            if len(self.live_state_history) > self.max_live_state_history:
                del self.live_state_history[: len(self.live_state_history) - self.max_live_state_history]

    def _record_ttl_event(self, states: Dict, previous_states: Dict):
        """Record TTL edge events and update pulse counters."""
        timestamp = time.time()
        for key in self.SIGNAL_KEYS:
            current_state = bool(states.get(key, False))
            previous_state = bool(previous_states.get(key, False))
            if current_state == previous_state:
                continue

            edge = "rising" if current_state else "falling"
            if edge == "rising":
                count_key = self.COUNT_KEY_MAP[key]
                if states.get("passive_mode"):
                    self.ttl_pulse_counts[key] += 1
                elif count_key in states:
                    count_value = self._safe_int(states[count_key], default=self.ttl_pulse_counts[key])
                    self.ttl_pulse_counts[key] = int(count_value)
                else:
                    self.ttl_pulse_counts[key] += 1

            self.ttl_event_history.append(
                {
                    "timestamp_software": timestamp,
                    "timestamp_arduino_ms": states.get(self.EDGE_KEY_MAP[key], None),
                    "signal": key,
                    "edge": edge,
                    "state": int(current_state),
                    "count": int(self.ttl_pulse_counts[key]),
                }
            )

        self.last_event_state = {key: bool(states.get(key, False)) for key in self.SIGNAL_KEYS}

    def _sync_counts_from_packet(self, states: Dict):
        """Sync local pulse counts from Arduino-provided counters when available."""
        for key in self.SIGNAL_KEYS:
            count_key = self.COUNT_KEY_MAP[key]
            if count_key not in states:
                continue
            count_value = self._safe_int(states[count_key], default=None)
            if count_value is None:
                continue
            if key in ("barcode0", "barcode1"):
                self.ttl_pulse_counts["barcode0"] = int(count_value)
                self.ttl_pulse_counts["barcode1"] = int(count_value)
            else:
                self.ttl_pulse_counts[key] = int(count_value)

    def _counts_changed(self, states: Dict, previous_states: Dict) -> bool:
        """Detect counter changes from Arduino."""
        for count_key in set(self.COUNT_KEY_MAP.values()):
            if count_key in states and count_key in previous_states:
                if int(states[count_key]) != int(previous_states[count_key]):
                    return True
        return False

    # ===== Helpers =====

    def _is_serial_io_error(self, error: Exception) -> bool:
        """Detect serial I/O access failures that require forced disconnect."""
        if isinstance(error, (serial.SerialException, serial.SerialTimeoutException, PermissionError, OSError)):
            return True

        message = str(error).lower()
        serial_error_markers = (
            "clearcommerror",
            "access is denied",
            "permissionerror",
            "invalid handle",
            "i/o operation has been aborted",
            "device is not connected",
            "cannot open port",
            "file not found",
        )
        return any(marker in message for marker in serial_error_markers)

    def _is_access_denied_error(self, error: Exception) -> bool:
        """Detect Windows permission/port-lock style serial failures."""
        if isinstance(error, PermissionError):
            return True
        message = str(error).lower()
        return ("access is denied" in message) or ("clearcommerror" in message and "denied" in message)

    def _emit_error_throttled(self, message: str):
        """Emit error signal while suppressing rapid duplicate spam."""
        now = time.time()
        if (
            message == self.last_serial_error_message
            and (now - self.last_serial_error_time) < self.serial_error_cooldown_s
        ):
            return
        self.last_serial_error_message = message
        self.last_serial_error_time = now
        self.error_occurred.emit(message)

    def _handle_serial_io_failure(self, error: Exception, context: str):
        """Close lost serial port once and surface a single actionable error."""
        error_text = f"{context}: {str(error)}"
        if self._is_access_denied_error(error):
            status_message = "Serial access denied. Close Arduino Serial Monitor/IDE, then reconnect."
        else:
            status_message = f"Serial connection lost: {str(error)}"

        with QMutexLocker(self.mutex):
            serial_port = self.serial_port
            self.serial_port = None
            self.is_generating = False
            self.passive_monitor_mode = False
            self.current_states = {key: False for key in self.current_states}
            self.transient_high_until = {key: 0.0 for key in self.transient_high_until}

        if serial_port and serial_port.is_open:
            try:
                serial_port.close()
            except Exception:
                pass

        self._emit_error_throttled(error_text)
        self.connection_status.emit(False, status_message)

    def _await_response(self, expected: str, timeout: float = 0.5) -> bool:
        """Wait for a specific response from the Arduino."""
        end_time = time.time() + timeout
        while time.time() < end_time:
            if self.serial_port.in_waiting > 0:
                response = self.serial_port.readline().decode(errors="ignore").strip()
                if self._apply_passive_line(response):
                    self.passive_monitor_mode = True
                if response == expected:
                    return True
            time.sleep(0.01)
        return False

    def _normalize_pin_list(self, value) -> List[int]:
        if isinstance(value, (list, tuple)):
            pins = []
            for entry in value:
                parsed = self._safe_int(entry)
                if parsed is not None:
                    pins.append(parsed)
            return pins

        if value is None:
            return []

        raw = str(value).replace(";", ",")
        pins = []
        for token in raw.split(","):
            parsed = self._safe_int(token.strip())
            if parsed is not None:
                pins.append(parsed)
        return pins

    def _parse_pin_setting_value(self, value) -> List[int]:
        return self._normalize_pin_list(value)

    def _safe_int(self, value, default=None):
        try:
            return int(str(value).strip())
        except Exception:
            return default

    def _safe_bool_int(self, value, default=0) -> int:
        parsed = self._safe_int(value, default=default)
        return 1 if int(parsed) != 0 else 0

    # ===== Public Data Accessors =====

    def stop(self):
        """Stop the worker thread."""
        self.running = False
        if self.is_generating:
            self.stop_recording()
        self.disconnect_port()

    def get_ttl_history(self) -> List[Dict]:
        """Get TTL history for CSV export."""
        with QMutexLocker(self.mutex):
            return self.ttl_history.copy()

    def clear_ttl_history(self):
        """Clear TTL history."""
        with QMutexLocker(self.mutex):
            self.ttl_history.clear()

    def get_ttl_event_history(self) -> List[Dict]:
        """Get TTL edge event history."""
        with QMutexLocker(self.mutex):
            return self.ttl_event_history.copy()

    def get_live_state_history(self) -> List[Dict]:
        """Get periodic live TTL/behavior state samples."""
        with QMutexLocker(self.mutex):
            return self.live_state_history.copy()

    def get_ttl_pulse_counts(self) -> Dict:
        """Get TTL pulse counts based on detected rising edges."""
        with QMutexLocker(self.mutex):
            return self.ttl_pulse_counts.copy()

    def clear_ttl_event_history(self):
        """Clear TTL event history."""
        with QMutexLocker(self.mutex):
            self.ttl_event_history.clear()
            self.live_state_history.clear()
