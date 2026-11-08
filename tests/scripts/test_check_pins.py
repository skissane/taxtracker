from pathlib import Path

import pytest

from scripts.check_pins import main, pins, steps

WORKFLOW = """\
jobs:
  build:
    steps:
      - uses: actions/checkout@v4

      # A comment between steps
      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          # pinned
          version: "0.13.6"
          python-version: ${{ matrix.python-version }}

      - name: Install just
        uses: extractions/setup-just@v3
        with:
          just-version: '1.58.0'

      - name: Other
        uses: some/action@v1
        with:
          version: "9.9.9"
          args:
            - --x
            - --y
  other:
    steps:
      - uses: "astral-sh/setup-uv@v5"
      - run: echo
      - uses: astral-sh/setup-uv@v6
        with:
          version: ${{ env.UV }}  # from the environment
      - uses: astral-sh/setup-uv@v6
        with:
          version: "0.13.6 # not a comment"
"""


def test_steps_splits_outermost_items_only() -> None:
    found = steps(WORKFLOW)
    assert [s[0].strip() for s in found] == [
        "- uses: actions/checkout@v4",
        "- name: Install uv",
        "- name: Install just",
        "- name: Other",
        '- uses: "astral-sh/setup-uv@v5"',
        "- run: echo",
        "- uses: astral-sh/setup-uv@v6",
        "- uses: astral-sh/setup-uv@v6",
    ]
    assert found[3][-1].strip() == "- --y"


def test_pins_reads_each_steps_own_input() -> None:
    # The unpinned setup-uv in `other` isn't masked by `Other`'s version, and
    # an unexpected value is read as written.
    assert pins(WORKFLOW, "astral-sh/setup-uv", "version") == [
        "0.13.6",
        None,
        "${{ env.UV }}",
        "0.13.6 # not a comment",
    ]
    assert pins(WORKFLOW, "extractions/setup-just", "just-version") == ["1.58.0"]


def test_pins_ignores_with_lines_deeper_in_a_step() -> None:
    # A `with:` line in a script isn't the step's own `with:` mapping.
    workflow = """\
steps:
  - uses: astral-sh/setup-uv@v6
    env:
      SCRIPT: |
        with:
          version: "1.0"
    with:
      version: "0.13.6"
"""
    assert pins(workflow, "astral-sh/setup-uv", "version") == ["0.13.6"]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text('[tool.uv]\nrequired-version = ">=0.13.6"\n')
    (tmp_path / "justfile").write_text("set minimum-version := '1.58.0'\n\ndefault:\n")
    (tmp_path / "Dockerfile").write_text("FROM ghcr.io/astral-sh/uv:0.13.6 AS uv\n\nFROM python:3.14-slim\n")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    return tmp_path


def test_main_passes_when_all_pinned(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / ".github/workflows/a.yml").write_text(WORKFLOW.split("  other:")[0])
    assert main(repo) == 0
    assert (
        capsys.readouterr().out
        == "workflows and actions pin uv 0.13.6 in 1 steps and just 1.58.0 in 1 steps, and the Dockerfile uv 0.13.6\n"
    )


def test_main_reports_unpinned_and_mismatched_in_yaml_files(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / ".github/workflows/a.yaml").write_text(WORKFLOW.replace("'1.58.0'", "1.57.0"))
    assert main(repo) == 1
    assert capsys.readouterr().err.splitlines() == [
        ".github/workflows/a.yaml: a astral-sh/setup-uv step has no version, not 0.13.6",
        ".github/workflows/a.yaml: a astral-sh/setup-uv step has version: ${{ env.UV }}, not 0.13.6",
        ".github/workflows/a.yaml: a astral-sh/setup-uv step has version: 0.13.6 # not a comment, not 0.13.6",
        ".github/workflows/a.yaml: a extractions/setup-just step has just-version: 1.57.0, not 1.58.0",
    ]


ACTION = """\
name: Setup
runs:
  using: composite
  steps:
    - uses: astral-sh/setup-uv@v5
      with:
        version: "0.13.5"
    - uses: extractions/setup-just@v3
      with:
        just-version: "1.58.0"
"""


def test_main_reads_composite_actions_too(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Only action.yml/action.yaml under .github/actions count, in any subdirectory.
    (repo / ".github/workflows/a.yml").write_text(WORKFLOW.split("  other:")[0])
    (repo / ".github/actions/setup").mkdir(parents=True)
    (repo / ".github/actions/setup/action.yml").write_text(ACTION)
    (repo / ".github/actions/nested/deeper").mkdir(parents=True)
    (repo / ".github/actions/nested/deeper/action.yaml").write_text(ACTION.replace("0.13.5", "0.13.6"))
    (repo / ".github/actions/setup/other.yml").write_text(ACTION)
    assert main(repo) == 1
    assert capsys.readouterr().err.splitlines() == [
        ".github/actions/setup/action.yml: a astral-sh/setup-uv step has version: 0.13.5, not 0.13.6",
    ]
    (repo / ".github/actions/setup/action.yml").write_text(ACTION.replace("0.13.5", "0.13.6"))
    assert main(repo) == 0
    assert (
        capsys.readouterr().out
        == "workflows and actions pin uv 0.13.6 in 3 steps and just 1.58.0 in 3 steps, and the Dockerfile uv 0.13.6\n"
    )


def test_main_fails_when_no_step_is_recognized(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / ".github/workflows/a.yml").write_text("jobs:\n  a:\n    steps:\n      - run: echo\n")
    assert main(repo) == 1
    assert capsys.readouterr().err.splitlines() == [
        "no workflow or action has a astral-sh/setup-uv step",
        "no workflow or action has a extractions/setup-just step",
    ]


def test_main_rejects_unexpected_minimums(repo: Path) -> None:
    (repo / "pyproject.toml").write_text('[tool.uv]\nrequired-version = "==0.13.6"\n')
    with pytest.raises(ValueError, match="isn't of the form"):
        main(repo)
    (repo / "pyproject.toml").write_text('[tool.uv]\nrequired-version = ">=0.13.6"\n')
    (repo / "justfile").write_text("default:\n")
    with pytest.raises(ValueError, match="no `set minimum-version"):
        main(repo)


def test_main_checks_the_dockerfile_uv_stage(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / ".github/workflows/a.yml").write_text(WORKFLOW.split("  other:")[0])
    (repo / "Dockerfile").write_text(
        "FROM ghcr.io/astral-sh/uv:0.13.5 AS uv\nfrom ghcr.io/astral-sh/uv:0.13.6@sha256:abc AS uv2\n"
    )
    assert main(repo) == 1
    assert capsys.readouterr().err.splitlines() == ["Dockerfile: FROM ghcr.io/astral-sh/uv:0.13.5, not 0.13.6"]
    (repo / "Dockerfile").write_text("FROM python:3.14-slim\n")
    assert main(repo) == 1
    assert capsys.readouterr().err.splitlines() == ["Dockerfile has no FROM ghcr.io/astral-sh/uv:<version> stage"]


def test_repo_pins_pass() -> None:
    assert main(Path(__file__).parents[2]) == 0
