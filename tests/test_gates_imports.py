from pathlib import Path
from typing import TYPE_CHECKING

ROOT = Path(__file__).parents[1]

if TYPE_CHECKING:

    def structural_violations(root: Path) -> set[str]: ...


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _registry(names: list[str]) -> str:
    members = ", ".join(repr(name) for name in names)
    return f"PAYLOAD_SCHEMAS = frozenset({{{members}}})\n"


def test_current_repository_passes_structural_gates() -> None:
    assert structural_violations(ROOT) == set()


def test_synthetic_tree_exposes_function_import_and_source_violations(tmp_path: Path) -> None:
    _write(tmp_path, "data/contracts.py", "from pathlib import Path\n" + _registry(["raw_frame"]))
    _write(tmp_path, "data/session.py", "from data.shard import ShardWriter\n")
    _write(tmp_path, "data/runtime.py", "import tests.helpers\nfrom research import evidence\n")
    _write(tmp_path, "ops/worker.py", "import execution.orders\n")
    _write(tmp_path, "strategy/model.py", "import ccxt\n")
    _write(tmp_path, "data/source.py", 'if event["source"] == "replay":\n    value = 1\n')
    long_body = "\n".join("    value = 1" for _ in range(60))
    _write(tmp_path, "data/long.py", f"def oversized():\n{long_body}\n")

    assert structural_violations(tmp_path) == {
        "contracts-io",
        "function-lines",
        "ops-order-import",
        "runtime-test-import",
        "session-shard-import",
        "source-branch",
        "strategy-sdk-import",
    }


def test_dynamic_payload_schema_registry_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "data/contracts.py", "PAYLOAD_SCHEMAS = frozenset(load_types())\n")
    assert structural_violations(tmp_path) == {"payload-schema-registry"}


def test_more_than_twenty_payload_schemas_are_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "data/contracts.py", _registry([f"event_{index}" for index in range(21)]))
    assert structural_violations(tmp_path) == {"event-types"}
