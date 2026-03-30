# PawPal+ (Module 2 Project)

You are building **PawPal+**, a Streamlit app that helps a pet owner plan care tasks for their pet.

## Scenario

A busy pet owner needs help staying consistent with pet care. They want an assistant that can:

- Track pet care tasks (walks, feeding, meds, enrichment, grooming, etc.)
- Consider constraints (time available, priority, owner preferences)
- Produce a daily plan and explain why it chose that plan

Your job is to design the system first (UML), then implement the logic in Python, then connect it to the Streamlit UI.

## What you will build

Your final app should:

- Let a user enter basic owner + pet info
- Let a user add/edit tasks (duration + priority at minimum)
- Generate a daily schedule/plan based on constraints and priorities
- Display the plan clearly (and ideally explain the reasoning)
- Include tests for the most important scheduling behaviors

## Features

- **Owner profile** — Set your name and daily availability window (e.g., 08:00–20:00). The app respects this window when placing every task.
- **Multi-pet support** — Register as many pets as you need. Each pet has its own task list; the scheduler plans across all of them at once.
- **Rich task model** — Every task has a type (walk, feeding, medication, etc.), priority (high/medium/low), duration, optional fixed time, and optional recurrence.
- **Priority-aware scheduling** — Flexible tasks are placed in priority order (HIGH before MEDIUM before LOW) within the available window. Fixed-time tasks are anchored to their requested slot.
- **Recurring task auto-spawn** — Completing a recurring task (e.g., daily medication) automatically adds the next occurrence at `scheduled_time + recurrence_interval`, so nothing falls through the cracks.
- **Sort & filter** — Browse tasks sorted by time or priority, and filter by pet, completion status, task type, or priority level.
- **Conflict detection** — After generating a schedule, the app scans for overlapping time windows and surfaces a clear warning with details — it never silently drops a task.
- **Schedule explanation** — Every slot includes a plain-English reason ("Scheduled at 09:00 — high priority") so the owner understands why the plan looks the way it does.
- **Overdue tracking** — The sidebar flags tasks whose scheduled time has passed and that haven't been completed.

## Bonus Features

### Weighted Priority Scheduling (Challenge 1)
Tasks are scored on three factors: **priority level** (10 pts per tier), **health importance** (medication +4, appointment +3, feeding +2, walk +1), and **urgency** (overdue +5, due within 2 h +3, due today +1). This lets the scheduler recognise that an overdue medication is more urgent than a non-urgent high-priority play session. Toggle "Smart weighted scheduling" in the Schedule tab to activate it.

### JSON Persistence (Challenge 2)
All owner, pet, and task data is auto-saved to `data.json` after every change and auto-loaded on startup — no manual save button needed. Implemented via `Owner.save_to_json()` / `Owner.load_from_json()` using custom `to_json_dict()` / `from_json_dict()` methods at each class level.

### Emoji & Priority Badges (Challenge 4)
Task types have icons (🍖 🦮 💊 🏥 ✂️ 🎾 📌) and priority levels use colour-coded badges (🔴 high / 🟡 medium / 🟢 low) in every table, dropdown, and task card. Constants are defined once in `pawpal_system.py` and shared by both the CLI and Streamlit UI.

## Smarter Scheduling

PawPal+ goes beyond a simple to-do list with four algorithmic features:

| Feature | How it works |
|---|---|
| **Sort by time** | `Scheduler.sort_by_time()` orders tasks by `scheduled_time` ascending using a `lambda` key; tasks with no fixed time are sorted to the end. |
| **Filter tasks** | `Scheduler.filter_tasks()` accepts any combination of `pet_name`, `completed`, `task_type`, and `priority` keyword arguments and applies them with AND logic. |
| **Recurring task auto-spawn** | When `Pet.complete_task(id)` is called, it calls `Task.mark_complete()`. If the task is recurring, `mark_complete()` returns a new `Task` with `scheduled_time = original + recurrence_interval_hours`; the pet's task list is updated automatically. |
| **Conflict detection** | After placing all tasks, `Scheduler.detect_conflicts()` scans `ScheduleEntry` windows for overlaps and returns warning pairs. Conflicts are displayed in the UI and CLI rather than silently dropping tasks. |

## Testing PawPal+

### Run the tests

```bash
python -m pytest
```

### What the tests cover

| Category | Tests | What is verified |
|---|---|---|
| `Task` lifecycle | 6 | `mark_complete()`, `is_overdue()`, `to_dict()` |
| `Pet` management | 6 | `add_task()`, `remove_task()`, `get_tasks_by_priority()`, duplicate guard |
| `Owner` management | 4 | `add_pet()`, `remove_pet()`, `get_all_tasks()`, `available_minutes()` |
| Scheduler — schedule generation | 4 | Returns entries, respects priority, skips tasks that don't fit, exactly-fills-window |
| Scheduler — sort by time | 2 | Ascending order, unscheduled tasks last |
| Scheduler — filter | 3 | By pet name, by completion, combined AND filters |
| Recurring tasks | 3 | Auto-spawn on completion, no spawn for one-off, correct time offset |
| Conflict detection | 3 | Overlap found, no overlap, exact same start time |
| Edge cases | 9 | Empty owner/pet, filter with no results, explain before generate, bad IDs, task that exactly fills window, recurring with no base time |

**Total: 40 tests — 40 passing**

### Confidence level

★★★★☆ (4/5)

The core scheduling logic (priority ordering, window fitting, recurring tasks, conflict detection) is thoroughly tested including edge cases. The one gap is integration with Streamlit session state — those code paths are not covered by automated tests and would need manual or browser-based testing.

## Getting started

### Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Suggested workflow

1. Read the scenario carefully and identify requirements and edge cases.
2. Draft a UML diagram (classes, attributes, methods, relationships).
3. Convert UML into Python class stubs (no logic yet).
4. Implement scheduling logic in small increments.
5. Add tests to verify key behaviors.
6. Connect your logic to the Streamlit UI in `app.py`.
7. Refine UML so it matches what you actually built.
