# Maintained board-owner deployment overlay

These source assets package the GUS gateway and board-controller changes without
overwriting the primary Hermes checkout's unrelated work. They are a deployment
overlay, not a claim that the newer release architecture has been ported to the
older primary checkout. Gateway and controller are separate trees; never replace
one tree's `kanban_db.py` with the other's entire file.

## Package explicit changes

Run `package_overlay.py` with both exact baseline/candidate roots and repeat
`--gateway-file` / `--controller-file` for every reviewed changed source or test.
No file is selected implicitly. The output directory must be new.

```sh
python3 deploy/board-owner-control/package_overlay.py \
  --gateway-base "$gateway_baseline" --gateway-candidate "$gateway_candidate" \
  --gateway-file tools/kanban_tools.py \
  --controller-base "$controller_baseline" --controller-candidate "$controller_candidate" \
  --controller-file hermes_cli/kanban_db.py \
  --output "$new_overlay_directory"
```

This produces `manifest.json`, `gateway.patch` and `controller.patch`. The
manifest records before/after SHA-256 values and each patch digest. Include all
changed tests in the real invocation; the command above illustrates selection,
not a complete list for any particular release. Preserve the generated package
alongside these maintained assets. Neither script stages, commits, deploys,
restarts services, or modifies the source/release trees.

## Verify before switching a release

```sh
python3 deploy/board-owner-control/preflight.py \
  --gateway "$gateway_candidate" --controller "$controller_candidate" \
  --python "$hermes_runtime_python" \
  --manifest "$new_overlay_directory/manifest.json"
```

Use a Python executable with the release dependencies installed. Each lane runs
in a fresh subprocess with temporary HOME/HERMES_HOME/database, no inherited
credentials, no bytecode writes, and blocked network/process-spawn operations.
Each lane has a 45-second deadline by default (maximum 60); there are no retries.
The manifest must match the candidate's selected files exactly.

The gateway probe imports the actual registry and verifies the owner can
discover `kanban_manage`. The controller probe creates disposable cards to prove
archive preserves working files and failure history, restore retains a paused
lane, incomplete prerequisites remain gating, completion releases a child, and
evidence-bound triage continuation works. It never starts a worker or model.
This is an offline deployment gate; it does not prove a real conversation or
actual worker execution. Those require the separately bounded live acceptance.

For the full focused regression files, use the repository's canonical runner:

```sh
bash scripts/run_tests.sh -j 1 --file-retries 0 <reviewed-test-files>
```

Before applying a packaged patch to a new candidate, verify every selected
file's `before_sha256` (missing files use null), run `git apply --check` on a
copy, apply there, then run this preflight and the focused regression files.
A baseline mismatch requires reviewing/rebasing the overlay; never force it.
After an intentional rebase, regenerate the manifest for the new baseline.

The live gateway switch script is not changed by these assets. The release
owner must wire a successful preflight as a prerequisite before activation;
the existing startup-health rollback remains a separate check. Always preserve
the previous verified gateway/controller pair and the exact generated package.

Current packaged release: `releases/20261004-native-owner-v2`. The installed
controller wrapper selects its controller; the switch script runs the maintained
preflight before interrupting the gateway. The preflight also distinguishes new
specified triage (`start_triage`) from stopped-card recovery (`resume_triage`).
A no-op unblock is an explicit error, never a successful receipt.

Live acceptance on October 4 verified Margot invoking native archival and
preserving the obsolete card history. The attempted worker acceptance did not
start: its new-triage fixture was routed to stopped-task recovery. Two bounded API
turns ended, no worker ran, and both fixture cards were retired. This package does
not claim a successful live worker/dependency chain. The new-triage correction
passed deterministic tests; no further paid acceptance run was started.
