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

Member base package: `releases/20261004-member-board-control-v2` (supersedes the Chris-only v2 overlay). The installed
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


## Authenticated member controls (October 4)

The member package derives the board, roster and return destination from protected
current enrollment. No model argument, environment profile name, or caller-selected
board grants access. Josh, Connor and new enrolled members use the same controls.
`own_work_list`, `own_work_show`, `own_work_manage` and `own_work_comment` provide inspection,
reversible cleanup, triage/review recovery and exact obsolete-dependency removal.
Existing attempt history, active work and real prerequisites remain enforced.
Normal fully specified delegation creates ready work, or dependency-waiting work;
there is no new automatic retry or triage sweeping scheduler.

Before activation, run the explicit one-time private transport installer:

```sh
python3 deploy/board-owner-control/initialize_member_controller.py --runtime "$gateway_candidate"
```

It installs a local 32-byte signing key and replay ledger, never prints their
contents and does not rotate existing material. Member controller requests bind
the exact task snapshot, payload, account, profile, board and session; expire
after 30 seconds; consume a nonce once; and revalidate enrollment inside the
mutation transaction. Scheduled jobs and delegated workers cannot inherit this
interactive account authority. The private transport directory must remain
outside worker filesystem access.

Deploy the matched gateway/controller pair and the corresponding app bridge
pins together. Preserve the previous controller wrapper and gateway plist;
restore the wrapper as well if gateway activation rolls back. The public app
must be built from origin/main through its supported deployment script. Do not
edit the stable deployment checkout manually. The app bridge manifest pins
`owner_delegation.py` and `owner_app_returns.py`; update its reviewed hashes for
any later changes to those two modules.

The acceptance checks use temporary accounts, databases and documents, without
provider calls or live worker jobs. They cover current and future member
creation, independent review, dependency release, conversation returns and Desk
custody. They do not claim a live user conversation or successful execution of
every existing blocked project. New profile names alone are not enrollment;
profiles must have their authenticated canonical account/board binding.

Josh's existing phone route required a separate protected configuration change
from the older company listener to his authenticated named main-gateway route.
`operational/route_josh_phone.py` performs only that exact reversible three-field
change, with a private backup. The live phone processor rereads this route for
each request, so no phone service restart is required. Chris Personal is a
separate older owner-context runtime and is not covered by this GUS member
rollout; changing that route requires its own authenticated owner migration.

The v2 gateway adds own-board listing to normal member catalogs. The app bridge
continues to pin the byte-identical authority/delegation/return modules in the
first member release; those pinned files were not changed by v2.

## Personal and held-review reference repair

The combined successor is `releases/20261004-artifact-ref-board-control`. It retains the member controls and adds an authenticated Personal capability report for the four own-board tools. Private memory, conversation storage and existing private-work admission remain separate. The app consumes the fresh tool report before giving board guidance.

`repair_review_reference` corrects only a redacted held-review locator to the exact original regular file within the existing task workspace. It verifies fresh task/goal snapshots, identical content digest and no active work; it preserves status, findings and counters and records an audit event. Recovery and reviewer dispatch remain separate operations.

`operational/activate_personal_board.py --package <combined-package>` defaults to a read-only preparation check. `--apply` backs up pinned operator files and reloads only Personal with authenticated capability health checks; `--rollback <receipt>` restores its exact files. Activate the matching main gateway separately through the existing guarded release switch. Do not treat either health check or deterministic fixture tests as conversational worker acceptance.

The active controller additionally requires `releases/20261004-controller-context-identity`. Its canonical module alias preserves signed member authority and the expected task snapshot across the actual `python -m` entrypoint and imported mutation helpers. This controller-only overlay was verified with a real subprocess preview/apply/stale-state regression; the gateway release remains the combined artifact-reference release. Apply this overlay after the combined package when reconstructing the controller. The maintained guarded wrapper points to that final controller.

## Restore native profile routing (October4)

`releases/20261004-native-profile-routing` removes the custom historical whole-profile fingerprint mismatch veto in the shared capability resolver. Ordinary configuration, SOUL and skill edits no longer disable otherwise permitted work. Current installed fingerprints are still computed and attached to admission results; matrix role/capability/realm/workspace/tool/MCP constraints, real profile existence, signed action approvals and independent member confinement are retained. This is a surgical rollback of a demonstrated local regression, not a full upstream code replacement.

Both source lanes contain the same resolver change and11 focused offline regressions. Ten existing member-worker isolation tests and both deployment preflight lanes pass. The read-only live inventory admits all12 department heads without rewriting the matrix or starting workers. Existing triage/explicit holds are not automatically bulk-released.

The main gateway uses the existing guarded release switch; point the guarded controller wrapper to the paired native-profile-controller first. `operational/switch_personal_runtime.py --manifest <package>/manifest.json --apply` changes only Personal's source path and working directory, retaining its config, credentials and authenticated identity, with exact backup and automatic startup rollback. It supports explicit rollback under input hashes. No additional phone restart is required for routes using the shared gateway.


## Prepared work (October 4)

`kanban_manage(action="prepare")` and authenticated `own_work_manage` now record
an idle existing card's current brief, acceptance criteria, eligible worker,
required tools/resources and explicit execution budget. Preparation preserves the
original request, status, dependencies, review findings and failure history. It
never starts a worker. Inspect its offline readiness and effective limits before
using the existing recovery action. Member operations remain confined to the
currently authenticated member's enrolled board; future members inherit the same
registration.

The normal dispatcher uses the prepared requirements and an authorized subset of
the worker's tools, then performs its existing admission checks. Member/private
worker policies remain authoritative. The current brief comes first in worker
context with bounded recent history; original instructions and paginated history
remain available. A changed plan invalidates outstanding admission. A changed
substantive brief creates a new input version; raising a budget alone does not
reset attempts or the rolling run allowance.

The preparation tool accepts one attempt per input and explicit bounded budgets.
Existing operator ceilings still apply and are reported. No recovery scheduler,
automatic retry loop or overnight job is added. A valid completion still depends
on the actual task's acceptance and review requirements.

`operational/switch_personal_runtime.py` reads the reviewed runtime pair from the
manifest and creates a separate receipt for each target. It changes only the
Personal service's source path and working directory, checks candidate hashes,
retains identity/configuration, and rolls back a failed startup.
