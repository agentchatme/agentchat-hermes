#!/usr/bin/env bash
# Opens, updates or closes the one issue that says the nightly hermes-main run
# is red. Run by .github/workflows/hermes-main.yml (job `report`), on
# scheduled runs (and dispatches with report_issue), with the workflow's
# GITHUB_TOKEN — no other secret.
#
# The issue is the open issue with the title below, created by
# github-actions[bot]:
#   green run (every job succeeded) → comment and close it;
#   anything else                   → comment on it with each leg's result,
#                                     or open it.
# A leg that timed out or lost its runner can report `cancelled`; that is
# red. A run that was itself cancelled (a newer run superseded it) never
# gets here: the job runs only when `!cancelled()`.
#
# A red night must not lose its alert to an API hiccup: when the open issue
# cannot be looked up, a new one is opened; when the job list cannot be
# read, the issue says so. A green night that cannot look up the issue
# fails this job, so the run shows red instead of leaving a stale issue.
#
# Environment: GH_TOKEN (issues: write, actions: read), GH_REPO, RUN_ID,
# RUN_URL, REFS_RESULT and SUITE_RESULT (the jobs' results: success,
# failure, cancelled or skipped).
set -euo pipefail

TITLE="Nightly: the suite fails against hermes-agent"

open_issue() {
  gh api -X GET "repos/$GH_REPO/issues" -f state=open -f creator='github-actions[bot]' -f per_page=100 --paginate \
    | jq -rs --arg title "$TITLE" '[.[][] | select(.pull_request == null and .title == $title) | .number] | first // empty'
}

legs() {
  # filter=latest: each job's newest attempt, so a re-run still lists every leg.
  gh api "repos/$GH_REPO/actions/runs/$RUN_ID/jobs?filter=latest&per_page=100" --paginate \
    | jq -rs '.[].jobs[] | select(.name | test("^(Resolve hermes-agent refs|Suite vs )")) | "- \(.name): **\(.conclusion // .status)**"'
}

lookup_ok=1
issue="$(open_issue)" || { echo "::warning::could not look up the open nightly issue"; issue=""; lookup_ok=0; }

if [[ "$REFS_RESULT" == success && "$SUITE_RESULT" == success ]]; then
  if [[ "$lookup_ok" == 0 ]]; then
    echo "::error::green, but the open nightly issue could not be looked up to close it"
    exit 1
  fi
  if [[ -n "$issue" ]]; then
    gh issue comment "$issue" --body "Green again: every leg passed in $RUN_URL. Closing."
    gh issue close "$issue" --reason completed
    echo "Green: closed #$issue."
  else
    echo "Green: no open nightly issue."
  fi
  exit 0
fi

body="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/nightly-issue.md"
{
  echo "The nightly run of this package's suite against hermes-agent is red."
  echo
  legs || echo "- (the job list could not be read; see the run)"
  echo
  # One echo per paragraph: GitHub shows a line break inside an issue paragraph as a break.
  echo "A red leg means a test failed with that Hermes installed; read the failing tests in the run. Often Hermes changed something this plugin uses (on \`main\`: the next Hermes release would break it; on \`latest release\`: the release users run now); sometimes the plugin itself is red (a test of its own, such as the dated legacy-settings test). A leg named \`unresolved\`: the latest release could not be looked up."
  echo
  echo "What each Hermes surface is for: \`docs/hermes-coupling.md\`. Run: $RUN_URL"
  echo
  echo "Every red scheduled run comments here; the next green scheduled run closes this issue (\`.github/workflows/hermes-main.yml\`)."
} > "$body"

if [[ -n "$issue" ]]; then
  gh issue comment "$issue" --body-file "$body"
  echo "Red: updated #$issue."
else
  gh issue create --title "$TITLE" --body-file "$body"
  echo "Red: opened the nightly issue."
fi
