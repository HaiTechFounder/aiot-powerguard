"""Training and evaluation end to end, on the deterministic fixture."""

from __future__ import annotations

import json

from powerguard_ml import evaluate, train
from powerguard_ml.artifact import METADATA_FILE, MODEL_FILE, read_metadata
from powerguard_ml.dataset import to_jsonl
from powerguard_ml.synthetic import DEFAULT_DEVICE, training_set


def test_training_on_the_fixture_writes_a_loadable_artifact(tmp_path, capsys) -> None:
    code = train.main(
        ["--synthetic", "600", "--artifacts", str(tmp_path), "--model-version", "v1"]
    )
    assert code == 0

    directory = tmp_path / DEFAULT_DEVICE / "v1"
    assert (directory / MODEL_FILE).exists()
    assert (directory / METADATA_FILE).exists()

    out = capsys.readouterr().out
    assert "quality_not_established" in out
    # The synthetic caveat is printed, not buried in a doc.
    assert "not evidence of real-world accuracy" in out


def test_training_reads_a_real_export_from_disk(tmp_path) -> None:
    path = tmp_path / "export.jsonl"
    to_jsonl(training_set(400), path)
    code = train.main(["--dataset", str(path), "--artifacts", str(tmp_path / "artifacts")])
    assert code == 0
    metadata = read_metadata(tmp_path / "artifacts" / DEFAULT_DEVICE / "v1")
    assert metadata["data_provenance"] == "real_export"
    # An unapproved export still establishes nothing.
    assert metadata["data_quality"] == "quality_not_established"


def test_training_refuses_a_dataset_with_no_usable_window(tmp_path, capsys) -> None:
    path = tmp_path / "short.jsonl"
    to_jsonl(training_set(20), path)
    assert train.main(["--dataset", str(path), "--artifacts", str(tmp_path / "a")]) == 2
    assert "no usable segment" in capsys.readouterr().err


def test_metadata_records_the_training_interval_and_counts(tmp_path) -> None:
    train.main(["--synthetic", "600", "--artifacts", str(tmp_path)])
    metadata = read_metadata(tmp_path / DEFAULT_DEVICE / "v1")
    assert metadata["training_windows"] > 0
    assert metadata["calibration_windows"] > 0
    assert all(metadata["training_interval"])
    assert metadata["random_state"] == 42


def test_evaluation_reports_counts_bounds_and_the_caveat(tmp_path, capsys) -> None:
    train.main(["--synthetic", "900", "--artifacts", str(tmp_path)])
    directory = tmp_path / DEFAULT_DEVICE / "v1"
    report_path = tmp_path / "report.json"

    code = evaluate.main(
        ["--artifact", str(directory), "--synthetic", "600", "--json", str(report_path)]
    )
    assert code == 0

    out = capsys.readouterr().out
    assert "quality_not_established" in out
    assert "DEFERRED" in out
    assert "1000 operator-approved real samples" in out.replace(",", "")

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["windows"] > 0
    assert payload["segments"] >= 1
    assert all(payload["time_bounds"])
    assert payload["data_quality"] == "quality_not_established"
    assert payload["data_provenance"] == "synthetic"
    assert set(payload["scenario_recall"]) == {"spike", "level_shift", "drift"}


def test_evaluation_refuses_an_artifact_it_cannot_trust(tmp_path, capsys) -> None:
    train.main(["--synthetic", "600", "--artifacts", str(tmp_path)])
    directory = tmp_path / DEFAULT_DEVICE / "v1"
    (directory / MODEL_FILE).write_bytes(b"tampered")

    assert evaluate.main(["--artifact", str(directory), "--synthetic", "300"]) == 2
    assert "artifact rejected" in capsys.readouterr().err


def test_a_rerun_with_the_same_seed_reports_the_same_decisions(tmp_path) -> None:
    # "Deterministic rerun produces equivalent decisions" is only a claim if
    # something checks it.
    first, second = tmp_path / "a", tmp_path / "b"
    reports = []
    for root in (first, second):
        train.main(["--synthetic", "600", "--artifacts", str(root)])
        path = root / "report.json"
        evaluate.main(
            [
                "--artifact",
                str(root / DEFAULT_DEVICE / "v1"),
                "--synthetic",
                "400",
                "--json",
                str(path),
            ]
        )
        reports.append(json.loads(path.read_text(encoding="utf-8")))

    for key in ("windows", "flagged_windows", "normal_flag_rate", "score_quantiles"):
        assert reports[0][key] == reports[1][key]


# -- promotion, stride and labels -------------------------------------------


def test_synthetic_data_can_never_be_promoted(tmp_path, capsys) -> None:
    code = train.main(["--synthetic", "600", "--artifacts", str(tmp_path), "--promote"])
    assert code == 3
    assert "DATA_GATE_BLOCKED" in capsys.readouterr().err
    # Refused before anything was written.
    assert not any(tmp_path.iterdir())


def test_an_unapproved_export_cannot_be_promoted(tmp_path, capsys) -> None:
    path = tmp_path / "export.jsonl"
    to_jsonl(training_set(400), path)
    code = train.main(
        ["--dataset", str(path), "--artifacts", str(tmp_path / "a"), "--promote"]
    )
    assert code == 3
    assert "DATA_GATE_BLOCKED" in capsys.readouterr().err
    assert not (tmp_path / "a").exists()


def test_the_artifact_records_the_stride_training_used(tmp_path) -> None:
    train.main(["--synthetic", "600", "--artifacts", str(tmp_path)])
    metadata = read_metadata(tmp_path / DEFAULT_DEVICE / "v1")
    assert metadata["expected_seq_stride"] == 2


def test_the_manifest_stride_reaches_the_artifact(tmp_path) -> None:
    import datetime as dt

    from powerguard_ml import review

    rows = training_set(400)
    path = tmp_path / "export.jsonl"
    to_jsonl(rows, path)
    payload = review.template(DEFAULT_DEVICE, rows)
    payload["expected_seq_stride"] = 1
    payload["approved_intervals"][0]["to"] = (
        (rows[-1].received_at + dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    )
    manifest = tmp_path / "review.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    code = train.main(
        [
            "--dataset",
            str(path),
            "--manifest",
            str(manifest),
            "--artifacts",
            str(tmp_path / "a"),
        ]
    )
    assert code == 0
    metadata = read_metadata(tmp_path / "a" / DEFAULT_DEVICE / "v1")
    assert metadata["expected_seq_stride"] == 1
    # The template attests to nothing, so nothing is established.
    assert metadata["data_quality"] == "quality_not_established"
    assert metadata["data_provenance"] == "unknown"


def test_rows_labelled_abnormal_never_train_the_baseline(tmp_path, capsys) -> None:
    from dataclasses import replace

    path = tmp_path / "faults.jsonl"
    to_jsonl([replace(row, label=1) for row in training_set(400)], path)
    code = train.main(["--dataset", str(path), "--artifacts", str(tmp_path / "a")])
    # Every row is a labelled fault, so nothing is left to fit a baseline on.
    assert code == 2
    assert "no usable segment" in capsys.readouterr().err
