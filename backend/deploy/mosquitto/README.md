# Local Mosquitto

Optional. The mandatory Phase 03 gate runs without a broker; this exists so the
real Paho path can be smoke-tested when someone has one.

Two paths. Neither is installed by this repository, and neither is required.
Run every command from the `backend` directory.

There are **two configuration files**, because the listener has to bind
differently in each case:

| File | Listener | Paths | Used by |
|---|---|---|---|
| `mosquitto.native.conf` | `127.0.0.1:1883` | relative to `backend/` | a native `mosquitto` process |
| `mosquitto.docker.conf` | `0.0.0.0:1883` inside the container | `/mosquitto/config/...` | `compose.yaml` |

The container binds `0.0.0.0` on purpose: that is its own network namespace, and
a loopback bind there would be unreachable through the published port. Exposure
is restricted on the host side instead, by the `127.0.0.1:1883:1883` mapping.

Only MQTT is served, on port **1883**. No WebSocket listener is configured and
no other port is published. (The backend's own WebSocket API is a separate HTTP
service on port 8000 — it has nothing to do with the broker.)

## 1. Create the password file (both paths)

Two accounts are needed: one for the backend, one for the simulated device.
`mosquitto_passwd` prompts for the password, so it never reaches shell history.

Native, if you have Mosquitto installed:

```powershell
mosquitto_passwd -c deploy\mosquitto\passwd powerguard-backend
mosquitto_passwd deploy\mosquitto\passwd powerguard-device
```

With Docker only — note `-c` on the first call and not on the second:

```powershell
docker run --rm -it -v "${PWD}/deploy/mosquitto:/c" eclipse-mosquitto:2.0 `
  mosquitto_passwd -c /c/passwd powerguard-backend
docker run --rm -it -v "${PWD}/deploy/mosquitto:/c" eclipse-mosquitto:2.0 `
  mosquitto_passwd /c/passwd powerguard-device
```

`deploy/mosquitto/passwd` is Git-ignored and must never be committed.

## 2a. Native Windows

Install Mosquitto, for example:

```powershell
winget install EclipseFoundation.Mosquitto
```

Then run it in the foreground, from `backend`:

```powershell
mosquitto -c deploy\mosquitto\mosquitto.native.conf -v
```

Stop it with Ctrl+C. Nothing is written to disk: `persistence false`.

## 2b. Docker

The password file from step 1 must already exist — it is mounted read-only and
compose fails without it.

```powershell
docker compose up -d mosquitto
docker compose logs -f mosquitto     # confirm it started
docker compose down -v               # stop and remove the data volume
```

Retained messages survive a restart in the `mosquitto-data` named volume;
`down -v` removes it.

## 3. Check it works

Passing a password to `mosquitto_sub` or `mosquitto_pub` as a command-line
argument would put it in the process argument list, where any other user on the
machine can read it. Use the in-process check instead: it reads the password
from the environment and never renders it.

```powershell
$env:POWERGUARD_BROKER_PASSWORD = "<password>"
.\.venv\Scripts\python.exe scripts\check_broker.py --username powerguard-backend
```

It reports the CONNACK and the QoS the broker granted on each v1 filter, and
passes only when **both** are confirmed at QoS 1 or better. A filter the broker
never answers for is reported as `no SUBACK` and fails the check — one
acknowledgement is not evidence for two. The same check works for the device
account with `--username powerguard-device`, which the ACL allows to write but
not to read — a refused subscription there is the ACL doing its job.

## Access control

`acl` confines each account:

- `powerguard-device` may **write** only
  `powerguard/v1/devices/powerguard-01/telemetry` and `.../status`;
- `powerguard-backend` may **read** only `powerguard/v1/devices/+/telemetry`
  and `.../status`.

Change the device id in `acl` if you publish as a different device; the file
lists it literally, so a new device id needs a new pair of lines.

## Running the smoke test against it

See [`../../tests/integration/README.md`](../../tests/integration/README.md).

## Where the broker is reachable from

The published port always carries an explicit host bind address, and it
defaults to loopback:

```yaml
- "${POWERGUARD_MQTT_BIND:-127.0.0.1}:1883:1883"
```

A bare `1883:1883` would publish on **every** interface the host has. This
broker has no TLS, so on a laptop that means any network it joins, including
untrusted Wi-Fi. That is why the address is never left implicit.

### Letting an ESP8266 reach it

A device is not on loopback, so a hardware capture needs LAN reach. That is an
explicit, temporary act:

```powershell
# The host's own LAN address - one named interface, not all of them.
$env:POWERGUARD_MQTT_BIND = "192.168.1.42"
docker compose up -d mosquitto
```

Unset it (or restart the shell) and the broker returns to loopback.

### Assumptions this relies on

Exposing an unencrypted broker on a LAN is only acceptable while all of these
hold. If any one of them does not, do not set `POWERGUARD_MQTT_BIND`.

- **The LAN is trusted and private** - a home or lab network you control, not a
  cafe, hotel, campus, coworking or guest network.
- **The host firewall allows 1883 inbound from that subnet only.** Windows
  Defender Firewall prompts on first bind; choose *Private networks* and deny
  public. To be explicit:

  ```powershell
  New-NetFirewallRule -DisplayName "PowerGuard MQTT (LAN capture)" `
    -Direction Inbound -Protocol TCP -LocalPort 1883 `
    -RemoteAddress 192.168.1.0/24 -Profile Private -Action Allow
  ```

  Remove the rule when the capture is finished.
- **No port forwarding.** Nothing on the router may expose 1883 to the
  internet. This broker must never be reachable from outside the LAN.
- **Authentication and the ACL stay on.** `allow_anonymous false`, a real
  `passwd`, and per-user topic rules (see below) apply on every interface -
  binding wider never relaxes them.
- **Credentials are still in the clear.** MQTT without TLS sends the username
  and password in plaintext; anyone on that LAN who is listening can read them.
  Use a password that protects nothing else, and rotate it after the capture.
- **It is temporary.** Bind to the LAN for the capture, then put it back.

TLS is out of scope for this local MVP. A broker that needs to stay reachable
is a broker that needs certificates, and that is a separate piece of work.
