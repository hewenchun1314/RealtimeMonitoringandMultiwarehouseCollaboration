# Project or product intake implementation plan

**Goal:** Admit a Story or Bug when its actual project is the configured project OR its actual product is the configured product. The user explicitly approved this rule for 海能发地质工程一体化 (product 2).

**Architecture:** Keep the existing project as the automation's queue/workspace identity so historical markers, chat bindings, completed items and stop instructions remain stable. Store actual source project/product separately. A shared collector verifies project/product identities, finishes all enabled list pages, unions by type and ID, and supplies original ownership for scan and detail rechecks. Product-only Stories may proceed without a project execution; Task synchronization must retain its existing missing-execution handling.

**Tech stack:** Python standard library, SQLite, existing ZenTao REST v2 client and Windows worker.

- [x] Add regression tests for product-only Bug/Story inclusion, project-only inclusion, duplicate union, both-outside rejection, disabled types, incomplete pages and detail reassignment before reserve.
- [x] Add shared scope/collector code and a tracked monitor CLI. Retain project-only behavior for configurations without the optional product scope.
- [x] Preserve actual ownership in the ledger; enforce the same OR rule at reserve. Update Story review ownership and worker context.
- [x] Configure project OR product 2, document the queue/source distinction, and update the existing automation prompt without changing schedule or notification settings.
- [x] Run focused and affected existing tests, independently review the change, then perform a live scan and detail eligibility check for Bug 2 without starting business development as part of scope-change verification.
- [x] Record current results and revised scope in automation memory.

Alternative considered: change Bug 2's project, which changes source data and does not implement the requested rule. Product-only scanning was also rejected because it would omit project-matching work in another product.

Verification: 96 tests passed; independent review passed; live union found Bug 2 and detail eligibility passed. A concurrent scheduled monitor had already reserved Bug 2 and prepared its original worker chat; this configuration-change chat did not launch a duplicate worker.
