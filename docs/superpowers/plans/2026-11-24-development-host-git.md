# Frontend/backend host Git implementation plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development with TDD and independent spec/quality reviews.

**Goal:** Remove missing-branch authentication and hook execution from the sandbox Git mutation path.

**Architecture:** The existing fixed launcher handoff gains validated per-attempt host Git requests/results. Code editing/testing remains sandboxed; branch preparation, candidate commits and safe integration execute as bounded host operations retaining hooks.

**Tech Stack:** Python standard library, SQLite, Git, Windows Job Objects, unittest.

## Chunk1: Host operations

- [x] Add intended-failing tests for status-only batch preparation, candidate commit and integration APIs.
- [x] Implement bounded fixed host operations with current/snapshot/plan/worktree/HEAD/stage authorization, stable requests/results and no arbitrary host command.
- [x] Support missing exact origin branch fetch, deterministic worktree creation, explicit file staging/candidate commit and safe fast-forward integration. Keep actual hooks and filters.
- [x] Enforce canonical common-Git-directory ownership across linked worktrees, protect targets checked out elsewhere, and reject dirty hook-generated candidate results with regression coverage.
- [x] Add per-repo pending/cleanup/reconciliation for lost receipts, post-commit outcomes, crash recovery and concurrency without duplicate effects.
- [x] Restrict commits to coding/repair-coding and route normal/repair unit-test edits back through machine-derived coding revision; verify new SHA invalidates downstream evidence.
- [x] Wire launcher handoff/recovery and return candidate/merged SHAs usable by existing evidence validators.
- [x] Update phase-agent/main-worker instructions, Skill and branch/stage/worker/CLI references; preserve user-stopped items and prior material/push/deploy flows.

## Chunk2: Validation and rollout

- [x] Run focused local-Git and Windows process tests; verify hook failure is returned and fix/retry succeeds with checks enabled.
- [x] Independent spec review then quality review; fix/review all actionable findings.
- [x] Run actual MXC-host-MXC smoke only in cloned repositories/temporary ledgers, exercise representative real Husky configuration on four affected projects and prior40-repository paths where feasible.
- [x] Run complete suite, integrate into actual project checkout, verify actual entry points without business commits/ZenTao writes/#41 runs.
- [x] Record evidence, update automation memory and close isolated development worktree.

## Final verification

Integrated into the actual project checkout at `62bacd8`. Independent spec and quality reviews approved. All 295 tests passed in the actual root (433.889s), with production SQLite and source state unchanged. Actual MXC request → bounded host Git → MXC receipt passed on four real frontend clones with original Husky checks, partial-failure correction, product runtime bootstrap and source-user-branch preservation, plus a real smart-geo clone. Requirement Git authentication preflight passed. Story41 was not restarted. The isolated development worktree and merged local branch were removed. Evidence: `.local/zentao-delivery/verification/development-host-git-20261124/final-verification.json`.
