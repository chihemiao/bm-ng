import json
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
