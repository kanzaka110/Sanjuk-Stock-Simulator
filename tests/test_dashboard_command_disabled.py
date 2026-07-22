import sys

import pytest


def test_dashboard_command_is_not_registered():
    import main

    assert "dashboard" not in main.COMMANDS
    assert "python main.py dashboard" not in main.USAGE


def test_dashboard_cli_fails_without_calling_dashboard(monkeypatch, capsys):
    import main

    called = False

    def forbidden_dashboard():
        nonlocal called
        called = True
        raise AssertionError("dashboard handler must not run")

    monkeypatch.setattr(main, "cmd_dashboard", forbidden_dashboard)
    if "dashboard" in main.COMMANDS:
        monkeypatch.setitem(main.COMMANDS, "dashboard", forbidden_dashboard)
    monkeypatch.setattr(sys, "argv", ["main.py", "dashboard"])

    with pytest.raises(SystemExit) as exc_info:
        main.main()

    assert exc_info.value.code == 1
    assert called is False
    assert "알 수 없는 명령" in capsys.readouterr().out
