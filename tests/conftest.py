"""Pytest configuration for Sanjuk-Stock-Simulator."""

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


@pytest.fixture
def project_root():
    return PROJECT_ROOT


@pytest.fixture
def python_source_dirs(project_root):
    return [project_root / d for d in ["core", "terminal", "db", "config"]]
