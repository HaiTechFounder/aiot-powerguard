# AIoT PowerGuard — Web Dashboard

A local React dashboard for the Phase 03 backend: live voltage, current and power per device,
history charts, device status and anomaly presentation.

It owns presentation and client state only. It computes no anomalies, stores nothing durable, and
never talks to MQTT or the database — it reads the published REST v1 and WebSocket v1 contracts and
nothing else.

```
browser ──REST /api/v1──▶ FastAPI backend ──▶ SQLite
   │                            ▲
   └──── WS /ws/v1/devices/{id} ┘
```

Stack: React 18, TypeScript (strict), Vite, Recharts, Vitest + React Testing Library + MSW.
No state-management library — `TECH_STACK.md` excludes one, which rules out React Query too.

## Quick start

Requires Node 20+ (developed on 24.16.0) and a running backend.

```powershell
# 1. Install
cd frontend
npm ci

# 2. Configure (optional)
Copy-Item .env.example .env
# Leave both values blank to use the dev proxy, which is the normal path.

# 3. Start the backend first, in another shell
cd ..\backend
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m powerguard serve        # http://127.0.0.1:8000

# 4. Start the dashboard
cd ..\frontend
npm run dev

# 5. Open it
#    http://127.0.0.1:5173
```

With no device and no broker the dashboard correctly shows an empty device list. To see data, run
the synthetic publisher against a broker, or set `POWERGUARD_MQTT_ENABLED=false` and insert rows
directly — see `backend/README.md`.

`vite.config.ts` proxies `/api` and `/ws` to `127.0.0.1:8000`, so the browser sees one origin and no
CORS preflight is needed. Point at a backend elsewhere by setting `VITE_API_BASE_URL` and
`VITE_WS_BASE_URL` in `.env`; that origin must then be in the backend's `POWERGUARD_CORS_ORIGINS`.

No credential belongs in `.env` or in any command here — the dashboard has no authentication to
carry, because the backend has none in this phase.

## Commands

| Command | Purpose |
|---|---|
| `npm run dev` | dev server on `127.0.0.1:5173` |
| `npm run typecheck` | `tsc --noEmit`, strict |
| `npm run lint` | ESLint, zero warnings tolerated |
| `npm run test` | Vitest, once |
| `npm run test:watch` | Vitest, watching |
| `npm run test:coverage` | coverage, gated at 80% on `src/api`, `src/realtime`, `src/format` |
| `npm run build` | typecheck then production build |

## How the live view works

**REST is the truth; the socket is a live tail.** That one sentence explains every decision below.

1. Opening a device view loads history over REST (300 rows) and seeds the series.
2. The socket opens and its `telemetry` frames are appended.
3. Rows are **deduped by database id**. A row already held is dropped, whether it arrived twice on
   the socket or once on the socket and again in a backfill.
4. The series is **bounded to 600 points** — about twenty minutes at the firmware's two-second
   cadence. The oldest are discarded; the newest never are. The backend's own hub is bounded for the
   same reason, and an unbounded browser buffer would be the same bug in a different process.
5. Ordering follows `received_at`, which ADR-006 makes authoritative. `sampled_at` is diagnostic and
   is `null` until the firmware has a time source, so it would make a useless axis.

### When the connection drops

A dropped socket never discards history that has already loaded.

- **Retryable** — an ordinary close, a network drop, or `1013` when the backend sheds a slow client.
  The dashboard reconnects under a capped exponential backoff with a fresh jitter per attempt
  (1 s base, 30 s cap), showing the attempt and the delay, with a "retry now" control.
- **Permanent** — `4404` (unknown device) and `4400` (malformed device id) are verdicts, not
  accidents. Retrying either would be a loop that can never succeed, so the dashboard stops, says
  why, and keeps the history it already has.

### Filling the gap

On the first open and every reconnect, the dashboard reconciles REST pages from its last proven
row. It follows the backend cursor until the overlap proves coverage or the newest 600-point window
is full. Failed or truncated recovery is shown as an incomplete-history warning; valid readings
remain visible, and failed recovery retries independently of the socket.

## Anomaly detection, honestly

Phase 03 ships `UnavailableInference`, which never flags, and `/api/v1/health` reports
`model: unavailable`. While that is true the dashboard says **"anomaly detection unavailable"**;
previously stored anomaly verdicts remain visible. An empty list is not proof of normal readings.

Reporting a clean bill of health from a detector that is switched off would be a false pass. The
distinction is enforced by test, not by convention.

The same applies to the broker: `mqtt: disconnected` is shown as a state, not an alarm, because
history stays readable over REST while MQTT is down.

## Tests

```powershell
npm run test
npm run test:coverage
```

Nothing in the suite needs a network, a backend, a broker or hardware. REST is mocked with MSW; the
WebSocket and its timers are injected fakes, so backoff is asserted by value rather than waited out.

| Area | What it proves |
|---|---|
| `tests/contract` | the client's types still match a committed export of the backend's `openapi.json` |
| `tests/unit/client.test.ts` | success, the error envelope, network failure, empty and non-JSON bodies, pagination, abort |
| `tests/unit/realtime/socket.test.ts` | close-code policy, jitter windows, the cap, backoff reset, stale-socket isolation, clean shutdown |
| `tests/unit/realtime/series.test.ts` | dedupe, `received_at` ordering, tie-breaking, the 600-point bound |
| `tests/unit/realtime/useDeviceStream.test.tsx` | seed → tail, duplicate dropped, gap backfilled, failed backfill reported, permanent close, unmount |
| `tests/unit/format.test.ts` | units, decimals, the visible UTC offset |
| `tests/views` | loading, error, empty and populated for every view, plus the honesty rules |

### Refreshing the contract fixture

After any backend contract change, regenerate the fixture and let the contract test judge it:

```powershell
cd backend
.\.venv\Scripts\python.exe -c "import json,pathlib,sys,tempfile; sys.path[:0]=['src','.']; from fastapi.testclient import TestClient; from alembic import command; from tests.conftest import alembic_config, make_settings; from powerguard.main import create_app; d=tempfile.mkdtemp(); u=f'sqlite:///{pathlib.Path(d).as_posix()}/x.db'; s=make_settings(u); command.upgrade(alembic_config(u),'head'); c=TestClient(create_app(s)); c.__enter__(); pathlib.Path('../frontend/tests/fixtures/openapi.json').write_text(json.dumps(c.get('/openapi.json').json(), indent=2, sort_keys=True)+'\n', encoding='utf-8')"
```

A fixture that no longer matches `src/api/contract.ts` fails the suite, which is the point: the
dashboard and the API cannot drift apart in silence.

## Known NOT_RUN

Truthfully unexercised, and not claimed otherwise:

| Item | Reason |
|---|---|
| A browser session against a backend fed by **real hardware** | no NodeMCU or INA226 available |
| A live Mosquitto path, device → broker → backend → dashboard | no broker installed |
| Cross-browser checks beyond jsdom | no browser matrix is run here |
| Long-session memory behaviour in a real browser | the 600-point bound is proven by unit test, not by a day-long session |
| Accessibility audit beyond roles and labels used in tests | no audit tool is run |

A manual check against a locally running backend is possible and is step 5 of the quick start. It is
a **manual** step and is never recorded as an automated pass.

## Current limitations

- No authentication — the backend has none in this phase, so do not expose either port.
- Read-only: no mutation, no device control.
- Anomaly paging stops at the first page; older verdicts are reachable through the API.
- One backend at a time; there is no environment switcher.
- `dist/` is ~580 kB before gzip (~170 kB after), mostly Recharts. Code-splitting was left out
  deliberately: it is a local single-page tool, and an unnecessary optimisation is a cost too.
