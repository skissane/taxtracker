"""Decides which Claude workflow, if any, a GitHub comment or review is for.

Reads the comment's text from stdin and prints ``review`` if it asks for
"@claude review", ``other`` if it's some other request to @claude, or
``none`` if it isn't one at all. ``claude-code-review.yml`` runs only for
``review``, and ``claude.yml`` for ``other`` (and for ``review`` on a plain
issue, which has no PR to review), so the two workflows share one rule and
every request goes to exactly one of them.

A request must start the text, with only whitespace before it, so quoting
"@claude" mid-sentence, in backticks or in a blockquote never starts a run.

Workflow expressions have no regex (only ``contains``, ``startsWith`` and
``endsWith``), hence this script. The workflows run it with the runner's own
``python3``, before anything is installed, so it must run on the Ubuntu
runner image's Python as well as ours.
"""

import re
import sys

# "@claude" as a word of its own, at the start (after any whitespace), and
# then followed by whitespace, punctuation that ends a phrase or the end:
# "@claude, fix this" or "@claude." count, "@claudebot" doesn't.
# On str patterns, \s is Unicode whitespace (as str.isspace()), and
# IGNORECASE matches contains(), which "@Claude" used to satisfy.
_END = r"(?=$|\s|[.,!?;:])"
REQUEST = re.compile(r"\s*@claude" + _END, re.IGNORECASE)
# The same, with "review" as the next word: "@claude review." or
# "@claude review, focus on X", but not "@claude reviewing".
REVIEW_REQUEST = re.compile(r"\s*@claude\s+review" + _END, re.IGNORECASE)


def classify(text: str) -> str:
    if REVIEW_REQUEST.match(text):
        return "review"
    if REQUEST.match(text):
        return "other"
    return "none"


def main() -> int:
    print(classify(sys.stdin.read()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
