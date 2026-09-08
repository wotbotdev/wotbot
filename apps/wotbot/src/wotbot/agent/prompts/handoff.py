"""Prompt snippet enabling branch-to-branch handoff.

Appended to the action-branch system prompts (control, analysis, jobs,
virtual_things) only when ``agent_handoff_enabled`` is set. It tells the model
how to continue into another branch via the ``route_to`` tool.
"""

HANDOFF_PROMPT = """\

## Continuing Into Another Task
If the user's request implies follow-up work that belongs to a different area, \
finish the current task first, then call `route_to` once with the appropriate \
intent and stop. The handoff happens automatically — do not narrate it.

Valid intents:
- **control**: perform a Thing action or build a control panel/widget.
- **analysis**: read, explore, visualise, or compute over Thing/graph data.
- **jobs**: create, inspect, run, or debug an automation job.
- **virtual_things**: create, update, or test a computed/virtual Thing.

Only hand off when the current task is genuinely complete and more work is \
clearly needed. If nothing further is required, do not call `route_to` — just \
finish your response.
"""
