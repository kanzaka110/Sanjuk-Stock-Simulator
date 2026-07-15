"""CLI contracts for the standalone KR market observation collector."""

from __future__ import annotations

import json


def test_cli_all_mode_passes_exact_symbols_and_prints_json(monkeypatch, capsys, tmp_path):
    import tools.collect_kr_market_observations as cli

    store = object()
    observed = {}
    monkeypatch.setattr(cli, "_open_store", lambda path: store)

    def run(symbols, **kwargs):
        observed["symbols"] = symbols
        observed.update(kwargs)
        return {
            "symbols": list(symbols),
            "orderbook": {"status": "success"},
            "investor": {"skipped": "throttled"},
        }

    monkeypatch.setattr(cli, "run_candidate_observation_cycle", run)

    rc = cli.main(
        [
            "--mode",
            "all",
            "--symbols",
            "005930.KS,035720.KQ",
            "--db",
            str(tmp_path / "v2.db"),
            "--run-id",
            "cli-run-1",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert observed == {
        "symbols": ("005930.KS", "035720.KQ"),
        "store": store,
        "run_id": "cli-run-1",
    }
    assert payload["ok"] is True
    assert payload["result"]["orderbook"]["status"] == "success"


def test_cli_provider_failure_returns_exit_two(monkeypatch, capsys, tmp_path):
    import tools.collect_kr_market_observations as cli

    monkeypatch.setattr(cli, "_open_store", lambda _path: object())
    monkeypatch.setattr(
        cli,
        "collect_orderbook_observations",
        lambda *_args, **_kwargs: {
            "source": "kis",
            "dataset": "domestic_orderbook",
            "status": "failed",
            "error_type": "network",
        },
    )

    rc = cli.main(
        [
            "--mode",
            "orderbook",
            "--symbols",
            "005930.KS",
            "--db",
            str(tmp_path / "v2.db"),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["ok"] is False
    assert payload["result"]["status"] == "failed"


def test_cli_returns_two_when_fallback_hides_a_degraded_primary(monkeypatch, capsys):
    import tools.collect_kr_market_observations as cli

    monkeypatch.setattr(cli, "_open_store", lambda _path: object())
    monkeypatch.setattr(
        cli,
        "run_candidate_observation_cycle",
        lambda *_args, **_kwargs: {
            "investor": {
                "kis": {"status": "skipped", "error_type": "not_configured"},
                "naver": {"status": "success", "error_type": "none"},
            }
        },
    )

    return_code = cli.main(["--mode", "all", "--symbols", "005930.KS"])

    assert return_code == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_cli_exception_prints_class_only(monkeypatch, capsys, tmp_path):
    import tools.collect_kr_market_observations as cli

    monkeypatch.setattr(cli, "_open_store", lambda _path: object())

    def fail(*_args, **_kwargs):
        raise RuntimeError("credential-like-text-must-not-be-printed")

    monkeypatch.setattr(cli, "collect_investor_observations", fail)

    rc = cli.main(
        [
            "--mode",
            "investor",
            "--symbols",
            "005930.KS",
            "--db",
            str(tmp_path / "v2.db"),
        ]
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert rc == 1
    assert payload == {"ok": False, "error": "RuntimeError"}
    assert "credential-like-text-must-not-be-printed" not in output
