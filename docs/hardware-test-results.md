# Hardware test results

**Status: NO RESULTS RECORDED.** This is a template. Every line below is `NOT_RUN` until
somebody runs it on the real ESP8266 + INA226 (+ MAX7219) and records the evidence here.

The procedure is [`hardware-test-checklist.md`](hardware-test-checklist.md). A commit message, a chat
log or a file under `.ai_workspace/` (Git-ignored) is not evidence; this file is. Record a result as
`PASS`, `FAIL` or `NOT_RUN`, never as passing on the strength of an automated test.

## Run record

| Field | Value |
|---|---|
| Date (local, with UTC offset) | NOT_RUN |
| Run by | NOT_RUN |
| Git revision (`git rev-parse HEAD`) | NOT_RUN |
| Firmware version flashed | NOT_RUN |
| Shunt fitted (Ω) | NOT_RUN |
| `MAX_EXPECTED_CURRENT_A` | NOT_RUN |
| Source and load | NOT_RUN |
| Broker (host, version) | NOT_RUN |
| `boot_id`(s) observed | NOT_RUN |

## Results

| Checklist section | Result | Evidence (excerpt or file) |
|---|---|---|
| Prerequisites | NOT_RUN | |
| Sensor bring-up | NOT_RUN | |
| Measurement plausibility | NOT_RUN | |
| Endurance (30 min; `seq` advances by 2 per published row) | NOT_RUN | |
| Connectivity | NOT_RUN | |
| Protocol conformance | NOT_RUN | |
| MAX7219 local display | NOT_RUN | |
| Backend ingestion (rows persist, duplicates rejected, reboot = new `boot_id`, LWT) | NOT_RUN | |

## Evidence

Paste raw serial (`pio device monitor -b 115200`) and `mosquitto_sub` excerpts below, one fenced
block per check. **Redact every credential** (Wi-Fi SSID/password, broker username/password) before
saving.

_None recorded._
