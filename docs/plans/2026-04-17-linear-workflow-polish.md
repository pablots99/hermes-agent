# Linear Workflow Polish Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make Jax's Linear-native execution flow reliable and operator-friendly: project-aware task creation, dependency-aware blocking, agent-side retry policy, explicit testing/approval loops, and project-type-specific completion actions.

**Architecture:** Keep Linear as the planning/execution control surface, but separate concerns into (1) issue intake + policy resolution, (2) execution orchestration, (3) completion/testing handoff, and (4) follow-up automation. Reuse the native Linear adapter for session ingress and issue state transitions, and add a deterministic workflow policy layer so Jax does not improvise per issue.

**Tech Stack:** Hermes gateway Linear adapter, Linear MCP/API, session metadata persistence, QA packet registry, approval routing, git/GitHub CLI hooks.

---

## Current grounded state

Based on code inspection in this checkout:

- **Already exists**
  - Linear native adapter with execution modes: `autonomous_dev`, `autonomous_with_testing`, `human_gate`, `manual_only`
  - State routing on success/failure: `In Progress`, `Testing`, `In Review`, `Done`, `Blocked`
  - Task-type routing using `type:*` labels and `LINEAR_SUPPORTED_TASK_TYPES`
  - Project-level execution-mode overrides using `LINEAR_PROJECT_EXECUTION_MODES`
  - Bounded concurrency and queue/start notices in Linear
  - QA packet selection + operator-visible handoff structure in Jax Control Plane docs/read model
  - Dangerous command approval plumbing in Hermes terminal tool
  - Generic terminal retries for transient execution failures

- **Missing / weak today**
  - No first-class **task dependency policy** driving execution gating from Linear relations
  - No explicit **project review loop** that continuously audits project docs/issues and creates missing executable tasks while other work continues
  - Retries are mostly **tool-level**, not **workflow-level** (e.g. issue-level attempt counters, backoff, retryable vs terminal classification)
  - Completion flow does not yet deterministically decide **what happens after Testing**
  - No centralized **project-type completion policy** (merge PR? create docs? leave review packet only?)
  - Approval messages are plumbing-level, but not yet polished as a workflow surface inside the Linear execution loop

---

## Target product behavior

### 1. Project review + proactive task creation loop
Jax should be able to keep doing active tasks while also periodically reviewing projects and proactively creating missing work.

**Desired behavior:**
- Each project gets an explicit review cadence/policy.
- Review runs proactively create tasks for:
  - fixes
  - security findings
  - reliability hardening
  - UX / operator improvements
  - roadmap follow-through
  - docs drift / missing continuity docs
- Review-created tasks must still be:
  - actionable
  - non-duplicate
  - linked to a concrete roadmap item, docs-of-record gap, repo finding, run failure, alert, or security finding
- Review work should not starve active execution.
- Review-created tasks should carry enough metadata to be auto-routable.
- Security / reliability findings should be eligible for autonomous task creation even when they were not manually requested, as long as they are concrete and scoped.

### 2. Dependency-aware execution gating
Jax should refuse to execute tasks that are blocked by unresolved dependencies.

**Desired behavior:**
- Use actual Linear issue relations (`blockedBy`, `blocks`) as the primary dependency source.
- If a dependency is unresolved, do not start work.
- Move the issue to **Blocked** and explain exactly which dependency is blocking it.
- Automatically re-check when upstream work completes or on the next review loop.

### 3. Agent-side retry policy
Retry behavior should be intentional, not accidental.

**Desired behavior:**
- Distinguish:
  - transient infrastructure/API failures
  - retryable tool failures
  - operator-required blockers
  - permanent/spec-related failures
- Keep issue-level retry counters/attempt history.
- Avoid infinite loops on bad specs or missing prerequisites.
- On final exhaustion, mark **Blocked** with a specific reason.

### 4. Agentic post-run outcome decisions
Finishing a processing run should not imply the task is finished.

**Desired behavior:**
- After each run, Jax should decide a **workflow outcome**, not just rely on transport/process success.
- Valid outcome classes should include at least:
  - `ready_for_testing`
  - `retry`
  - `blocked`
  - `backlog`
  - `stay_in_progress`
  - `change_scope`
  - `needs_human_review`
- Example meanings:
  - `ready_for_testing` → implementation is complete enough to validate
  - `retry` → transient failure, try again automatically
  - `blocked` → cannot proceed due to dependency/spec/env/operator blocker
  - `backlog` → valid work exists, but this issue should be deferred instead of actively executed now
  - `stay_in_progress` → partial progress was made but the task is not yet finished and should continue autonomously
  - `change_scope` → current issue should be narrowed, split, or reframed before continued execution
  - `needs_human_review` → autonomy is insufficient because the result is ambiguous/risky
- **Autonomy-first rule:** before choosing `backlog`, `blocked`, or `needs_human_review`, Jax should first try to retrofit the work into an executable next step:
  - narrow the scope
  - create or update follow-up tasks
  - attach dependencies
  - rewrite the immediate objective into a smaller executable slice
  - then automatically trigger the next agent run again
- State transitions should follow this workflow outcome, not generic processing success.
- If an issue goes to **Testing**, create a follow-up path:
  - automated validation run if possible
  - operator-visible QA packet always
  - optional human approval gate depending on project type/risk
- Testing should be one possible outcome, not the default sink for every successful run.

### 5. Project-type completion policy
What Jax does after implementation should depend on project type.

**Desired behavior:**
- Internal tooling / infra project:
  - update docs when required
  - run smoke checks
  - merge PR automatically only when policy allows
- Product/frontend/backend project:
  - generate QA packet
  - attach preview/validation evidence
  - require explicit approval before merge if configured
- Research/docs project:
  - update notes/docs/tasks, maybe no PR/merge path at all

### 6. Approval message polish
Approval prompts should be first-class workflow messages, not raw tool interruptions.

**Desired behavior:**
- Summarize what needs approval and why.
- Attach the exact action that will happen after approval.
- Thread approval into the issue/session lifecycle rather than forcing the operator to infer context.

---

## Proposed policy model

### Workflow policy record
Add a normalized workflow policy per issue/session, resolved from project metadata + labels + defaults.

Suggested fields:

```python
@dataclass
class LinearWorkflowPolicy:
    execution_mode: str  # autonomous_dev | autonomous_with_testing | human_gate | manual_only
    task_type: str
    dependency_mode: str  # strict | advisory
    retry_policy: str  # none | conservative | standard | aggressive
    testing_policy: str  # none | automated_first | manual_packet | hybrid
    merge_policy: str  # never | after_testing | after_approval
    docs_policy: str  # none | required_on_completion | required_before_done
    approval_policy: str  # none | risky_actions | merge_only | all_mutations
    project_type: str  # infra | internal_tool | backend | web | mobile | hybrid | research
```
```

Resolution order:
1. explicit issue labels (`type:*`, `mode:*`, maybe future `retry:*`, `merge:*`)
2. project-level registry/config
3. global defaults

---

## Proposed feature slices

### Slice A — Dependency-aware blocking
**Objective:** Prevent Jax from starting work when upstream issue dependencies are unresolved.

**Behavior:**
- Before `on_processing_start()`, fetch related issue statuses using Linear relations.
- If any blocking issue is not completed/canceled, do not execute.
- Move the issue to **Blocked**.
- Post a Linear activity like:
  - `Jax did not start because this issue depends on PAB-123 (In Progress).`

**Files likely touched:**
- `gateway/platforms/linear.py`
- `tests/gateway/test_linear.py`

**Tests needed:**
- blocks on unresolved dependency
- ignores completed/canceled dependencies
- uses Blocked state, not label
- reassigns to correct human if execution is prevented

---

### Slice B — Workflow-level retry classification
**Objective:** Add issue/session-level retry policy above raw terminal retries.

**Behavior:**
- Classify failures into:
  - `transient_api`
  - `transient_tool`
  - `spec_or_dependency`
  - `approval_needed`
  - `permanent_failure`
- Persist attempt count in session metadata and/or a lightweight store.
- If retryable and under budget:
  - post a Linear thought activity with next retry timing/reason
- If not retryable or exhausted:
  - move issue to **Blocked** with explicit reason

**Files likely touched:**
- `gateway/platforms/linear.py`
- `gateway/platforms/base.py` (if shared outcome classification hooks are needed)
- `tests/gateway/test_linear.py`

**Tests needed:**
- retryable transient error increments attempt counter
- non-retryable errors block immediately
- exhausted retries transition to Blocked with message

---

### Slice C — Agentic post-run outcome decisions
**Objective:** Make the post-run state transition depend on an explicit workflow decision, not generic processing success.

**Behavior:**
- Introduce a workflow decision object emitted after each run, e.g.:
  - `ready_for_testing`
  - `retry`
  - `blocked`
  - `backlog`
  - `stay_in_progress`
  - `change_scope`
  - `needs_human_review`
- **Autonomy-first execution rule:** `backlog`, `blocked`, and `needs_human_review` should be last-resort outcomes. Before selecting them, Jax should attempt a self-retrofit step that:
  - rewrites the immediate objective into a smaller executable slice
  - splits the task or creates a follow-up subtask if scope is too large
  - adds dependency links if hidden prerequisites were discovered
  - updates the issue/comment trail with the narrower next step
  - then re-triggers the agent automatically on the refit scope when safe
- Map those decisions to Linear behavior:
  - `ready_for_testing` → move to `Testing` (or fallback `In Review`)
  - `retry` → stay `In Progress`, increment retry metadata, re-dispatch if policy allows
  - `blocked` → move to `Blocked`
  - `backlog` → move to `Backlog`/`Todo` depending on policy and explain why it was deferred
  - `stay_in_progress` → remain `In Progress` and continue autonomously later
  - `change_scope` → create/suggest narrower follow-up tasks, optionally move the current issue to `Backlog` or `Blocked`
  - `needs_human_review` → move to `In Review` with a clear rationale
- If an issue goes to **Testing**, create a follow-up run contract:
  - attach/emit QA packet
  - optionally schedule or trigger automated validation
  - optionally request human approval depending on policy
- Store structured post-run decision metadata in session/run metadata so Jax Control Plane can render it.

**Files likely touched:**
- `gateway/platforms/linear.py`
- `gateway/platforms/base.py` (if shared outcome transport is needed)
- session metadata persistence path
- Jax Control Plane read model / project registry surfaces (later slice)
- `tests/gateway/test_linear.py`

**Tests needed:**
- success transport does not automatically imply `Testing`
- `backlog` decision routes issue out of active execution
- `change_scope` decision leaves a clear rationale and follow-up path
- `retry` vs `stay_in_progress` are distinct
- no Testing state -> fallback still only happens when decision is `ready_for_testing`

---

### Slice D — Project-type completion actions
**Objective:** Decide merge/docs/follow-up behavior from project type instead of ad-hoc instructions.

**Suggested defaults:**

| Project type | Merge policy | Docs policy | Testing policy |
|---|---|---|---|
| infra | `after_testing` or `after_approval` | `required_on_completion` | `hybrid` |
| internal_tool | `after_testing` | `required_on_completion` | `manual_packet` |
| backend | `after_approval` | `required_on_completion` | `hybrid` |
| web | `after_approval` | `required_on_completion` | `hybrid` |
| mobile | `after_approval` | `required_on_completion` | `manual_packet` |
| research | `never` | `required_on_completion` | `none` |

**Behavior:**
- Resolve project type from project metadata / registry pointers.
- At completion:
  - docs required? create/update required docs task or block completion until done
  - merge allowed? post approval or merge automatically if policy permits
  - no merge path? keep issue in Testing/In Review with docs/evidence only

---

### Slice E — Autonomous review loop + proactive task creation
**Objective:** Continuously review projects and proactively create missing tasks without clobbering active work.

**Behavior:**
- Introduce a separate review runner with bounded concurrency (e.g. max 1 review run globally).
- For each project review:
  1. load docs-of-record + repo current-state + recent issues
  2. inspect recent run failures, alerts, and security findings
  3. compare against roadmap / milestone / known gaps
  4. identify missing concrete work
  5. search Linear for duplicates / near-duplicates
  6. create issue only if actionable
  7. attach dependency links where applicable
- Review-created tasks should be eligible for:
  - bug fixes
  - security findings
  - reliability hardening
  - roadmap follow-through
  - documentation / continuity maintenance
  - operator/product improvements
- Review-created tasks should include:
  - project
  - task type
  - execution mode hint
  - dependency links
  - docs/repo pointers
  - source rationale (`roadmap_gap`, `run_failure`, `security_finding`, `docs_drift`, `operator_improvement`)

**Important:** review loop should not share concurrency budget with active implementation sessions.

---

### Slice F — Approval UX polish
**Objective:** Turn approvals into workflow-native messages.

**Behavior:**
- Normalize approval requests into a structured packet:
  - action summary
  - why approval is required
  - exact command / PR merge / destructive action
  - expected effect
  - issue/project context
- Post into the active Linear session and/or operator channel.
- On approval resolution, resume the waiting run and post status.

**Suggested approval categories:**
- `merge_pr`
- `prod_mutation`
- `destructive_command`
- `docs_publish`
- `external_side_effect`

---

## Recommended implementation order

1. **Dependency-aware blocking**
2. **Workflow-level retry classification**
3. **Testing follow-up orchestration**
4. **Project-type completion policies**
5. **Approval UX polish**
6. **Autonomous review loop + task creation**

Reason: slices 1–5 make active task execution reliable first; slice 6 safely expands automation after execution policy is trustworthy.

---

## Concrete data model additions

### Issue/session metadata
Add fields into `self._session_info` and persistent session metadata:
- `workflow_policy`
- `dependency_issue_ids`
- `unresolved_dependency_ids`
- `retry_attempt_count`
- `retry_last_error_class`
- `retry_next_attempt_at`
- `testing_packet_type`
- `testing_required`
- `merge_policy`
- `docs_required`
- `approval_required_for`

### Optional new labels / conventions
Keep current labels, but consider adding:
- `type:engineering`
- `type:ops`
- `type:research`
- `mode:autonomous_with_testing`
- `mode:human_gate`

Avoid using labels for blocked status; use the actual Linear **Blocked** state.

---

## Test plan

Run and extend focused tests in:
- `tests/gateway/test_linear.py`

Add new coverage for:
1. dependency gating
2. retry classification and exhaustion
3. testing follow-up metadata emission
4. project-type policy resolution
5. approval packet generation

Suggested commands:
```bash
source venv/bin/activate
python -m pytest tests/gateway/test_linear.py -q
python -m pytest tests/gateway/test_linear.py tests/hermes_cli/test_config.py tests/tools/test_local_env_blocklist.py -q
```

---

## Immediate product recommendations

If you want a strong default policy right now:

- `LINEAR_DEFAULT_EXECUTION_MODE=autonomous_with_testing`
- `LINEAR_SUPPORTED_TASK_TYPES=engineering,ops,research`
- use real Linear issue dependencies (`blockedBy` / `blocks`) as the gating mechanism
- use `Blocked` status/state, not blocked label
- require QA packet for every success path except explicit `autonomous_dev` infra maintenance
- require approval for PR merges unless the project type is low-risk internal infra and tests/docs/smoke checks all passed

---

## Suggested next implementation slice

**Start with Slice A + Slice B together:**
- dependency-aware blocking
- workflow-level retry classification

Those two changes will immediately make Jax stop doing obviously dumb work and reduce noisy failure loops.
