import io
import sys

import pytest

from scripts.claude_review_request import is_review_request, main


@pytest.mark.parametrize(
    "text",
    [
        "@claude review",
        "@claude review\n",
        "  @claude review  ",
        "@claude review please",
        "@claude review.",
        "@claude review, focus on the regex",
        "@claude review! now?",
        "@claude review;",
        "@claude review:",
        "@Claude Review",
        "@CLAUDE REVIEW",
        "@claude  review",
        "@claude\treview",
        "@claude\nreview",
        "@claude\r\nreview",
        "Looks fine.\r\n\r\n@claude review",
        "first line\n@claude review",
        "please @claude review this",
        # A blockquote's "> " leaves whitespace before it, so quoting it this way counts.
        "> @claude review",
        # Unicode whitespace: no-break space, ideographic space, line separator.
        "@claude\u00a0review",
        "x\u3000@claude review",
        "@claude review\u2028more",
    ],
)
def test_review_requests(text: str) -> None:
    assert is_review_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "@claude",
        "@claude fix this",
        "@claudereview",
        "@claude reviewing this",
        "@claude reviewer",
        "@claude review-ish",
        "@claude review)",
        "foo@claude review",
        "`@claude review`",
        ">@claude review",
        "(@claude review)",
        '"@claude review"',
        "@claude, review",
        "@claude-review",
        # Not whitespace: zero-width space and byte order mark.
        "@claude\u200breview",
        "\ufeff@claude review",
    ],
)
def test_not_review_requests(text: str) -> None:
    assert not is_review_request(text)


@pytest.mark.parametrize(("text", "expected"), [("@claude review", "review"), ("@claude fix it", "other")])
def test_main(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], text: str, expected: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    assert main() == 0
    assert capsys.readouterr().out == f"{expected}\n"
