# Operations

Local development only. Nothing here is hardened for exposure beyond a trusted LAN.

## Start and stop

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head     # migrations are never automatic
.\.venv\Scripts\python.exe -m powerguard serve
```

Startup order: validate settings → open the engine and verify the migration revision → build
repositories, hub and services → start the stale scan → connect MQTT. If the database is at a
different Alembic revision than the build expects, startup refuses with a message telling you to
migrate. It does not migrate for you.

Shutdown refuses new intake immediately — anything arriving after that point is neither enqueued
nor acknowledged, so the broker keeps it — then unsubscribes, drains what was already accepted within
`MQTT_SHUTDOWN_GRACE_S` — the network thread is still alive, so those acknowledgements are actually
sent — then disconnects, stops the loop, cancels background tasks and disposes the engine. Work that
did not finish inside the grace period is left unacknowledged and will be redelivered.

## Checks

```powershell
.\.venv\Scripts\python.exe -m powerguard check-config   # redacted; secrets show as <set>/<missing>
.\.venv\Scripts\python.exe -m powerguard check-db       # journal mode, pragmas, current revision
curl http://127.0.0.1:8000/api/v1/health
```

Health returns 200 while HTTP and the database work. `mqtt` and `model` are reported separately: a
broker outage or a missing model does **not** make the service unhealthy, because history stays
readable. A database failure does.

## Broker

Mosquitto is optional for development and is not installed by this repository. Both start-up paths
live in [`deploy/mosquitto/README.md`](../deploy/mosquitto/README.md); the compose file is
[`compose.yaml`](../compose.yaml).

There are two configurations, because the listener has to bind differently in each case:

| File | Listener | Paths | Used by |
|---|---|---|---|
| `mosquitto.native.conf` | `127.0.0.1:1883` | relative to `backend/` | a native `mosquitto` process |
| `mosquitto.docker.conf` | `0.0.0.0:1883` in the container | `/mosquitto/config/...` | `compose.yaml` |

The container binds `0.0.0.0` because that is its own network namespace; a loopback bind there is
unreachable through the published port. Host exposure is restricted by the `127.0.0.1:1883:1883`
mapping instead. Only MQTT is served — no WebSocket listener is configured on the broker, and no
other port is published. Anonymous access is off in both, and `acl` confines each account.

`deploy/mosquitto/passwd` must exist before `docker compose up`: it is mounted read-only and compose
fails without it. Create it with `mosquitto_passwd`, which prompts rather than taking the password on
the command line.

To verify a broker, use `scripts/check_broker.py` rather than `mosquitto_sub`. It reads the password
from `POWERGUARD_BROKER_PASSWORD`, hands it straight to the client, reports the CONNACK and the QoS
granted on each v1 filter, and passes only when both are confirmed at QoS 1 or better — a filter the
broker never answers for fails the check rather than being assumed. No PowerGuard command ever takes
a password as an argument, because an argument list is readable by every other user on the machine.

When you configure your own broker instead:

- disable anonymous access and create separate device and backend accounts;
- the device may publish only its own `powerguard/v1/devices/{id}/telemetry` and `/status`;
- the backend may subscribe only to the two v1 filters;
- bind to localhost or the private LAN; TLS is required before anything wider;
- keep the password file out of Git — `deploy/mosquitto/passwd` is already ignored.

Until then, use `scripts/publish_synthetic.py --dry-run` to inspect payloads and the integration
tests to exercise ingestion.

**Current status: Mosquitto smoke NOT_RUN — no broker available on this machine.** Neither a native
Mosquitto executable nor a local `eclipse-mosquitto` image is present, and none was installed. The
enabling command is in `tests/integration/README.md`.

## Backup and recovery

The database is the only durable state; everything the adapter, the hub and the status tracker hold
is process-local and is rebuilt on start. A restart therefore recovers exactly what was committed,
which `tests/integration/test_restart_recovery.py` asserts, including that idempotency still holds
for a QoS 1 redelivery that arrives after the restart.

To back up, stop the writer and copy the `.db`, `-wal` and `-shm` files together, or use the SQLite
online backup API. Never copy an active database file with a live WAL. To recover, restore the files
and run `python -m alembic upgrade head` before starting the service; startup refuses to run against
an unmigrated or differently-versioned schema rather than guessing.

## Data

SQLite in WAL mode at `POWERGUARD_DATABASE_URL`. One process is the sole writer. Database, `-wal`
and `-shm` files are Git-ignored runtime data.

For a demo backup, stop the writer or use the SQLite online backup API. Never copy an active
database file with a live WAL. There is no automatic retention deletion in this phase; every API is
bounded, so the database grows until you prune it deliberately.

## Logs and counters

`LOG_FORMAT` selects `console` or `json`; both carry a UTC timestamp, level, event name and
non-secret correlation fields: device id, boot id, sequence, row id, message id, topic. Payload
bytes, passwords and full settings objects are never logged, and the configured broker password is
scrubbed from every record — including from a rendered traceback, which a filter alone would miss.

Counters are process-local diagnostics only; there is no metrics endpoint in this phase:

| Group | Counters |
|---|---|
| Ingestion | accepted, duplicate, accepted status, rejected by reason, transient failures, sequence gaps |
| Inference | unavailable, failures, anomalies detected |
| MQTT adapter | connected, connect failures, disconnected, unexpected disconnects, subscribed, subscribe failures, subscriptions refused, sessions failed, stale SUBACKs ignored, SUBACK timeouts, reconnect attempts, reconnect attempt failures, received, enqueued, saturated, dropped after shutdown, acknowledged, withheld, handler errors, reconnects scheduled, reconnects requested, drain timeouts |
| Device status | status transitions and a per-status breakdown, from one tracker shared by telemetry, status messages and the stale scan; stale scans / transitions / failures |
| WebSocket | opened, closed, rejected, slow clients dropped, events published, events delivered |

## Failure behaviour

| Failure | Effect |
|---|---|
| Broker down | reconnect in the background with capped exponential backoff and jitter; REST keeps serving persisted data |
| CONNACK refused | not reported as connected; retried under the backoff policy |
| SUBACK refused, granting QoS 0, or never arriving | the whole subscription session fails at once: every filter still awaiting a SUBACK is abandoned with it, later SUBACKs for that session are ignored, and the connection is retried. A subscription counts as active only once the broker has confirmed it at QoS 1 |
| Unexpected disconnect | the adapter schedules the retry itself, computing a fresh jittered delay for each attempt. Paho's automatic reconnect is disabled (`reconnect_on_failure=False`), so there is exactly one reconnect owner and at most one attempt in flight |
| Database error | rollback, no broadcast, no acknowledgement — and the session is dropped once so the broker redelivers |
| Ingress queue saturated | nothing acknowledged, session dropped, messages redelivered |
| Inference unavailable or failing | telemetry still accepted and broadcast, no anomaly |
| One slow WebSocket client | that connection is woken and closed with 1013; others unaffected |
| A WebSocket client that disappears while idle | the disconnect watchdog releases the slot |
| Invalid payload | counted and acknowledged, no write, no reconnect storm |
| Stale scan error | logged, retried on the next interval; HTTP and ingestion keep running |

## Security notes

- No authentication and no mutation endpoints exist in the local MVP; do not expose this port.
- CORS is an exact allow-list; `*` is rejected at startup.
- Credentials live only in `.env` or the environment and are held as `SecretStr`.
- Error responses never contain stack traces, paths, SQL, settings or broker payloads.
