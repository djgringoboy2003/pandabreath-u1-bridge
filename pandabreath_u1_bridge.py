#!/usr/bin/env python3
"""Snapmaker U1 to PandaBreath chamber-control bridge."""

from __future__ import annotations

import argparse
import asyncio
import copy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import websockets
import yaml


OFF_STATES = {"standby", "idle", "complete", "cancelled", "canceled", "error", "unknown"}
PRINTING_STATES = {"printing"}
PAUSED_STATES = {"paused"}


class BridgeStatus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "service": "starting",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": None,
            "moonraker_ok": None,
            "pandabreath_ok": None,
            "printer_state": None,
            "filename": None,
            "material_raw": None,
            "material_normalized": None,
            "material_source": None,
            "active_tool_index": None,
            "selected_target_c": None,
            "decision_reason": None,
            "chamber_temp_c": None,
            "u1_cavity_temp_c": None,
            "temp_delta_c": None,
            "effective_control_temp_c": None,
            "temperature_strategy": "pandabreath",
            "panda_set_temp_c": None,
            "panda_work_mode": None,
            "panda_work_on": None,
            "cooldown_active": False,
            "cooldown_until": None,
            "manual_off_until": None,
            "session_active": False,
            "session_id": None,
            "session_started_at": None,
            "session_elapsed_minutes": None,
            "last_session_summary": None,
            "heater_action": None,
            "alerts": [],
            "dry_run": True,
            "control_enabled": True,
            "dashboard_controls_enabled": False,
            "last_error": None,
        }

    def update(self, **values: Any) -> None:
        with self._lock:
            self._data.update(values)
            self._data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)


def load_config(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    required = [
        ("printer", "moonraker_url"),
        ("panda_breath", "web_url"),
        ("control", "dry_run"),
        ("materials", "DEFAULT"),
    ]
    missing = [f"{section}.{key}" for section, key in required if key not in config.get(section, {})]
    if missing:
        raise ValueError(f"Missing required config keys: {', '.join(missing)}")

    strategy = str(config.get("temperature_control", {}).get("strategy", "pandabreath")).lower()
    if strategy not in {"pandabreath", "u1_cavity", "max", "average"}:
        raise ValueError(f"Unsupported temperature_control.strategy: {strategy}")

    dashboard_port = int(config.get("dashboard", {}).get("port", 8081))
    if not 1 <= dashboard_port <= 65535:
        raise ValueError(f"dashboard.port out of range: {dashboard_port}")

    safety_limit = int(
        config.get("safety", {}).get(
            "absolute_safety_limit_c",
            config.get("control", {}).get("absolute_safety_limit_c", 70),
        )
    )
    if safety_limit <= 0:
        raise ValueError("safety.absolute_safety_limit_c must be greater than 0")
    for material, target in config.get("materials", {}).items():
        if int(target) < 0:
            raise ValueError(f"materials.{material} cannot be negative")


def sd_notify(message: str) -> None:
    notify_socket = os.environ.get("NOTIFY_SOCKET")
    if not notify_socket:
        return
    address: str | bytes = notify_socket
    if notify_socket.startswith("@"):
        address = "\0" + notify_socket[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        logging.debug("systemd notify failed: %s", exc)


def setup_logging(config: dict[str, Any]) -> None:
    log_cfg = config.get("logging", {})
    level = getattr(logging, str(log_cfg.get("level", "INFO")).upper(), logging.INFO)
    log_file = Path(log_cfg.get("file", "logs/pandabreath-u1.log"))
    log_file.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=int(log_cfg.get("max_bytes", 1048576)),
        backupCount=int(log_cfg.get("backup_count", 5)),
    )
    file_handler.setFormatter(fmt)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    root.addHandler(file_handler)
    root.addHandler(console_handler)


def normalize_material(material: str | None) -> str:
    if not material:
        return "DEFAULT"
    value = re.sub(r"\s+", " ", str(material).strip().upper())
    if ";" in value or "," in value:
        parts = [normalize_material(part) for part in re.split(r"[;,]", value) if part.strip()]
        non_default = [part for part in parts if part != "DEFAULT"]
        if non_default and len(set(non_default)) == 1:
            return non_default[0]
        if non_default:
            return non_default[0]
    value = value.replace("_", "-")
    aliases = {
        "PLA+": "PLA",
        "PLA PLUS": "PLA",
        "PETG-CF": "PETG",
        "PETG CF": "PETG",
        "ASA-CF": "ASA",
        "ASA CF": "ASA",
        "ABS-GF": "ABS",
        "ABS CF": "ABS",
        "PA6": "PA",
        "PA12": "PA",
        "PA-CF": "PA",
        "NYLON": "PA",
    }
    if value in aliases:
        return aliases[value]
    for prefix in ("PLA", "PETG", "ASA", "ABS", "TPU"):
        if value == prefix or value.startswith(prefix + " ") or value.startswith(prefix + "-"):
            return prefix
    if value.startswith("PA6") or value.startswith("PA12") or value.startswith("PA-"):
        return "PA"
    return value


def material_target(material: str | None, config: dict[str, Any]) -> tuple[str, int]:
    materials = config.get("materials", {})
    if material and (";" in str(material) or "," in str(material)):
        parts = [normalize_material(part) for part in re.split(r"[;,]", str(material)) if part.strip()]
        known = [(part, int(materials[part])) for part in parts if part in materials]
        if known:
            chosen = max(known, key=lambda item: item[1])
            return chosen

    normalized = normalize_material(material)
    if normalized in materials:
        return normalized, int(materials[normalized])
    logging.warning("Unknown material %r normalized to %s; using DEFAULT", material, normalized)
    return normalized, int(materials.get("DEFAULT", 0))


def is_mixed_material_metadata(material: str | None, config: dict[str, Any]) -> bool:
    if not material or (";" not in str(material) and "," not in str(material)):
        return False
    materials = config.get("materials", {})
    parts = [normalize_material(part) for part in re.split(r"[;,]", str(material)) if part.strip()]
    known = [part for part in parts if part in materials]
    return len(set(known)) > 1


def cap_target(target_c: int, config: dict[str, Any]) -> int:
    limit = int(config.get("safety", {}).get("absolute_safety_limit_c", config.get("control", {}).get("absolute_safety_limit_c", 70)))
    if target_c > limit:
        logging.error("Configured target %sC exceeds safety limit %sC; capping", target_c, limit)
        return limit
    return max(0, int(target_c))


def control_temperature(strategy: str, panda_temp: float | None, u1_temp: float | None) -> float | None:
    strategy = (strategy or "pandabreath").lower()
    if strategy == "u1_cavity":
        return u1_temp
    if strategy == "max":
        values = [value for value in (panda_temp, u1_temp) if value is not None]
        return max(values) if values else None
    if strategy == "average":
        values = [value for value in (panda_temp, u1_temp) if value is not None]
        return round(sum(values) / len(values), 1) if values else None
    return panda_temp


def html_escape(value: Any) -> str:
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def history_path(config: dict[str, Any]) -> Path:
    return Path(config.get("history", {}).get("file", "logs/history.jsonl"))


def append_history(config: dict[str, Any], point: dict[str, Any]) -> None:
    if not bool(config.get("history", {}).get("enabled", True)):
        return
    path = history_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(point, sort_keys=True) + "\n")
    prune_history(config)


def prune_history(config: dict[str, Any]) -> None:
    max_lines = int(config.get("history", {}).get("max_lines", 10000))
    if max_lines <= 0:
        return
    path = history_path(config)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= max_lines:
            return
        path.write_text("\n".join(lines[-max_lines:]) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logging.error("Failed to prune history: %s", exc)


def read_history(config: dict[str, Any]) -> list[dict[str, Any]]:
    path = history_path(config)
    limit = int(config.get("history", {}).get("dashboard_points", 240))
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(line) for line in lines if line.strip()]
    except Exception as exc:  # noqa: BLE001
        logging.error("Failed to read history: %s", exc)
        return []


def sessions_path(config: dict[str, Any]) -> Path:
    return Path(config.get("sessions", {}).get("file", "logs/sessions.jsonl"))


def append_session_summary(config: dict[str, Any], summary: dict[str, Any]) -> None:
    if not bool(config.get("sessions", {}).get("enabled", True)):
        return
    path = sessions_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(summary, sort_keys=True) + "\n")


def read_session_summaries(config: dict[str, Any]) -> list[dict[str, Any]]:
    path = sessions_path(config)
    limit = int(config.get("sessions", {}).get("dashboard_sessions", 20))
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(line) for line in lines if line.strip()]
    except Exception as exc:  # noqa: BLE001
        logging.error("Failed to read session summaries: %s", exc)
        return []


class PrintSessionTracker:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.current: dict[str, Any] | None = None
        self.last_summary: dict[str, Any] | None = None

    def update(
        self,
        snapshot: PrinterSnapshot,
        normalized_material: str,
        target_c: int,
        panda_temp_c: float | int | None,
        u1_cavity_temp_c: float | int | None,
        effective_temp_c: float | int | None,
        alerts: list[str],
        now: float | None = None,
    ) -> dict[str, Any]:
        now = now if now is not None else time.time()
        active = snapshot.state in PRINTING_STATES or snapshot.state in PAUSED_STATES
        if active and self.current is None:
            self.current = self._new_session(snapshot, normalized_material, target_c, now)
            logging.info("Print session started: %s", self.current["session_id"])
        if self.current is not None:
            self._add_sample(target_c, panda_temp_c, u1_cavity_temp_c, effective_temp_c, alerts, now)
            self.current["last_state"] = snapshot.state
            self.current["filename"] = snapshot.filename or self.current.get("filename")
            self.current["material"] = normalized_material or self.current.get("material")
            self.current["max_target_c"] = max(int(self.current.get("max_target_c", 0)), int(target_c or 0))
        if not active and self.current is not None:
            summary = self._finish(snapshot.state, now)
            self.last_summary = summary
            append_session_summary(self.config, summary)
            logging.info("Print session finished: %s", summary)
        return self.snapshot(now)

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.time()
        if self.current is None:
            return {"active": False, "current": None, "last": self.last_summary}
        current = dict(self.current)
        current["elapsed_minutes"] = round((now - float(current["started_ts"])) / 60, 1)
        return {"active": True, "current": self._summary_from(current), "last": self.last_summary}

    def _new_session(
        self,
        snapshot: PrinterSnapshot,
        normalized_material: str,
        target_c: int,
        now: float,
    ) -> dict[str, Any]:
        return {
            "session_id": time.strftime("%Y%m%d-%H%M%S", time.localtime(now)),
            "started_ts": now,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "filename": snapshot.filename,
            "material": normalized_material,
            "max_target_c": int(target_c or 0),
            "last_state": snapshot.state,
            "samples": 0,
            "panda_sum": 0.0,
            "panda_count": 0,
            "panda_min_c": None,
            "panda_max_c": None,
            "u1_sum": 0.0,
            "u1_count": 0,
            "u1_min_c": None,
            "u1_max_c": None,
            "effective_sum": 0.0,
            "effective_count": 0,
            "near_target_samples": 0,
            "heater_target_samples": 0,
            "alerts": [],
        }

    def _add_sample(
        self,
        target_c: int,
        panda_temp_c: float | int | None,
        u1_cavity_temp_c: float | int | None,
        effective_temp_c: float | int | None,
        alerts: list[str],
        now: float,
    ) -> None:
        if self.current is None:
            return
        self.current["samples"] += 1
        self.current["last_sample_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        self._record_temp("panda", panda_temp_c)
        self._record_temp("u1", u1_cavity_temp_c)
        self._record_effective(effective_temp_c)
        if target_c > 0:
            self.current["heater_target_samples"] += 1
            if effective_temp_c is not None and effective_temp_c >= target_c - 2:
                self.current["near_target_samples"] += 1
        for alert in alerts:
            if alert not in self.current["alerts"]:
                self.current["alerts"].append(alert)

    def _record_temp(self, prefix: str, value: float | int | None) -> None:
        if self.current is None or value is None:
            return
        value = float(value)
        self.current[f"{prefix}_sum"] += value
        self.current[f"{prefix}_count"] += 1
        min_key = f"{prefix}_min_c"
        max_key = f"{prefix}_max_c"
        self.current[min_key] = value if self.current[min_key] is None else min(float(self.current[min_key]), value)
        self.current[max_key] = value if self.current[max_key] is None else max(float(self.current[max_key]), value)

    def _record_effective(self, value: float | int | None) -> None:
        if self.current is None or value is None:
            return
        self.current["effective_sum"] += float(value)
        self.current["effective_count"] += 1

    def _finish(self, end_state: str, now: float) -> dict[str, Any]:
        if self.current is None:
            return {}
        self.current["ended_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        self.current["end_state"] = end_state
        summary = self._summary_from(self.current, now)
        self.current = None
        return summary

    def _summary_from(self, data: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else float(data.get("started_ts", time.time()))
        panda_count = int(data.get("panda_count", 0))
        u1_count = int(data.get("u1_count", 0))
        effective_count = int(data.get("effective_count", 0))
        heater_samples = int(data.get("heater_target_samples", 0))
        return {
            "session_id": data.get("session_id"),
            "started_at": data.get("started_at"),
            "ended_at": data.get("ended_at"),
            "end_state": data.get("end_state"),
            "elapsed_minutes": round((now - float(data.get("started_ts", now))) / 60, 1),
            "filename": data.get("filename"),
            "material": data.get("material"),
            "max_target_c": data.get("max_target_c"),
            "samples": data.get("samples", 0),
            "panda_avg_c": round(data["panda_sum"] / panda_count, 1) if panda_count else None,
            "panda_min_c": data.get("panda_min_c"),
            "panda_max_c": data.get("panda_max_c"),
            "u1_avg_c": round(data["u1_sum"] / u1_count, 1) if u1_count else None,
            "u1_min_c": data.get("u1_min_c"),
            "u1_max_c": data.get("u1_max_c"),
            "effective_avg_c": round(data["effective_sum"] / effective_count, 1) if effective_count else None,
            "near_target_percent": round(100 * int(data.get("near_target_samples", 0)) / heater_samples, 1)
            if heater_samples
            else None,
            "alerts": data.get("alerts", []),
        }


@dataclass
class PrinterSnapshot:
    state: str
    filename: str
    material: str | None
    u1_cavity_temp_c: float | None = None
    active_tool_index: int | None = None
    material_source: str = "unknown"


class MoonrakerClient:
    def __init__(self, config: dict[str, Any]) -> None:
        printer = config["printer"]
        self.base_url = printer["moonraker_url"].rstrip("/")
        self.timeout = int(printer.get("request_timeout_seconds", 5))

    def get_snapshot(self) -> PrinterSnapshot:
        status = self._get_json("/printer/objects/query?print_stats&temperature_sensor%20cavity&extruder")["result"]["status"]
        stats = status.get("print_stats", {})
        cavity = status.get("temperature_sensor cavity", {})
        extruder = status.get("extruder", {})
        state = str(stats.get("state") or "unknown").lower()
        filename = str(stats.get("filename") or "")
        u1_cavity_temp_c = self._numeric(cavity.get("temperature"))
        active_tool_index = self._active_tool_index(extruder)
        material = self._metadata_material(filename, active_tool_index)
        material_source = "metadata"
        if not material:
            material = self._filename_material(filename)
            material_source = "filename" if material else "unknown"
        return PrinterSnapshot(
            state=state,
            filename=filename,
            material=material,
            u1_cavity_temp_c=u1_cavity_temp_c,
            active_tool_index=active_tool_index,
            material_source=material_source,
        )

    def _get_json(self, path: str) -> dict[str, Any]:
        response = requests.get(f"{self.base_url}{path}", timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _numeric(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _active_tool_index(extruder: dict[str, Any]) -> int | None:
        candidates = []
        real_stats = extruder.get("real_extruder_stats")
        if isinstance(real_stats, list) and real_stats:
            candidates.append(real_stats[0])
        candidates.append(extruder.get("extruder_index"))
        for candidate in candidates:
            if isinstance(candidate, str):
                match = re.search(r"extruder(\d+)$", candidate)
                if match:
                    return int(match.group(1))
                if candidate == "extruder":
                    return 0
            if isinstance(candidate, int):
                return candidate
        return None

    def _metadata_material(self, filename: str, active_tool_index: int | None = None) -> str | None:
        if not filename:
            return None
        try:
            data = self._get_json(f"/server/files/metadata?filename={requests.utils.quote(filename)}")
        except Exception as exc:  # noqa: BLE001 - metadata is optional
            logging.debug("Moonraker metadata unavailable for %s: %s", filename, exc)
            return None
        result = data.get("result", {})
        candidates: list[Any] = [
            result.get("filament_type"),
            result.get("filament_name"),
            result.get("material"),
        ]
        for key in ("filament_settings_id", "filament_vendor"):
            candidates.append(result.get(key))
        for candidate in candidates:
            if isinstance(candidate, list) and candidate:
                if active_tool_index is not None and 0 <= active_tool_index < len(candidate):
                    candidate = candidate[active_tool_index]
                else:
                    candidate = ";".join(str(item) for item in candidate if item)
            elif isinstance(candidate, str):
                candidate = self._indexed_metadata_value(candidate, active_tool_index)
            if candidate:
                return str(candidate)
        return None

    @staticmethod
    def _indexed_metadata_value(value: str, active_tool_index: int | None = None) -> str:
        parts = [part.strip().strip('"') for part in re.split(r'";"|[;,]', value) if part.strip()]
        if active_tool_index is not None and 0 <= active_tool_index < len(parts):
            return parts[active_tool_index]
        return value

    @staticmethod
    def _filename_material(filename: str) -> str | None:
        base = Path(filename).name.upper()
        tokens = re.split(r"[^A-Z0-9+]+", base)
        for token in tokens:
            normalized = normalize_material(token)
            if normalized in {"PLA", "TPU", "PETG", "ABS", "ASA", "PA"}:
                return token
        return None


class PandaBreathClient:
    # Delay between reconnect attempts for the persistent connection.
    RECONNECT_DELAY = 5.0

    def __init__(self, config: dict[str, Any]) -> None:
        panda = config["panda_breath"]
        control = config["control"]
        self.web_url = panda["web_url"].rstrip("/")
        self.ws_url = self.web_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
        self.connect_timeout = int(panda.get("connect_timeout_seconds", 5))
        self.command_timeout = int(panda.get("command_timeout_seconds", 5))
        # A persistent connection is healthy only if a frame (state push or pong)
        # arrived within this window; otherwise it is treated as stale/down.
        self.stale_seconds = int(panda.get("stale_seconds", 30))
        self.dry_run = bool(control.get("dry_run", True))
        self.last_state: dict[str, Any] = {}
        # Persistent-connection machinery (idle until start()).
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._conn_task: asyncio.Task[Any] | None = None
        self._ws: Any = None
        self._connected = False
        self._last_message_at = 0.0
        self._desired: list[dict[str, Any]] | None = None
        self._stop = threading.Event()

    # ── lifecycle ───────────────────────────────────────────────────────────

    def check_status(self) -> dict[str, Any]:
        """Start the persistent connection and wait briefly for first state."""
        self.start()
        deadline = time.time() + max(self.connect_timeout, 10)
        while time.time() < deadline:
            if self.is_fresh():
                logging.info("PandaBreath status ok; dry_run=%s", self.dry_run)
                return self.snapshot_state()
            time.sleep(0.25)
        logging.warning("PandaBreath did not report state within the startup window")
        return self.snapshot_state()

    def snapshot_state(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.last_state)

    def is_fresh(self) -> bool:
        """True if connected and a frame arrived within stale_seconds."""
        with self._lock:
            if not self._connected:
                return False
            return (time.time() - self._last_message_at) < self.stale_seconds

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, name="panda-ws", daemon=True)
        self._thread.start()
        logging.info("PandaBreath persistent connection thread started")

    def stop(self) -> None:
        self._stop.set()
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._thread = None

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._conn_task = loop.create_task(self._run_connection())
        try:
            loop.run_forever()
        finally:
            self._conn_task.cancel()
            try:
                loop.run_until_complete(asyncio.gather(self._conn_task, return_exceptions=True))
            except Exception:  # noqa: BLE001
                pass
            loop.close()
            self._loop = None

    async def _run_connection(self) -> None:
        """Maintain one persistent connection; reconnect on any error and
        re-send the last desired command so the device stays in sync."""
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.ws_url,
                    open_timeout=self.connect_timeout,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as ws:
                    with self._lock:
                        self._ws = ws
                        self._connected = True
                        self._last_message_at = time.time()
                        desired = self._desired
                    logging.info("PandaBreath persistent connection established")
                    if desired is not None and not self.dry_run:
                        try:
                            await self._send_over(ws, desired)
                            logging.info("PandaBreath re-sent desired state after connect")
                        except Exception as exc:  # noqa: BLE001
                            logging.warning("PandaBreath resend after connect failed: %s", exc)
                    async for message in ws:
                        try:
                            data = json.loads(message)
                        except (ValueError, TypeError):
                            continue
                        if isinstance(data, dict):
                            with self._lock:
                                self._deep_merge(self.last_state, data)
                                self._last_message_at = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logging.warning(
                    "PandaBreath WS error (%s); reconnecting in %ss", exc, self.RECONNECT_DELAY
                )
            finally:
                with self._lock:
                    self._ws = None
                    self._connected = False
            if self._stop.is_set():
                break
            try:
                await asyncio.sleep(self.RECONNECT_DELAY)
            except asyncio.CancelledError:
                raise

    @staticmethod
    def _deep_merge(target: dict[str, Any], update: dict[str, Any]) -> None:
        for key, value in update.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                target[key].update(value)
            else:
                target[key] = value

    def get_current_temperature(self) -> int | None:
        with self._lock:
            state = self.last_state
            settings = state.get("settings", {}) if isinstance(state, dict) else {}
            # Prefer the device's ADC-calibrated reading; fall back to the raw one.
            value = settings.get("cal_warehouse_temp", settings.get("warehouse_temper"))
        if value is None:
            return None
        try:
            return int(round(float(value)))
        except (TypeError, ValueError):
            return None

    def get_control_fields(self) -> dict[str, Any]:
        with self._lock:
            settings = self.last_state.get("settings", {}) if isinstance(self.last_state, dict) else {}
            return {
                "panda_set_temp_c": settings.get("set_temp"),
                "panda_work_mode": settings.get("work_mode"),
                "panda_work_on": settings.get("work_on"),
            }

    @staticmethod
    def has_complete_control_state(state: dict[str, Any]) -> bool:
        settings = state.get("settings", {}) if isinstance(state, dict) else {}
        return all(settings.get(key) is not None for key in ("set_temp", "work_mode", "work_on"))

    def set_target_temperature(self, target_c: int) -> None:
        if target_c <= 0:
            self.heater_off()
            return
        # Discrete, ordered writes that match the stock web UI's update order for
        # device-firmware (v1.0.3) compatibility — the device applies separate
        # setting writes more reliably than one combined payload. The leading
        # isrunning:0 clears any active drying/auto cycle before commanding
        # manual (work_mode 2) heat.
        messages = [
            {"settings": {"isrunning": 0}},
            {"settings": {"work_mode": 2}},
            {"settings": {"set_temp": int(target_c)}},
            {"settings": {"work_on": True}},
        ]
        self._send(messages, f"set target {target_c}C")

    def heater_off(self) -> None:
        # isrunning:0 first so an active drying/auto cycle is actually stopped,
        # not just the manual heater flag cleared.
        messages = [
            {"settings": {"isrunning": 0}},
            {"settings": {"work_on": False}},
        ]
        self._send(messages, "heater off")

    def _send(self, messages: list[dict[str, Any]], reason: str) -> None:
        # Record the latest desired command so it can be re-sent on reconnect.
        with self._lock:
            self._desired = messages
        if self.dry_run:
            logging.info("DRY-RUN PandaBreath command suppressed (%s): %s", reason, messages)
            return
        try:
            if self._loop is not None and self._connected:
                self._send_persistent(messages)
            else:
                # No live persistent connection (not started, or mid-reconnect):
                # fall back to a one-shot connection.
                asyncio.run(self._send_ws(messages, reason))
            logging.info(
                "PandaBreath command sent (%s): %d message(s): %s", reason, len(messages), messages
            )
        except Exception as exc:  # noqa: BLE001
            # Best-effort: the desired state is recorded and will be re-sent on
            # the next (re)connect, so a transient failure is not fatal.
            logging.warning(
                "PandaBreath send failed (%s): %s; will re-send on reconnect", reason, exc
            )

    def _send_persistent(self, messages: list[dict[str, Any]]) -> None:
        loop = self._loop
        if loop is None:
            raise RuntimeError("PandaBreath event loop not running")
        fut = asyncio.run_coroutine_threadsafe(self._send_over(self._ws, messages), loop)
        fut.result(timeout=self.command_timeout * max(1, len(messages)) + 2)

    async def _send_over(self, ws: Any, messages: list[dict[str, Any]]) -> None:
        if ws is None:
            raise RuntimeError("no active PandaBreath connection")
        for payload in messages:
            await asyncio.wait_for(ws.send(json.dumps(payload)), timeout=self.command_timeout)

    async def _send_ws(self, messages: list[dict[str, Any]], reason: str) -> None:
        # One-shot fallback: open a connection, send the ordered messages, close.
        async with websockets.connect(self.ws_url, open_timeout=self.connect_timeout) as ws:
            await self._send_over(ws, messages)

    def command_matches_state(self, target_c: int) -> tuple[bool, str]:
        if self.dry_run:
            return True, "dry-run"
        with self._lock:
            state = copy.deepcopy(self.last_state)
        if not self.has_complete_control_state(state):
            return False, "control state incomplete"
        settings = state.get("settings", {})
        if target_c <= 0:
            actual_on = settings.get("work_on")
            return actual_on in (0, False), f"work_on={actual_on}"
        actual_target = settings.get("set_temp")
        actual_mode = settings.get("work_mode")
        actual_on = settings.get("work_on")
        matched = actual_target == target_c and actual_mode == 2 and actual_on in (1, True)
        return matched, f"set_temp={actual_target} work_mode={actual_mode} work_on={actual_on}"


def render_dashboard(status: dict[str, Any], config: dict[str, Any]) -> str:
    controls_enabled = bool(config.get("dashboard", {}).get("allow_controls", False))
    disabled = "" if controls_enabled else "disabled"
    panda_work_on = status.get("panda_work_on")
    panda_heating = "ON" if panda_work_on in (1, True) else "OFF" if panda_work_on in (0, False) else "unknown"
    stored_set_temp = status.get("panda_set_temp_c")
    stored_set_temp_text = (
        f"{stored_set_temp} C ({'active' if panda_work_on in (1, True) else 'stored, heater off'})"
        if stored_set_temp is not None
        else "unavailable"
    )
    rows = [
        ("Service", status.get("service")),
        ("Updated", status.get("updated_at")),
        ("Moonraker", "OK" if status.get("moonraker_ok") else "FAIL" if status.get("moonraker_ok") is False else ""),
        ("PandaBreath", "OK" if status.get("pandabreath_ok") else "FAIL" if status.get("pandabreath_ok") is False else ""),
        ("Printer state", status.get("printer_state")),
        ("Filename", status.get("filename")),
        ("Material", status.get("material_raw")),
        ("Normalized material", status.get("material_normalized")),
        ("Material source", status.get("material_source")),
        ("Active tool index", status.get("active_tool_index")),
        ("Selected target", f"{status.get('selected_target_c')} C" if status.get("selected_target_c") is not None else ""),
        ("Effective control temp", f"{status.get('effective_control_temp_c')} C" if status.get("effective_control_temp_c") is not None else "unavailable"),
        ("Temperature strategy", status.get("temperature_strategy")),
        (
            "PandaBreath chamber temp",
            f"{status.get('chamber_temp_c')} C" if status.get("chamber_temp_c") is not None else "unavailable",
        ),
        (
            "U1 cavity temp",
            f"{status.get('u1_cavity_temp_c')} C" if status.get("u1_cavity_temp_c") is not None else "unavailable",
        ),
        (
            "Sensor delta",
            f"{status.get('temp_delta_c')} C" if status.get("temp_delta_c") is not None else "unavailable",
        ),
        ("PandaBreath heating", panda_heating),
        ("PandaBreath stored set temp", stored_set_temp_text),
        ("PandaBreath work mode", status.get("panda_work_mode")),
        ("PandaBreath work on", status.get("panda_work_on")),
        ("Cooldown active", status.get("cooldown_active")),
        ("Cooldown until", status.get("cooldown_until")),
        ("Manual off until", status.get("manual_off_until")),
        ("Session active", status.get("session_active")),
        ("Session ID", status.get("session_id")),
        ("Session started", status.get("session_started_at")),
        ("Session elapsed", f"{status.get('session_elapsed_minutes')} min" if status.get("session_elapsed_minutes") is not None else ""),
        ("Last heater action", status.get("heater_action")),
        ("Alerts", "; ".join(status.get("alerts") or [])),
        ("Decision", status.get("decision_reason")),
        ("Dry run", status.get("dry_run")),
        ("Control enabled", status.get("control_enabled")),
        ("Dashboard controls", controls_enabled),
        ("Last error", status.get("last_error")),
    ]
    row_html = "\n".join(
        f"<tr><th>{html_escape(label)}</th><td>{html_escape(value)}</td></tr>" for label, value in rows
    )
    # Optional quick-link buttons to local web UIs. Rendered only when the
    # corresponding URL is set in config, so the dashboard ships with no
    # hardcoded LAN addresses.
    panda_ui_url = config.get("panda_breath", {}).get("web_url", "")
    fluidd_url = config.get("dashboard", {}).get("fluidd_url", "")
    external_links = []
    if panda_ui_url:
        external_links.append(f'<a class="button" href="{html_escape(panda_ui_url)}">PandaBreath UI</a>')
    if fluidd_url:
        external_links.append(f'<a class="button" href="{html_escape(fluidd_url)}">Fluidd</a>')
    external_links_html = "\n      ".join(external_links)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="5">
  <title>U1 PandaBreath</title>
  <style>
    body {{ margin: 0; font-family: Arial, sans-serif; background: #171717; color: #f3f3f3; }}
    header {{ padding: 18px 22px; border-bottom: 1px solid #333; background: #202020; }}
    main {{ max-width: 980px; margin: 0 auto; padding: 20px; }}
    h1 {{ margin: 0; font-size: 24px; font-weight: 700; }}
    table {{ width: 100%; border-collapse: collapse; background: #222; }}
    th, td {{ text-align: left; padding: 11px 12px; border-bottom: 1px solid #343434; vertical-align: top; }}
    th {{ width: 220px; color: #bfbfbf; font-weight: 600; }}
    .actions {{ display: flex; gap: 10px; margin: 18px 0; flex-wrap: wrap; }}
    button, a.button {{ border: 1px solid #555; background: #2d2d2d; color: #fff; padding: 10px 14px; border-radius: 6px; text-decoration: none; cursor: pointer; }}
    button:disabled {{ opacity: .45; cursor: not-allowed; }}
    .note {{ color: #bfbfbf; font-size: 14px; }}
    .warn {{ color: #ffca6a; }}
    canvas {{ width: 100%; height: 260px; background: #202020; border: 1px solid #343434; margin-top: 18px; }}
  </style>
</head>
<body>
  <header><h1>U1 PandaBreath</h1></header>
  <main>
    <div class="actions">
      <form method="post" action="/api/heater-off"><button {disabled}>Heater Off</button></form>
      <form method="post" action="/api/clear-manual-off"><button {disabled}>Clear Manual Off</button></form>
      <form method="post" action="/api/toggle-dry-run"><button {disabled}>Toggle Dry Run</button></form>
      <form method="post" action="/api/reload-config"><button {disabled}>Reload Config</button></form>
      <a class="button" href="/api/status">JSON Status</a>
      <a class="button" href="/api/history">History JSON</a>
      <a class="button" href="/api/sessions">Sessions JSON</a>
      {external_links_html}
    </div>
    <p class="note {'warn' if not controls_enabled else ''}">
      Dashboard controls are {'enabled' if controls_enabled else 'disabled'}. Set dashboard.allow_controls to true in config.yaml to enable buttons.
    </p>
    <table>{row_html}</table>
    <canvas id="history" width="980" height="260"></canvas>
    <script>
      fetch('/api/history').then(r => r.json()).then(points => {{
        const canvas = document.getElementById('history');
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = '#bfbfbf';
        ctx.font = '14px Arial';
        ctx.fillText('Temperature history', 12, 22);
        if (!points.length) return;
        const vals = points.flatMap(p => [p.pandabreath_temp_c, p.u1_cavity_temp_c, p.selected_target_c]).filter(v => v !== null && v !== undefined);
        const min = Math.max(0, Math.floor(Math.min(...vals) - 2));
        const max = Math.ceil(Math.max(...vals) + 2);
        const x = i => 40 + (canvas.width - 60) * (i / Math.max(1, points.length - 1));
        const y = v => canvas.height - 28 - ((v - min) / Math.max(1, max - min)) * (canvas.height - 60);
        function line(key, color) {{
          ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 2;
          let started = false;
          points.forEach((p, i) => {{
            const v = p[key];
            if (v === null || v === undefined) return;
            if (!started) {{ ctx.moveTo(x(i), y(v)); started = true; }} else ctx.lineTo(x(i), y(v));
          }});
          ctx.stroke();
        }}
        line('pandabreath_temp_c', '#66d9ef');
        line('u1_cavity_temp_c', '#ffca6a');
        line('selected_target_c', '#a6e22e');
        ctx.fillStyle = '#66d9ef'; ctx.fillText('PandaBreath', 12, 44);
        ctx.fillStyle = '#ffca6a'; ctx.fillText('U1 cavity', 130, 44);
        ctx.fillStyle = '#a6e22e'; ctx.fillText('Target', 220, 44);
        ctx.fillStyle = '#bfbfbf'; ctx.fillText(min + 'C', 8, canvas.height - 28); ctx.fillText(max + 'C', 8, 58);
      }}).catch(() => {{}});
    </script>
  </main>
</body>
</html>
"""


class DashboardRequestHandler(BaseHTTPRequestHandler):
    bridge: "ChamberBridge"

    def log_message(self, fmt: str, *args: Any) -> None:
        logging.debug("Dashboard %s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/", "/index.html"}:
            self._send_html(render_dashboard(self.bridge.status.snapshot(), self.bridge.config))
            return
        if self.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        if self.path == "/api/status":
            self._send_json(self.bridge.status.snapshot())
            return
        if self.path == "/api/health":
            status_code, payload = self.bridge.health()
            self._send_json(payload, status_code)
            return
        if self.path == "/api/history":
            self._send_json_list(read_history(self.bridge.config))
            return
        if self.path == "/api/sessions":
            self._send_json(
                {
                    "current": self.bridge.session_tracker.snapshot().get("current"),
                    "last": self.bridge.session_tracker.last_summary,
                    "recent": read_session_summaries(self.bridge.config),
                }
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if not bool(self.bridge.config.get("dashboard", {}).get("allow_controls", False)):
            self.send_error(HTTPStatus.FORBIDDEN, "Dashboard controls disabled")
            return
        if self.path == "/api/heater-off":
            self.bridge.manual_heater_off()
            self._redirect("/")
            return
        if self.path == "/api/clear-manual-off":
            self.bridge.clear_manual_heater_off()
            self._redirect("/")
            return
        if self.path == "/api/toggle-dry-run":
            self.bridge.toggle_dry_run()
            self._redirect("/")
            return
        if self.path == "/api/reload-config":
            self.bridge.reload_config()
            self._redirect("/")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _send_json(self, data: dict[str, Any], status_code: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json_list(self, data: list[dict[str, Any]]) -> None:
        body = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()


def decide_target(
    snapshot: PrinterSnapshot,
    config: dict[str, Any],
    failed_polls: int,
    paused_since: float | None,
    now: float | None = None,
) -> tuple[int, str]:
    now = now if now is not None else time.time()
    control = config.get("control", {})
    failed_limit = int(config.get("printer", {}).get("failed_poll_limit", 2))
    if not bool(control.get("enabled", True)):
        return 0, "control disabled"
    if failed_polls >= failed_limit:
        return 0, "Moonraker failed poll limit reached"

    state = snapshot.state.lower()
    if state in OFF_STATES:
        return 0, f"printer state {state}"
    if state in PAUSED_STATES:
        hold_seconds = int(config.get("pause", {}).get("hold_heat_minutes", 10)) * 60
        if paused_since and now - paused_since > hold_seconds:
            return 0, "paused timeout exceeded"
    elif state not in PRINTING_STATES:
        return 0, f"printer state {state}"

    normalized, target = material_target(snapshot.material, config)
    target = cap_target(target, config)
    if target == 0:
        return 0, f"material {normalized} target is 0"
    return target, f"material {normalized}"


class ChamberBridge:
    def __init__(self, config: dict[str, Any], config_path: str | Path = "config.yaml") -> None:
        self.config = config
        self.config_path = Path(config_path)
        self.moonraker = MoonrakerClient(config)
        self.panda = PandaBreathClient(config)
        self.status = BridgeStatus()
        self.failed_polls = 0
        self.paused_since: float | None = None
        self.running = True
        self.last_state: str | None = None
        self.last_filename: str | None = None
        self.last_target: int | None = None
        self.last_material: str | None = None
        self.last_apply_attempt_at = 0.0
        self.last_mixed_material_warning: str | None = None
        self.cooldown_until: float | None = None
        self.manual_off_until: float | None = None
        self.session_tracker = PrintSessionTracker(config)
        self.dashboard_server: ThreadingHTTPServer | None = None
        self.status.update(
            dry_run=self.panda.dry_run,
            control_enabled=bool(config.get("control", {}).get("enabled", True)),
            dashboard_controls_enabled=bool(config.get("dashboard", {}).get("allow_controls", False)),
            temperature_strategy=str(config.get("temperature_control", {}).get("strategy", "pandabreath")),
        )

    def shutdown(self, *_args: Any) -> None:
        logging.info("Signal shutdown requested")
        self.running = False
        self.status.update(service="stopping")
        if self.config.get("control", {}).get("turn_off_on_shutdown", True):
            try:
                self.panda.heater_off()
            except Exception as exc:  # noqa: BLE001
                logging.error("Failed to turn PandaBreath off during shutdown: %s", exc)
                self.status.update(last_error=f"shutdown heater_off failed: {exc}")
        try:
            self.panda.stop()
        except Exception as exc:  # noqa: BLE001
            logging.warning("Failed to stop PandaBreath connection cleanly: %s", exc)
        self.stop_dashboard()

    def start_dashboard(self) -> None:
        dashboard = self.config.get("dashboard", {})
        if not bool(dashboard.get("enabled", True)):
            logging.info("Dashboard disabled")
            return
        host = str(dashboard.get("host", "0.0.0.0"))
        port = int(dashboard.get("port", 8081))
        handler = type("BoundDashboardRequestHandler", (DashboardRequestHandler,), {})
        handler.bridge = self
        self.dashboard_server = ThreadingHTTPServer((host, port), handler)
        thread = threading.Thread(target=self.dashboard_server.serve_forever, daemon=True)
        thread.start()
        logging.info("Dashboard listening on http://%s:%s", host, port)

    def stop_dashboard(self) -> None:
        if self.dashboard_server is not None:
            self.dashboard_server.shutdown()
            self.dashboard_server.server_close()
            self.dashboard_server = None

    def manual_heater_off(self) -> None:
        logging.warning("Dashboard requested heater off")
        self.panda.heater_off()
        self.last_target = 0
        minutes = float(self.config.get("dashboard", {}).get("manual_off_minutes", 30))
        self.manual_off_until = time.time() + max(1, minutes) * 60
        manual_until = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.manual_off_until))
        self.status.update(
            selected_target_c=0,
            decision_reason="manual dashboard heater off",
            manual_off_until=manual_until,
            heater_action="manual off",
        )

    def clear_manual_heater_off(self) -> None:
        logging.warning("Dashboard cleared manual heater-off hold")
        self.manual_off_until = None
        self.status.update(manual_off_until=None, decision_reason="manual dashboard heater off cleared")

    def toggle_dry_run(self) -> None:
        self.panda.dry_run = not self.panda.dry_run
        self.config.setdefault("control", {})["dry_run"] = self.panda.dry_run
        logging.warning("Dashboard toggled dry_run to %s", self.panda.dry_run)
        self.status.update(dry_run=self.panda.dry_run)
        if not self.panda.dry_run and self.last_target and self.last_target > 0:
            logging.warning("Dashboard dry-run disabled; applying current target %sC", self.last_target)
            self.panda.set_target_temperature(self.last_target)

    def reload_config(self) -> None:
        logging.warning("Dashboard requested config reload")
        new_config = load_config(self.config_path)
        self.config = new_config
        self.moonraker = MoonrakerClient(new_config)
        self.panda.dry_run = bool(new_config.get("control", {}).get("dry_run", self.panda.dry_run))
        self.status.update(
            dry_run=self.panda.dry_run,
            control_enabled=bool(new_config.get("control", {}).get("enabled", True)),
            dashboard_controls_enabled=bool(new_config.get("dashboard", {}).get("allow_controls", False)),
            temperature_strategy=str(new_config.get("temperature_control", {}).get("strategy", "pandabreath")),
        )

    def run(self) -> None:
        logging.info("PandaBreath U1 bridge starting")
        logging.info("Config loaded; dry_run=%s", self.config.get("control", {}).get("dry_run", True))
        logging.info(
            "Startup config: moonraker=%s pandabreath=%s dashboard=%s:%s strategy=%s safety_limit=%s",
            self.config.get("printer", {}).get("moonraker_url"),
            self.config.get("panda_breath", {}).get("web_url"),
            self.config.get("dashboard", {}).get("host", "0.0.0.0"),
            self.config.get("dashboard", {}).get("port", 8081),
            self.config.get("temperature_control", {}).get("strategy", "pandabreath"),
            self.config.get("safety", {}).get("absolute_safety_limit_c", self.config.get("control", {}).get("absolute_safety_limit_c", 70)),
        )
        self.status.update(service="starting")
        state = self.panda.check_status()
        if self.panda.get_current_temperature() is None:
            logging.warning("PandaBreath temperature feedback unavailable from initial status")
        else:
            logging.info("PandaBreath chamber temperature: %sC", self.panda.get_current_temperature())
        logging.debug("Initial PandaBreath state: %s", state)
        self.status.update(
            service="running",
            pandabreath_ok=self.panda.is_fresh(),
            chamber_temp_c=self.panda.get_current_temperature(),
            dry_run=self.panda.dry_run,
        )
        self.start_dashboard()
        sd_notify("READY=1\nSTATUS=U1 PandaBreath bridge running")

        interval = int(self.config.get("printer", {}).get("poll_interval_seconds", 5))
        while self.running:
            self.poll_once()
            sd_notify("WATCHDOG=1\nSTATUS=U1 PandaBreath bridge polling")
            time.sleep(interval)
        logging.info("PandaBreath U1 bridge stopped")
        self.status.update(service="stopped")

    def poll_once(self) -> None:
        try:
            snapshot = self.moonraker.get_snapshot()
            self.failed_polls = 0
            logging.debug("Moonraker poll ok: %s", snapshot)
            self.status.update(moonraker_ok=True, last_error=None)
        except Exception as exc:  # noqa: BLE001
            self.failed_polls += 1
            logging.error("Moonraker poll failed (%s): %s", self.failed_polls, exc)
            snapshot = PrinterSnapshot(state="unknown", filename="", material=None)
            self.status.update(moonraker_ok=False, last_error=f"Moonraker poll failed: {exc}")

        if snapshot.state != self.last_state:
            logging.info("Printer state changed: %s -> %s", self.last_state, snapshot.state)
            self.last_state = snapshot.state
        if snapshot.filename != self.last_filename:
            logging.info("Filename changed: %s -> %s", self.last_filename, snapshot.filename)
            self.last_filename = snapshot.filename
        if snapshot.material != self.last_material:
            logging.info("Detected material changed: %s -> %s", self.last_material, snapshot.material)
            self.last_material = snapshot.material
            self.last_mixed_material_warning = None

        if snapshot.state in PAUSED_STATES and self.paused_since is None:
            self.paused_since = time.time()
            logging.info("Printer paused; holding heat for configured timeout")
        elif snapshot.state not in PAUSED_STATES:
            self.paused_since = None

        target, reason = decide_target(snapshot, self.config, self.failed_polls, self.paused_since)
        normalized, _configured_target = material_target(snapshot.material, self.config)
        if is_mixed_material_metadata(snapshot.material, self.config) and snapshot.material != self.last_mixed_material_warning:
            logging.warning(
                "Mixed material metadata %r resolved to %s configured target %sC",
                snapshot.material,
                normalized,
                _configured_target,
            )
            self.last_mixed_material_warning = snapshot.material
        if self.panda.is_fresh():
            current_temp = self.panda.get_current_temperature()
            self.status.update(pandabreath_ok=True, chamber_temp_c=current_temp)
        else:
            logging.error("PandaBreath connection stale or down; turning heater off")
            self.panda.heater_off()
            self.last_target = 0
            self.status.update(
                pandabreath_ok=False,
                selected_target_c=0,
                decision_reason="PandaBreath unreachable",
                last_error="PandaBreath connection stale or down",
            )
            return

        safety = self.config.get("safety", {})
        absolute_limit = int(safety.get("absolute_safety_limit_c", self.config.get("control", {}).get("absolute_safety_limit_c", 70)))
        panda_limit = int(safety.get("pandabreath_limit_c", absolute_limit))
        u1_limit = int(safety.get("u1_cavity_limit_c", absolute_limit))
        alerts: list[str] = []
        if current_temp is not None and current_temp > panda_limit:
            logging.error("Safety shutoff: PandaBreath temperature %sC exceeds %sC", current_temp, panda_limit)
            target = 0
            reason = "PandaBreath safety temperature limit exceeded"
            alerts.append(f"PandaBreath temp over limit: {current_temp}C > {panda_limit}C")
        if snapshot.u1_cavity_temp_c is not None and snapshot.u1_cavity_temp_c > u1_limit:
            logging.error("Safety shutoff: U1 cavity temperature %sC exceeds %sC", snapshot.u1_cavity_temp_c, u1_limit)
            target = 0
            reason = "U1 cavity safety temperature limit exceeded"
            alerts.append(f"U1 cavity temp over limit: {snapshot.u1_cavity_temp_c}C > {u1_limit}C")

        manual_off_active = self.manual_off_until is not None and time.time() < self.manual_off_until
        if manual_off_active:
            target = 0
            reason = "manual dashboard heater-off hold"
        elif self.manual_off_until is not None:
            logging.info("Manual dashboard heater-off hold expired")
            self.manual_off_until = None

        strategy = str(self.config.get("temperature_control", {}).get("strategy", "pandabreath")).lower()
        effective_temp = control_temperature(strategy, current_temp, snapshot.u1_cavity_temp_c)
        overshoot_stop = float(self.config.get("control", {}).get("overshoot_stop_buffer_c", 2))
        overshoot_resume = float(self.config.get("control", {}).get("overshoot_resume_buffer_c", 2))
        heater_action = "hold"

        if target == 0 and self.last_target and self.last_target > 0:
            minutes = float(self.config.get("cooldown", {}).get("monitor_minutes", 0))
            if minutes > 0:
                self.cooldown_until = time.time() + minutes * 60
                logging.info("Post-print cooldown monitoring active for %.1f minutes", minutes)

        if target != self.last_target:
            logging.info("Selected target changed: %s -> %sC (%s)", self.last_target, target, reason)
            if target > 0:
                self.panda.set_target_temperature(target)
                self.last_apply_attempt_at = time.time()
                heater_action = f"set {target}C"
            else:
                self.panda.heater_off()
                self.last_apply_attempt_at = time.time()
                heater_action = "off"
            self.last_target = target
        elif target > 0 and strategy != "pandabreath" and effective_temp is not None and not self.panda.dry_run:
            if effective_temp >= target + overshoot_stop:
                self.panda.heater_off()
                heater_action = f"off by {strategy} temp"
                self.last_apply_attempt_at = time.time()
            elif effective_temp <= target - overshoot_resume:
                self.panda.set_target_temperature(target)
                heater_action = f"set {target}C by {strategy} temp"
                self.last_apply_attempt_at = time.time()
        elif target > 0 and not self.panda.dry_run:
            matched, detail = self.panda.command_matches_state(target)
            if not matched:
                if detail == "control state incomplete":
                    logging.debug("PandaBreath control state incomplete; skipping target reassertion")
                elif time.time() - self.last_apply_attempt_at > 30:
                    logging.warning(
                        "PandaBreath target not applied; reasserting %sC (%s)",
                        target,
                        detail,
                    )
                    self.panda.set_target_temperature(target)
                    self.last_apply_attempt_at = time.time()
                    heater_action = f"reassert {target}C"
        temp_delta = None
        if current_temp is not None and snapshot.u1_cavity_temp_c is not None:
            temp_delta = round(snapshot.u1_cavity_temp_c - current_temp, 1)
        fields = self.panda.get_control_fields()
        if target == 0 and fields.get("panda_work_on") in (1, True):
            alerts.append("PandaBreath reports work_on while target is 0")
            if not self.panda.dry_run and time.time() - self.last_apply_attempt_at > 10:
                logging.warning("PandaBreath still reports work_on while target is 0; reasserting heater off")
                self.panda.heater_off()
                self.last_apply_attempt_at = time.time()
                heater_action = "reassert off"
        cooldown_active = self.cooldown_until is not None and time.time() < self.cooldown_until
        cooldown_until = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.cooldown_until)) if cooldown_active else None
        manual_until = (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.manual_off_until))
            if self.manual_off_until is not None and time.time() < self.manual_off_until
            else None
        )
        session_state = self.session_tracker.update(
            snapshot,
            normalized,
            target,
            current_temp,
            snapshot.u1_cavity_temp_c,
            effective_temp,
            alerts,
        )
        current_session = session_state.get("current") or {}
        last_session = session_state.get("last")
        self.status.update(
            printer_state=snapshot.state,
            filename=snapshot.filename,
            material_raw=snapshot.material,
            material_normalized=normalized,
            material_source=snapshot.material_source,
            active_tool_index=snapshot.active_tool_index,
            selected_target_c=target,
            decision_reason=reason,
            u1_cavity_temp_c=snapshot.u1_cavity_temp_c,
            temp_delta_c=temp_delta,
            effective_control_temp_c=effective_temp,
            temperature_strategy=strategy,
            cooldown_active=cooldown_active,
            cooldown_until=cooldown_until,
            manual_off_until=manual_until,
            session_active=bool(session_state.get("active")),
            session_id=current_session.get("session_id"),
            session_started_at=current_session.get("started_at"),
            session_elapsed_minutes=current_session.get("elapsed_minutes"),
            last_session_summary=last_session,
            heater_action=heater_action,
            alerts=alerts,
            **fields,
            dry_run=self.panda.dry_run,
            control_enabled=bool(self.config.get("control", {}).get("enabled", True)),
            dashboard_controls_enabled=bool(self.config.get("dashboard", {}).get("allow_controls", False)),
        )
        append_history(
            self.config,
            {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "printer_state": snapshot.state,
                "filename": snapshot.filename,
                "material": normalized,
                "material_raw": snapshot.material,
                "selected_target_c": target,
                "pandabreath_temp_c": current_temp,
                "u1_cavity_temp_c": snapshot.u1_cavity_temp_c,
                "effective_control_temp_c": effective_temp,
                "panda_set_temp_c": fields.get("panda_set_temp_c"),
                "panda_work_mode": fields.get("panda_work_mode"),
                "panda_work_on": fields.get("panda_work_on"),
                "manual_off_until": manual_until,
                "dry_run": self.panda.dry_run,
                "alerts": alerts,
            },
        )

    def health(self) -> tuple[HTTPStatus, dict[str, Any]]:
        snapshot = self.status.snapshot()
        ok = (
            snapshot.get("service") in {"running", "starting"}
            and snapshot.get("moonraker_ok") is not False
            and snapshot.get("pandabreath_ok") is not False
            and not snapshot.get("alerts")
        )
        status_code = HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE
        return status_code, {
            "ok": ok,
            "service": snapshot.get("service"),
            "moonraker_ok": snapshot.get("moonraker_ok"),
            "pandabreath_ok": snapshot.get("pandabreath_ok"),
            "alerts": snapshot.get("alerts"),
            "last_error": snapshot.get("last_error"),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--check", action="store_true", help="Load config and check endpoints once")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    setup_logging(config)
    bridge = ChamberBridge(config, args.config)
    signal.signal(signal.SIGTERM, bridge.shutdown)
    signal.signal(signal.SIGINT, bridge.shutdown)

    if args.check:
        bridge.panda.check_status()
        snapshot = bridge.moonraker.get_snapshot()
        target, reason = decide_target(snapshot, config, 0, None)
        logging.info("Check snapshot=%s target=%s reason=%s", snapshot, target, reason)
        return 0

    try:
        bridge.run()
        return 0
    except Exception as exc:  # noqa: BLE001
        logging.exception("Bridge crashed: %s", exc)
        try:
            bridge.panda.heater_off()
        except Exception:
            logging.exception("Failed to turn PandaBreath off after crash")
        return 1


if __name__ == "__main__":
    sys.exit(main())
