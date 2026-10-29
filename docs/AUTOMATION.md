# Automation runtime (release 0.7.0)

NEXUS used to map one sentence to one action and say "Opened X" as soon as the launch call returned. It now has a runtime that
plans a task as a short list of tool calls, runs them, **looks at the result of each one**, and only then says what happened.
A weak local model is compensated by orchestration, not by trusting it more: the model proposes, plain code validates,
runs, checks and decides what to do when something goes wrong.

```
request ─► deterministic interpretation ─► (only if needed) planner model ─► validated Plan
            "close it", "organize my downloads"      one JSON object, one repair try
                                                                   │
        ┌──────────────────────────────────────────────────────────┘
        ▼
   for each step:  resolve $refs → check `when` → approval policy → pre-check ("already true?")
                   → invoke (timeout) → postcondition check against the environment
                   → on failure: retry / recovery tool / replan / stop     → checkpoint saved
        ▼
   report: done · not done · could not be checked          (state: one AgentRun row, resumable)
```

## Files

| File | Role |
|---|---|
| `app/agent/capabilities.py` | Tool contract (`Tool`, `ToolResult`, `ToolContext`) and the `Registry` (discovery by purpose, validation, invoke, diagnose) |
| `app/agent/builtin_tools.py` | The 29 tools: apps, windows, reminders/notes/to-dos, messages, email, calendar, web, approved-folder files, project tools, volume, diagnose |
| `app/agent/plan.py` | `Plan`/`PlanStep`, validation, `$s1.key` references, `when` conditions, the `organize_folder` workflow |
| `app/agent/planner.py` | Deterministic interpretation, compact planner prompt, JSON extraction and one repair attempt |
| `app/agent/runtime.py` | State machine, budgets, approval policy, failure analysis, resume, lessons, saved workflows |
| `app/services/automation.py` | Glue between a conversation turn and the runtime (when to plan, how to phrase the reply, the yes/no card) |
| `app/api/routes_runs.py` | `/api/runs…`, `/api/capabilities`, `/api/capabilities/diagnose`, `/api/workflows` |
| `frontend/src/features/automation/` | The **Automation** page |

## The tool contract

Each tool declares: name, description, a pydantic **argument schema**, **risk** (`read` / `low` / `medium` / `high`), what it
**needs** (apps, windows, files, workspace, network, contacts), purpose **tags** for discovery, a **timeout**, **retries**,
whether repeating it is harmless (**idempotent**), an **`already_done`** pre-check, a **`verify`** postcondition that reads the
environment, an optional **`poll`** for work the user decides elsewhere, a **recovery** map (`error class → tool that clears
the obstacle`), an availability **health check** and honest **limits**. A tool never raises: failures are results with an
error class (`transient`, `timeout`, `not_found`, `blocked`, `invalid`, `permission`, `unavailable`, `verify_failed`), and
the class decides what happens next. A tool that returns something that is not a `ToolResult` is a failed step, not a success.
A step that reports success but fails its postcondition is **turned into a failure**.

## Observe, act, verify

Verification reads the world, in this order of preference (the first that exists for the tool): the database (reminders,
notes), the file system (moved files exist, sources are gone), structured state (change-set and command status), the window
list (an app's window appeared / disappeared, a dialog is in the way), the foreground window. Handled situations:
app already open (nothing is launched), app slow to appear (polls up to 6 s), app never appears (the step fails and the
report says so), a blocking dialog ("Update available") is **not** counted as the app being open, then `window.dismiss`
presses Esc and the app is re-checked, closed apps, stale `$refs`.
**Not implemented:** Windows UI Automation trees, DOM access, screenshots and vision models. Installed vision-capable models
are listed on the Automation page as "installed but not used". Window listing, focus and close are Windows-only and have
**not been run on Windows** (see STATUS.md).

## Autonomy

`read` and `low` tools always run alone. `medium` (closing an app, dismissing a dialog) runs alone by default; set
*Runs without asking* to "Only harmless things" and it asks. `high` (moving files, pressing Send, editing code, running a
command) **always** asks. Edits and commands proposed by the model still go through the existing change-set and command
approval screens (their tools are `self_gated`: they only create the proposal). Approval can be given by typing/saying "yes",
the card at the bottom right, or the Automation page. Folders must be in *approved folders*; paths cannot leave them; moves
never overwrite.

## Durable runs

One `AgentRun` row holds goal, plan, per-step state/results, checkpoint, pending decision and metrics. States: `queued`,
`planning`, `running`, `waiting_for_approval`, `waiting_for_input`, `retrying`, `completed`, `partially_completed`, `failed`,
`cancelled`. Nothing runs in the background: a run executes while a request is awaiting it, and a run that needs a person is
parked until they answer (approve / decline / cancel / resume / "check again").
After a restart, active runs become `waiting_for_input` ("Resume it?"). Resume re-checks the step that was in flight against
the world; an idempotent step is simply re-evaluated, a step with an unknown outcome that is **not** safe to repeat asks
first ("repeating it could do it twice"). A run is `completed` only if no step failed its check; steps that cannot be observed
are listed in the report ("I couldn't check that … really happened").

Budgets (Settings → Automation, per run): steps (8), tool calls (30), model calls (3), seconds (180), retries (2), replans (1).

## Workflows, memory, lessons

* `organize_folder` is built in (preview → approval → move → check). A **completed** run can be saved as a workflow; its
  steps are re-validated and re-observed each time it runs.
* A verified failure→fix pair (e.g. "app.open blocked by a dialog → window.dismiss, then retry") is stored as a lesson and
  offered to the planner for the relevant tools. Lessons and memories are passed as *data, not instructions*.
* The existing `TaskEngine` coding templates are unchanged; they are not re-expressed as runtime workflows.

## Local-model economy

Deterministic things never reach the model ("open X", "close it", "organize my downloads", reminders, notes). A request only
reaches the planner if it sounds like an action **and** mentions something a tool deals with, so "make it shorter" never costs
a planner call. The planner prompt carries the 8 most relevant available tools (≈230–450 tokens instead of ≈830 for all 29),
asks for one JSON object, is limited to 500 output tokens and 90 s, and gets one repair attempt with the exact defect. If the
model is offline or answers badly the request falls back to the previous router / chat. Nothing is downloaded automatically.

## Known limitations

* Planner quality on **real Qwen 2.5 7B has not been measured** (no Ollama in the build environment). The benchmark uses a
  scripted model and therefore measures orchestration only. Expect 10–30 s per planner call on this CPU; deterministic paths are instant.
* Everything touching real windows (list, focus, close, Esc, clipboard) is untested on Windows.
* Messages (`message.prepare/send`) work through the existing skills; delivery cannot be observed.
  The conversation still routes "message Mom…" through the 0.6.0 skills pipeline, which has its own confirm-before-send card.
* A timed-out tool is abandoned, not killed (Python threads cannot be killed); cancellation takes effect between steps.
* Replanning exists and is tested only through scripted input.
