"""Runs each script's ``if __name__ == "__main__":`` block, as CI's
``python scripts/<name>.py`` or ``python -m scripts.<name>`` would, so that
coverage measures them and a mistake there (e.g. passing ``sys.argv`` rather
than ``sys.argv[1:]``, or dropping ``main()``'s exit code) fails a test.

One file for every script, rather than a test in each script's own test file,
as they share the runner and are one idea (scripts/ has only check_deploy.py
now that repoutils has the rest, but the next script gets a case here too).
Each case picks arguments for which ``main()`` returns rather than exits itself
(as argparse's ``--help`` does), so the guard's own lines run, and which need no
network and change nothing. Ported from skissane/exolot.
"""

import io
import json
import runpy
import sys
import urllib.request
from typing import Any

import pytest


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
