import signal

import pytest

from pandabreath_u1_bridge import (
    BridgeStatus,
    PandaBreathClient,
    PrintSessionTracker,
    PrinterSnapshot,
    cap_target,
    control_temperature,
    decide_target,
    append_history,
    read_history,
    read_session_summaries,
    load_config,
    material_target,
    normalize_material,
    render_dashboard,
    is_mixed_material_metadata,
    validate_config,
)


def cfg():
    return {
        "printer": {"failed_poll_limit": 2},
        "panda_breath": {"web_url": "http://127.0.0.1"},
        "control": {"enabled": True, "dry_run": True, "absolute_safety_limit_c": 70},
        "safety": {"absolute_safety_limit_c": 70, "pandabreath_limit_c": 70, "u1_cavity_limit_c": 70},
        "temperature_control": {"strategy": "pandabreath"},
        "cooldown": {"monitor_minutes": 10, "fan_only_supported": False},
        "history": {"enabled": True, "file": "/tmp/pandabreath-u1-history-test.jsonl", "dashboard_points": 240, "max_lines": 10000},
        "sessions": {"enabled": True, "file": "/tmp/pandabreath-u1-sessions-test.jsonl", "dashboard_sessions": 20},
        "pause": {"hold_heat_minutes": 10},
        "dashboard": {"enabled": True, "host": "127.0.0.1", "port": 8081, "allow_controls": False, "manual_off_minutes": 30},
        "materials": {
            "PLA": 0,
            "TPU": 0,
            "PETG": 38,
            "ABS": 55,
            "ASA": 55,
            "PC": 55,
            "PA": 50,
            "NYLON": 50,
            "DEFAULT": 0,
        },
        "logging": {"file": "/tmp/pandabreath-u1-test.log"},
    }


def target_for(material):
    return material_target(material, cfg())[1]


def target_for_state(state, material="ASA", failed=0):
    target, _reason = decide_target(
        PrinterSnapshot(state=state, filename="part.gcode", material=material),
        cfg(),
        failed,
        None,
        now=1000,
    )
    return target


def test_config_loading():
    config = load_config("config.example.yaml")
    assert config["control"]["dry_run"] is True
    assert config["materials"]["ASA"] == 55
    assert config["dashboard"]["port"] == 8081
    assert config["dashboard"]["manual_off_minutes"] == 30
    assert config["temperature_control"]["strategy"] == "pandabreath"
    assert config["sessions"]["dashboard_sessions"] == 20


def test_config_validation_rejects_bad_strategy():
    config = cfg()
    config["temperature_control"]["strategy"] = "risky"
    with pytest.raises(ValueError):
        validate_config(config)


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        (" pla+ ", "PLA"),
        ("PLA PLUS", "PLA"),
        ("PLA;PLA;PLA;PLA", "PLA"),
        ("PETG-CF", "PETG"),
        ("PETG CF", "PETG"),
        ("ASA-CF", "ASA"),
        ("ASA CF", "ASA"),
        ("ABS-GF", "ABS"),
        ("ABS CF", "ABS"),
        ("PA6", "PA"),
        ("PA12", "PA"),
        ("PA-CF", "PA"),
        ("NYLON", "PA"),
    ],
)
def test_material_normalisation(raw, normalized):
    assert normalize_material(raw) == normalized


def test_material_targets():
    assert target_for("PLA") == 0
    assert target_for("TPU") == 0
    assert target_for("PETG") == 38
    assert target_for("PLA;PLA;PETG;PLA") == 38
    assert target_for("ASA") == 55
    assert target_for("ABS") == 55
    assert target_for("PA6") == 50
    assert target_for("mystery") == 0


def test_semicolon_metadata_uses_active_tool_index():
    from pandabreath_u1_bridge import MoonrakerClient

    assert MoonrakerClient._indexed_metadata_value("PLA;PLA;PETG;ASA", 0) == "PLA"
    assert MoonrakerClient._indexed_metadata_value("PLA;PLA;PETG;ASA", 2) == "PETG"
    assert MoonrakerClient._indexed_metadata_value(
        'U1 SUNLU PLA GREEN @Snapmaker";"Snapmaker PLA SnapSpeed @U1";"U1 SUNLU PETG @P1s";"Generic ASA',
        3,
    ) == "Generic ASA"


@pytest.mark.parametrize("state", ["idle", "standby", "complete", "cancelled", "canceled", "error", "unknown"])
def test_off_states_mean_heater_off(state):
    assert target_for_state(state) == 0


def test_printing_targets():
    assert target_for_state("printing", "ASA") == 55
    assert target_for_state("printing", "PLA") == 0


def test_failed_moonraker_polls_trigger_heater_off():
    assert target_for_state("printing", "ASA", failed=2) == 0


def test_safety_limit_blocks_target_above_70():
    config = cfg()
    config["materials"]["ASA"] = 90
    target, _reason = decide_target(
        PrinterSnapshot(state="printing", filename="part.gcode", material="ASA"),
        config,
        0,
        None,
    )
    assert target == 70
    assert cap_target(90, config) == 70


def test_sigterm_handler_calls_heater_off():
    class DummyBridge:
        def __init__(self):
            self.config = {"control": {"turn_off_on_shutdown": True}}
            self.running = True
            self.called = False
            self.panda = self
            self.status = BridgeStatus()

        def heater_off(self):
            self.called = True

        def stop_dashboard(self):
            pass

    from pandabreath_u1_bridge import ChamberBridge

    dummy = DummyBridge()
    ChamberBridge.shutdown(dummy, signal.SIGTERM, None)
    assert dummy.called is True
    assert dummy.running is False


def test_dry_run_client_does_not_send(monkeypatch):
    client = PandaBreathClient(
        {
            "panda_breath": {"web_url": "http://127.0.0.1"},
            "control": {"dry_run": True},
        }
    )
    monkeypatch.setattr(client, "_send_ws", lambda *_args, **_kwargs: pytest.fail("should not send"))
    client.set_target_temperature(55)
    client.heater_off()


def test_live_client_uses_boolean_work_on(monkeypatch):
    sent = []
    client = PandaBreathClient(
        {
            "panda_breath": {"web_url": "http://127.0.0.1"},
            "control": {"dry_run": False},
        }
    )

    async def fake_send_ws(payload, _reason):
        sent.append(payload)

    monkeypatch.setattr(client, "_send_ws", fake_send_ws)
    client.set_target_temperature(38)
    client.heater_off()
    assert sent[0]["settings"]["work_on"] is True
    assert sent[1]["settings"]["work_on"] is False


def test_deep_merge_preserves_incremental_status():
    data = {"settings": {"work_on": 0, "set_temp": 15}}
    PandaBreathClient._deep_merge(data, {"settings": {"warehouse_temper": 29}})
    assert data["settings"]["work_on"] == 0
    assert data["settings"]["warehouse_temper"] == 29


def test_complete_control_state_required_for_reassertion():
    assert PandaBreathClient.has_complete_control_state(
        {"settings": {"set_temp": 38, "work_mode": 2, "work_on": 1}}
    )
    assert not PandaBreathClient.has_complete_control_state({"settings": {"warehouse_temper": 38}})


def test_command_matches_state():
    client = PandaBreathClient({"panda_breath": {"web_url": "http://127.0.0.1"}, "control": {"dry_run": False}})
    client.last_state = {"settings": {"set_temp": 38, "work_mode": 2, "work_on": True}}
    assert client.command_matches_state(38)[0]
    assert not client.command_matches_state(55)[0]
    client.last_state = {"settings": {"work_on": 0, "set_temp": 38, "work_mode": 2}}
    assert client.command_matches_state(0)[0]


def test_control_temperature_strategies():
    assert control_temperature("pandabreath", 38, 42) == 38
    assert control_temperature("u1_cavity", 38, 42) == 42
    assert control_temperature("max", 38, 42) == 42
    assert control_temperature("average", 38, 42) == 40


def test_mixed_material_metadata_detection():
    assert is_mixed_material_metadata("PLA;PLA;PETG;PLA", cfg())
    assert not is_mixed_material_metadata("PLA;PLA;PLA;PLA", cfg())


def test_dashboard_status_snapshot_and_html():
    status = BridgeStatus()
    status.update(
        service="running",
        printer_state="complete",
        selected_target_c=0,
        chamber_temp_c=37,
        u1_cavity_temp_c=40.0,
        temp_delta_c=3.0,
    )
    snap = status.snapshot()
    assert snap["service"] == "running"
    html = render_dashboard(snap, cfg())
    assert "U1 PandaBreath" in html
    assert "complete" in html
    assert "U1 cavity temp" in html
    assert "Sensor delta" in html
    assert "Temperature history" in html
    assert "Dashboard controls are disabled" in html
    assert "Manual off until" in html
    assert "PandaBreath heating" in html
    assert "PandaBreath stored set temp" in html
    assert "Sessions JSON" in html


def test_printer_snapshot_accepts_u1_cavity_temp():
    snapshot = PrinterSnapshot(state="printing", filename="x.gcode", material="PETG", u1_cavity_temp_c=40.0)
    assert snapshot.u1_cavity_temp_c == 40.0


def test_toggle_dry_run_applies_existing_live_target():
    class DummyPanda:
        def __init__(self):
            self.dry_run = True
            self.applied = None

        def set_target_temperature(self, target):
            self.applied = target

    from pandabreath_u1_bridge import ChamberBridge

    dummy = type("DummyBridge", (), {})()
    dummy.panda = DummyPanda()
    dummy.config = {"control": {"dry_run": True}}
    dummy.status = BridgeStatus()
    dummy.last_target = 38
    ChamberBridge.toggle_dry_run(dummy)
    assert dummy.panda.dry_run is False
    assert dummy.panda.applied == 38


def test_manual_heater_off_sets_timed_hold(monkeypatch):
    class DummyPanda:
        def __init__(self):
            self.off_called = False

        def heater_off(self):
            self.off_called = True

    from pandabreath_u1_bridge import ChamberBridge

    dummy = type("DummyBridge", (), {})()
    dummy.panda = DummyPanda()
    dummy.config = {"dashboard": {"manual_off_minutes": 5}}
    dummy.status = BridgeStatus()
    dummy.last_target = 38
    monkeypatch.setattr("pandabreath_u1_bridge.time.time", lambda: 1000)
    monkeypatch.setattr("pandabreath_u1_bridge.time.strftime", lambda *_args, **_kwargs: "manual-until")
    ChamberBridge.manual_heater_off(dummy)
    assert dummy.panda.off_called is True
    assert dummy.last_target == 0
    assert dummy.manual_off_until == 1300
    assert dummy.status.snapshot()["manual_off_until"] == "manual-until"


def test_health_reports_alerts_unhealthy():
    from pandabreath_u1_bridge import ChamberBridge

    dummy = type("DummyBridge", (), {})()
    dummy.status = BridgeStatus()
    dummy.status.update(service="running", moonraker_ok=True, pandabreath_ok=True, alerts=["hot"])
    status_code, payload = ChamberBridge.health(dummy)
    assert status_code.value == 503
    assert payload["ok"] is False


def test_history_roundtrip(tmp_path):
    config = cfg()
    config["history"]["file"] = str(tmp_path / "history.jsonl")
    append_history(config, {"ts": "now", "pandabreath_temp_c": 38})
    assert read_history(config)[0]["pandabreath_temp_c"] == 38


def test_history_pruning(tmp_path):
    config = cfg()
    config["history"]["file"] = str(tmp_path / "history.jsonl")
    config["history"]["max_lines"] = 2
    append_history(config, {"ts": "1", "pandabreath_temp_c": 35})
    append_history(config, {"ts": "2", "pandabreath_temp_c": 36})
    append_history(config, {"ts": "3", "pandabreath_temp_c": 37})
    history = read_history(config)
    assert [point["ts"] for point in history] == ["2", "3"]


def test_print_session_tracker_writes_summary(tmp_path):
    config = cfg()
    config["sessions"]["file"] = str(tmp_path / "sessions.jsonl")
    tracker = PrintSessionTracker(config)
    active = PrinterSnapshot(state="printing", filename="part.gcode", material="ASA")
    finished = PrinterSnapshot(state="complete", filename="part.gcode", material="ASA")

    state = tracker.update(active, "ASA", 55, 40, 42, 40, [], now=1000)
    assert state["active"] is True
    tracker.update(active, "ASA", 55, 54, 56, 54, [], now=1060)
    state = tracker.update(finished, "ASA", 0, 55, 57, 55, [], now=1120)

    assert state["active"] is False
    summaries = read_session_summaries(config)
    assert len(summaries) == 1
    assert summaries[0]["material"] == "ASA"
    assert summaries[0]["end_state"] == "complete"
    assert summaries[0]["panda_max_c"] == 55.0
    assert summaries[0]["near_target_percent"] == 50.0
