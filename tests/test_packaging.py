"""Packaging checks for PyPI-ready distributions."""

from __future__ import annotations

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 has no stdlib tomllib
    import tomli as tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent


def _pyproject() -> dict:
    return tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_pypi_metadata_has_publish_required_fields():
    project = _pyproject()["project"]

    assert project["name"] == "symbio"
    assert project["readme"] == "README.md"
    assert project["requires-python"] == ">=3.10"
    assert project["authors"]
    assert project["keywords"]
    assert "Programming Language :: Python :: 3" in project["classifiers"]
    assert "Homepage" in project["urls"]
    assert "Repository" in project["urls"]


def test_plain_pip_install_includes_web_server_dependencies():
    deps = set(_pyproject()["project"]["dependencies"])

    assert "fastapi>=0.115.0" in deps
    assert "uvicorn[standard]>=0.30.0" in deps


def test_wheel_force_includes_runtime_ui_and_eval_assets():
    wheel = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]
    force_include = wheel["force-include"]

    assert force_include["web"] == "symbio/interfaces/web/static"
    assert force_include["data/eval_suites"] == "symbio/data/eval_suites"


def test_api_resolves_source_tree_static_assets():
    from symbio.interfaces.api import _get_default_eval_suite_dir, _get_web_dir

    web_dir = _get_web_dir()
    eval_dir = _get_default_eval_suite_dir()

    assert web_dir is not None
    assert (web_dir / "index.html").exists()
    assert eval_dir is not None
    assert (eval_dir / "smoke.json").exists()
