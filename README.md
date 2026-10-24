# FedRAMP 2026 Knowledge Base

A Mintlify site that explains the FedRAMP Consolidated Rules for 2026 and FedRAMP community discussions in plain English for cloud service providers. Mintlify deploys automatically from `main`.

## How it stays current

```
Nightly sync (07:17 UTC, no AI)                Update docs (Claude)
─────────────────────────────                  ─────────────────────
mirror FedRAMP/2026-markdown → upstream/       read the diff + changed discussion records
pull FedRAMP/community → data/discussions/  →  edit rules/, discussions/, changelog.mdx
nothing changed? stop                          check the Mintlify build
changed? commit to main, start Update docs     open a PR for review (never pushes to main)
```

- `.github/workflows/sync.yml` runs nightly, or from **Actions → Nightly sync → Run workflow**. It runs a full discussions rescan on Sundays, or when you tick **Full discussions rescan**.
- `.github/workflows/update-docs.yml` is started by the sync. You can also run it from **Actions → Update docs → Run workflow**. Leave the inputs blank to process the latest commit on `main`. Each run uses Claude Sonnet 5.5, capped at 150 turns and $5.

## Layout

| Path | Contents |
| --- | --- |
| `upstream/` | Exact mirror of [FedRAMP/2026-markdown](https://github.com/FedRAMP/2026-markdown). Never edit it by hand. |
| `data/discussions/` | JSON from `scripts/fedramp_discussions_sync.py`. Never edit it by hand. |
| `rules/` | Rule summaries, one page per ruleset |
| `discussions/` | Discussion digests, with FedRAMP staff positions |
| `changelog.mdx` | "What changed" |
| `CLAUDE.md` | House style and rules for the update agent |

`upstream/` and `data/` are excluded from the site by `.mintignore`.

## One-time setup

1. **Add the secret.** In GitHub, go to **Settings → Secrets and variables → Actions → New repository secret**. Set **Name** to `ANTHROPIC_API_KEY` and **Secret** to a key from [console.anthropic.com](https://console.anthropic.com/settings/keys). No other secrets are needed; the workflows use GitHub's built-in token.
2. **Let workflows write and open PRs.** Go to **Settings → Actions → General → Workflow permissions** and select **Read and write permissions**. Then tick **Allow GitHub Actions to create and approve pull requests** and click **Save**.
3. **Connect Mintlify.** In the [Mintlify dashboard](https://dashboard.mintlify.com), install the GitHub app on this repository and set the deploy branch to `main`.
4. **Recommended:** set a monthly spend limit for the API key's workspace in the Anthropic Console.

## Notes

- PRs opened by the workflow don't trigger other GitHub Actions workflows (a GitHub rule for the built-in token). The Update docs workflow runs the Mintlify build check itself and reports the result in the PR.
- If you protect `main` with a branch rule, allow `github-actions[bot]` to push, or the nightly data commit will fail.
