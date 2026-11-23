"""The regexes pyproject.toml gives `repoutils added-strings`, which should
match suppression comments only, not strings or test data that name them."""

from pathlib import Path

import pytest
from repoutils.commands.added_strings import Rule, load_config

ROOT = Path(__file__).parent.parent


def rules() -> dict[str, Rule]:
    return {rule.name or rule.pattern: rule for rule in load_config(ROOT).rules}


# Lines that name a suppression without being one: in a string literal, a
# docstring, or a comment that only mentions the word.
NOT_COMMENTS = [
    'x = "# {}"',
    "x = '# {}'",
    'x = "a \\" # {}"',
    '"""Docs that mention # {}."""',
    "x = '''It's # {}, quoted'''",
    "x = 1  # see {} docs",
]


@pytest.mark.parametrize(
    "line",
    [
        "x = 1  # NOSONAR",
        "x = 1  #NOSONAR(python:S1234)",
        "x = 1  # nosonar",
        'x = "a#b"  # NOSONAR',
        "x = 1  # type: ignore # NOSONAR",
        "x = '''it's'''  # NOSONAR",
    ],
)
def test_nosonar_matches(line: str) -> None:
    assert rules()["NOSONAR"].matches(line)


@pytest.mark.parametrize("line", [*(s.format("NOSONAR") for s in NOT_COMMENTS), "x = 1  # NOSONARX"])
def test_nosonar_does_not_match(line: str) -> None:
    assert not rules()["NOSONAR"].matches(line)


@pytest.mark.parametrize(
    "line",
    [
        "import x  # noqa: F401",
        "import x  # noqa",
        "import x  #noqa:F401",
        "import x  # NOQA: F401",
        "x = 'it\\'s'  # noqa: E501",
        'x = f"{y}" + "z"  # noqa: E501',
        "import x  # type: ignore # noqa: F401",
        'x = """Don\'t document this"""  # noqa: E501',
        'x = """a\'b"c"""  # noqa: E501',
    ],
)
def test_noqa_matches(line: str) -> None:
    assert rules()["# noqa"].matches(line)


@pytest.mark.parametrize("line", [*(s.format("noqa") for s in NOT_COMMENTS), "# noqanot"])
def test_noqa_does_not_match(line: str) -> None:
    assert not rules()["# noqa"].matches(line)


@pytest.mark.parametrize(
    "line",
    [
        "x: int = f()  # type: ignore",
        "x: int = f()  # type: ignore[assignment]",
        "x: int = f()  #type:ignore",
        "x: int = f()  # noqa: E501 # type: ignore",
        'x: str = """say "hi" there"""  # type: ignore',
    ],
)
def test_type_ignore_matches(line: str) -> None:
    assert rules()["# type: ignore"].matches(line)


@pytest.mark.parametrize("line", [*(s.format("type: ignore") for s in NOT_COMMENTS), "x = f()  # type: ignored here"])
def test_type_ignore_does_not_match(line: str) -> None:
    assert not rules()["# type: ignore"].matches(line)
