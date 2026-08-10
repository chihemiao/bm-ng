"""Exact-token duplication is conservative, not a semantic clone detector.

Renamed identifiers evade detection, so 0% means no exact token-level duplicates,
not no semantic duplication. The six-line window and normalization rules are frozen
before observing this repository's result.
"""

from pathlib import Path

ROOT = Path(__file__).parents[1]


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
