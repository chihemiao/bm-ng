import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
RUNTIME_DIRS = frozenset({"data", "execution", "strategy", "risk", "reconciliation", "ops"})
IO_MODULES = frozenset(
    {"aiohttp", "asyncio", "httpx", "os", "pathlib", "requests", "socket", "subprocess", "urllib"}
)
EXCHANGE_SDKS = frozenset({"bybit", "ccxt", "hyperliquid", "hyperliquid_sdk", "pybit"})
BUILTIN_IO = frozenset({"__import__", "compile", "eval", "exec", "input", "open", "print"})


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


def _imports(tree: ast.AST, relative: Path) -> set[str]:
    names = set()
    package = list(relative.with_suffix("").parts[:-1])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            ascend = max(0, node.level - 1)
            base = package[: len(package) - ascend] if node.level and ascend <= len(package) else []
            module_parts = base + (node.module.split(".") if node.module else [])
            module = ".".join(module_parts)
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


def _dynamic_source_dispatch(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Subscript) and _mentions_source(node.func.slice):
            return True
        is_getattr = isinstance(node.func, ast.Name) and node.func.id == "getattr"
        if is_getattr and any(_mentions_source(argument) for argument in node.args[1:]):
            return True
    return False


def _contract_builtin_io(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in BUILTIN_IO
        for node in ast.walk(tree)
    )


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
    if _dynamic_source_dispatch(tree):
        violations.add("source-branch")
    return violations


def _payload_schemas(tree: ast.Module) -> frozenset[str] | None:
    assignments = []
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        is_registry = any(
            isinstance(target, ast.Name) and target.id == "PAYLOAD_SCHEMAS"
            for target in targets
        )
        if is_registry:
            assignments.append(node)
    if len(assignments) != 1 or not isinstance(assignments[0], (ast.Assign, ast.AnnAssign)):
        return None
    value = assignments[0].value
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


def structural_violations(root: Path) -> set[str]:
    violations = set()
    schema_tree = None
    for path in _python_files(root):
        tree = ast.parse(path.read_text())
        relative = path.relative_to(root)
        if relative == Path("data/schema_dispatch.py"):
            schema_tree = tree
            if any(isinstance(node, (ast.FunctionDef, ast.ClassDef)) for node in tree.body):
                violations.add("schema-dispatch-code")
        if relative.parts[0] in RUNTIME_DIRS:
            violations |= _import_violations(relative, _imports(tree, relative))
        if relative == Path("data/contracts.py") and _contract_builtin_io(tree):
            violations.add("contracts-io")
        violations |= _tree_violations(tree)
    schemas = _payload_schemas(schema_tree) if schema_tree else None
    if schemas is None:
        violations.add("payload-schema-registry")
    elif len(schemas) > 20:
        violations.add("event-types")
    return violations


def test_current_repository_passes_structural_gates() -> None:
    assert structural_violations(ROOT) == set()


def test_synthetic_tree_exposes_function_import_and_source_violations(tmp_path: Path) -> None:
    _write(tmp_path, "data/schema_dispatch.py", _registry(["raw_frame"]))
    _write(tmp_path, "data/contracts.py", "from pathlib import Path\n")
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
    _write(tmp_path, "data/schema_dispatch.py", "PAYLOAD_SCHEMAS = frozenset(load_types())\n")
    assert structural_violations(tmp_path) == {"payload-schema-registry"}


def test_more_than_twenty_payload_schemas_are_rejected(tmp_path: Path) -> None:
    registry = _registry([f"event_{index}" for index in range(21)])
    _write(tmp_path, "data/schema_dispatch.py", registry)
    assert structural_violations(tmp_path) == {"event-types"}


def test_relative_session_import_cannot_bypass_the_shard_boundary(tmp_path: Path) -> None:
    _write(tmp_path, "data/schema_dispatch.py", _registry(["raw_frame"]))
    _write(tmp_path, "data/session.py", "from . import shard\n")
    assert structural_violations(tmp_path) == {"session-shard-import"}


def test_contract_builtin_io_is_rejected_without_an_import(tmp_path: Path) -> None:
    _write(tmp_path, "data/schema_dispatch.py", _registry(["raw_frame"]))
    source = 'def load():\n    return open("evidence")\n'
    _write(tmp_path, "data/contracts.py", source)
    assert structural_violations(tmp_path) == {"contracts-io"}


def test_payload_schema_registry_cannot_be_reassigned(tmp_path: Path) -> None:
    source = _registry(["raw_frame"]) + 'PAYLOAD_SCHEMAS = PAYLOAD_SCHEMAS | {"extra"}\n'
    _write(tmp_path, "data/schema_dispatch.py", source)
    assert structural_violations(tmp_path) == {"payload-schema-registry"}


def test_schema_dispatch_cannot_grow_behavior(tmp_path: Path) -> None:
    source = _registry(["raw_frame"]) + "def validate():\n    return True\n"
    _write(tmp_path, "data/schema_dispatch.py", source)
    assert structural_violations(tmp_path) == {"schema-dispatch-code"}


def test_source_driven_dynamic_dispatch_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "data/schema_dispatch.py", _registry(["raw_frame"]))
    source = "HANDLERS[event.source]()\ngetattr(handler, event.source)()\n"
    _write(tmp_path, "data/dispatch.py", source)
    assert structural_violations(tmp_path) == {"source-branch"}
