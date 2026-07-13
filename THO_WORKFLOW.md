# THO explicit-request workflow

This is an explicit-request lane, not autonomous THO access. `Project-Go-Forward`
remains outside `ALLOWLIST`, backlog mining, and regression sweeps. The stale or dirty
Windows clone is never used by this workflow.

## 1. Normalize and stage on the Mac

Search/read Gmail on the Mac or in the connected cloud environment. Copy only Mark's
requested work into a local UTF-8 text file; do not put Gmail credentials or raw mailbox
access on the Windows worker. Refresh and inspect the Mac clone's `origin/main`, then run:

```sh
python3 stage_tho_request.py \
  --repo /path/to/Project-Go-Forward \
  --request-file /path/to/normalized-request.txt \
  --message-id '<gmail-message-id>' \
  --message-date 'YYYY-MM-DD' \
  --test 'python -m pytest path/to/explicit_test.py -q' \
  --output-dir /path/to/staged
```

The script resolves the exact `origin/main` SHA, creates a full Git bundle for that ref,
verifies it with `git bundle verify`, and writes a task carrying `source`, `message_id`,
`message_date`, `base_sha`, `base_bundle`, and the mandatory test command. It does not
fetch Gmail, use the network, modify the source clone, or send a reply.

## 2. Transfer the two artifacts

Copy the generated `.bundle` into the Windows worker's
`agent-worker\incoming-bundles\` directory. Copy the generated `.md` task into
`agent-worker\queue\`. Keep the generated filenames unchanged: the task refers to the
bundle by basename, which the worker resolves only beneath `incoming-bundles`.

## 3. Build only in the isolated workspace

The Windows worker verifies the provenance and bundle again, clones the bundle into
`agent-worker\tho-workspaces\<task>-<sha>`, and checks out the exact recorded SHA before
creating a `worker/*` branch. It rejects missing or mismatched provenance, traversal,
stale SHAs, prohibited paths, diffs over 500 lines, missing tests, failing task tests,
and failing canonical regressions for source changes. It creates and verifies the result
bundle before deleting only that generated workspace.

The worker never stashes, switches, cleans, or edits the existing Windows
`Project-Go-Forward` clone. It never pushes, merges, deploys, changes DNS, reads Gmail,
or sends email.

## 4. Review and land from the Mac

Pull the report and result bundle to the Mac. Verify the bundle, fetch it into a Mac THO
clone, inspect the full diff and test evidence, and handle any push, PR, and merge from
the Mac under the normal THO gates. Any email response remains a draft until Ari gives
explicit approval to send it.
