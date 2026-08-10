"""Exact-token duplication is conservative, not a semantic clone detector.

Renamed identifiers evade detection, so 0% means no exact token-level duplicates,
not no semantic duplication. The six-line window and normalization rules are frozen
before observing this repository's result.
"""

import ast
import hashlib
import io
import tokenize
from pathlib import Path

ROOT = Path(__file__).parents[1]
RUNTIME_DIRS = frozenset({"data", "execution", "strategy", "risk", "reconciliation", "ops"})
MINIMUM_DUPLICATE_LINES = 6
MAX_DUPLICATION_RATIO = 0.03


def _docstring_lines(source: str) -> set[int]:
    lines = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body or not isinstance(node.body[0], ast.Expr):
            continue
        value = node.body[0].value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            lines.update(range(node.body[0].lineno, node.body[0].end_lineno + 1))
    return lines


def normalized_source_lines(source: str) -> list[str]:
    docstrings = _docstring_lines(source)
    by_line: dict[int, list[str]] = {}
    ignored = {
        tokenize.COMMENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
        tokenize.INDENT,
        tokenize.NEWLINE,
        tokenize.NL,
    }
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in ignored or token.start[0] in docstrings:
            continue
        pieces = token.string.splitlines() or [token.string]
        for offset, piece in enumerate(pieces):
            line_number = token.start[0] + offset
            if line_number not in docstrings:
                by_line.setdefault(line_number, []).append(f"{token.type}:{piece}")
    return ["\0".join(by_line[line]) for line in sorted(by_line) if by_line[line]]


def duplication_counts(sources: dict[str, str]) -> tuple[int, int]:
    normalized = {path: normalized_source_lines(source) for path, source in sources.items()}
    windows: dict[tuple[bytes, tuple[str, ...]], list[tuple[str, int]]] = {}
    for path, lines in normalized.items():
        for start in range(len(lines) - MINIMUM_DUPLICATE_LINES + 1):
            window = tuple(lines[start : start + MINIMUM_DUPLICATE_LINES])
            digest = hashlib.sha256("\n".join(window).encode()).digest()
            windows.setdefault((digest, window), []).append((path, start))
    duplicated = set()
    for locations in windows.values():
        if len(locations) < 2:
            continue
        for path, start in locations:
            duplicated.update(
                (path, line) for line in range(start, start + MINIMUM_DUPLICATE_LINES)
            )
    return len(duplicated), sum(map(len, normalized.values()))


def duplication_ratio(sources: dict[str, str]) -> float:
    duplicated, total = duplication_counts(sources)
    return duplicated / total if total else 0.0


def _runtime_sources(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text()
        for directory in RUNTIME_DIRS
        if (root / directory).is_dir()
        for path in sorted((root / directory).rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def duplication_violations(root: Path) -> set[str]:
    return {"duplication"} if duplication_ratio(_runtime_sources(root)) > MAX_DUPLICATION_RATIO else set()


def _source(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def _write(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


def test_duplication_parameters_are_frozen() -> None:
    assert MINIMUM_DUPLICATE_LINES == 6
    assert MAX_DUPLICATION_RATIO == 0.03


def test_exact_duplicate_ratio_below_three_percent_passes(tmp_path: Path) -> None:
    lines = [f"value_{index} = {index}" for index in range(500)]
    lines[250:256] = lines[:6]
    _write(tmp_path, "data/low.py", _source(lines))

    assert duplication_ratio({"data/low.py": _source(lines)}) < 0.03
    assert duplication_violations(tmp_path) == set()


def test_exact_duplicate_ratio_above_three_percent_fails(tmp_path: Path) -> None:
    lines = [f"value_{index} = {index}" for index in range(100)]
    lines[50:56] = lines[:6]
    _write(tmp_path, "data/high.py", _source(lines))

    assert duplication_ratio({"data/high.py": _source(lines)}) > 0.03
    assert duplication_violations(tmp_path) == {"duplication"}


def test_overlapping_windows_do_not_double_count_duplicated_lines() -> None:
    lines = [f"value_{index} = {index}" for index in range(100)]
    lines[50:57] = lines[:7]

    duplicated, total = duplication_counts({"data/overlap.py": _source(lines)})

    assert (duplicated, total) == (14, 100)


def test_comments_docstrings_blank_lines_and_indentation_do_not_dilute() -> None:
    clean = """\
class Example:
    def method(self):
        value = 1
        return value
"""
    noisy = '''\
"""module documentation"""

class Example:
        """class documentation"""
        def method(self):
                """method documentation"""
                # an ignored comment
                value = 1  # an ignored inline comment

                return value
'''
    assert normalized_source_lines(noisy) == normalized_source_lines(clean)


def test_current_repository_stays_within_exact_duplication_budget() -> None:
    assert duplication_violations(ROOT) == set()
