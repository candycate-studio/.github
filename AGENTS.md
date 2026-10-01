# .github — CandyCate organization profile and shared GitHub files

## Codex studio context — read explicitly at session start

This repo belongs to CandyCate; studio canon lives in `candycate-studio/meta`. Read the studio checkpoint
`MIGRATION-PLAYBOOK.md` §5 first, then `COMPANY.md`, `STANDARDS/git-flow.md`,
`STANDARDS/versioning.md`, `STANDARDS/session-pipeline.md`,
`STANDARDS/doc-verification.md`, `STANDARDS/artifact-canonicality.md` and
`STANDARDS/secrets.md`.
Locate the actual CandyCate workspace and its `meta` checkout; a temporary checkout
may be elsewhere. If meta is unavailable locally, fetch each canonical file from
GitHub, for example `gh api repos/candycate-studio/meta/contents/COMPANY.md -H 'Accept: application/vnd.github.raw+json' --method GET -f ref=main`.
Treat `meta/…` references as paths inside that named repository, not this repo.
Read relevant repo state and task docs listed below after studio context.

Codex discovers `AGENTS.md` within its project instruction chain; do not assume
workspace/domain files above this Git root loaded automatically. `@file` syntax
is a Claude compatibility import, not an automatic Codex import: read referenced
files explicitly. Claude skills, agents and hooks remain product/tooling assets;
slash commands run only when the corresponding installed capability is available.
Otherwise use the actual scripts/CLI documented by the capability, and report
unavailable checks. Do not claim hook protection without checking this clone.
Before changes inspect Git status; work in a feature branch, preserve unrelated
work, use Conventional Commits without Co-Authored-By, and follow `STANDARDS/session-pipeline.md` for PR scope and studio Git flow.


## Repository scope

Read `README.md`, `profile/README.md` and only the workflows affected by the task.
This repository holds the organization profile, community-health files and dynamic
profile widgets. `scripts/org_activity.py` uses Python standard library only.
`profile/assets/org-activity.svg` and `profile/assets/metrics.svg` are generated
by `.github/workflows/org-activity.yml` and `metrics.yml`; avoid hand-editing them.
`ORG_METRICS_TOKEN` is an environment/Actions secret: never commit or print it.
Without the token both workflows intentionally skip generation; a no-op is not
evidence that authenticated API access or widget generation works.
For Python edits validate syntax without executing network or publishing actions.
Human task changes follow studio Git flow; existing widget automation is a
separate mechanism and does not authorize direct pushes by an agent.
