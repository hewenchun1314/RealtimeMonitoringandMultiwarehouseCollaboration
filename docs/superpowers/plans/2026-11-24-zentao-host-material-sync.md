# ZenTao host material synchronization implementation plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development to implement this plan. Steps use checkbox syntax for tracking.

**Goal:** Prevent MXC Git credential failures in newly admitted Story material checks.

**Architecture:** `record material_sync` requests/reads a host-controlled operation. The Windows launcher performs the fixed operation after a verified chat stop, uses the original attempt deadline and records its result, then resumes the original chat. A host Git preflight prevents starting new ticket business when requirement authentication is unavailable.

**Tech Stack:** Python standard library, SQLite, Git, Windows Job Objects, unittest.

## Chunk 1: Host handoff and integration

### Task 1: Implement and verify the durable host material-sync path

Files:
- Modify `.agents/skills/zentao-autodelivery/scripts/workflow.py` (separate status from host execution).
- Modify `.agents/skills/zentao-autodelivery/scripts/ledger.py` (idempotent outcome schema).
- Create `.agents/skills/zentao-autodelivery/scripts/material_sync_runner.py` (fixed host execution, synchronization lock, receipt recovery and bounded preflight).
- Modify `.agents/skills/zentao-autodelivery/scripts/material_sync.py` only for shared noninteractive Git/preflight helpers if needed.
- Modify `.agents/skills/zentao-autodelivery/scripts/worker.py` (host handoff, preflight, same-chat continuation and prompts).
- Modify/add focused `test_material_sync.py`, `test_material_sync_runner.py`, `test_worker.py` tests.

- [ ] Write regression tests proving `record material_sync` yields without Git and successful host results unblock original material review.
- [ ] Run those tests and retain expected failing assertions.
- [ ] Add per-attempt outcome persistence with exact active binding/path validation, preserving the existing success receipt format. The host child computes only; the outer runner atomically publishes the success receipt and final outcome after verified Job Object stop and fresh binding/deadline/HEAD validation. Test that success output with uncertain cleanup cannot permit review.
- [ ] Implement the fixed host executor and runner with original absolute deadline, Windows Job Object receipts, checkout serialization and uncertain-run reconciliation. Do not broaden worker CLI permissions.
- [ ] Make sync failure readable by the original chat; preserve retry handling and no-material-missing guard.
- [ ] Test stale/foreign attempts, config changes, deadline expiry, locks, failures and unverified cleanup before continuing.
- [ ] Add launcher handoff routing and explicit prompt instructions. Assert prior chat and Task sync are stopped before host work; resume same chat for both result outcomes. Make CLI and Task jobs recoverable for material-sync-only snapshots as well as story-review snapshots, and test this combination.
- [ ] Add read-only Git preflight using configured upstream and prompts disabled. Enforce before prepare and initial business run, expose to monitor, and skip when Story sync is disabled/not applicable. On a definitively stopped preflight failure, atomically requeue only that never-started reservation and preserve any original chat; test that live/uncertain/stale work cannot release a slot.
- [ ] Verify actual failing baseline then passing tests, including existing material conflict/preservation and worker push/deployment cases.
- [ ] Self-review changes for secret leakage, uncontrolled shell execution and premature success receipts.

## Chunk 2: Operating instructions, review and rollout

### Task 2: Document and validate future unattended intake

Files:
- Modify `.agents/skills/zentao-autodelivery/SKILL.md`.
- Modify `.agents/skills/zentao-autodelivery/references/worker.md`, `workflow-cli.md`, `stages.md`.

- [ ] Replace sandbox material-sync execution guidance with handoff/status semantics; document preflight before reserve and backstop gating inside prepare/run.
- [ ] Document safe failure/recovery receipts, original binding/deadline, no direct credential probing, and no automatic restart of user-stopped tickets.
- [ ] Run the relevant Python test suite and then the full workflow suite once changes stabilize.
- [ ] Independent spec compliance review, fix findings, then independent code-quality review and fix findings.
- [ ] Run actual host read-only preflight and isolated-ledger host-sync smoke checks without writing ZenTao or changing the original ticket.
- [ ] Integrate verified workflow changes into the automation's actual project checkout and rerun a bounded targeted smoke check.
- [ ] Update automation memory and report the concrete fix, validation and remaining external authentication/server dependency.
