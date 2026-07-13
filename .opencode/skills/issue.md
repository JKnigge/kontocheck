---
description: "Fix a single issue following the mandatory 5-step protocol with pauses after each step"
---

You are fixing ONE issue from the bug list. The user will provide the
issue description. Follow these steps strictly.

For each step:
- Execute ONLY that step.
- After completing it, output exactly:
  ⏸️ PAUSED — Step N/5 complete. Reply "continue" to proceed.
- STOP immediately. Do not continue to the next step.

### Step 1 — Check for Existing Tests
Search the codebase for tests that already cover the described issue.
Report what you found (or did not find). Then pause.

### Step 2 — Write a Failing Test
If no test covers the issue, write at least one test that reproduces
the bug. The test should assert the expected (correct) behavior so
that it fails before the fix is applied. Then pause.

### Step 3 — Run the Issue-Specific Tests
Run only the tests related to this issue. They MUST fail. Show the
test output. If they pass unexpectedly, stop and report — the test
may not be catching the bug correctly. Then pause.

### Step 4 — Implement the Fix
Apply the proposed fix for the issue. Keep the change minimal and
focused on the issue. Do not refactor unrelated code. Then pause.

### Step 5 — Run the Full Test Suite
Run all tests. They must ALL pass. Show the test output. If any test
fails, do not proceed — report the failure and wait for guidance.
Then pause.

### After All Steps
Ask the user whether to proceed to the next issue. Do not start the
next issue automatically.

CRITICAL: Never combine steps. Never skip the pause. If you find
yourself wanting to continue without waiting for "continue", that is
an error. Stop immediately.