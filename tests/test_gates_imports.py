import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
RUNTIME_DIRS = frozenset({"data", "execution", "strategy", "risk", "reconciliation", "ops"})
IO_MODULES = frozenset(
    {"aiohttp", "asyncio", "httpx", "os", "pathlib", "requests", "socket", "subprocess", "urllib"}
)
EXCHANGE_SDKS = frozenset({"bybit", "ccxt", "hyperliquid", "hyperliquid_sdk", "pybit"})


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _registry(names: list[str]) -> str:
    members = ", ".join(repr(name) for name in names)
    return f"PAYLOAD_SCHEMAS = frozenset({{{members}}})\n"


def _python_files(root: Path) -> list[Path]:
    scopes = RUNTIME_DIRS | {"tests", "research"}
    return [
        path
        for scope in scopes
        if (root / scope).is_dir()
        for path in (root / scope).rglob("*.py")
        if "__pycache__" not in path.parts
    ]


def _imports(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                names.add(module)
            names.update(f"{module}.{alias.name}".strip(".") for alias in node.names)
    return names


def _import_violations(relative: Path, names: set[str]) -> set[str]:
    violations = set()
    package = relative.parts[0]
    roots = {name.split(".")[0] for name in names}
    if roots & {"tests", "research"}:
        violations.add("runtime-test-import")
    if package == "ops" and any(name.startswith("execution.orders") for name in names):
        violations.add("ops-order-import")
    if package == "strategy" and roots & EXCHANGE_SDKS:
        violations.add("strategy-sdk-import")
    if relative == Path("data/contracts.py") and roots & IO_MODULES:
        violations.add("contracts-io")
    if relative == Path("data/session.py") and any(name.startswith("data.shard") for name in names):
        violations.add("session-shard-import")
    return violations


def _mentions_source(node: ast.AST) -> bool:
    return any(
        (isinstance(part, ast.Name) and part.id == "source")
        or (isinstance(part, ast.Attribute) and part.attr == "source")
        or (isinstance(part, ast.Constant) and part.value == "source")
        for part in ast.walk(node)
    )


def _control_subjects(tree: ast.AST) -> list[ast.AST]:
    subjects = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp, ast.While)):
            subjects.append(node.test)
        elif isinstance(node, ast.Match):
            subjects.append(node.subject)
        elif isinstance(node, ast.comprehension):
            subjects.extend(node.ifs)
    return subjects


def _tree_violations(tree: ast.AST) -> set[str]:
    violations = set()
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    if any(
        node.end_lineno - node.lineno + 1 > 60
        for node in ast.walk(tree)
        if isinstance(node, functions)
    ):
        violations.add("function-lines")
    if any(_mentions_source(subject) for subject in _control_subjects(tree)):
        violations.add("source-branch")
    return violations


def _payload_schemas(tree: ast.Module) -> frozenset[str] | None:
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        is_registry = any(
            isinstance(target, ast.Name) and target.id == "PAYLOAD_SCHEMAS"
            for target in targets
        )
        if not is_registry:
            continue
        value = node.value
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name):
            return None
        if value.func.id != "frozenset" or len(value.args) != 1:
            return None
        members = value.args[0]
        if not isinstance(members, (ast.Set, ast.List, ast.Tuple)):
            return None
        values = [item.value for item in members.elts if isinstance(item, ast.Constant)]
        all_strings = all(isinstance(item, str) for item in values)
        if len(values) != len(members.elts) or not values or not all_strings:
            return None
        return frozenset(values)
    return None


def structural_violations(root: Path) -> set[str]:
    violations = set()
    contracts_tree = None
    for path in _python_files(root):
        tree = ast.parse(path.read_text())
        relative = path.relative_to(root)
        if relative == Path("data/contracts.py"):
            contracts_tree = tree
        if relative.parts[0] in RUNTIME_DIRS:
            violations |= _import_violations(relative, _imports(tree))
        violations |= _tree_violations(tree)
    schemas = _payload_schemas(contracts_tree) if contracts_tree else None
    if schemas is None:
        violations.add("payload-schema-registry")
    elif len(schemas) > 20:
        violations.add("event-types")
    return violations


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


def test_relative_session_import_cannot_bypass_the_shard_boundary(tmp_path: Path) -> None:
    _write(tmp_path, "data/contracts.py", _registry(["raw_frame"]))
    _write(tmp_path, "data/session.py", "from . import shard\n")
    assert structural_violations(tmp_path) == {"session-shard-import"}


def test_contract_builtin_io_is_rejected_without_an_import(tmp_path: Path) -> None:
    source = _registry(["raw_frame"]) + 'def load():\n    return open("evidence")\n'
    _write(tmp_path, "data/contracts.py", source)
    assert structural_violations(tmp_path) == {"contracts-io"}


def test_payload_schema_registry_cannot_be_reassigned(tmp_path: Path) -> None:
    source = _registry(["raw_frame"]) + 'PAYLOAD_SCHEMAS = PAYLOAD_SCHEMAS | {"extra"}\n'
    _write(tmp_path, "data/contracts.py", source)
    assert structural_violations(tmp_path) == {"payload-schema-registry"}


def test_source_driven_dynamic_dispatch_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "data/contracts.py", _registry(["raw_frame"]))
    source = "HANDLERS[event.source]()\ngetattr(handler, event.source)()\n"
    _write(tmp_path, "data/dispatch.py", source)
    assert structural_violations(tmp_path) == {"source-branch"}
