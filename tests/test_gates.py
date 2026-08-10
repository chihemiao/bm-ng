import json
from pathlib import Path
from typing import TYPE_CHECKING

ROOT = Path(__file__).parents[1]
VALID_CONFIG = """\
[project]
dependencies = []
[tool.ruff.lint]
select = ["C90"]
[tool.ruff.lint.mccabe]
max-complexity = 10
"""

if TYPE_CHECKING:

    def repository_violations(root: Path) -> set[str]: ...


def _write(root: Path, relative: str, content: str = "") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _configured(root: Path) -> Path:
    _write(root, "pyproject.toml", VALID_CONFIG)
    return root


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
