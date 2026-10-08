# Mote release -> Discord #updates announcer

Self-contained, stdlib-only. Nothing here touches `hovernote`; nothing was pushed anywhere.

```
.github/workflows/discord-release-announce.yml   workflow (release + workflow_dispatch)
scripts/announce_release.py                      the whole implementation (stdlib only)
tests/test_announce_release.py                   44 unit tests, no network
fixtures/raw/v*.md                               real release bodies pulled via gh (test data)
fixtures/event-release-published.json            real `release: published` payload (CLI test)
```

Copy `.github/` and `scripts/` into the repo root of `marcelvict/mote-releases` on `main`.
`tests/` and `fixtures/` are optional in the repo (nothing runs them in CI as configured).

## Behaviour

| Trigger | Result |
| --- | --- |
| `release: published`, stable tag (`v1.5.6`, `v1.5.6.1`) | posts the notes, pings `@everyone` once |
| `release: published`, prerelease / draft / odd tag | logs `skipped: ...`, posts nothing, exits 0 |
| `workflow_dispatch` mode `dry_run` (default) | renders the exact messages into the job log, posts nothing |
| `workflow_dispatch` mode `test_connection` | posts one short `-#` note with `flags: 4\|4096` (no embeds, no notifications), then deletes it. No ping, no release content |
| `workflow_dispatch` mode `send_silent` | posts the real notes with no ping and **no receipt**, so the real pinged send is not blocked |
| `workflow_dispatch` mode `send` | posts the real notes with the ping |

Stable = `prerelease: false`, not a draft, **and** a purely numeric tag. `v1.5.6-rc1`,
`v1.5.6rc1`, `v1.5.6+build.1`, `nightly` are all skipped. Four-part hotfix tags like
`v1.5.6.1` are stable and do get the ping, per the brief.

Content rules, all enforced in `clean_notes()`:
- **Current version only, fail closed.** Bodies are cumulative (`## Mote 1.5.4 Release Notes`
  then `## Mote 1.5.3 Release Notes` ...). Only the section whose heading matches the tag's
  version exactly is posted. `1.5.6` never matches `1.5.6.1`. Non-version headings such as
  `## What's Inside` are not section boundaries. Bodies with no version heading at all (v1.5.5,
  v1.5.6, v1.5.6.1) are this version's notes by construction and are used whole. If the body
  **does** carry version headings but none matches the tag, the run **errors and posts nothing** —
  guessing the topmost section would mean announcing a historical release to `@everyone`. The
  error names the headings it found and the heading it wanted.
- **No download links.** Markdown and bare URLs matching `releases/download/`, `releases/latest`,
  `/download`, `.dmg`, `.zip`, `.pkg`, `appcast.xml` are deleted, label and all. Other links keep
  their label and get wrapped in `<...>`; every message also carries flag `4` (SUPPRESS_EMBEDS),
  so there are no link previews.
- **Mentions can't be hijacked.** `@everyone` / `@here` in a body lose their `@`, role and user
  mentions are deleted, other `@handles` get a zero-width space. On top of that,
  `allowed_mentions` is explicit: `{"parse":["everyone"],"roles":[],"users":[]}` on message 1 only,
  `{"parse":[],"roles":[],"users":[]}` on every later message.
- **Splitting.** 2000-char limit with a 120-char reserve; splits on blank lines, then line breaks,
  then a hard cut. An open ``` fence is closed and reopened across a split. Every non-blank line
  appears exactly once (asserted in tests). Real worst case today: v1.5.0 = 6 messages.
- Every message ends with a deterministic marker: `-# Mote release <release id>` plus
  `- part i/n` when split.

Delivery: each POST uses `?wait=true`, so Discord confirms persistence before the next message
goes out (that is also what keeps the order). 429 honours `retry_after`; 5xx retries with backoff.

Secrets: the webhook is read from `DISCORD_WEBHOOK_URL` and never printed. All errors go through
`redact()`, which strips the URL, its token tail, and any `…/api/webhooks/…` shaped string.
Tracebacks are never printed (the top-level handler prints one redacted line). GitHub's own secret
masking is the second layer, not the only one.

## Dedup: what it does and what it cannot do

A Discord **webhook token cannot read channel history** — there is no `GET /messages` for webhooks.
So the channel cannot be scanned for "did I already post release 407226090?". Options considered:

- **Chosen: run-artifact receipts.** After a successful pinged send the script writes
  `receipt.json` and the workflow uploads it as an artifact named `discord-receipt-<release id>`.
  Before sending, the script asks the Actions API whether an artifact with that name already
  exists; if it does, it skips. No hosted state, no commits to the repo, no third-party service.
- Rejected: committing a marker file to the repo (needs `contents: write`), `actions/cache`
  (extra action, same expiry problem), any external KV.

Limitations, stated plainly:
1. **Partial failures still duplicate.** Receipts are per release, not per message. If message 4
   of 6 fails, the re-run starts again at message 1. Fix by hand (delete the stragglers) — the
   `-# Mote release <id> - part i/n` marker tells you exactly which ones to delete.
2. **Retention is the dedup window.** `retention-days: 90` (capped by the repo setting). After
   expiry, re-running an old release would repost it.
3. **Fail closed.** If the artifact lookup errors (token scope, API hiccup), the script raises and
   posts nothing: it cannot tell whether the release was already announced, and a duplicate
   `@everyone` ping is worse than a late one. Fix the cause and re-run the job — a release that
   really was already sent will then be skipped by its receipt.
4. **Not atomic.** Two runs started within the same second could both pass the check. The
   `concurrency` group (keyed on `github.event.release.tag_name || inputs.tag`, so an automatic
   run and a manual run for the same release share one queue) makes that effectively impossible.
5. `send_silent` deliberately writes no receipt, so a test send never suppresses the real one.

## Deployment considerations (for the parent to action)

1. **Secret name.** The workflow reads `secrets.DISCORD_UPDATES_WEBHOOK` in
   `marcelvict/mote-releases`. Already created per the parent.
2. **Default branch.** The file must be on `main` for `workflow_dispatch` to appear in the UI, and
   `release:` workflows always run the version on the default branch.
3. **Who publishes the release matters — this is the main trap.** A release published by a
   workflow using that repo's own `secrets.GITHUB_TOKEN` **does not trigger further workflows**,
   so the announcer would never fire. Releases published by a human in the UI, by `gh release
   create` from a laptop, or by another repo's pipeline using a **PAT or GitHub App token** do
   trigger it. If the publishing pipeline is cross-repo and uses a PAT, this works as-is. If it
   ever moves to `GITHUB_TOKEN`, the pipeline must instead call
   `gh workflow run discord-release-announce.yml -f mode=send -f tag=vX.Y.Z` (PAT needs the
   `workflow` scope / Actions: write on `mote-releases`), and the artifact dedup then protects
   against both paths firing.
4. **Actions config.** Verified on the repo today: Actions enabled, `allowed_actions: all`,
   `sha_pinning_required: false`. If "allow select actions" or SHA pinning is ever turned on,
   allowlist / SHA-pin `actions/checkout@v4` and `actions/upload-artifact@v4` (the only two
   third-party-hosted pieces; both official GitHub actions).
5. **Permissions.** `contents: read` + `actions: read`. `actions: read` is **required**, not
   optional: without it the duplicate-send check fails and, because it fails closed, nothing is
   posted. Nothing needs write access.
6. **Verification order** that avoids a public duplicate: `dry_run` on `v1.5.6.1` (log only) ->
   `test_connection` (silent, self-deleting, confirms the webhook and the channel) -> then leave
   it to fire on the next real release. The parent can also hit the webhook directly; the probe
   exists so the workflow path itself is proven end to end.
7. **`@everyone` from a webhook** only lands if the channel does not restrict it. The probe does
   not test the ping (by design); the first real release is the first ping. If a ping ever fails to
   notify, check the channel's permissions for the webhook integration.
8. **Discord renders no markdown tables.** v1.5.0-style "By the numbers" tables post as raw pipe
   rows, and a table split across two messages loses its header row. Worth keeping future notes
   table-free, or accept it.
9. Script is Python 3 stdlib only; `ubuntu-latest` ships python3 preinstalled, so there is no
   `setup-python` step and no install step at all.
10. The repo is public, so this code is public. It contains no secrets, no tokens, no URLs.

## Tests

```
cd discord-automation
python3 -m unittest discover -s tests -v
```

45 tests, no network. Covers: stable/prerelease/draft/odd-tag filtering (incl. the real
`v1.5.6.1` hotfix), cumulative-section isolation against the real `v1.5.4` body (12k chars,
5 stacked versions), exact-vs-prefix version header matching, heading variants, no-heading bodies,
fail-closed on a heading-bearing body with no matching version, download-URL stripping, preview suppression, hostile `@everyone`/`@here`/
role/user mentions, long-body splitting with lossless line preservation, ping and `allowed_mentions`
policy per message, embed suppression, deterministic markers, code-fence repair, a single overlong
line, webhook redaction, the connectivity probe shape, and receipt contents.

Manual CLI checks run against the live repo (read-only, nothing posted):

```
python3 scripts/announce_release.py --event-path fixtures/event-release-published.json --dry-run
python3 scripts/announce_release.py --repo marcelvict/mote-releases --tag v1.5.6.1 --dry-run
python3 scripts/announce_release.py --repo marcelvict/mote-releases --tag v1.5.6-rc1 --dry-run
python3 scripts/announce_release.py --repo marcelvict/mote-releases --tag v1.5.0   --dry-run
```
