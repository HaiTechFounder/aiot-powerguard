"""The real-data gate, the review manifest, and reading the backend database.

The gate is the only thing that can turn a fit into a promotable artifact, so
these tests are mostly about the ways it must say no: too little data, no
attestation, a missing calibration fingerprint, two regimes, two devices, or
rows that do not survive the contiguity rule training itself applies.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest

from powerguard_ml import gate, review, sqlite_source, synthetic
from powerguard_ml.dataset import Sample

DEVICE = "powerguard-01"
REGIME = "ina226-r010-bench-a"
START = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
BOOT = "boot-0001"
#: A witnessed fault, timed by the operator, after the normal window.
EVENT_START = START + dt.timedelta(days=1)
EVENT_END = EVENT_START + dt.timedelta(minutes=10)


def _z(moment: dt.datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _manifest(
    *,
    provenance: str = review.PROVENANCE_HARDWARE,
    start: dt.datetime = START,
    end: dt.datetime | None = None,
    fingerprint: str = REGIME,
    boot_ids: tuple[str, ...] = (BOOT,),
    labelled_events: bool = True,
) -> review.Manifest:
    """A complete attestation unless a test deliberately leaves something out."""
    return review.parse_manifest(
        {
            "manifest_version": review.MANIFEST_VERSION,
            "device_id": DEVICE,
            "calibration_fingerprint": fingerprint,
            "provenance": provenance,
            "firmware_version": "0.1.0",
            "shunt_resistance_ohm": 0.01,
            "boot_ids": list(boot_ids),
            "reviewed_by": "operator",
            "reviewed_at": "2026-09-23T00:00:00Z",
            "approved_intervals": [
                {"from": _z(start), "to": _z(end or EVENT_START), "label": 0}
            ],
            "labelled_events": (
                [{"from": _z(EVENT_START), "to": _z(EVENT_END), "label": 1}]
                if labelled_events
                else []
            ),
        }
    )


def _approved_rows(count: int, *, abnormal: int = 20) -> list[Sample]:
    """Contiguous normal rows plus a labelled fault, stamped as an operator would."""
    rows = synthetic.training_set(count + abnormal, seed=11)
    normal = [
        Sample(
            id=row.id,
            device_id=DEVICE,
            boot_id=row.boot_id,
            seq=row.seq,
            received_at=START + dt.timedelta(seconds=2 * index),
            voltage_v=row.voltage_v,
            current_a=row.current_a,
            power_w=row.power_w,
            energy_wh=row.energy_wh,
            sensor_status=row.sensor_status,
        )
        for index, row in enumerate(rows[:count])
    ]
    fault = [
        Sample(
            id=row.id,
            device_id=DEVICE,
            boot_id=row.boot_id,
            seq=row.seq,
            received_at=EVENT_START + dt.timedelta(seconds=2 * index),
            voltage_v=row.voltage_v,
            current_a=row.current_a * 3,
            power_w=row.power_w * 3,
            energy_wh=row.energy_wh,
            sensor_status=row.sensor_status,
        )
        for index, row in enumerate(rows[count:])
    ]
    return review.apply_manifest(normal + fault, _manifest())


# -- the manifest -----------------------------------------------------------


def test_a_manifest_without_an_attestation_is_refused() -> None:
    with pytest.raises(review.ReviewError, match="missing"):
        review.parse_manifest(
            {
                "manifest_version": review.MANIFEST_VERSION,
                "device_id": DEVICE,
                "provenance": review.PROVENANCE_HARDWARE,
                "firmware_version": "0.1.0",
                "shunt_resistance_ohm": 0.01,
                "reviewed_by": "operator",
                "reviewed_at": "2026-09-23T00:00:00Z",
                "approved_intervals": [],
            }
        )


def test_a_manifest_that_approves_nothing_is_refused() -> None:
    with pytest.raises(review.ReviewError, match="approves nothing"):
        review.parse_manifest(
            {
                "manifest_version": review.MANIFEST_VERSION,
                "device_id": DEVICE,
                "calibration_fingerprint": REGIME,
                "provenance": review.PROVENANCE_HARDWARE,
                "firmware_version": "0.1.0",
                "shunt_resistance_ohm": 0.01,
                "reviewed_by": "operator",
                "reviewed_at": "2026-09-23T00:00:00Z",
                "approved_intervals": [],
            }
        )


def test_normal_and_abnormal_claims_may_not_overlap() -> None:
    with pytest.raises(review.ReviewError, match="cannot be both"):
        review.parse_manifest(
            {
                "manifest_version": review.MANIFEST_VERSION,
                "device_id": DEVICE,
                "calibration_fingerprint": REGIME,
                "provenance": review.PROVENANCE_HARDWARE,
                "firmware_version": "0.1.0",
                "shunt_resistance_ohm": 0.01,
                "reviewed_by": "operator",
                "reviewed_at": "2026-09-23T00:00:00Z",
                "approved_intervals": [
                    {"from": "2026-09-01T00:00:00Z", "to": "2026-09-02T00:00:00Z"}
                ],
                "labelled_events": [
                    {"from": "2026-09-01T12:00:00Z", "to": "2026-09-01T13:00:00Z"}
                ],
            }
        )


def test_rows_outside_every_interval_are_dropped_not_approved() -> None:
    manifest = _manifest(start=START, end=START + dt.timedelta(seconds=20), boot_ids=())
    rows = [
        Sample(
            id=index,
            device_id=DEVICE,
            boot_id="b",
            seq=index,
            received_at=START + dt.timedelta(seconds=2 * index),
            voltage_v=8.0,
            current_a=0.4,
            power_w=3.2,
            energy_wh=0.1,
            sensor_status="ok",
        )
        for index in range(40)
    ]
    approved = review.apply_manifest(rows, manifest)
    # 20 seconds at a 2-second cadence, half-open: ten rows.
    assert len(approved) == 10
    assert all(row.calibration_fingerprint == REGIME for row in approved)


def test_another_devices_rows_are_never_approved() -> None:
    rows = [
        Sample(
            id=1,
            device_id="some-other-device",
            boot_id="b",
            seq=1,
            received_at=START,
            voltage_v=8.0,
            current_a=0.4,
            power_w=3.2,
            energy_wh=0.1,
            sensor_status="ok",
        )
    ]
    assert review.apply_manifest(rows, _manifest()) == []


def test_the_template_cannot_pass_the_gate_as_written() -> None:
    payload = review.template(DEVICE, [])
    manifest = review.parse_manifest(payload)
    assert manifest.provenance == review.PROVENANCE_UNKNOWN
    assert not manifest.is_hardware


# -- the gate ---------------------------------------------------------------


def test_no_manifest_means_no_promotion() -> None:
    result = gate.evaluate_gate(_approved_rows(1500), None)
    assert not result.passed
    assert "no operator review manifest" in result.reasons[0]
    assert result.data_quality == gate.QUALITY_NOT_ESTABLISHED


def test_too_few_approved_samples_blocks() -> None:
    result = gate.evaluate_gate(_approved_rows(200), _manifest())
    assert not result.passed
    assert any("at least 1000" in reason for reason in result.reasons)


def test_synthetic_provenance_blocks_however_much_data_there_is() -> None:
    rows = _approved_rows(1500)
    result = gate.evaluate_gate(rows, _manifest(provenance=review.PROVENANCE_SYNTHETIC))
    assert not result.passed
    assert any("provenance" in reason for reason in result.reasons)


def test_a_missing_calibration_fingerprint_is_never_a_wildcard() -> None:
    rows = [
        Sample(
            id=row.id,
            device_id=row.device_id,
            boot_id=row.boot_id,
            seq=row.seq,
            received_at=row.received_at,
            voltage_v=row.voltage_v,
            current_a=row.current_a,
            power_w=row.power_w,
            energy_wh=row.energy_wh,
            sensor_status=row.sensor_status,
            label=row.label,
            calibration_fingerprint=None,
        )
        for row in _approved_rows(1500)
    ]
    result = gate.evaluate_gate(rows, _manifest())
    assert not result.passed
    assert any("no calibration fingerprint" in reason for reason in result.reasons)


def test_a_fully_approved_hardware_dataset_passes() -> None:
    result = gate.evaluate_gate(_approved_rows(1500), _manifest())
    assert result.passed, result.reasons
    assert result.data_quality == gate.QUALITY_VALIDATED
    assert result.device_id == DEVICE
    assert result.calibration_fingerprint == REGIME


# -- the SQLite source ------------------------------------------------------


def _write_db(
    path: Path, rows: list[tuple[object, ...]], *, with_fingerprint: bool = False
) -> None:
    connection = sqlite3.connect(path)
    extra = ", calibration_fingerprint TEXT" if with_fingerprint else ""
    connection.execute(
        "CREATE TABLE telemetry (id INTEGER PRIMARY KEY, device_id TEXT, boot_id TEXT, "
        "seq INTEGER, received_at TEXT, voltage_v REAL, current_a REAL, power_w REAL, "
        f"energy_wh REAL, sensor_status TEXT{extra})"
    )
    connection.execute("CREATE TABLE anomalies (id INTEGER PRIMARY KEY)")
    placeholders = ",".join("?" * (10 + (1 if with_fingerprint else 0)))
    connection.executemany(f"INSERT INTO telemetry VALUES ({placeholders})", rows)
    connection.commit()
    connection.close()


def _db_rows(count: int, *, stride: int = 1) -> list[tuple[object, ...]]:
    return [
        (
            index + 1,
            DEVICE,
            "boot-a",
            index * stride,
            (START + dt.timedelta(seconds=2 * index)).isoformat().replace("+00:00", "Z"),
            8.0,
            0.4,
            3.2,
            0.1 * index,
            "ok",
        )
        for index in range(count)
    ]


def test_the_export_never_invents_a_calibration_fingerprint(tmp_path: Path) -> None:
    database = tmp_path / "powerguard.db"
    _write_db(database, _db_rows(50))
    samples = sqlite_source.read_telemetry(database)
    assert len(samples) == 50
    # The schema has no such column, so every row must come back unattested.
    assert all(sample.calibration_fingerprint is None for sample in samples)


def test_the_audit_names_the_missing_attestations(tmp_path: Path) -> None:
    database = tmp_path / "powerguard.db"
    _write_db(database, _db_rows(50))
    report = sqlite_source.audit(database)

    assert not report.calibration_fingerprint_available
    assert not report.provenance_recorded
    joined = " ".join(report.blocking_findings)
    assert "calibration_fingerprint" in joined
    assert "publish_synthetic.py" in joined
    assert "1000" in joined
    assert sqlite_source.render(report)


def test_the_documented_firmware_stride_is_contiguous(tmp_path: Path) -> None:
    """Stride 2 is what a healthy ESP8266 produces, not a hole.

    1000 ms sampling, 2000 ms publishing, and `seq` consumed on every read
    attempt, so each published row advances it by two.
    """
    database = tmp_path / "powerguard.db"
    _write_db(database, _db_rows(60, stride=2))
    report = sqlite_source.audit(database)

    assert not any("seq stride" in finding for finding in report.blocking_findings)
    device = report.devices[0]
    assert device.usable_segments == 1
    assert device.usable_samples == 60


def test_a_stride_beyond_the_documented_one_is_reported(tmp_path: Path) -> None:
    # Stride 3 is neither 1 nor the firmware's 2: this capture did not come
    # from the configuration the pipeline is calibrated for.
    database = tmp_path / "powerguard.db"
    _write_db(database, _db_rows(60, stride=3))
    report = sqlite_source.audit(database)

    assert any("seq stride" in finding for finding in report.blocking_findings)
    assert report.devices[0].usable_segments == 0


def test_a_contiguous_capture_reports_usable_segments(tmp_path: Path) -> None:
    database = tmp_path / "powerguard.db"
    _write_db(database, _db_rows(120))
    report = sqlite_source.audit(database)
    device = report.devices[0]

    assert device.usable_segments == 1
    assert device.usable_samples == 120
    assert device.longest_segment == 120


def test_a_missing_database_is_reported_not_crashed(tmp_path: Path) -> None:
    with pytest.raises(sqlite_source.SqliteSourceError, match="no database"):
        sqlite_source.audit(tmp_path / "absent.db")


def test_off_state_rows_are_counted(tmp_path: Path) -> None:
    database = tmp_path / "powerguard.db"
    rows = _db_rows(40)
    idle = [(*row[:5], 0.05, 0.0, 0.0, row[8], "ok") for row in rows[:10]]
    _write_db(database, idle + rows[10:])
    report = sqlite_source.audit(database)

    assert report.devices[0].off_state_samples == 10


def test_the_manifest_round_trips_through_a_file(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    payload = review.template(DEVICE, [])
    payload["provenance"] = review.PROVENANCE_HARDWARE
    payload["calibration_fingerprint"] = REGIME
    payload["reviewed_by"] = "operator"
    payload["firmware_version"] = "0.1.0"
    path.write_text(json.dumps(payload), encoding="utf-8")

    manifest = review.load_manifest(path)
    assert manifest.is_hardware
    assert manifest.calibration_fingerprint == REGIME


# -- capture attestation ----------------------------------------------------


def test_a_manifest_without_a_firmware_version_is_refused() -> None:
    with pytest.raises(review.ReviewError, match="firmware_version"):
        review.parse_manifest(
            {
                "manifest_version": review.MANIFEST_VERSION,
                "device_id": DEVICE,
                "calibration_fingerprint": REGIME,
                "provenance": review.PROVENANCE_HARDWARE,
                "shunt_resistance_ohm": 0.01,
                "reviewed_by": "operator",
                "reviewed_at": "2026-09-23T00:00:00Z",
                "approved_intervals": [
                    {"from": "2026-09-01T00:00:00Z", "to": "2026-09-02T00:00:00Z"}
                ],
            }
        )


def test_a_manifest_without_a_shunt_value_is_refused() -> None:
    # The shunt is part of the regime's identity: the same board with a
    # different shunt is a different calibration.
    with pytest.raises(review.ReviewError, match="shunt_resistance_ohm"):
        review.parse_manifest(
            {
                "manifest_version": review.MANIFEST_VERSION,
                "device_id": DEVICE,
                "calibration_fingerprint": REGIME,
                "provenance": review.PROVENANCE_HARDWARE,
                "firmware_version": "0.1.0",
                "reviewed_by": "operator",
                "reviewed_at": "2026-09-23T00:00:00Z",
                "approved_intervals": [
                    {"from": "2026-09-01T00:00:00Z", "to": "2026-09-02T00:00:00Z"}
                ],
            }
        )


def test_rows_from_an_unwitnessed_boot_are_not_approved() -> None:
    payload = {
        "manifest_version": review.MANIFEST_VERSION,
        "device_id": DEVICE,
        "calibration_fingerprint": REGIME,
        "provenance": review.PROVENANCE_HARDWARE,
        "firmware_version": "0.1.0",
        "shunt_resistance_ohm": 0.01,
        "boot_ids": ["witnessed-boot"],
        "reviewed_by": "operator",
        "reviewed_at": "2026-09-23T00:00:00Z",
        "approved_intervals": [
            {"from": "2026-09-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"}
        ],
    }
    manifest = review.parse_manifest(payload)
    rows = [
        Sample(
            id=index,
            device_id=DEVICE,
            boot_id="some-other-boot",
            seq=index * 2,
            received_at=START + dt.timedelta(seconds=2 * index),
            voltage_v=8.0,
            current_a=0.4,
            power_w=3.2,
            energy_wh=0.1,
            sensor_status="ok",
        )
        for index in range(50)
    ]
    assert review.apply_manifest(rows, manifest) == []


def test_the_gate_uses_the_stride_the_operator_attested_to() -> None:
    """A capture published at stride 2 must be declared as stride 2.

    Declaring 1 means every published row looks like it skipped one, so the
    gate refuses rather than quietly accepting the wider advance.
    """
    rows = _approved_rows(1500)
    strided = [
        Sample(
            id=row.id,
            device_id=row.device_id,
            boot_id=row.boot_id,
            seq=row.seq * 2,
            received_at=row.received_at,
            voltage_v=row.voltage_v,
            current_a=row.current_a,
            power_w=row.power_w,
            energy_wh=row.energy_wh,
            sensor_status=row.sensor_status,
            label=row.label,
            calibration_fingerprint=row.calibration_fingerprint,
        )
        for row in rows
    ]
    assert gate.evaluate_gate(strided, _manifest()).passed

    strict = review.parse_manifest(
        {
            "manifest_version": review.MANIFEST_VERSION,
            "device_id": DEVICE,
            "calibration_fingerprint": REGIME,
            "provenance": review.PROVENANCE_HARDWARE,
            "firmware_version": "0.1.0",
            "shunt_resistance_ohm": 0.01,
            "expected_seq_stride": 1,
            "reviewed_by": "operator",
            "reviewed_at": "2026-09-23T00:00:00Z",
            "approved_intervals": [
                {"from": "2026-09-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"}
            ],
        }
    )
    result = gate.evaluate_gate(strided, strict)
    assert not result.passed
    assert any("contiguity rule" in reason for reason in result.reasons)


def test_a_mixed_device_dataset_is_refused() -> None:
    rows = list(_approved_rows(1500))
    rows.append(
        Sample(
            id=999_999,
            device_id="a-second-device",
            boot_id=rows[0].boot_id,
            seq=1,
            received_at=rows[-1].received_at + dt.timedelta(seconds=2),
            voltage_v=8.0,
            current_a=0.4,
            power_w=3.2,
            energy_wh=0.1,
            sensor_status="ok",
            label=0,
            calibration_fingerprint=REGIME,
        )
    )
    result = gate.evaluate_gate(rows, _manifest())
    assert not result.passed
    assert any("span 2 devices" in reason for reason in result.reasons)


def test_a_mixed_regime_dataset_is_refused() -> None:
    rows = list(_approved_rows(1500))
    rows.append(
        Sample(
            id=999_998,
            device_id=DEVICE,
            boot_id=rows[0].boot_id,
            seq=rows[-1].seq + 2,
            received_at=rows[-1].received_at + dt.timedelta(seconds=2),
            voltage_v=8.0,
            current_a=0.4,
            power_w=3.2,
            energy_wh=0.1,
            sensor_status="ok",
            label=0,
            calibration_fingerprint="a-different-shunt",
        )
    )
    result = gate.evaluate_gate(rows, _manifest())
    assert not result.passed
    assert any("more than one calibration regime" in reason for reason in result.reasons)


# -- every production requirement blocks on its own ------------------------


def test_a_manifest_naming_no_witnessed_boot_blocks() -> None:
    manifest = _manifest(boot_ids=())
    rows = review.apply_manifest(_approved_rows(1500), manifest)
    result = gate.evaluate_gate(rows, manifest)
    assert not result.passed
    assert result.verdict == gate.DATA_GATE_BLOCKED
    assert any("no witnessed boot_ids" in reason for reason in result.reasons)


def test_no_labelled_abnormal_event_blocks() -> None:
    manifest = _manifest(labelled_events=False)
    rows = review.apply_manifest(_approved_rows(1500), manifest)
    result = gate.evaluate_gate(rows, manifest)
    assert not result.passed
    assert any("labels no abnormal events" in reason for reason in result.reasons)


def test_a_labelled_event_with_no_rows_inside_it_blocks() -> None:
    rows = [row for row in _approved_rows(1500) if row.label != 1]
    result = gate.evaluate_gate(rows, _manifest())
    assert not result.passed
    assert any("no approved sample falls inside one" in r for r in result.reasons)


def test_abnormal_rows_never_count_toward_the_normal_minimum() -> None:
    rows = _approved_rows(990, abnormal=40)
    result = gate.evaluate_gate(rows, _manifest())
    assert result.approved_samples == 1030
    assert result.normal_samples == 990
    assert result.labelled_abnormal_samples == 40
    assert not result.passed
    assert any("990 operator-approved normal" in reason for reason in result.reasons)


@pytest.mark.parametrize("field", ["calibration_fingerprint", "firmware_version", "reviewed_by"])
def test_a_template_placeholder_left_in_the_manifest_blocks(field: str) -> None:
    manifest = _manifest()
    unfinished = review.Manifest(
        **{
            name: getattr(manifest, name)
            for name in review.Manifest.__dataclass_fields__
        }
        | {field: "REPLACE-ME"}
    )
    rows = _approved_rows(1500)
    result = gate.evaluate_gate(rows, unfinished)
    assert not result.passed
    assert any(f"placeholders in: {field}" in reason for reason in result.reasons)


def test_rows_from_another_device_than_the_manifest_names_block() -> None:
    from dataclasses import replace

    rows = [replace(row, device_id="powerguard-02") for row in _approved_rows(1500)]
    result = gate.evaluate_gate(rows, _manifest())
    assert not result.passed
    assert any("manifest attests to 'powerguard-01'" in r for r in result.reasons)


def test_rows_from_another_regime_than_the_manifest_names_block() -> None:
    from dataclasses import replace

    rows = [
        replace(row, calibration_fingerprint="ina226-r020-other")
        for row in _approved_rows(1500)
    ]
    result = gate.evaluate_gate(rows, _manifest())
    assert not result.passed
    assert any("but the manifest attests to" in reason for reason in result.reasons)


def test_a_blocked_verdict_carries_the_operator_checklist() -> None:
    text = gate.render(gate.evaluate_gate(_approved_rows(200), None))
    assert "real-data gate: DATA_GATE_BLOCKED" in text
    assert "to unblock:" in text
    assert "witnessed 60-90 minute capture" in text


def test_a_passing_verdict_has_no_checklist() -> None:
    text = gate.render(gate.evaluate_gate(_approved_rows(1500), _manifest()))
    assert "real-data gate: DATA_GATE_PASSED" in text
    assert "to unblock:" not in text


# -- the gate CLI: reads, judges, never trains -----------------------------


def test_the_gate_cli_blocks_a_database_with_no_manifest(tmp_path: Path, capsys) -> None:
    database = tmp_path / "powerguard.db"
    _write_db(database, _db_rows(1200, stride=2))
    report = tmp_path / "gate.json"
    code = gate.main(["--database", str(database), "--json", str(report)])
    assert code == gate.EXIT_BLOCKED
    out = capsys.readouterr().out
    assert "DATA_GATE_BLOCKED" in out
    assert "no operator review manifest" in out
    assert json.loads(report.read_text(encoding="utf-8"))["verdict"] == "DATA_GATE_BLOCKED"


def test_the_gate_cli_reports_a_malformed_manifest_as_blocked(tmp_path: Path, capsys) -> None:
    database = tmp_path / "powerguard.db"
    _write_db(database, _db_rows(40))
    manifest = tmp_path / "review.json"
    manifest.write_text("{ not json", encoding="utf-8")
    code = gate.main(["--database", str(database), "--manifest", str(manifest)])
    assert code == gate.EXIT_BLOCKED
    assert "DATA_GATE_BLOCKED" in capsys.readouterr().err


def test_the_gate_cli_blocks_the_unedited_template(tmp_path: Path, capsys) -> None:
    database = tmp_path / "powerguard.db"
    _write_db(database, _db_rows(1200, stride=2))
    samples = sqlite_source.read_telemetry(database)
    manifest = tmp_path / "review.json"
    manifest.write_text(
        json.dumps(review.template(samples[0].device_id, samples)), encoding="utf-8"
    )
    code = gate.main(["--database", str(database), "--manifest", str(manifest)])
    assert code == gate.EXIT_BLOCKED
    out = capsys.readouterr().out
    assert "not 'hardware'" in out
    assert "placeholders" in out
    assert "labels no abnormal events" in out


def test_the_audit_does_not_call_same_instant_rows_contiguous(tmp_path: Path) -> None:
    """A replayed or backfilled boot lands many rows on one timestamp."""
    database = tmp_path / "powerguard.db"
    rows = _db_rows(60)
    frozen = [(*row[:4], rows[0][4], *row[5:]) for row in rows]
    _write_db(database, frozen)
    report = sqlite_source.audit(database)
    assert report.devices[0].usable_samples == 0
