#!/usr/bin/env bash
# Opens, updates or closes the one issue that says the nightly hermes-main run
# is red. Run by .github/workflows/hermes-main.yml (job `report`), on
# scheduled runs only, with the workflow's GITHUB_TOKEN — no other secret.
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

issue="$(open_issue)"

if [[ "$REFS_RESULT" == success && "$SUITE_RESULT" == success ]]; then
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
  legs
  echo
  echo "- A red \`main\` leg: Hermes main changed something this plugin uses; the next Hermes release would break it."
  echo "- A red \`latest release\` leg: this plugin's main fails on the Hermes release users run now."
  echo "- A red \`Resolve hermes-agent refs\`: the latest release could not be looked up, so nothing was tested."
  echo
  echo "What each Hermes surface is for: \`docs/hermes-coupling.md\`. Run: $RUN_URL"
  echo
  echo "Every red nightly comments here; the next green one closes this issue (\`.github/workflows/hermes-main.yml\`)."
} > "$body"

if [[ -n "$issue" ]]; then
  gh issue comment "$issue" --body-file "$body"
  echo "Red: updated #$issue."
else
  gh issue create --title "$TITLE" --body-file "$body"
  echo "Red: opened the nightly issue."
fi
