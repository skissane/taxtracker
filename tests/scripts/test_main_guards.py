"""Runs each script's ``if __name__ == "__main__":`` block, as CI's
``python scripts/<name>.py`` or ``python -m scripts.<name>`` would, so that
coverage measures them and a mistake there (e.g. passing ``sys.argv`` rather
than ``sys.argv[1:]``, or dropping ``main()``'s exit code) fails a test.

One file for every script, rather than a test in each script's own test file,
as they share the runner and are one idea. Each case picks arguments for which
``main()`` returns rather than exits itself (as argparse's ``--help`` does), so
the guard's own lines run, and which need no network and change nothing.
Ported from skissane/exolot.
"""

import io
import json
import runpy
import sys
import urllib.request
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]


def run_as_main(monkeypatch: pytest.MonkeyPatch, module: str, *args: str) -> object:
    """Runs ``python -m module args...`` in this process, returning its exit
    code (``None`` if it didn't raise ``SystemExit``)."""
    monkeypatch.setattr(sys, "argv", [module, *args])
    # The tests have already imported most of these; run_module warns about
    # running a module that's in sys.modules. monkeypatch puts it back after.
    monkeypatch.delitem(sys.modules, module, raising=False)
    try:
        runpy.run_module(module, run_name="__main__")
    except SystemExit as e:
        return e.code
    return None


@pytest.mark.parametrize(
    ("module", "args", "expected"),
    [
        ("scripts.check_checkout_credentials", [], "set persist-credentials"),
        ("scripts.check_pins", [], "pin uv"),
        ("scripts.check_screenshots_dir", [], "screenshots/"),
        ("scripts.check_sentence_per_line", [], "one-sentence-per-line"),
        ("scripts.check_whitespace", [], "clean whitespace"),
        ("scripts.long_files", ["--base", ""], "500 lines"),
        ("scripts.requirements_min_versions", ["check"], "Dependency minimum versions match uv.lock."),
        ("scripts.validate_uv_lock_os_arch", [], "uv.lock"),
    ],
)
def test_repo_checks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], module: str, args: list[str], expected: str
) -> None:
    # They check the repo in the current directory: this one.
    monkeypatch.chdir(ROOT)
    assert run_as_main(monkeypatch, module, *args) == 0
    assert expected in capsys.readouterr().out


def test_check_blame_ignore_revs(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # Against a ref that doesn't exist, as whether this repo's entries pass
    # against HEAD depends on how much history the checkout has; this way the
    # target argument reaches main() too.
    monkeypatch.chdir(ROOT)
    assert run_as_main(monkeypatch, "scripts.check_blame_ignore_revs", "no-such-ref") == 1
    assert "Problems in .git-blame-ignore-revs" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("module", "args", "usage"),
    [
        ("scripts.check_blame_ignore_revs", ["a", "b"], "[<target>]"),
        ("scripts.dependabot_relock", [], "<base-ref>"),
        ("scripts.requirements_min_versions", ["neither"], "[check|fix]"),
    ],
)
def test_usage_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], module: str, args: list[str], usage: str
) -> None:
    assert run_as_main(monkeypatch, module, *args) == 2
    assert usage in capsys.readouterr().err


@pytest.mark.parametrize(
    ("module", "args"),
    [
        ("scripts.post_exolot_findings", ["1"]),
        ("scripts.post_patch_coverage", ["1", "--report"]),
        ("scripts.post_screenshots", ["1", "--dir"]),
    ],
)
def test_comment_posters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    module: str,
    args: list[str],
) -> None:
    # A missing report fails before anything talks to GitHub.
    monkeypatch.setenv("GITHUB_TOKEN", "unused")
    monkeypatch.setenv("SCREENSHOTS_TOKEN", "unused")
    assert run_as_main(monkeypatch, module, *args, str(tmp_path / "missing")) == 1
    assert "missing" in capsys.readouterr().err


def test_claude_review_request(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("@claude review"))
    assert run_as_main(monkeypatch, "scripts.claude_review_request") == 0
    assert capsys.readouterr().out == "review\n"


def test_recreate_pr(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # An invalid URL fails before anything runs gh.
    assert run_as_main(monkeypatch, "scripts.recreate_pr", "https://github.com/o/r/issues/1") == 1
    assert "Invalid GitHub PR URL" in capsys.readouterr().err


class _Response(io.BytesIO):
    """A urlopen response: its body, and the status the scripts read."""

    status = 200


def _respond(monkeypatch: pytest.MonkeyPatch, url_prefix: str, body: object) -> None:
    """Make urlopen answer requests under url_prefix with a 200 and body as JSON."""

    def urlopen(request: urllib.request.Request, **kwargs: Any) -> _Response:
        assert request.full_url.startswith(url_prefix)
        return _Response(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


def test_check_deploy(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _respond(monkeypatch, "https://app.example/", {"status": "ok", "commit": "abc"})
    assert run_as_main(monkeypatch, "scripts.check_deploy", "https://app.example/healthz/", "abc") == 0
    assert "healthy, running abc" in capsys.readouterr().out


def test_wait_for_base_coverage(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _respond(monkeypatch, "https://api.codecov.io/", {"totals": {"coverage": 98.5}})
    assert run_as_main(monkeypatch, "scripts.wait_for_base_coverage", "o/r", "abc") == 0
    assert "Codecov has the base report for abc (coverage 98.5%)" in capsys.readouterr().out
