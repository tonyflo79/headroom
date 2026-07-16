# Issue tracker: GitHub

Issues and PRDs for this project live in the writable fork at `tonyflo79/headroom`. Use the GitHub CLI for issue operations and pass `--repo tonyflo79/headroom` when the target might otherwise be ambiguous.

The source project at `domanski-ai/headroom` is the upstream repository. Keep `upstream` read-only, develop on branches pushed to `origin`, and open upstream pull requests only after the work passes the fork's release gates.

## Conventions

- Create an issue with `gh issue create --repo tonyflo79/headroom --title "..." --body-file <file>`.
- Read an issue with `gh issue view <number> --repo tonyflo79/headroom --comments`.
- List issues with `gh issue list --repo tonyflo79/headroom --state open` plus the labels and JSON fields needed for the task.
- Comment with `gh issue comment <number> --repo tonyflo79/headroom --body "..."`.
- Apply or remove labels with `gh issue edit <number> --repo tonyflo79/headroom --add-label "..."` or `--remove-label "..."`.
- Close an issue with `gh issue close <number> --repo tonyflo79/headroom --comment "..."`.

## Publishing

When a skill says to publish a PRD or issue to the project tracker, create a GitHub issue in `tonyflo79/headroom`.

When code is ready for review, push its branch to `origin`. Use a pull request in the fork for delivery integration; create a pull request to `domanski-ai/headroom` only when explicitly approved for upstream submission.
