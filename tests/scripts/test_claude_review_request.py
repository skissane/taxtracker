import io
import sys

import pytest

from scripts.claude_review_request import classify, main


@pytest.mark.parametrize(
    "text",
    [
        "@claude review",
        "@claude review\n",
        "  @claude review  ",
        "\n\n@claude review",
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
        # Unicode whitespace: no-break space, ideographic space, line separator.
        "@claude\u00a0review",
        "\u3000@claude review",
        "@claude review\u2028more",
    ],
)
def test_review_requests(text: str) -> None:
    assert classify(text) == "review"


@pytest.mark.parametrize(
    "text",
    [
        "@claude",
        "@claude\n",
        "  @claude fix this",
        "@claude, fix this",
        "@claude.",
        "@Claude please",
        "@claude reviewing this",
        "@claude reviewer",
        "@claude review-ish",
        "@claude review)",
        "@claude, review",
    ],
)
def test_other_requests(text: str) -> None:
    assert classify(text) == "other"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "@claudereview",
        "@claude-review",
        "@claudebot fix this",
        "foo@claude review",
        "please @claude review this",
        "Looks fine.\n\n@claude review",
        "thanks, @claude fix this",
        "`@claude review`",
        "` @claude review `",
        "> @claude review",
        ">@claude review",
        "(@claude review)",
        '"@claude review"',
        # Not whitespace: zero-width space and byte order mark.
        "@claude\u200breview",
        "\ufeff@claude review",
    ],
)
def test_not_requests(text: str) -> None:
    assert classify(text) == "none"


@pytest.mark.parametrize(
    ("text", "expected"), [("@claude review", "review"), ("@claude fix it", "other"), ("hello", "none")]
)
def test_main(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], text: str, expected: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    assert main() == 0
    assert capsys.readouterr().out == f"{expected}\n"
