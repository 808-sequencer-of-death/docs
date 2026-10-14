# FedRAMP knowledge base: house style and agent rules

This repo is a Mintlify site for cloud service provider (CSP) clients. It explains the FedRAMP Consolidated Rules for 2026 and what FedRAMP staff say in the FedRAMP community discussions. Readers are CSP executives who need the bottom line and engineers who need to know exactly what to build or produce.

## Repo layout

| Path | What it is | Who edits it |
| --- | --- | --- |
| `upstream/` | Exact mirror of [FedRAMP/2026-markdown](https://github.com/FedRAMP/2026-markdown). Source of truth for rule text. | Automation only. **Never edit.** |
| `data/discussions/` | Output of `scripts/fedramp_discussions_sync.py`: `records/<n>.json`, `index.jsonl`, `changes/<date>.jsonl`, `state.sqlite`. | Automation only. **Never edit.** |
| `rules/` | Rule summaries. `rules/<slug>.mdx` summarizes `upstream/reference/<slug>.md`. | Writers and the update agent |
| `discussions/` | Discussion digests, with FedRAMP staff positions called out. | Writers and the update agent |
| `changelog.mdx` | "What changed", newest entry first. | Writers and the update agent |
| `docs.json` | Mintlify config and navigation. | Writers and the update agent |
| `scripts/`, `.github/` | Sync script and workflows. | Humans only |

`upstream/` and `data/` are listed in `.mintignore`, so they never render on the site.

## Non-negotiable content rules

1. **Never invent guidance.** Every statement about what a rule requires must come from `upstream/` or from a FedRAMP staff post in `data/discussions/`. If the sources don't answer a question, say "The rules don't address this" and link the closest official source. Don't fill gaps with general compliance knowledge, best practice or your own opinion.
2. **Always cite and link.** Name the rule ID (for example `VDR-CSO-DET`) and link the official page. Name the discussion number and link the exact thread or comment.
3. **Keep the force exact.** MUST, MUST NOT, SHOULD, SHOULD NOT and MAY keep their FedRAMP meaning. Write them in capitals, and never soften a MUST or harden a SHOULD. When the force varies by class, say so and give each class's force.
4. **Separate staff from community.** Only posts where `is_official` is `true` in `data/discussions/records/<n>.json` can be FedRAMP positions. The flag is a heuristic (org membership, or a login ending in `-gov`), so a flagged post that only asks a question isn't a position. Community comments are context, never guidance. Staff positions in a discussion don't change the published rule; if they conflict, say so and treat the published rule as governing.
5. **Summaries aren't the rules.** Each rule page opens with the source callout (see the template) so readers know the official text governs.
6. **Dates and versions.** Use absolute dates (`2026-12-07`, not "in two months"). Record the upstream release (the top entry of `upstream/changelog.md`) in each page's source callout.

## Linking

- **Official rule pages:** `upstream/<path>.md` is published at `https://www.fedramp.gov/2026/<path>/`. For example, `upstream/reference/vulnerability-detection-and-response.md` is at `https://www.fedramp.gov/2026/reference/vulnerability-detection-and-response/`.
- **A specific rule:** add the anchor, which is the rule's name in lowercase with spaces turned into hyphens and punctuation dropped. For example, `AFC-CSO-INB` ("Maintain a FedRAMP Security Inbox") is at `https://www.fedramp.gov/2026/reference/addressing-fedramp-communication/#maintain-a-fedramp-security-inbox`. Copy anchors from the `[... ](file.md#anchor)` links already in `upstream/` when you can.
- **Discussions:** use the `url` field from the record. For a staff comment, use that comment's `url`, not the thread URL.
- **Internal links:** root-relative without the extension, for example `/rules/vulnerability-detection-and-response`.

## Writing style

- Write in plain English, active voice and second person ("you must…"). The reader is the CSP.
- Use one idea per sentence. Define FedRAMP terms on first use or link `https://www.fedramp.gov/2026/definitions/`.
- Lead with the bottom line for executives, then give engineers the detail.
- Use sentence case for headings, bold for UI elements, and code format for rule IDs, file names and paths.
- Use tables for lists of rules (`Rule | Force | What it means for you`).
- Skip marketing language and filler, and don't use emoji.
- Use Mintlify components only where they help: `<Note>`, `<Warning>`, `<Info>`, `<Tip>`, `<Card>`, `<CardGroup>`, `<Accordion>`, `<Update>`.
- MDX is strict. Escape `<`, `>`, `{` and `}` in prose (write "less than", or use code formatting). Close every tag.

## Page templates

### Rule summary (`rules/<slug>.mdx`)

```mdx
---
title: "Vulnerability detection and response"
description: "One sentence on what this ruleset makes you do."
sidebarTitle: "VDR: Vulnerability detection"
---

<Info>
  **Official source:** [Vulnerability Detection and Response](https://www.fedramp.gov/2026/reference/vulnerability-detection-and-response/) (`VDR`), FedRAMP Consolidated Rules release `2026.10.14.01`. This page is a plain-English summary. The official rule text governs.
</Info>

## In short
2–4 sentences for an executive.

## Who it applies to
Types (20x, Rev5), paths, classes, audience, straight from the upstream applicability block.

## Key dates
Table of the effective dates from the upstream "Effective Date(s)" block.

## What you need to do
One table per subset: `| Rule | Force | What it means for you |`. Link every rule ID to its anchor.

## Differences by class
Only if the force or wording varies by class.

## What FedRAMP staff have said
Staff positions from discussions that cite these rule IDs, each linked to the comment. Leave this section out if there are none.

## Related
Links to related summaries and digests.
```

### Discussion digest (`discussions/<slug>.mdx`)

- `<Info>` callout naming the thread(s), with links and a "last reviewed" date.
- **Bottom line:** 2–4 sentences.
- **FedRAMP staff positions:** each in a `<Note>` with the staff author's login, the date and a link to the comment, paraphrased closely. Quote only short phrases.
- **Open questions:** questions still unanswered in the thread.
- **What it means for you:** practical implications, tied to rule IDs, and only what the sources support.

### Changelog entry (`changelog.mdx`)

Add new entries at the top, below the frontmatter:

```mdx
<Update label="2026-10-14" description="Upstream 2026.10.14.01">
  - **Rules:** what changed and which pages were updated, with links.
  - **Discussions:** new or updated threads and staff positions, with links.
</Update>
```

## Update agent procedure

When automation asks you to process a change:

1. Read the diff of `upstream/` and the listed `data/discussions/changes/*.jsonl` lines, then the matching `records/<n>.json` files.
2. Ignore cosmetic upstream changes (whitespace, build metadata, `_sources.json`, `.generated-markdown.json`), unless they're the only change. Then add only a short changelog line.
3. Update the affected `rules/` and `discussions/` pages, keeping their existing structure. Create a page only for a new ruleset or a significant new thread, and add it to `docs.json` navigation.
4. Add one changelog entry for the run.
5. Never touch `upstream/`, `data/`, `scripts/` or `.github/`.
</content>
