# ZenTao host-controlled requirement synchronization

## Problem and accepted direction

Story #41 proved that the worker CLI is `never` plus an MXC workspace permission profile. Git Credential Manager cannot initialize its MSYS shell or enumerate Windows credentials there. Host Git and the host native helper succeed. Existing writable Git metadata grants and network access do not fix this OS credential boundary. The user requests a durable fix for future Story intake, without questions, temporary permissions, or a weaker coding sandbox.

## Design

Keep the coding/review CLI sandbox and approval policy unchanged. Make requirement synchronization an explicit host-controlled handoff, following the existing push/deployment pattern. `record material_sync` becomes a status/readiness operation; it must never execute Git from the sandbox. A missing result returns `material_sync_worker_required` and instructs the original chat to end its turn. The launcher verifies that the original CLI and its Task synchronizer stopped, then runs the fixed synchronization operation under a Windows Job Object using the original material-check attempt deadline, and resumes the same chat.

Use the current item identity, request, chat, active attempt, config snapshot and matching current requirement path to authorize the host operation. Do not accept a model-supplied repository, branch, shell command or credential. Keep the existing fast-forward-only implementation and safe preservation of local/ignored files. The host child computes an unpublished result only. After its Job Object is confirmed stopped and the original binding/config/deadline and synced HEAD are revalidated, the outer runner atomically records the verified branch/upstream/SHA in the existing `requirement_sync_receipts` together with the finalized per-attempt outcome. A new per-attempt outcome table records sanitized success/failure and controlled-process receipts so the resumed chat can register the original attempt result and retries. Never publish an early success receipt from inside the still-running child. Both formal CLI and Task synchronizer jobs must be named/recoverable for material handoffs even when story_review_sync is false. Missing/uncertain stop evidence prevents relaunch and preserves occupancy; confirmed failure becomes an ordinary material-sync failure, never a missing-material conclusion.

Serialize synchronization/preflight against the requirement checkout. Persist stable launch, stdout, stderr, stop and result evidence by attempt. On restart, reconcile existing receipts and the named Job Object before rerun. Preserve old successful receipts and already-bound chats. Never automatically recover a user-stopped item.

## Preflight and future intake

Add `worker.py git-preflight`: a bounded host read-only `git ls-remote --exit-code` against the configured requirement checkout's actual tracking branch, with interactive credential prompts disabled. Store only safe identity, commit and result information; never print credentials or raw authentication responses. Run this before `prepare` creates a chat and before the first `run` starts ticket business, including when page E2E is disabled. Expose the same result during monitor `sync-tasks` so the scheduled monitor sees environment failures and keeps candidates queued; document running preflight before reserve. A genuine credential expiry or server failure must produce an environment/preflight result, not start and then terminally fail a new Story for an MXC-only error. When prepare/initial-run preflight definitively fails and its owned processes are stopped, atomically release only the same never-started reserved/bound dispatch to the queue, preserving its existing chat. Unknown stop or an already-started item cannot release occupancy. This avoids stranding an uncreated chat in dispatch_uncertain.

## Alternatives

- Recommended: host handoff. Reuses existing launcher authority and bounded-process pattern; retains worker isolation and adds no credentials to prompts/config.
- Credential bridging into the sandbox: adds secret distribution and expiry/matching responsibilities and does not repair general MSYS compatibility; excluded.
- Full-access coding CLI: unnecessarily broadens every development tool; excluded.

## Verification and acceptance

1. Regression test: sandbox-facing `record material_sync` does no Git and asks for the host; existing successful receipts remain readable.
2. A local Git remote publishes new intent material; the host runner fetches, fast-forwards, saves an attempt-bound receipt, and permits review. Conflicting or ignored local files remain intact and failure is reported safely.
3. A successful child result with unknown cleanup cannot publish a receipt or unblock material review; include a crash-after-compute test. Current/snapshot path mismatch, stale request/chat/attempt, expired deadline, concurrent lock, uncertain stop and missing result cannot cause unauthorized or duplicate sync. Recorded failures are visible after resume and retries use a new attempt.
4. Launcher tests cover chat -> host sync -> same chat, already-pending startup, failed sync -> chat, and prevention of handoff after unverified CLI/Task stop. Preserve push/deploy/review routing.
5. Git preflight is real and read-only, mandatory in prepare/initial run and independent of page E2E; failure prevents a new business CLI launch. Bugs and disabled material-sync settings skip the Story-specific probe.
6. Unit/integration suites pass. A controlled host probe of the actual requirement repository succeeds; an isolated temporary ledger + real configured checkout can prove host sync without mutating the production ticket ledger or ZenTao. Keep real ticket #41 closed unless explicitly resumed.

## Deployment scope

Develop on an isolated parent-repository worktree. Integrate only the automation workflow change after independent spec/quality review and tests, leaving existing untracked backend workspaces alone. Update project skill/references so future scheduler rounds use the host handoff. Do not change the scheduled automation cadence, ZenTao business states, user credentials or global Git/Codex configuration.
