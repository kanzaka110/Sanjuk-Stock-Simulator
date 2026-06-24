"""Pytest configuration for Sanjuk-Stock-Simulator."""

import importlib
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent

# Toss live-pilot 관련 sqlite DB는 테스트에서 절대 production 경로에 쓰면 안 된다.
# (events/ledger/verification은 각각 별도 _db_path를 가지므로 모두 격리한다.)
_TOSS_DB_MODULES = [
    ("core.toss_live_pilot_events", "toss_live_pilot_events.db"),
    ("core.toss_live_pilot_ledger", "toss_live_pilot.db"),
    ("core.toss_live_pilot_verification", "toss_live_pilot_verifications.db"),
]


@pytest.fixture(autouse=True)
def _isolate_toss_live_pilot_dbs(tmp_path):
    """모든 테스트가 production toss live-pilot DB에 쓰지 못하도록 임시 경로 강제.

    각 모듈의 _db_path()를 per-test 임시 파일로 patch하고 _schema_created를
    리셋한다. 테스트가 자체적으로 _db_path를 다시 patch하면 그쪽이 우선한다
    (이 fixture는 누락 시의 안전망). production db/data 오염을 구조적으로 차단.
    """
    patchers = []
    for mod_name, fname in _TOSS_DB_MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        if not hasattr(mod, "_db_path"):
            continue
        p = patch.object(mod, "_db_path", return_value=tmp_path / fname)
        p.start()
        patchers.append((p, mod))
        if hasattr(mod, "_schema_created"):
            mod._schema_created = False
    try:
        yield
    finally:
        for p, mod in patchers:
            p.stop()
            if hasattr(mod, "_schema_created"):
                mod._schema_created = False


@pytest.fixture
def project_root():
    return PROJECT_ROOT


@pytest.fixture
def python_source_dirs(project_root):
    return [project_root / d for d in ["core", "terminal", "db", "config"]]
