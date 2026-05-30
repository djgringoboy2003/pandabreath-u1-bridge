# PandaBreath U1 Bridge

An external, host-side bridge that integrates a [BIQU PandaBreath](https://biqu.equipment/products/biqu-panda-breath-smart-air-filtration-and-heating-system-with-precise-temperature-regulation) smart chamber heater with a **Snapmaker U1** running Klipper/Moonraker.

It runs on any always-on host on the same network (a Raspberry Pi, a NAS, a mini-PC — anything that can run Python), **not** on the printer's firmware. It watches the U1 through Moonraker, picks a chamber target from the print's detected material, and drives the PandaBreath over its WebSocket API. It also serves a small status dashboard and writes per-print session summaries.

> **Relationship to the firmware-native integration**
> The [Snapmaker U1 Extended Firmware (PAXX)](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware) ships a *native* PandaBreath integration that exposes the device to Klipper as a `heater_generic` driven by slicer `M141`/`M191` commands. That is the right choice for most people: it's integrated, slicer-driven, and maintained in the firmware.
>
> **This project is an external alternative, not an add-on — run one or the other, never both** (they would both try to command the heater). It exists for setups that want host-side resilience and observability that are decoupled from the firmware: automatic material → target mapping (including multi-tool "highest chamber target wins"), an independent dashboard and per-print history, an independent U1 enclosure-temperature safety shutoff, and dry-run validation. If you simply want slicer-controlled chamber heating, use the firmware-native integration instead.

## How it works

1. Polls Moonraker for printer state, the active filename, and the detected material.
2. Maps the material to a chamber target from the `materials` table in config.
3. Commands the PandaBreath to that target over its WebSocket API (or just logs the decision when `dry_run` is true).
4. Continuously enforces independent safety limits and turns the heater off on idle / error / shutdown.

## Requirements

- A Snapmaker U1 reachable via Moonraker (Klipper).
- A BIQU PandaBreath on the same network (firmware `V1.0.3` confirmed).
- Python 3.10+ on the host.

> ⚠️ **Thermal safety.** Sustained elevated chamber temperatures stress the U1's electronics; the mainboard has limited thermal headroom (the RK3562 SoC throttles at 85 °C). Add active motherboard cooling before running heated, and keep `u1_cavity_limit_c` set conservatively. Use at your own risk.

## Install

```bash
git clone https://github.com/<you>/pandabreath-u1-bridge.git
cd pandabreath-u1-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp config.example.yaml config.yaml
# edit config.yaml: set printer.moonraker_url and panda_breath.ip
```

Validate connectivity and the resolved config without commanding anything:

```bash
.venv/bin/python pandabreath_u1_bridge.py --config config.yaml --check
```

The bundled config ships with `control.dry_run: true`. Once the readings and material map look right, set `dry_run: false` for live operation.

### Run as a service (systemd)

`pandabreath-u1.service` is a ready template — edit `User`, `WorkingDirectory`, and the `ExecStart` paths for where you cloned the repo, then:

```bash
sudo install -m 0644 pandabreath-u1.service /etc/systemd/system/pandabreath-u1.service
sudo systemctl daemon-reload
sudo systemctl enable --now pandabreath-u1.service
sudo systemctl status pandabreath-u1.service --no-pager
journalctl -u pandabreath-u1.service -n 100 --no-pager
```

The unit uses systemd watchdog notifications (`Type=notify`, `WatchdogSec=30`), so systemd restarts the bridge if it stops polling.

## Material map

Chamber target per material (°C), from `config.yaml`. `0` means no chamber heating. Unknown materials use `DEFAULT`.

| Material | Default target |
|---|---:|
| PLA | 0 |
| TPU | 0 |
| PETG | 38 |
| ABS / ASA / PC | 55 |
| PA / NYLON | 50 |
| DEFAULT | 0 |

Multi-tool prints use the **highest** configured target among the loaded materials (e.g. `PLA;PLA;PETG;PLA` → PETG/38 °C).

## Safety model

Safety limits are enforced **independently of the control strategy**. The heater is commanded off when:

- the printer is idle, standby, complete, cancelled, errored, unknown, unreachable, or paused beyond the configured hold;
- the PandaBreath reports a chamber temperature above `pandabreath_limit_c`;
- the U1 **enclosure** temperature (`[temperature_sensor cavity]`) exceeds `u1_cavity_limit_c` — an independent cutoff that protects the mainboard, since the PandaBreath's own thermostat only sees its own sensor;
- the PandaBreath reports it is heating while the selected target is 0 (the bridge reasserts off);
- on `SIGTERM`/`SIGINT` and service shutdown.

If temperature feedback is unavailable the bridge still caps commanded targets but does not claim verified temperature safety. Invalid config (bad strategy, bad port, negative targets, bad limits) stops startup before the control loop runs.

```yaml
safety:
  absolute_safety_limit_c: 70
  pandabreath_limit_c: 70
  u1_cavity_limit_c: 70
```

### Control strategies

```yaml
temperature_control:
  strategy: "pandabreath"   # pandabreath | u1_cavity | max | average
```

`pandabreath` (default) lets the PandaBreath's own firmware thermostat regulate against its own sensor. `u1_cavity`, `max`, and `average` exist for supervised experiments; safety shutoffs apply regardless.

## Dashboard

The bridge serves a small status page (default `http://<host>:8081`) showing Moonraker/PandaBreath reachability, printer state, detected/normalized material, selected target, PandaBreath and U1 cavity temperatures, sensor delta, dry-run state, the last control decision, safety alerts, cooldown state, and a temperature history chart.

- `/api/status` — full JSON status
- `/api/health` — health payload (HTTP 503 when unhealthy)
- `/api/history` — temperature history (JSON lines, also powers the chart)
- `/api/sessions` — current + recent per-print session summaries

Controls are **read-only by default** (`dashboard.allow_controls: false`). Setting it true enables manual heater-off, clearing manual-off, config reload, and runtime dry-run toggle. Manual heater-off holds the heater off for `manual_off_minutes` so an active print cannot immediately re-enable it. Runtime dry-run toggles are not persisted — restart to return to the file setting.

Optional quick-link buttons (PandaBreath UI, Fluidd) appear only when you set their URLs in config (`panda_breath.web_url`, `dashboard.fluidd_url`); nothing is hardcoded.

## Sessions & history

A session starts when Moonraker reports printing/paused and ends when the printer returns to an off state. Each summary records filename, material, max target, elapsed time, PandaBreath and U1 cavity min/max/average temperatures, the fraction of samples near target, end state, and alerts. History and sessions are JSON-lines files under `logs/` (configurable, rotated by line count).

## Tests

```bash
.venv/bin/python -m pytest test_bridge.py
```

## Known limitations

- Material detection depends on Moonraker job metadata or filename tokens.
- PandaBreath command support is based on the confirmed Web UI WebSocket messages used by firmware `V1.0.3`.
- Post-print cooldown is monitoring-only (`cooldown.fan_only_supported: false`) — no confirmed fan-only PandaBreath command yet.
- Session reporting is passive; it records behavior but does not change control decisions.

## License

MIT — see [LICENSE](LICENSE).
