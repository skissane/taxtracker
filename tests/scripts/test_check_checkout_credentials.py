from pathlib import Path

import pytest

from scripts.check_checkout_credentials import main

WORKFLOW = """\
jobs:
  build:
    steps:
      - uses: actions/checkout@v7
        with:  # a comment

          persist-credentials: false
      - uses: actions/checkout@v7
        with:
          token: ${{ secrets.TOKEN }}
          # pushes below
          persist-credentials: true
"""

BAD = """\
jobs:
  build:
    steps:
      - uses: actions/checkout@v7
      - uses: actions/checkout@v7
        with:
          persist-credentials: ${{ inputs.persist }}
      - uses: some/action@v1
        with:
          persist-credentials: false
      - uses: actions/checkout@v7
        env:
          persist-credentials: false
        with:
          fetch-depth: 0
      - uses: actions/checkout@v7
        with:
          sparse-checkout: |
            persist-credentials: false
"""


@pytest.fixture
def workflows(tmp_path: Path) -> Path:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    return tmp_path


def test_main_passes_when_every_checkout_says(workflows: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (workflows / ".github/workflows/a.yml").write_text(WORKFLOW)
    assert main(workflows) == 0
    assert capsys.readouterr().out == "all 2 actions/checkout steps set persist-credentials\n"


def test_main_reports_unset_and_unexpected_values(workflows: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (workflows / ".github/workflows/a.yml").write_text(WORKFLOW)
    (workflows / ".github/workflows/b.yaml").write_text(BAD)
    assert main(workflows) == 1
    assert capsys.readouterr().err.splitlines() == [
        ".github/workflows/b.yaml: a actions/checkout step has no persist-credentials, not false or true",
        ".github/workflows/b.yaml: a actions/checkout step has persist-credentials: ${{ inputs.persist }},"
        " not false or true",
        ".github/workflows/b.yaml: a actions/checkout step has no persist-credentials, not false or true",
        ".github/workflows/b.yaml: a actions/checkout step has no persist-credentials, not false or true",
    ]


def test_main_reads_composite_actions_too(workflows: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (workflows / ".github/workflows/a.yml").write_text(WORKFLOW)
    (workflows / ".github/actions/setup").mkdir(parents=True)
    (workflows / ".github/actions/setup/action.yml").write_text(
        "runs:\n  using: composite\n  steps:\n    - uses: actions/checkout@v7\n"
    )
    assert main(workflows) == 1
    assert capsys.readouterr().err.splitlines() == [
        ".github/actions/setup/action.yml: a actions/checkout step has no persist-credentials, not false or true",
    ]


def test_main_fails_when_no_checkout_is_recognized(workflows: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (workflows / ".github/workflows/a.yml").write_text("jobs:\n  a:\n    steps:\n      - run: echo\n")
    assert main(workflows) == 1
    assert capsys.readouterr().err.splitlines() == ["no workflow or action has a actions/checkout step"]


def test_repo_workflows_pass() -> None:
    assert main(Path(__file__).parents[2]) == 0
