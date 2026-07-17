"""관세청 10일 수출 수집 CLI 계약."""

from datetime import datetime, timedelta, timezone
import json

import pytest


def test_cli_passes_decoding_key_without_printing_it(monkeypatch, capsys, tmp_path):
    import tools.collect_customs_export_observations as cli

    observed = {}
    store = object()
    monkeypatch.setattr(cli, "_open_store", lambda _path: store)
    monkeypatch.setattr(cli, "_load_service_key", lambda: "decoding-secret-value")

    def collect(start, end, **kwargs):
        observed.update({"start": start, "end": end, **kwargs})
        return {
            "source": "korea_customs",
            "dataset": "ten_day_product_exports",
            "status": "success",
            "rows_seen": 1,
            "rows_inserted": 1,
            "rows_duplicate": 0,
            "rows_skipped": 0,
            "rows_invalid": 0,
            "error_type": "",
        }

    monkeypatch.setattr(cli, "collect_customs_export_observations", collect)
    rc = cli.main(
        [
            "--start",
            "202506",
            "--end",
            "202607",
            "--db",
            str(tmp_path / "v2.db"),
            "--run-id",
            "customs-cli-1",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert observed == {
        "start": "202506",
        "end": "202607",
        "store": store,
        "run_id": "customs-cli-1",
        "service_key": "decoding-secret-value",
        "workday_fetcher": None,
        "collection_mode": "research_backfill",
    }
    assert payload["ok"] is True
    assert "decoding-secret-value" not in captured.out
    assert "decoding-secret-value" not in captured.err


def test_cli_implicit_default_range_is_scheduled_live(monkeypatch, capsys, tmp_path):
    import tools.collect_customs_export_observations as cli

    observed = {}
    monkeypatch.setattr(cli, "_default_query_range", lambda _now: ("202506", "202607"))
    monkeypatch.setattr(cli, "_open_store", lambda _path: object())
    monkeypatch.setattr(cli, "_load_service_key", lambda: "scheduled-secret-value")

    def collect(start, end, **kwargs):
        observed.update({"start": start, "end": end, **kwargs})
        return {
            "source": "korea_customs",
            "dataset": "ten_day_product_exports",
            "status": "success",
            "rows_seen": 0,
            "rows_inserted": 0,
            "rows_duplicate": 0,
            "rows_skipped": 0,
            "rows_invalid": 0,
            "error_type": "",
        }

    monkeypatch.setattr(cli, "collect_customs_export_observations", collect)
    rc = cli.main(["--db", str(tmp_path / "v2.db")])

    captured = capsys.readouterr()
    assert rc == 0
    assert observed["start"] == "202506"
    assert observed["end"] == "202607"
    assert observed["collection_mode"] == "scheduled_live"
    assert observed["workday_fetcher"] is cli.fetch_kcs_workday_observations
    assert "scheduled-secret-value" not in captured.out
    assert "scheduled-secret-value" not in captured.err


def test_default_query_range_has_prior_year_and_previous_cutoff_month():
    import tools.collect_customs_export_observations as cli

    kst = timezone(timedelta(hours=9))
    assert cli._default_query_range(datetime(2026, 7, 16, tzinfo=kst)) == (
        "202506",
        "202607",
    )


def test_default_query_range_spans_14_months_across_january_year_boundary():
    import tools.collect_customs_export_observations as cli

    kst = timezone(timedelta(hours=9))
    assert cli._default_query_range(datetime(2026, 1, 15, tzinfo=kst)) == (
        "202412",
        "202601",
    )


def test_default_query_range_converts_utc_before_selecting_month():
    import tools.collect_customs_export_observations as cli

    assert cli._default_query_range(
        datetime(2025, 12, 31, 15, 30, tzinfo=timezone.utc)
    ) == ("202412", "202601")


@pytest.mark.parametrize(
    "argv",
    (["--start", "202601"], ["--end", "202601"]),
)
def test_cli_rejects_start_or_end_when_only_one_is_given(
    argv, monkeypatch, capsys
):
    import tools.collect_customs_export_observations as cli

    def must_not_open(_path):
        raise AssertionError("store must not open for an invalid range")

    monkeypatch.setattr(cli, "_open_store", must_not_open)

    rc = cli.main(argv)

    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out) == {"error": "ValueError", "ok": False}
    assert captured.err == ""


def test_clean_empty_is_successful_exit_zero(monkeypatch, capsys, tmp_path):
    import tools.collect_customs_export_observations as cli

    monkeypatch.setattr(cli, "_open_store", lambda _path: object())
    monkeypatch.setattr(cli, "_load_service_key", lambda: "empty-secret-value")
    monkeypatch.setattr(
        cli,
        "collect_customs_export_observations",
        lambda *_args, **_kwargs: {
            "source": "korea_customs",
            "dataset": "ten_day_product_exports",
            "status": "empty",
            "rows_seen": 0,
            "rows_inserted": 0,
            "rows_duplicate": 0,
            "rows_skipped": 0,
            "rows_invalid": 0,
            "error_type": "",
        },
    )

    rc = cli.main(["--db", str(tmp_path / "v2.db")])

    captured = capsys.readouterr()
    assert rc == 0
    assert json.loads(captured.out)["ok"] is True
    assert "empty-secret-value" not in captured.out
    assert "empty-secret-value" not in captured.err


def test_missing_key_returns_exit_two_without_key_text(monkeypatch, capsys, tmp_path):
    import tools.collect_customs_export_observations as cli

    monkeypatch.setattr(cli, "_open_store", lambda _path: object())
    monkeypatch.setattr(cli, "_load_service_key", lambda: "")
    monkeypatch.setattr(
        cli,
        "collect_customs_export_observations",
        lambda *_args, **_kwargs: {
            "source": "korea_customs",
            "dataset": "ten_day_product_exports",
            "status": "skipped",
            "rows_seen": 0,
            "rows_inserted": 0,
            "rows_duplicate": 0,
            "rows_skipped": 0,
            "rows_invalid": 0,
            "error_type": "not_configured",
        },
    )

    rc = cli.main(["--db", str(tmp_path / "v2.db")])

    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["ok"] is False
    assert payload["result"]["error_type"] == "not_configured"


def test_cli_exception_prints_class_only(monkeypatch, capsys, tmp_path):
    import tools.collect_customs_export_observations as cli

    monkeypatch.setattr(cli, "_open_store", lambda _path: object())
    monkeypatch.setattr(cli, "_load_service_key", lambda: "secret")

    def fail(*_args, **_kwargs):
        raise RuntimeError("serviceKey=must-not-be-printed")

    monkeypatch.setattr(cli, "collect_customs_export_observations", fail)
    rc = cli.main(["--db", str(tmp_path / "v2.db")])

    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out) == {"error": "RuntimeError", "ok": False}
    assert "must-not-be-printed" not in captured.out
    assert "must-not-be-printed" not in captured.err
