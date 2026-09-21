# Integration tests

Two kinds live here.

**Default — no broker, no hardware.** Everything except `test_mosquitto_smoke.py`
runs on every `pytest` invocation. A fake MQTT *transport* is injected into the
real adapter, so the production connect, subscribe, acknowledge, reconnect and
shutdown paths all execute; only the Paho socket is absent. These are the
mandatory Phase 03 software gate.

**Opt-in — a real broker.** `test_mosquitto_smoke.py` uses real Paho clients
against a real Mosquitto instance. It is skipped unless explicitly enabled, and
the skip message repeats the enabling command. A skipped run is **NOT_RUN**, not
a pass.

```powershell
# start a broker first: see ../../deploy/mosquitto/README.md
$env:POWERGUARD_SMOKE_BROKER = "1"
$env:POWERGUARD_SMOKE_USERNAME = "powerguard-backend"
$env:POWERGUARD_SMOKE_PASSWORD = "<the password you created>"
.\.venv\Scripts\python.exe -m pytest tests/integration/test_mosquitto_smoke.py -m integration
```

Optional: `POWERGUARD_SMOKE_HOST`, `POWERGUARD_SMOKE_PORT`,
`POWERGUARD_SMOKE_DEVICE_USERNAME`, `POWERGUARD_SMOKE_DEVICE_PASSWORD`.

Passwords are only ever read from the environment. Never put one in a file here.

## What the smoke test adds over the fakes

| Proven only with a real broker |
|---|
| A real CONNACK reason code and a real SUBACK with granted QoS |
| Real QoS 1 delivery and redelivery timing |
| A retained Last Will published by the broker when a device drops |
| Reconnect against a live session |
| A WebSocket frame produced by a message that really crossed the broker, and no frame for a duplicate |

The WebSocket assertions connect a real client to `/ws/v1/devices/{id}` and wait for the frame that
ingestion broadcasts. Nothing is injected into the hub: the frame can only appear if a real QoS 1
publication was ingested, committed and broadcast.

The skip logic itself is tested without a broker, in
[`../unit/test_smoke_orchestration.py`](../unit/test_smoke_orchestration.py).
