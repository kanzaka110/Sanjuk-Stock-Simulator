"""Harness validation tests for Sanjuk-Stock-Simulator."""

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent

SECRET_PATTERNS = [
    re.compile(r'sk-ant-[a-zA-Z0-9_-]{20,}'),
    re.compile(r'sk-proj-[a-zA-Z0-9_-]{20,}'),
    re.compile(r'API_KEY\s*=\s*["\'][a-zA-Z0-9_-]{10,}["\']'),
    re.compile(r'password\s*=\s*["\'][^"\']{8,}["\']', re.IGNORECASE),
]


class TestProjectStructure:
    def test_claude_md_exists(self):
        assert (PROJECT_ROOT / "CLAUDE.md").exists()

    def test_main_entry_exists(self):
        assert (PROJECT_ROOT / "main.py").exists()

    def test_required_modules(self):
        for d in ["core", "terminal", "db", "config"]:
            assert (PROJECT_ROOT / d).exists(), f"{d}/ missing"

    def test_requirements_exists(self):
        assert (PROJECT_ROOT / "requirements.txt").exists()

    def test_config_directory_has_files(self):
        config = PROJECT_ROOT / "config"
        py_files = list(config.glob("*.py"))
        assert len(py_files) >= 1, "config/ has no Python files"


class TestNoHardcodedSecrets:
    def test_no_secrets_in_source(self):
        violations = []
        for py_file in PROJECT_ROOT.rglob("*.py"):
            if "test_" in py_file.name or "__pycache__" in str(py_file):
                continue
            content = py_file.read_text(encoding="utf-8", errors="ignore")
            for pattern in SECRET_PATTERNS:
                if pattern.findall(content):
                    violations.append(str(py_file.relative_to(PROJECT_ROOT)))
        assert not violations, f"Secrets found: {violations}"


class TestRequirements:
    def test_requirements_parseable(self):
        content = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
        for line in content.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            assert re.match(r'^[a-zA-Z0-9_-]', line), f"Invalid: {line}"


class TestPythonSyntax:
    def test_all_py_files_valid(self):
        errors = []
        for py_file in PROJECT_ROOT.rglob("*.py"):
            if "__pycache__" in str(py_file):
                continue
            try:
                source = py_file.read_text(encoding="utf-8", errors="ignore")
                ast.parse(source)
            except SyntaxError as e:
                errors.append(f"{py_file.relative_to(PROJECT_ROOT)}: {e}")
        assert not errors, f"Syntax errors: {errors}"


class TestDeployment:
    def test_deploy_directory_exists(self):
        assert (PROJECT_ROOT / "deploy").exists()

    def test_db_directory_structure(self):
        assert (PROJECT_ROOT / "db").exists()
