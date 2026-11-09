"""Decides whether a GitHub comment or review asks for "@claude review".

Reads the comment's text from stdin and prints ``review`` if it does, else
``other``. ``claude-code-review.yml`` runs only for ``review``, and
``claude.yml`` only for ``other``, so the two workflows share one rule and
every comment goes to exactly one of them.

Workflow expressions have no regex (only ``contains``, ``startsWith`` and
``endsWith``), hence this script. The workflows run it with the runner's own
``python3``, before anything is installed, so it must run on the Ubuntu
runner image's Python as well as ours.
"""

import re
import sys

# "@claude" and "review" as words of their own, separated by whitespace:
# starting the text or after whitespace, and ending the text or followed by
# whitespace or punctuation that ends a phrase ("@claude review." or
# "@claude review, focus on X"). So quoting it in backticks or parentheses,
# or writing "@claude reviewing" or "foo@claude review", doesn't count.
# On str patterns, \s is Unicode whitespace (as str.isspace()), and
# IGNORECASE matches contains(), which "@Claude Review" used to satisfy.
REVIEW_REQUEST = re.compile(r"(?:^|\s)@claude\s+review(?=$|\s|[.,!?;:])", re.IGNORECASE)


def is_review_request(text: str) -> bool:
    return REVIEW_REQUEST.search(text) is not None


def main() -> int:
    print("review" if is_review_request(sys.stdin.read()) else "other")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
