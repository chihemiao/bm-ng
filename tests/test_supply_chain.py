import json
import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).parents[1]
RUNTIME_DIRS = frozenset({"data", "execution", "strategy", "risk", "reconciliation", "ops"})
EXACT_DEV_DEPENDENCIES = {
    "import-linter==2.13",
    "pip-audit==2.10.1",
    "pre-commit==4.6.1",
    "pytest==9.1.1",
    "ruff==0.16.2",
    "vulture==2.16",
}
EXPECTED_PRE_COMMIT_CONFIG = """\
repos:
  - repo: local
    hooks:
      - id: uv-lock-check
        name: uv lock --check
        entry: uv lock --check
        language: unsupported
        pass_filenames: false
        always_run: true
      - id: locked-pytest
        name: locked pytest
        entry: uv run --locked python -B -m pytest -q
        language: unsupported
        pass_filenames: false
        always_run: true
      - id: locked-ruff
        name: locked ruff
        entry: uv run --locked ruff check data tests research
        language: unsupported
        pass_filenames: false
        always_run: true
"""
UV_RELEASE_SHA256 = {
    "0.9.21": "0a1ab27383c28ef1c041f85cbbc609d8e3752dfb4b238d2ad97b208a52232baf",
}


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)


def _assert_success(command: list[str]) -> subprocess.CompletedProcess[str]:
    result = _run(command)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _config() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def _runtime_packages() -> list[str]:
    return sorted(
        directory
        for directory in RUNTIME_DIRS
        if (ROOT / directory).is_dir() and any((ROOT / directory).rglob("*.py"))
    )


def sbom_components_are_complete(components: list[dict]) -> bool:
    return all({"name", "version"} <= component.keys() for component in components)


def test_supply_chain_dependencies_and_tools_are_exactly_pinned() -> None:
    config = _config()

    assert config["project"]["dependencies"] == ["websockets==17.0.1"]
    assert set(config["dependency-groups"]["dev"]) == EXACT_DEV_DEPENDENCIES
    assert config["build-system"] == {
        "requires": ["uv_build==0.9.21"],
        "build-backend": "uv_build",
    }
    assert config["tool"]["uv"]["required-version"] == "==0.9.21"
    assert config["tool"]["vulture"] == {"min_confidence": 80, "paths": ["data"]}


def test_build_and_import_roots_match_the_current_runtime_packages() -> None:
    tools = _config()["tool"]
    build = tools["uv"]["build-backend"]
    imports = tools["importlinter"]
    runtime_packages = _runtime_packages()

    assert build["module-name"] == imports["root_packages"] == runtime_packages == ["data"]
    assert build["module-root"] == ""
    assert build["namespace"] is True
    assert imports["include_external_packages"] is True
    assert imports["contracts"] == [
        {
            "name": "Runtime cannot import tests or research",
            "type": "forbidden",
            "source_modules": ["data"],
            "forbidden_modules": ["tests", "research"],
        }
    ]


def test_lockfile_exists_and_is_current() -> None:
    assert (ROOT / "uv.lock").is_file()
    _assert_success(["uv", "lock", "--check"])


def test_pre_commit_uses_only_the_three_locked_local_hooks() -> None:
    config = ROOT / ".pre-commit-config.yaml"
    assert config.is_file()
    assert config.read_text() == EXPECTED_PRE_COMMIT_CONFIG
    _assert_success([sys.executable, "-m", "pre_commit", "validate-config", str(config)])


def test_ci_workflow_enforces_only_the_locked_gate_semantics() -> None:
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
    assert [path.name for path in workflows] == ["gates.yml"]
    workflow = workflows[0].read_text()

    assert re.search(
        r"(?m)^on:\n  pull_request:\n  push:\n    branches: \[main\]$", workflow
    )
    assert workflow.count("permissions:") == 1
    assert "permissions:\n  contents: read\n" in workflow
    jobs = workflow.partition("\njobs:\n")[2]
    assert re.findall(r"(?m)^  ([a-z][a-z0-9_-]*):$", jobs) == ["gates"]
    assert "\n    name: gates\n" in workflow
    assert "\n    runs-on: ubuntu-24.04\n" in workflow
    timeout = re.findall(r"(?m)^    timeout-minutes: (\d+)$", workflow)
    assert len(timeout) == 1 and int(timeout[0]) <= 20

    forbidden_keys = (
        "uses:", "continue-on-error:", "if:", "paths:", "paths-ignore:", "concurrency:"
    )
    for forbidden in forbidden_keys:
        assert forbidden not in workflow
    assert "${{ github.event.pull_request.head.sha || github.sha }}" in workflow
    assert 'git fetch --depth=1 origin "$TARGET_SHA"' in workflow
    assert 'test "$(git rev-parse HEAD)" = "$TARGET_SHA"' in workflow

    uv_version = _config()["tool"]["uv"]["required-version"].removeprefix("==")
    assert f'UV_VERSION: "{uv_version}"' in workflow
    assert f'UV_SHA256: "{UV_RELEASE_SHA256[uv_version]}"' in workflow
    assert "uv python install 3.14.2" in workflow
    assert "uv sync --locked --python 3.14.2" in workflow
    assert "uv run --locked pre-commit run --all-files" in workflow


def test_dead_code_and_import_contract_tools_pass() -> None:
    _assert_success([sys.executable, "-m", "vulture"])
    _assert_success(["import-linter", "lint"])


def test_exported_lock_passes_strict_vulnerability_audit() -> None:
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "pylock.audit.toml"
        _assert_success(
            [
                "uv",
                "export",
                "--locked",
                "--all-groups",
                "--no-emit-project",
                "--format",
                "pylock.toml",
                "--output-file",
                str(target),
            ]
        )
        _assert_success(
            [
                sys.executable,
                "-m",
                "pip_audit",
                "--locked",
                directory,
                "--strict",
                "--progress-spinner",
                "off",
            ]
        )


def test_sbom_component_validation_rejects_a_missing_version() -> None:
    assert not sbom_components_are_complete([{"name": "unversioned"}])


def test_locked_export_is_a_cyclonedx_15_sbom() -> None:
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "sbom.json"
        _assert_success(
            [
                "uv",
                "export",
                "--locked",
                "--all-groups",
                "--format",
                "cyclonedx1.5",
                "--output-file",
                str(target),
            ]
        )
        sbom = json.loads(target.read_text())

    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.5"
    assert sbom["metadata"]["component"]["name"] == "hl-funding-carry"
    assert sbom_components_are_complete(sbom["components"])


def test_wheel_contains_only_current_runtime_packages_and_metadata() -> None:
    with tempfile.TemporaryDirectory() as directory:
        _assert_success(["uv", "build", "--wheel", "--out-dir", directory])
        wheels = list(Path(directory).glob("*.whl"))
        assert len(wheels) == 1
        with zipfile.ZipFile(wheels[0]) as wheel:
            names = set(wheel.namelist())

    packages = set(_runtime_packages())
    metadata = {name.split("/", 1)[0] for name in names if ".dist-info/" in name}
    assert {name.split("/", 1)[0] for name in names} == packages | metadata
    assert {name for name in names if name.endswith(".py")} == {
        str(path.relative_to(ROOT))
        for package in packages
        for path in (ROOT / package).rglob("*.py")
    }
