import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
RUNTIME_DIRS = frozenset({"data", "execution", "strategy", "risk", "reconciliation", "ops"})
IGNORED_DIRS = frozenset({".git", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__"})
TEXT_SUFFIXES = frozenset(
    {".env", ".ipynb", ".json", ".md", ".py", ".sh", ".toml", ".txt", ".yaml", ".yml"}
)
VERSIONED_NAME = re.compile(
    r"(?:^|[_-])(?:v\d+|new|fixed|final|copy|old|backup|retry\d+|attempt\d+)(?:[_\-.]|$)",
    re.IGNORECASE,
)
CREDENTIAL = re.compile(
    r"(?:0x[0-9a-f]{64}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?:api[_-]?key|private[_-]?key|mnemonic|seed[_-]?phrase)\s*[:=]\s*"
    r"[\"'][^\"'\n]{8,}[\"'])",
    re.IGNORECASE,
)
VALID_CONFIG = """\
[project]
dependencies = []
[tool.ruff.lint]
select = ["C90"]
[tool.ruff.lint.mccabe]
max-complexity = 10
"""


def _write(root: Path, relative: str, content: str = "") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _configured(root: Path) -> Path:
    _write(root, "pyproject.toml", VALID_CONFIG)
    return root


def _files(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and not IGNORED_DIRS.intersection(path.relative_to(root).parts)
    ]


def _lines(path: Path) -> int:
    return len(path.read_text(errors="ignore").splitlines())


def _dependency_count(config: dict) -> int:
    project = config.get("project", {})
    groups = config.get("dependency-groups", {})
    dependencies = list(project.get("dependencies", []))
    for values in project.get("optional-dependencies", {}).values():
        dependencies.extend(values)
    for values in groups.values():
        dependencies.extend(values)
    return len(set(dependencies))


def _config_violations(root: Path) -> set[str]:
    try:
        config = tomllib.loads((root / "pyproject.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {"dependencies", "ruff-complexity"}
    violations = set()
    if _dependency_count(config) > 25:
        violations.add("dependencies")
    lint = config.get("tool", {}).get("ruff", {}).get("lint", {})
    if "C90" not in lint.get("select", []):
        violations.add("ruff-complexity")
    if lint.get("mccabe", {}).get("max-complexity") != 10:
        violations.add("ruff-complexity")
    return violations


def _budget_violations(files: list[Path], relative: dict[Path, Path]) -> set[str]:
    violations = set()
    runtime_packages = {
        parts.parts[0]
        for path, parts in relative.items()
        if path.suffix == ".py" and parts.parts[0] not in {"tests", "research"}
    }
    if not runtime_packages <= RUNTIME_DIRS or len(runtime_packages) > 6:
        violations.add("runtime-package")
    runtime = [path for path, parts in relative.items() if parts.parts[0] in RUNTIME_DIRS]
    if len(runtime) > 40:
        violations.add("runtime-files")
    if sum(_lines(path) for path in runtime) > 8_000:
        violations.add("runtime-lines")
    harness = [path for path, parts in relative.items() if parts.parts[:2] == ("tests", "harness")]
    if sum(_lines(path) for path in harness if path.suffix == ".py") > 600:
        violations.add("harness-lines")
    if sum(parts.parts[0] == "research" for parts in relative.values()) > 10:
        violations.add("research-files")
    if sum(path.suffix.lower() == ".md" for path in files) > 8:
        violations.add("markdown-files")
    python_scope = [
        path for path, parts in relative.items()
        if path.suffix == ".py" and parts.parts[0] in RUNTIME_DIRS | {"tests", "research"}
    ]
    if any(_lines(path) > 400 for path in python_scope):
        violations.add("python-file-lines")
    return violations


def _path_violations(files: list[Path], relative: dict[Path, Path]) -> set[str]:
    violations = set()
    if any(VERSIONED_NAME.search(path.name) for path in files):
        violations.add("versioned-filename")
    invalid_tests = [
        path for path, parts in relative.items()
        if parts.parts[0] == "tests" and parts.parts[:2] != ("tests", "harness")
        and (path.suffix != ".py" or not path.name.startswith("test_"))
    ]
    if invalid_tests:
        violations.add("test-filename")
    text_files = [path for path in files if path.suffix.lower() in TEXT_SUFFIXES]
    if any(CREDENTIAL.search(path.read_text(errors="ignore")) for path in text_files):
        violations.add("credential")
    return violations


def repository_violations(root: Path) -> set[str]:
    files = _files(root)
    relative = {path: path.relative_to(root) for path in files}
    return _config_violations(root) | _budget_violations(files, relative) | _path_violations(
        files, relative
    )


def test_current_repository_passes_inventory_budget_and_secret_gates() -> None:
    assert repository_violations(ROOT) == set()


def test_synthetic_tree_exposes_every_inventory_and_budget_violation(tmp_path: Path) -> None:
    root = _configured(tmp_path)
    for index in range(41):
        _write(root, f"data/part_{index}.py", "value = 1\n")
    _write(root, "data/huge.py", "value = 1\n" * 8_001)
    _write(root, "data/foo_v2.py", "value = 1\n")
    _write(root, "utils/extra.py", "value = 1\n")
    _write(root, "tests/helper.py", "value = 1\n")
    _write(root, "tests/harness/fault.py", "value = 1\n" * 601)
    for index in range(11):
        _write(root, f"research/evidence_{index}.json", "{}")
    for index in range(9):
        _write(root, f"document_{index}.md", "evidence\n")

    assert repository_violations(root) == {
        "harness-lines",
        "markdown-files",
        "python-file-lines",
        "research-files",
        "runtime-files",
        "runtime-lines",
        "runtime-package",
        "test-filename",
        "versioned-filename",
    }


def test_synthetic_dependency_and_ruff_regressions_are_rejected(tmp_path: Path) -> None:
    dependencies = ", ".join(f'"package-{index}"' for index in range(26))
    _write(
        tmp_path,
        "pyproject.toml",
        f"""\
[project]
dependencies = [{dependencies}]
[tool.ruff.lint]
select = ["E"]
[tool.ruff.lint.mccabe]
max-complexity = 11
""",
    )
    assert repository_violations(tmp_path) == {"dependencies", "ruff-complexity"}


def test_notebook_output_with_a_credential_signature_is_rejected(tmp_path: Path) -> None:
    root = _configured(tmp_path)
    signature = "private" + "_key='0x" + "a" * 64 + "'"
    notebook = {"cells": [{"outputs": [{"text": [signature]}]}]}
    _write(root, "evidence.ipynb", json.dumps(notebook))
    assert repository_violations(root) == {"credential"}
