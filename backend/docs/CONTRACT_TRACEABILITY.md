# Contract traceability

Where each authoritative Phase 01 rule is implemented and where it is proven. Anything marked
NOT_RUN has no passing evidence and must not be reported as verified.

## MQTT_SPEC.md

| Rule | Implementation | Evidence |
|---|---|---|
| Topics `powerguard/v1/devices/{id}/telemetry` and `/status` | `mqtt/topics.py` | `tests/unit/test_mqtt_validation.py::test_topic_parsing_accepts_exactly_the_v1_shape` |
| Device id `^[a-z0-9][a-z0-9_-]{0,31}$`, topic is authoritative | `mqtt/topics.py`, payload has no `device_id` | `test_topic_parsing_rejects_anything_else`, `test_unknown_and_missing_fields_are_rejected` |
| Backend client id `powerguard-backend-{instance_id}`, clean session off | `mqtt/client.py` `_build_paho_transport` | `tests/integration/test_mqtt_adapter.py::test_default_transport_is_a_persistent_authenticated_session` |
| Subscribe at QoS 1 to both filters | `mqtt/client.py` `_on_connect` | `test_start_connects_and_subscribes_at_qos_1` (the fake transport rejects any other QoS) |
| Connected state requires a successful CONNACK | `mqtt/client.py` `_on_connect` | `test_refused_connack_is_never_reported_as_connected` |
| A subscription is active only after a broker SUBACK granting QoS 1 | `mqtt/client.py` `_on_subscribe`, `granted_qos` | `test_local_subscribe_success_is_not_a_subscription`, `test_connection_is_reported_only_after_every_suback`, `test_a_rejected_or_downgraded_suback_drops_the_session`, `test_granted_qos_reads_a_suback_entry_not_a_connack_reason` |
| A missing SUBACK times out rather than hanging deaf | `mqtt/client.py` `_on_suback_timeout` | `test_no_suback_at_all_times_out_and_retries`, `test_a_suback_that_arrives_in_time_cancels_the_timeout` |
| A disconnect before the SUBACK leaves no stale state | `mqtt/client.py` `_on_disconnect` | `test_a_disconnect_before_the_suback_clears_the_pending_state`, `test_a_stale_suback_from_a_previous_session_is_ignored` |
| A subscription session fails as a whole, and stays failed | `mqtt/client.py` `_fail_session`, `_PendingSubscription.generation` | `test_a_rejected_suback_kills_the_whole_session`, `test_a_session_that_failed_on_timeout_cannot_be_revived`, `test_a_stale_suback_after_a_reconnect_cannot_confirm_the_new_session`, `test_a_clean_session_after_a_failed_one_still_connects` |
| Manual acknowledgement, enforced by the adapter | `mqtt/client.py` `__init__` | `test_start_connects_and_subscribes_at_qos_1`, `test_delivery_is_acknowledged_only_after_the_handler_decides` |
| A withheld acknowledgement enters a controlled redelivery path | `mqtt/client.py` `_withhold` | `test_withheld_delivery_is_not_acked_and_forces_redelivery`, `test_repeated_withholding_does_not_storm_the_broker` |
| Ingress saturation acknowledges nothing and drops the session | `mqtt/client.py` `_enqueue` | `test_saturated_queue_never_acknowledges_and_requests_redelivery` |
| Exactly one reconnect owner; Paho auto-reconnect disabled | `mqtt/client.py` `_build_paho_transport` (`reconnect_on_failure=False`) | `test_the_real_client_has_paho_auto_reconnect_disabled`, `test_the_adapter_never_configures_a_paho_retry_policy`, `test_an_unexpected_disconnect_has_exactly_one_scheduler` |
| At most one attempt in flight; the blocking call is off the loop | `mqtt/client.py` `_schedule_on_loop`, `_run_reconnect`, `_connect_blocking` | `test_a_burst_of_failure_signals_produces_one_attempt`, `test_a_trigger_during_an_attempt_does_not_overlap_it`, `test_the_event_loop_keeps_running_while_reconnect_blocks` |
| Shutdown cancels a pending attempt and discards a completing one | `mqtt/client.py` `stop`, `_connect_blocking`, `_teardown_transport` | `test_shutdown_while_waiting_for_a_retry_never_starts_it`, `test_a_reconnect_finishing_after_shutdown_cannot_revive_the_session` |
| Every reconnect attempt independently jittered, by one scheduler | `mqtt/client.py` `_schedule_reconnect`, `_attempt_reconnect` | `test_each_reconnect_attempt_gets_its_own_jitter`, `test_the_adapter_schedules_every_attempt_itself`, `test_a_scheduled_attempt_actually_reconnects`, `test_a_failing_attempt_schedules_the_next_one_without_spinning`, `test_no_reconnect_is_scheduled_once_shutdown_starts` |
| Capped exponential reconnect with jitter, on every path | `mqtt/client.py` `_schedule_reconnect` | `test_reconnect_delay_is_capped_exponential_with_jitter`, `test_successful_connect_resets_the_backoff`, `test_an_unexpected_disconnect_uses_the_jitter_policy`, `test_repeated_unexpected_disconnects_back_off` |
| No inbound message is accepted once shutdown starts | `mqtt/client.py` `_on_message`, `_enqueue` | `test_messages_arriving_after_shutdown_begins_are_refused`, `test_a_delivery_racing_the_start_of_shutdown_is_not_enqueued`, `test_a_disconnect_during_shutdown_schedules_nothing` |
| Shutdown stops intake, drains, acknowledges, then disconnects | `mqtt/client.py` `stop` | `test_shutdown_unsubscribes_drains_then_disconnects`, `test_shutdown_gives_up_after_the_grace_period` |
| `schema_version == 1` | `mqtt/schemas.py` | `test_unsupported_schema_version_has_its_own_category` |
| `boot_id` eight lowercase hex | `mqtt/schemas.py` | `test_schema_violations_are_rejected` |
| `seq` unsigned 32-bit, monotonic within boot | `mqtt/schemas.py`, gap counter in `mqtt/ingestion.py` | `test_schema_violations_are_rejected`, `test_sequence_gaps_are_counted_but_never_backfilled` |
| `sampled_at` null or RFC 3339 with ms | `mqtt/schemas.py` | `test_sampled_at_timestamp_is_parsed_when_present` |
| `sampled_at` must be a real calendar timestamp | `mqtt/schemas.py` `parse_sampled_at` | `test_impossible_sampled_at_dates_are_rejected_deterministically`, `test_a_real_leap_day_is_accepted` |
| Finite measurements, non-negative energy | `mqtt/schemas.py` | `test_non_finite_numbers_are_rejected` |
| `sensor_status == "ok"` | `mqtt/schemas.py` | `test_schema_violations_are_rejected` |
| Firmware version SemVer, max 32 chars | `mqtt/schemas.py` | `test_schema_violations_are_rejected` |
| Payload at most 2 KiB | `mqtt/validation.py` — size checked before decode | `test_oversized_payload_is_rejected_before_parsing` |
| Unknown fields rejected | `extra="forbid"` | `test_unknown_and_missing_fields_are_rejected` |
| Status payload: four fields, `online\|offline` | `mqtt/schemas.py` | `test_invalid_status_payloads_are_rejected` |
| Retained flag kept as metadata, payload untouched | `mqtt/validation.py` `ValidStatus.retained` | `test_valid_status_is_accepted_with_retained_metadata` |
| Duplicates deduplicated by `(device_id, boot_id, seq)` | unique constraint, `db/repositories.py` | `test_duplicate_delivery_is_acked_without_a_second_row_or_event` |
| No telemetry on sensor error | v1 payload only carries `sensor_status="ok"` | `test_schema_violations_are_rejected` |
| Credentials never in payloads, logs or fixtures | `SecretStr`, `Invalid.detail` carries no payload | `test_rejection_never_echoes_payload_content`, `test_secret_never_appears_in_repr_or_errors` |

## DATABASE_SCHEMA.md

| Rule | Implementation | Evidence |
|---|---|---|
| Three tables with the approved columns | `alembic/versions/0001_initial_schema.py` | `tests/db/test_schema.py::test_tables_and_columns` |
| `UNIQUE(device_id, boot_id, seq)` | migration | `test_idempotency_unique_key` |
| `telemetry_device_received(device_id, received_at DESC, id DESC)` | migration | `test_descending_query_indexes_exist` |
| `anomalies_device_detected(device_id, detected_at DESC, id DESC)` | migration | `test_descending_query_indexes_exist` |
| One anomaly per telemetry row, cascade | migration | `test_one_anomaly_per_telemetry_row`, `test_foreign_keys_cascade` |
| Structural checks (seq, energy, sensor status, boot id) | migration | `test_check_constraints_reject_invalid_rows` |
| UTC ISO-8601 text with fixed microseconds | `db/types.py` | `tests/unit/test_time_types.py` |
| `foreign_keys=ON`, WAL, finite `busy_timeout` | `db/engine.py` `sqlite_pragmas` | `test_connection_pragmas`, `test_pragma_list_is_the_single_source_of_truth` |
| Alembic connections use the same pragmas | `alembic/env.py` | `tests/db/test_schema.py::test_alembic_applies_the_same_pragmas_as_the_application` |
| Operational failures surface as `TransientStorageError` | `db/repositories.py` `_SqlRepository`, `db/uow.py` | `test_operational_failures_surface_as_transient_storage_errors`, `test_a_failed_read_is_also_transient` |
| A failing rollback or close never escapes raw, and never masks the original error | `db/uow.py` `__exit__`, `commit` | `test_a_failed_rollback_on_exit_is_reported_as_transient`, `test_a_failed_close_is_reported_as_transient`, `test_a_failed_close_never_masks_the_original_error`, `test_no_raw_sqlalchemy_error_escapes_the_unit_of_work` |
| In-memory databases report their real mode | `db/engine.py` | `test_in_memory_database_reports_its_real_journal_mode` |
| Alembic owns schema, also in tests | `tests/conftest.py` | `test_alembic_upgrade_downgrade_upgrade`, `test_second_upgrade_is_a_no_op` |
| Device upsert + telemetry insert in one transaction | `mqtt/ingestion.py::_persist_telemetry` | `test_accepted_telemetry_persists_broadcasts_and_acks` |
| Anomaly in a second transaction after commit | `mqtt/ingestion.py::_persist_anomaly` | `test_anomaly_is_committed_and_broadcast_after_telemetry` |
| Raw payloads never stored | no column holds them | code review |

## ADR-006 (time and idempotency)

| Rule | Implementation | Evidence |
|---|---|---|
| Server `received_at` is authoritative order | stamped in `mqtt/client.py` callback | `test_received_at_is_server_time_not_payload_time` |
| `sampled_at` nullable and diagnostic | `db/models.py`, DTOs | same test |
| Uniqueness on `(device_id, boot_id, seq)` | database constraint | `test_duplicate_unique_key_resolves_to_existing_row` |

## API_CONTRACT.md

| Rule | Implementation | Evidence |
|---|---|---|
| Five read endpoints under `/api/v1` | `api/router.py` | `tests/integration/test_api.py` |
| Health 200 with independent mqtt/model fields | `api/routes/health.py` | `test_health_reports_subsystems_independently` |
| Devices list with optional `latest` | `api/routes/devices.py` | `test_list_devices_includes_latest_telemetry` |
| Latest telemetry includes its anomaly | `api/routes/devices.py` | `test_latest_telemetry_carries_its_anomaly`, `test_device_list_latest_carries_the_stored_anomaly` |
| Telemetry history includes each row's anomaly | `api/routes/devices.py`, `anomalies.by_telemetry_ids` | `test_telemetry_history_carries_the_stored_anomaly`, `test_paginated_history_resolves_anomalies_on_every_page` |
| Persisted ids and anomaly measurements are never null | `api/schemas.py` `_persisted_id` | `test_openapi_never_promises_a_null_persisted_id`, `test_openapi_anomaly_history_measurements_are_required`, `test_openapi_keeps_genuinely_optional_fields_nullable` |
| Only the five read endpoints are published | `api/router.py` | `test_openapi_publishes_exactly_the_v1_read_endpoints` |
| 404 for unknown device or no telemetry | `api/errors.py` | `test_latest_is_404_for_unknown_device_and_for_no_telemetry` |
| `from` inclusive, `to` exclusive and later | `api/dependencies.py` | `test_history_time_window`, `test_invalid_time_windows_are_rejected` |
| `limit` 1..5000 default 500, positive `before_id` | `api/dependencies.py` | `test_out_of_range_query_parameters_are_422` |
| Newest first, stable `next_before_id` | `db/repositories.py` | `test_history_is_newest_first_and_paginates_stably` |
| Anomaly history includes measurements | `api/routes/devices.py` | `test_anomaly_history_includes_the_measurements` |
| RFC 3339 `Z` timestamps | `api/schemas.py` | `test_list_devices_includes_latest_telemetry` |
| One error envelope, no internals leaked | `api/errors.py` | `test_error_envelope_never_leaks_internals` |
| CORS exact allow-list | `main.py`, `config.py` | `tests/unit/test_app.py::test_cors_allow_list_is_exact` |

## WebSocket v1

| Rule | Implementation | Evidence |
|---|---|---|
| `/ws/v1/devices/{device_id}` | `api/routes/websocket.py` | `tests/integration/test_websocket.py` |
| Unknown device closes 4404, malformed 4400 | `api/routes/websocket.py` | `test_unknown_device_is_closed_with_4404`, `test_malformed_device_id_is_closed_with_4400` |
| v1 envelope with `schema_version`, `type`, `emitted_at`, `data` | `realtime/events.py` | `tests/unit/test_hub.py::test_telemetry_event_envelope_matches_v1` |
| Telemetry then anomaly per reading | `mqtt/ingestion.py` | `test_anomaly_is_committed_and_broadcast_after_telemetry` |
| Status frame: device id, status, last seen | `realtime/events.py` | `test_anomaly_and_status_envelopes` |
| Connection cap before registration | `realtime/hub.py` | `test_connection_cap_is_enforced_before_registration` |
| Bounded per-client queue, slow client dropped 1013 | `realtime/hub.py` | `test_slow_client_is_dropped_without_blocking_others`, `test_slow_client_is_actually_closed_with_1013` |
| A dropped subscription wakes its pump so the close is sent | `realtime/hub.py` `Subscription.close` | `test_dropping_a_slow_client_wakes_its_pump`, `test_a_waiting_pump_is_released_by_unsubscribe` |
| An idle disconnect is noticed on a send-only endpoint | `api/routes/websocket.py` `_watch_client` | `test_the_pump_notices_a_disconnect_with_no_traffic` |
| Configured ping interval and timeout reach the server | `__main__.py` `_cmd_serve` | `tests/unit/test_app.py::test_serve_passes_the_configured_websocket_ping_settings` |
| Subscription always removed | `finally` block | `test_connection_slot_is_released_on_disconnect`, `test_a_dropped_client_is_counted_as_closed_exactly_once` |
| Only committed records broadcast | ingestion publishes after commit | `test_transient_storage_failure_is_not_acknowledged` |

## Configuration and observability

| Rule | Implementation | Evidence |
|---|---|---|
| Electrical bounds must be finite | `config.py` `_require_finite_bound` | `tests/unit/test_config.py::test_non_finite_bounds_are_rejected` |
| `DATABASE_URL` is parsed and validated, not prefix-matched | `config.py` `parse_sqlite_url` | `test_malformed_sqlite_urls_fail_fast`, `test_supported_sqlite_urls_resolve_their_path`, `test_existing_directory_is_not_a_database` |
| Build requirements are pinned | `pyproject.toml`, `requirements-build.lock` | exact pins; reproducible install documented in `README.md` |
| Structured events with non-secret fields | `observability.py` `log_event` | `test_event_line_carries_the_event_name_and_fields`, `test_json_format_emits_one_object_per_record` |
| Payload bytes never reach a log line | `observability.py` `redact_fields` | `test_a_payload_passed_as_a_field_never_reaches_the_output` |
| Credentials never reach a log line, including in tracebacks | `observability.py` `SecretScrubber` | `test_a_secret_logged_by_careless_code_is_still_scrubbed`, `test_scrubbing_survives_percent_formatting_and_exceptions` |
| Adapter counters exist for diagnostics | `mqtt/client.py` `MqttCounters` | asserted throughout `tests/integration/test_mqtt_adapter.py` |
| Ingest events: accepted, duplicate, rejected, storage failure, sequence gap | `mqtt/ingestion.py` | `test_accepted_duplicate_and_rejected_each_emit_their_event`, `test_a_storage_failure_emits_its_own_event_and_counter`, `test_a_sequence_gap_is_counted_and_named` |
| Inference events: positive, unavailable, failed | `mqtt/ingestion.py` | `test_a_positive_verdict_is_counted_and_named`, `test_an_unavailable_model_is_counted_separately_from_a_failure`, `test_an_inference_failure_is_counted_and_named` |
| Every status transition on one path, whatever its origin | `device_status.py` `DeviceStatusTracker`, used by `mqtt/ingestion.py` and `bootstrap.py` | `tests/integration/test_status_transitions.py`, notably `test_telemetry_reports_a_stale_device_coming_back`, `test_continuing_telemetry_is_not_a_transition_every_reading`, `test_the_stale_scan_reports_through_the_same_path`, `test_the_container_shares_one_tracker_with_ingestion` |
| Device status transition counted once per real change | `device_status.py` | `test_a_status_transition_is_counted_once`, `test_a_retained_status_is_marked_as_such`, `test_only_a_real_change_is_a_transition` |
| Stale scan counters and events | `bootstrap.py` `StaleCounters` | `test_stale_counters_describe_what_the_scan_did`, `test_a_failing_scan_does_not_kill_the_loop` |
| WebSocket opened / rejected / closed / slow-client dropped events | `realtime/hub.py`, `api/routes/websocket.py` | `test_websocket_open_close_and_drop_are_named` |
| HTTP unexpected error event with a correlation id | `api/errors.py` | `test_error_envelope_never_leaks_internals` |
| No event leaks a payload or a credential | `observability.py` | `test_no_event_leaks_the_broker_password_or_a_payload` |
| In-process migrations do not disable application logging | `alembic/env.py` | every test in `tests/integration/test_required_events.py` (they migrate first) |

## Verification and tooling (Block 3)

| Rule | Implementation | Evidence |
|---|---|---|
| The software gate needs no broker, hardware, Docker or internet | fake transport + migrated temporary databases | the whole default `pytest` run |
| Restart recovers exactly what was committed | the database is the only durable state | `tests/integration/test_restart_recovery.py` |
| Idempotency survives a restart | `UNIQUE(device_id, boot_id, seq)` | `test_idempotency_still_holds_after_a_restart` |
| Startup refuses an unmigrated database | `bootstrap.verify_migrations` | `test_startup_refuses_a_database_that_was_never_migrated` |
| Nine synthetic scenarios, documented in `--help` | `scripts/publish_synthetic.py` | `test_the_nine_specified_scenarios_are_offered`, `test_help_documents_every_scenario` |
| Generated valid payloads satisfy the ingestion contract | shared `validate_message` | `test_valid_scenarios_are_accepted_by_the_backend_validator` |
| Generated payloads stay inside the hardware contract (8.4 V) | `PACK_CEILING_V` clamp | `test_no_scenario_breaches_the_hardware_contract` |
| The publisher never prints or accepts a credential on the command line | `--password-env` only | `test_the_password_is_only_ever_read_from_the_environment`, `test_a_missing_password_refuses_to_publish` |
| The publisher addresses only its own device's v1 topics | `plan()` assertion | `test_a_scenario_only_addresses_its_own_device_topics` |
| A run is bounded | `MAX_MESSAGES` | `test_a_run_is_bounded_however_large_the_count_is` |
| A seeded run reproduces the complete plan | `scripts/publish_synthetic.py` `plan()`, `build_messages()` | `test_the_same_seed_reproduces_the_whole_run`, `test_a_seeded_run_is_reproducible_through_the_cli`, `test_planning_reads_no_clock_and_no_global_random_state` |
| `sampled_at` is a logical UTC timestamp, not a wall clock | `logical_timestamp()` | `test_logical_timestamps_are_derived_not_read_from_a_clock`, `test_a_logical_timestamp_is_accepted_by_the_backend` |
| One session, one `boot_id` — telemetry, status and the will | `Session`, `split_sessions()` | `test_one_session_uses_one_boot_id_everywhere`, `test_the_will_is_never_a_fresh_boot_id`, `test_only_a_deliberate_reboot_introduces_a_second_boot_id` |
| A reconnect establishes a new session identity, and the old one is retired | `split_sessions()`, `ScenarioResult.boot` | `test_a_reconnect_splits_the_run_into_two_sessions`, `test_session_b_has_its_own_will_and_telemetry`, `test_the_old_boot_id_is_never_used_after_the_reconnect`, `test_a_later_scenario_continues_in_the_replacement_session` |
| The will always describes the session holding the connection | `execute()` | `test_execution_registers_the_next_session_will_before_reconnecting`, `test_the_final_offline_belongs_to_the_session_that_is_connected` |
| The publisher is the only reconnect owner | `build_client()` (`reconnect_on_failure=False`) | `test_paho_automatic_reconnect_is_disabled`, `test_no_hidden_paho_automatic_reconnect` |
| A session handoff retires the old network loop before starting one loop for the replacement, and checks the result | `handoff()`, `start_loop()` | `test_a_drop_retires_the_old_loop_before_opening_a_new_one`, `test_only_one_network_loop_runs_at_a_time`, `test_a_refused_loop_start_fails_the_run`, `test_shutdown_leaves_no_network_thread_running` |
| The broker check passes only when every required filter is confirmed at QoS 1 | `SubscriptionTracker`, `Result.ok` | `tests/unit/test_check_broker.py` — both-confirmed, single-answer, refusal, QoS 0, duplicate, stale mid, early SUBACK, exactly-once completion |
| An unconfirmed goodbye fails the command | `execute()` return code | `test_an_unconfirmed_goodbye_exits_non_zero`, `test_the_cli_returns_the_execution_exit_code` |
| No credential appears in an argument list, output or error text | `scripts/check_broker.py` | `tests/unit/test_check_broker.py`, `test_no_document_passes_a_password_on_a_command_line` |
| A graceful offline is confirmed at QoS 1 before disconnecting | `publish_confirmed()` | `test_a_graceful_publication_waits_for_the_broker_to_confirm`, `test_an_unconfirmed_publication_is_reported_not_assumed` |
| An unexpected drop lets the **broker** publish the will | `drop_connection()`, the `DROP` action | `test_the_drop_step_carries_the_will_of_the_session_it_kills`, `test_no_scenario_publishes_a_will_payload_by_hand` |
| Broker configuration matches its own instructions | `compose.yaml`, `deploy/mosquitto/**` | `tests/unit/test_broker_config.py` (25 cases) |
| The smoke test skips honestly rather than silently passing | `skip_reason()` | `tests/unit/test_smoke_orchestration.py` |
| Authenticated broker smoke, when one exists — including the WebSocket path | `tests/integration/test_mosquitto_smoke.py`, `deploy/mosquitto/**`, `compose.yaml` | **NOT_RUN** — no broker installed; enabling command in `tests/integration/README.md` |

## Phase 04 — the dashboard's side of these contracts

The consumer of the contracts above. Tests live in `frontend/tests`.

| Rule | Implementation | Evidence |
|---|---|---|
| The five read endpoints, and no mutating operation | `frontend/src/api/client.ts` | `tests/contract/openapi.test.ts` |
| Client types match the published schemas, in both directions | `frontend/src/api/contract.ts` | `frontend/tests/contract/dto.test.ts` and the committed OpenAPI fixture |
| `from` / `to` / `limit` (1..5000) / `before_id` built as specified | `buildHistoryQuery` | `tests/unit/client.test.ts` |
| The single error envelope is what the user is shown | `ApiError`, `ErrorState` | `test_client` envelope cases, `tests/views` error states |
| `received_at` is the authoritative order | `realtime/series.ts` | `orders by received_at`, `breaks a tie on received_at by id` |
| WebSocket v1 envelope, and only `telemetry`/`anomaly`/`status` | `realtime/socket.ts` `isDeviceEvent` | `tests/unit/realtime/socket.test.ts` |
| 4404 / 4400 are permanent; 1013 and drops retry | `isPermanentClose`, `DeviceSocket` | same file, close-code cases |
| Reconnect uses capped exponential backoff with per-attempt jitter | `backoffDelayMs` | jitter-window, cap and reset cases |
| First open and reconnect backfill the retained window through REST pages | `useDeviceStream` | `tests/unit/realtime/useDeviceStream.test.tsx` first-open and multi-page cases |
| Failed/truncated backfill is reported and retried, not hidden | `useDeviceStream` `catchUp` | same file, failure and truncation cases |
| The client buffer is bounded, like the server's hub | `MAX_POINTS = 600` | `tests/unit/realtime/series.test.ts` |
| `mqtt: disconnected` is a state, not a failure | `HealthHeader` | `treats a broker outage as a state, not as a broken service` |
| `model != ready` reads as "detection unavailable", never "no anomalies" | `AnomalyList`, `isModelReady` | `says detection is unavailable, never that there are no anomalies` |
| Every view has loading, error, empty and populated states | `src/views/**`, `components/states.tsx` | `tests/views/views.test.tsx` |
| Timestamps show the local zone with the UTC offset visible | `format/time.ts`, `HealthHeader` | `tests/unit/format.test.ts` |

## Outstanding — no evidence yet

| Item | Reason |
|---|---|
| Live Mosquitto connect, subscribe, QoS 1 ack, reconnect | no broker installed — **NOT_RUN**. The adapter's reaction to each event is proven against a fake transport; the broker producing them is not. |
| Retained LWT observed from a real device | no hardware — **NOT_RUN** |
| End-to-end device → backend → dashboard | dashboard built; hardware and broker absent — **NOT_RUN** |
| Anomaly detection quality | only the unavailable adapter exists; no model yet |
| Python 3.12 run | interpreter not installed here — **NOT_RUN** |
