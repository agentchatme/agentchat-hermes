#!/usr/bin/env python3
"""The PR description must say how the change was verified.

Pull requests auto-merge when CI is green, and any agent or human may open
one. This check makes sure nobody forgets to say how a change was verified:
the description needs a `## Verification` section with three filled lines,
`Tests:`, `Review:` and `Live check:` (.github/pull_request_template.md). It
checks only that each line says something; it does not judge what it says.

Run by .github/workflows/pr-verification.yml. Reads the pull_request event
payload ($GITHUB_EVENT_PATH), prints what is missing and writes the same to
the job summary ($GITHUB_STEP_SUMMARY). Exit 0: filled in, or opened by a
bot. Exit 1: something is missing. Exit 2: no pull request in the payload.
Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys

BOT_AUTHORS = frozenset({"dependabot[bot]", "github-actions[bot]"})

LABELS = ("Tests", "Review", "Live check")

RULE = (
    "PRs auto-merge on green. Say how this change was verified: Tests, "
    "Review (independent review, or 'not needed: reason'), Live check "
    "(or 'n/a: reason'). Edit the PR description; this check re-runs."
)

EXAMPLE = """\
## Verification

- Tests: <!-- what ran, with counts -->
- Review: <!-- independent review and its verdict, or "not needed: <reason>" -->
- Live check: <!-- what was checked after release or deploy, or "n/a: <reason>" -->

Write after each comment; the comments themselves count as empty."""

# An HTML comment, or an unterminated one, which hides the rest of the text.
_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
# An ATX heading: up to three spaces, 1-6 '#', the text, optional closing '#'s.
_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
# A labelled line: optional list marker, optional emphasis around the label,
# the label, then ':' (bold may close before or after the colon).
_LABEL = re.compile(
    r"^[ \t]*(?:[-*+]|\d{1,9}[.)])?[ \t]*[*_]{0,3}[ \t]*"
    r"(tests|review|live[ \t-]*check)"
    r"[ \t]*[*_]{0,3}[ \t]*:(.*)$",
    re.IGNORECASE,
)
_WORD = re.compile(r"\w")


def _canonical_label(raw: str) -> str:
    raw = raw.lower()
    if raw.startswith("live"):
        return "Live check"
    return "Tests" if raw == "tests" else "Review"


def _heading(line: str) -> tuple[int, str] | None:
    match = _HEADING.match(line)
    if not match:
        return None
    text = (match.group(2) or "").strip().strip("*_ \t").rstrip(":").strip()
    return len(match.group(1)), text.lower()


def _verification_section(lines: list[str]) -> list[str] | None:
    """The lines under the Verification heading, up to the next heading of
    the same or a higher level; None when there is no such heading."""
    for start, line in enumerate(lines):
        heading = _heading(line)
        if heading and heading[1] == "verification":
            level = heading[0]
            section: list[str] = []
            for following in lines[start + 1 :]:
                other = _heading(following)
                if other and other[0] <= level:
                    break
                section.append(following)
            return section
    return None


def _filled(value: str) -> bool:
    """Says something: a letter or digit left once comments are gone (bold
    markers, list dashes and whitespace alone are nothing)."""
    return bool(_WORD.search(value.replace("_", "")))


def problems(body: str | None) -> list[str]:
    """What the description is missing; empty when it is filled in."""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _COMMENT.sub("", text)
    section = _verification_section(text.split("\n"))
    if section is None:
        return ["The `## Verification` heading is missing."]

    # Each label's text: the rest of its line, plus the lines below it up to
    # the next label (a value may sit on the lines under the label).
    values: dict[str, list[list[str]]] = {}
    current: list[str] | None = None
    for line in section:
        match = _LABEL.match(line)
        if match:
            current = [match.group(2)]
            values.setdefault(_canonical_label(match.group(1)), []).append(current)
        elif current is not None:
            current.append(line)

    found: list[str] = []
    for label in LABELS:
        entries = values.get(label)
        if not entries:
            found.append(f"The `{label}:` line is missing.")
        elif not any(_filled("\n".join(entry)) for entry in entries):
            found.append(f"`{label}:` is empty (only a placeholder or whitespace).")
    return found


def _summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")


def main() -> int:
    try:
        with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
            event = json.load(handle)
        pull = event["pull_request"]
    except (KeyError, OSError, ValueError, TypeError) as error:
        print(f"::error title=PR verification::No pull request in the event payload ({error!r}).")
        return 2

    author = ((pull.get("user") or {}).get("login")) or ""
    if author in BOT_AUTHORS:
        message = f"Opened by {author}: no Verification section needed."
        print(message)
        _summary(f"### PR verification: passed\n\n{message}")
        return 0

    found = problems(pull.get("body"))
    if not found:
        message = "The Verification section is filled in: Tests, Review, Live check."
        print(message)
        _summary(f"### PR verification: passed\n\n{message}")
        return 0

    print("PR verification failed. In the PR description:")
    for problem in found:
        print(f"  - {problem}")
    print()
    print(RULE)
    print()
    print("The section (the template's Verification section):")
    print(EXAMPLE)
    print(f"::error title=PR verification::{' '.join(found)} {RULE}")
    _summary(
        "### PR verification: failed\n\n"
        + "\n".join(f"- {problem}" for problem in found)
        + f"\n\n{RULE}\n\nThe section (the template's Verification section):\n\n```markdown\n{EXAMPLE}\n```"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
