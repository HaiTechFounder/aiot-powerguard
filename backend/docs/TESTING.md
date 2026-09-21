# Testing

## Layout

| Directory | Scope |
|---|---|
| `tests/unit` | pure logic: configuration, timestamps, MQTT validation, event hub, logging, app wiring |
| `tests/db` | migrated SQLite: schema shape, pragmas, repositories, unit of work |
| `tests/integration` | the MQTT adapter, the lifespan, ingestion, REST and WebSocket against a real database |
| `tests/fakes` | `FakeMqttTransport`, `FakeMqttClient`, `RecordingPublisher`, `FakeInference` |

`tests/integration/test_mosquitto_smoke.py` is the one exception to "no external dependency": it is
skipped unless a broker is deliberately enabled. See [`tests/integration/README.md`](../tests/integration/README.md).

Every database in the suite is a temporary file migrated by Alembic — the same migrations production
runs. Nothing calls `metadata.create_all()`.

## Running

```powershell
.\.venv\Scripts\python.exe -m pytest                       # everything
.\.venv\Scripts\python.exe -m pytest --cov=powerguard      # with coverage
.\.venv\Scripts\python.exe -m pytest tests/integration     # the no-broker end-to-end path
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src
```

## What is actually proven

**The adapter, not a description of it.** `FakeMqttTransport` implements only `MqttTransport` — the
Paho calls `PahoMqttAdapter` makes — so the production adapter code runs unchanged. It records every
side effect in order, which is what lets `tests/integration/test_mqtt_adapter.py` assert on
behaviour that a totals-only fake could not distinguish:

| Behaviour | Asserted by |
|---|---|
| Not connected until CONNACK succeeds | `test_start_connects_and_subscribes_at_qos_1` |
| A refused CONNACK is never reported as connected | `test_refused_connack_is_never_reported_as_connected` |
| A local `subscribe()` success is not a subscription | `test_local_subscribe_success_is_not_a_subscription` |
| Connected only after a SUBACK for every filter | `test_connection_is_reported_only_after_every_suback` |
| A refused or QoS-0 SUBACK drops the session | `test_a_rejected_or_downgraded_suback_drops_the_session` |
| One refusal kills the whole session, including still-pending filters | `test_a_rejected_suback_kills_the_whole_session` |
| A stale SUBACK cannot revive a failed or replaced session | `test_a_session_that_failed_on_timeout_cannot_be_revived`, `test_a_stale_suback_after_a_reconnect_cannot_confirm_the_new_session` |
| A failed session does not poison the next one | `test_a_clean_session_after_a_failed_one_still_connects` |
| No SUBACK at all times out and retries | `test_no_suback_at_all_times_out_and_retries` |
| A disconnect before the SUBACK clears the pending state | `test_a_disconnect_before_the_suback_clears_the_pending_state` |
| A locally rejected SUBSCRIBE drops the session | `test_failed_subscription_drops_the_useless_session` |
| Nothing is acknowledged before the handler decides | `test_delivery_is_acknowledged_only_after_the_handler_decides` |
| A withheld decision forces redelivery | `test_withheld_delivery_is_not_acked_and_forces_redelivery` |
| One reconnect per session, not one per message | `test_repeated_withholding_does_not_storm_the_broker` |
| A saturated queue acknowledges nothing | `test_saturated_queue_never_acknowledges_and_requests_redelivery` |
| Reconnect delay is capped exponential with jitter | `test_reconnect_delay_is_capped_exponential_with_jitter` |
| An unexpected disconnect uses that same policy | `test_an_unexpected_disconnect_uses_the_jitter_policy` |
| Each attempt is jittered independently, not once per sequence | `test_each_reconnect_attempt_gets_its_own_jitter` (scripted RNG) |
| Paho's own auto-reconnect is disabled at the client | `test_the_real_client_has_paho_auto_reconnect_disabled`, `test_the_adapter_never_configures_a_paho_retry_policy` |
| The adapter is the only scheduler | `test_an_unexpected_disconnect_has_exactly_one_scheduler`, `test_the_adapter_schedules_every_attempt_itself`, `test_a_scheduled_attempt_actually_reconnects` |
| A burst of failure signals produces one attempt | `test_a_burst_of_failure_signals_produces_one_attempt` |
| A trigger during an attempt does not overlap it | `test_a_trigger_during_an_attempt_does_not_overlap_it` |
| The loop stays responsive while `reconnect()` blocks | `test_the_event_loop_keeps_running_while_reconnect_blocks` |
| Shutdown while waiting for a retry never starts it | `test_shutdown_while_waiting_for_a_retry_never_starts_it` |
| A reconnect finishing after shutdown cannot revive the session | `test_a_reconnect_finishing_after_shutdown_cannot_revive_the_session` |
| A failing attempt grows the delay instead of spinning | `test_a_failing_attempt_schedules_the_next_one_without_spinning` |
| Nothing is enqueued once shutdown has begun | `test_messages_arriving_after_shutdown_begins_are_refused` |
| Shutdown unsubscribes, drains, acknowledges, *then* disconnects | `test_shutdown_unsubscribes_drains_then_disconnects` |

The ordering assertion matters: an acknowledgement sent after `loop_stop()` would never reach the
broker, and every drained message would be redelivered.

Reconnection has one owner. Paho's automatic reconnect is switched off with
`reconnect_on_failure=False`, so the adapter's scheduler is the only thing that decides when to
connect — including the first connect, which runs through the same path rather than through Paho's
retry-the-first-connection loop. The blocking `reconnect()` call runs in a worker thread; the fake
transport can park it on an Event, which is how "the loop stays responsive" is asserted by handling
a real delivery while the attempt is stuck.

**The whole process.** `tests/integration/test_lifespan.py` starts the real application through the
real lifespan with that transport injected. One delivery becomes a persisted row that the REST API
serves, health reports the adapter state, duplicates and poison messages are acknowledged without a
second row, and shutdown drains before leaving.

**The outcome matrix.** `tests/integration/test_ingestion.py` drives `IngestionService` directly with
a `FakeMqttClient` that records which delivery ids were acknowledged, so "accepted acks", "duplicate
acks", "invalid acks" and "transient failure does **not** ack" are assertions, not claims.

**The required events and counters.** `tests/integration/test_required_events.py` captures what the
configured handler actually writes and asserts each BACKEND_SPEC section 13 category is present with
its correlation fields: ingest accepted/duplicate/rejected/storage failure/sequence gap, inference
positive/unavailable/failed, device status transition, WebSocket opened/rejected/slow-client dropped.
`tests/integration/test_stale_scan.py` covers the stale category, including that a device already
stale is not counted twice and that a failing scan is counted and retried.

**One status transition path.** `tests/integration/test_status_transitions.py` proves that telemetry,
status messages and the stale scan all report through the same `DeviceStatusTracker`: a
`stale -> online` recovery is reported, a device that keeps publishing is not counted again on every
reading, and the container hands ingestion the same tracker object the stale scan uses.

**Redaction.** `tests/unit/test_observability.py` and
`test_no_event_leaks_the_broker_password_or_a_payload` assert the broker password and payload bytes
are absent from rendered output — including from a rendered traceback, which filters do not see.

**The storage boundary.** `tests/db/test_uow.py` drives every failing combination of commit,
rollback and close and asserts no raw `SQLAlchemyError` escapes: only `TransientStorageError` makes
ingestion withhold an acknowledgement, so a raw error would silently acknowledge an unstored
message.

**Restart recovery.** `tests/integration/test_restart_recovery.py` runs the real lifespan twice over
one database file: committed rows survive, a QoS 1 redelivery after the restart is acknowledged
without a second row, process-local counters start empty, the device status comes from the row
rather than from memory, and startup still refuses an unmigrated database.

**The synthetic publisher.** `tests/unit/test_publisher.py` runs every generated valid payload
through `validate_message` — the same function ingestion uses — so the publisher and the contract
cannot drift apart silently. It also asserts each of the nine scenarios does what its name says, that
no scenario addresses a topic outside its device's v1 namespace, that the password is only ever read
from the environment, and that a run is bounded whatever `--count` says.

Three properties get their own attention, because each was a defect:

| Property | Asserted by |
|---|---|
| The same arguments and seed reproduce the **whole** plan — boot ids, topics, order and bytes | `test_the_same_seed_reproduces_the_whole_run`, `test_a_seeded_run_is_reproducible_through_the_cli` |
| Planning reads no clock and no global random state | `test_planning_reads_no_clock_and_no_global_random_state`, `test_logical_timestamps_are_derived_not_read_from_a_clock` |
| One session uses one boot id, the will included | `test_one_session_uses_one_boot_id_everywhere`, `test_the_will_is_never_a_fresh_boot_id` |
| A reconnect establishes a **new** session: new boot id, new will, and the old id is never used again | `test_a_reconnect_splits_the_run_into_two_sessions`, `test_session_b_has_its_own_will_and_telemetry`, `test_the_old_boot_id_is_never_used_after_the_reconnect`, `test_a_later_scenario_continues_in_the_replacement_session` |
| The final offline belongs to the session that is connected | `test_the_final_offline_belongs_to_the_session_that_is_connected`, `test_the_graceful_offline_uses_the_final_session` |
| The next session's will is registered before the reconnect | `test_execution_registers_the_next_session_will_before_reconnecting` |
| Paho's automatic reconnect is off, so nothing races the deliberate drop | `test_paho_automatic_reconnect_is_disabled`, `test_no_hidden_paho_automatic_reconnect` |
| A drop retires the old network loop *before* the replacement one is started, and only one ever runs | `test_a_drop_retires_the_old_loop_before_opening_a_new_one`, `test_only_one_network_loop_runs_at_a_time`, `test_the_handoff_registers_the_will_between_stop_and_reconnect` |
| A refused `loop_start()` fails the run instead of being ignored | `test_a_refused_loop_start_fails_the_run`, `test_start_loop_accepts_only_success`, `test_a_failed_initial_loop_start_fails_the_cli` |
| Shutdown leaves no network thread running | `test_shutdown_leaves_no_network_thread_running` |
| An unconfirmed goodbye exits non-zero; a confirmed one exits zero | `test_an_unconfirmed_goodbye_exits_non_zero`, `test_the_cli_returns_the_execution_exit_code` |
| A graceful offline waits for the broker to confirm it, and a timeout is reported rather than assumed | `test_a_graceful_publication_waits_for_the_broker_to_confirm`, `test_an_unconfirmed_publication_is_reported_not_assumed` |
| An unexpected drop is an action, not a hand-published will payload | `test_the_drop_step_carries_the_will_of_the_session_it_kills`, `test_no_scenario_publishes_a_will_payload_by_hand` |

**Broker configuration and the instructions that describe it.**
`tests/unit/test_broker_config.py` parses `compose.yaml`, both mosquitto configurations, the ACL and
the READMEs, and asserts they agree: the listeners bind where each path needs them to, the mounted
paths match the configured ones, only port 1883 is published, no document names a file that does not
exist, and no document quotes a real password. Documentation drift is what this catches — the
container listener used to bind loopback, which made the published port unreachable while the README
happily told people to use it.

**The smoke test's own guard rails.** `tests/unit/test_smoke_orchestration.py` exercises the skip
logic without a broker, because a wrong skip reason is how a NOT_RUN quietly becomes a believed PASS.

**No credential in an argument list.** `tests/unit/test_check_broker.py` holds
`scripts/check_broker.py` to the reason it exists: the password reaches the client and nothing else —
not the output, not the `Result`, not even a connection error's text, which reports the exception
type rather than its message. `test_no_document_passes_a_password_on_a_command_line` checks that no
document tells anyone to pass one either.

**Every required subscription, confirmed one for one.** The same file holds the check to its other
claim: it passes only when *all* required filters are confirmed at QoS 1 or better. A broker that
acknowledges telemetry and silently drops status must not look healthy. `SubscriptionTracker` keeps
one-to-one ownership between a filter, the `subscribe()` call that requested it and the SUBACK that
answers it, which is what makes a duplicate, a stale message id and an answer that arrives *before*
`subscribe()` returns all decidable:

| Case | Test |
|---|---|
| Both answered → pass | `test_both_required_subacks_pass` |
| Only one answered → fail, and it names which | `test_only_the_first_suback_times_out`, `test_only_the_second_suback_times_out` |
| One accepted, one refused → fail | `test_one_accepted_and_one_refused_fails` |
| QoS 1 and QoS 0 → fail | `test_a_qos_0_grant_alongside_a_qos_1_grant_fails` |
| A duplicate SUBACK cannot fill the other slot | `test_a_duplicate_suback_cannot_satisfy_the_other_subscription` |
| An unrelated message id is ignored | `test_an_unrelated_mid_is_ignored` |
| An early SUBACK is matched to its own filter | `test_an_early_suback_is_still_matched_to_its_own_filter`, `test_an_early_partial_answer_still_fails` |
| Completion happens exactly once | `test_completion_is_reached_exactly_once` |

## Coverage

```powershell
.\.venv\Scripts\python.exe -m pytest --cov=powerguard
```

The Block 3 gate is 80% statement coverage for domain, MQTT ingestion, persistence and the inference
adapter. Current totals are well above it; the numbers are in the Block 3 handoff rather than copied
here, because a figure in prose goes stale the moment a test is added.

## What is not proven

| Item | Status |
|---|---|
| Live Mosquitto session, real QoS 1 redelivery, reconnect behaviour | NOT_RUN — `test_mosquitto_smoke.py` covers it when a broker exists |
| Paho manual-ack timing against a real broker | NOT_RUN |
| A real CONNACK reason code and a real SUBACK | NOT_RUN |
| Real SUBACK timing, and a broker that grants a lower QoS than requested | NOT_RUN |
| A WebSocket frame produced by a message that really crossed a broker | NOT_RUN — `test_a_reading_over_the_real_broker_reaches_a_websocket_client` covers it when a broker exists |
| Real device publishing over Wi-Fi | NOT_RUN |
| Python 3.12 | NOT_RUN — only 3.11.8 is installed here |
| Sustained write load / SQLite contention | NOT_RUN |

The adapter tests prove that the backend reacts correctly to each of those events; they cannot prove
that a real broker produces them with the timing assumed. Record results in the phase workspace when
the infrastructure exists. A test that was not run is written down as NOT_RUN, never as passing.

## Adding a test

- Put pure logic in `tests/unit`; anything that needs tables in `tests/db` or `tests/integration`.
- Use `tests/builders.py` for domain objects and `tests/conftest.py` fixtures for settings, the
  migrated engine, the unit-of-work factory and the fixed clock.
- Prefer asserting on behaviour that the specification names. If a test would pass with the feature
  deleted, it is not worth keeping — and if it asserts on a fake instead of on production code, it
  proves nothing about the system.
