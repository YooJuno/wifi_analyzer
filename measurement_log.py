"""Bounded, asynchronous measurement sessions in CSV and JSON Lines.

Every accepted event is drained on a normal shutdown. Each scan is flushed
before it is counted as written; file errors stop recording visibly rather
than silently dropping samples. Segments are retained, never overwritten.
"""

from __future__ import annotations

import csv
import io
import json
import os
import platform
import queue
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from diagnostics import SEVERITY_ORDER, potential_rf_pressure_dbm, rf_relations


CSV_FIELDS = (
    "timestamp", "connected", "ssid", "bssid", "interface", "band_ghz",
    "channel", "width_mhz", "rssi_dbm", "rssi_estimated", "noise_dbm", "snr_db", "tx_rate_mbps",
    "observed_bss", "overlapping_bss", "strong_overlapping_bss",
    "rf_pressure_dbm", "rf_comparison_complete", "severity", "finding_codes", "scan_duration_ms", "source",
)
MAX_SEGMENT_BYTES = 10 * 1024 * 1024


def timestamp(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value).astimezone().isoformat(
        timespec="milliseconds")


def observation(ap):
    return {
        "key": ap.key, "ssid": ap.ssid, "bssid": ap.bssid, "band": ap.band,
        "channel": ap.ch, "width": ap.width if ap.width_known else None,
        "width_known": ap.width_known, "rssi": ap.rssi, "noise": ap.noise,
        "rssi_estimated": ap.rssi_estimated,
        "snr": ap.snr if ap.noise is not None and ap.noise < 0 else None,
        "security": ap.sec, "phy": ap.phy, "current": ap.cur,
        "channel_utilization": ap.channel_utilization, "station_count": ap.station_count,
    }


def scan_record(snapshot, current, findings, recommendations, elapsed):
    """Copy mutable scanner objects before passing a sample to the writer."""
    observed_at = snapshot.timestamp or time.time()
    connection = snapshot.connection
    relations = rf_relations(current, snapshot.aps) if current else []
    rssi = current.rssi if current else connection.rssi
    noise = current.noise if current else connection.noise
    band = current.band if current else connection.band
    severity = min((item.severity for item in findings),
                   key=lambda value: SEVERITY_ORDER.get(value, 99), default="unknown")
    summary = {
        "timestamp": timestamp(observed_at), "connected": connection.connected,
        "ssid": current.ssid if current else connection.ssid,
        "bssid": current.bssid if current else connection.bssid,
        "interface": connection.interface, "band_ghz": 2.4 if band == 2 else band,
        "channel": current.ch if current else connection.channel,
        "width_mhz": (current.width if current.width_known else None) if current else connection.width,
        "rssi_dbm": rssi, "noise_dbm": noise,
        "rssi_estimated": current.rssi_estimated if current else connection.rssi_estimated,
        "snr_db": rssi - noise if rssi is not None and noise is not None and noise < 0 else None,
        "tx_rate_mbps": connection.tx_rate, "observed_bss": len(snapshot.aps),
        "overlapping_bss": len(relations),
        "strong_overlapping_bss": sum(item.other.rssi >= -67 for item in relations),
        "rf_pressure_dbm": potential_rf_pressure_dbm(current, snapshot.aps) if current else None,
        "rf_comparison_complete": bool(current and current.width_known and all(
            ap.width_known for ap in snapshot.aps if ap.band == current.band)),
        "severity": severity, "finding_codes": ";".join(item.code for item in findings),
        "scan_duration_ms": round(elapsed * 1000, 1), "source": snapshot.source,
    }
    return {
        "schema_version": 1, "type": "scan", "timestamp": summary["timestamp"],
        "timestamp_unix": observed_at, "summary": summary, "connection": asdict(connection),
        "warnings": list(snapshot.warnings), "findings": [asdict(item) for item in findings],
        "recommendations": [asdict(item) for item in recommendations],
        "observations": [observation(ap) for ap in snapshot.aps],
        "overlaps": [{"bssid": item.other.bssid, "ssid": item.other.ssid,
                       "channel": item.other.ch, "rssi": item.other.rssi,
                       "kind": item.kind, "overlap_mhz": item.overlap_mhz,
                       "target_overlap_ratio": item.target_overlap_ratio} for item in relations],
    }


def _csv_bytes(values):
    # Keep network-controlled names as literal spreadsheet cells. Raw values
    # remain unchanged in JSONL, including names beginning with '=' or '+'.
    cells = ["'" + value if isinstance(value, str) and value.lstrip().startswith(
        ("=", "+", "-", "@")) else value for value in values]
    stream = io.StringIO(newline="")
    csv.writer(stream).writerow(cells)
    return stream.getvalue().encode("utf-8")


class MeasurementRecorder:
    def __init__(self, directory, *, metadata=None, max_segment_bytes=MAX_SEGMENT_BYTES,
                 queue_size=32, on_finished=None):
        if max_segment_bytes < 1024 or queue_size < 1:
            raise ValueError("Invalid log segment or queue size")
        self.session_id = f"wifi-log-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.directory = Path(directory).expanduser().resolve() / self.session_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self._metadata = dict(metadata or {})
        self._max_bytes = max_segment_bytes
        self._queue = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self.finished = threading.Event()
        self._lock = threading.Lock()
        self._accepting = True
        self._error = None
        self._samples_written = 0
        self._rejected = 0
        self._on_finished = on_finished
        self._json_file = self._csv_file = None
        self._segment = 0
        self._segment_events = 0
        self._worker = threading.Thread(target=self._run, name="wifi-log", daemon=False)
        self._worker.start()

    @property
    def error(self):
        with self._lock:
            return self._error

    @property
    def samples_written(self):
        with self._lock:
            return self._samples_written

    @property
    def recording(self):
        with self._lock:
            return self._accepting

    def record_scan(self, snapshot, current, findings, recommendations, elapsed):
        return self._enqueue(scan_record(snapshot, current, findings, recommendations, elapsed))

    def record_event(self, event_type, data=None):
        return self._enqueue({"schema_version": 1, "type": event_type,
                              "timestamp": timestamp(), "data": deepcopy(data) if data else {}})

    def _enqueue(self, record):
        with self._lock:
            if not self._accepting:
                return False
            try:
                self._queue.put_nowait(record)
                return True
            except queue.Full:
                self._error = "로그 저장 대기열이 가득 차 기록을 중지했습니다. 저장 장치를 확인하세요."
                self._rejected += 1
                self._accepting = False
                self._stop.set()
                return False

    def stop(self):
        with self._lock:
            self._accepting = False
            self._stop.set()

    def close(self, timeout=None):
        self.stop()
        self._worker.join(timeout)
        if self._worker.is_alive():
            raise TimeoutError("Measurement log is still being flushed")
        if self.error:
            raise OSError(self.error)

    def _open_segment(self):
        self._segment += 1
        name = self.directory / f"measurements-{self._segment:04d}"
        self._json_file = name.with_suffix(".jsonl").open("xb")
        self._csv_file = name.with_suffix(".csv").open("xb")
        self._csv_file.write(b"\xef\xbb\xbf" + _csv_bytes(CSV_FIELDS))
        self._segment_events = 0
        self._write({"schema_version": 1,
                     "type": "session_start" if self._segment == 1 else "segment_start",
                     "timestamp": timestamp(), "session_id": self.session_id,
                     "segment": self._segment, "platform": platform.system(),
                     "rf_metric": "observed_bss_pressure_not_measured_interference",
                     "metadata": self._metadata}, rotate=False)

    def _write(self, record, *, rotate=True):
        encoded = (json.dumps(record, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")) + "\n").encode("utf-8")
        row = _csv_bytes(record["summary"].get(key) for key in CSV_FIELDS) if record["type"] == "scan" else b""
        if rotate and self._segment_events and max(
                self._json_file.tell() + len(encoded), self._csv_file.tell() + len(row)) > self._max_bytes:
            self._close_files()
            self._open_segment()
        self._json_file.write(encoded)
        self._json_file.flush()
        os.fsync(self._json_file.fileno())
        if row:
            self._csv_file.write(row)
            self._csv_file.flush()
            os.fsync(self._csv_file.fileno())
        self._segment_events += 1

    def _close_files(self):
        first_error = None
        for handle in (self._json_file, self._csv_file):
            if handle is not None:
                try:
                    handle.close()
                except OSError as exc:
                    first_error = first_error or exc
        self._json_file = self._csv_file = None
        if first_error:
            raise first_error

    def _run(self):
        try:
            self._open_segment()
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    record = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                self._write(record)
                if record["type"] == "scan":
                    with self._lock:
                        self._samples_written += 1
            self._write({"schema_version": 1, "type": "session_end", "timestamp": timestamp(),
                         "samples_written": self.samples_written, "error": self.error,
                         "rejected_records": self._rejected})
        except Exception as exc:
            with self._lock:
                self._error = self._error or f"로그 저장 실패: {exc}"
        finally:
            self.stop()
            try:
                self._close_files()
            except OSError as exc:
                with self._lock:
                    self._error = self._error or f"로그 마감 실패: {exc}"
            self.finished.set()
            if self._on_finished:
                self._on_finished(self)
