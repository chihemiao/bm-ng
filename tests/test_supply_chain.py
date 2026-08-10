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
    assert config["tool"]["vulture"] == {
        "min_confidence": 80,
        "paths": ["data", "execution"],
    }


def test_build_and_import_roots_match_the_current_runtime_packages() -> None:
    tools = _config()["tool"]
    build = tools["uv"]["build-backend"]
    imports = tools["importlinter"]
    runtime_packages = _runtime_packages()

    expected_packages = ["data", "execution"]
    assert build["module-name"] == imports["root_packages"] == runtime_packages
    assert tools["ruff"]["lint"]["isort"]["known-first-party"] == runtime_packages
    assert runtime_packages == expected_packages
    assert build["module-root"] == ""
    assert build["namespace"] is True
    assert imports["include_external_packages"] is True
    assert imports["contracts"] == [
        {
            "name": "Runtime cannot import tests or research",
            "type": "forbidden",
            "source_modules": ["data", "execution"],
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
    assert workflow.count("Enforce the pull request net line budget") == 1
    assert "EVENT_NAME: ${{ github.event_name }}" in workflow
    assert "BASE_REPOSITORY: ${{ github.event.pull_request.base.repo.full_name }}" in workflow
    assert "BASE_SHA: ${{ github.event.pull_request.base.sha }}" in workflow
    assert 'if [ "$EVENT_NAME" != "pull_request" ]; then' in workflow
    assert 'git fetch --unshallow origin "$HEAD_SHA"' in workflow
    assert '"https://github.com/${BASE_REPOSITORY}.git" "$BASE_SHA"' in workflow
    assert 'git diff --numstat "$BASE_SHA...$HEAD_SHA"' in workflow
    assert '$1 == "-" || $2 == "-" { bad = 1; next }' in workflow
    assert 'test "$NET_LINES" -le 200' in workflow

    goal = (ROOT / "GOAL.md").read_text()
    assert "**豁免规则**" not in goal

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
# pull request net line canary 001
# pull request net line canary 002
# pull request net line canary 003
# pull request net line canary 004
# pull request net line canary 005
# pull request net line canary 006
# pull request net line canary 007
# pull request net line canary 008
# pull request net line canary 009
# pull request net line canary 010
# pull request net line canary 011
# pull request net line canary 012
# pull request net line canary 013
# pull request net line canary 014
# pull request net line canary 015
# pull request net line canary 016
# pull request net line canary 017
# pull request net line canary 018
# pull request net line canary 019
# pull request net line canary 020
# pull request net line canary 021
# pull request net line canary 022
# pull request net line canary 023
# pull request net line canary 024
# pull request net line canary 025
# pull request net line canary 026
# pull request net line canary 027
# pull request net line canary 028
# pull request net line canary 029
# pull request net line canary 030
# pull request net line canary 031
# pull request net line canary 032
# pull request net line canary 033
# pull request net line canary 034
# pull request net line canary 035
# pull request net line canary 036
# pull request net line canary 037
# pull request net line canary 038
# pull request net line canary 039
# pull request net line canary 040
# pull request net line canary 041
# pull request net line canary 042
# pull request net line canary 043
# pull request net line canary 044
# pull request net line canary 045
# pull request net line canary 046
# pull request net line canary 047
# pull request net line canary 048
# pull request net line canary 049
# pull request net line canary 050
# pull request net line canary 051
# pull request net line canary 052
# pull request net line canary 053
# pull request net line canary 054
# pull request net line canary 055
# pull request net line canary 056
# pull request net line canary 057
# pull request net line canary 058
# pull request net line canary 059
# pull request net line canary 060
# pull request net line canary 061
# pull request net line canary 062
# pull request net line canary 063
# pull request net line canary 064
# pull request net line canary 065
# pull request net line canary 066
# pull request net line canary 067
# pull request net line canary 068
# pull request net line canary 069
# pull request net line canary 070
# pull request net line canary 071
# pull request net line canary 072
# pull request net line canary 073
# pull request net line canary 074
# pull request net line canary 075
# pull request net line canary 076
# pull request net line canary 077
# pull request net line canary 078
# pull request net line canary 079
# pull request net line canary 080
# pull request net line canary 081
# pull request net line canary 082
# pull request net line canary 083
# pull request net line canary 084
# pull request net line canary 085
# pull request net line canary 086
# pull request net line canary 087
# pull request net line canary 088
# pull request net line canary 089
# pull request net line canary 090
# pull request net line canary 091
# pull request net line canary 092
# pull request net line canary 093
# pull request net line canary 094
# pull request net line canary 095
# pull request net line canary 096
# pull request net line canary 097
# pull request net line canary 098
# pull request net line canary 099
# pull request net line canary 100
# pull request net line canary 101
# pull request net line canary 102
# pull request net line canary 103
# pull request net line canary 104
# pull request net line canary 105
# pull request net line canary 106
# pull request net line canary 107
# pull request net line canary 108
# pull request net line canary 109
# pull request net line canary 110
# pull request net line canary 111
# pull request net line canary 112
# pull request net line canary 113
# pull request net line canary 114
# pull request net line canary 115
# pull request net line canary 116
# pull request net line canary 117
# pull request net line canary 118
# pull request net line canary 119
# pull request net line canary 120
# pull request net line canary 121
# pull request net line canary 122
# pull request net line canary 123
# pull request net line canary 124
# pull request net line canary 125
# pull request net line canary 126
# pull request net line canary 127
# pull request net line canary 128
# pull request net line canary 129
# pull request net line canary 130
# pull request net line canary 131
# pull request net line canary 132
# pull request net line canary 133
# pull request net line canary 134
# pull request net line canary 135
# pull request net line canary 136
# pull request net line canary 137
# pull request net line canary 138
# pull request net line canary 139
# pull request net line canary 140
# pull request net line canary 141
# pull request net line canary 142
# pull request net line canary 143
# pull request net line canary 144
# pull request net line canary 145
# pull request net line canary 146
# pull request net line canary 147
# pull request net line canary 148
# pull request net line canary 149
# pull request net line canary 150
# pull request net line canary 151
# pull request net line canary 152
# pull request net line canary 153
# pull request net line canary 154
# pull request net line canary 155
# pull request net line canary 156
# pull request net line canary 157
# pull request net line canary 158
# pull request net line canary 159
# pull request net line canary 160
# pull request net line canary 161
# pull request net line canary 162
# pull request net line canary 163
# pull request net line canary 164
# pull request net line canary 165
# pull request net line canary 166
# pull request net line canary 167
# pull request net line canary 168
# pull request net line canary 169
# pull request net line canary 170
# pull request net line canary 171
# pull request net line canary 172
# pull request net line canary 173
# pull request net line canary 174
# pull request net line canary 175
# pull request net line canary 176
# pull request net line canary 177
# pull request net line canary 178
# pull request net line canary 179
# pull request net line canary 180
# pull request net line canary 181
# pull request net line canary 182
# pull request net line canary 183
# pull request net line canary 184
# pull request net line canary 185
# pull request net line canary 186
# pull request net line canary 187
# pull request net line canary 188
# pull request net line canary 189
# pull request net line canary 190
# pull request net line canary 191
# pull request net line canary 192
# pull request net line canary 193
# pull request net line canary 194
# pull request net line canary 195
# pull request net line canary 196
# pull request net line canary 197
# pull request net line canary 198
# pull request net line canary 199
# pull request net line canary 200
# pull request net line canary 201
