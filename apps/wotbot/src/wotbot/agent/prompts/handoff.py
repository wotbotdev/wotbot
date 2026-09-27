"""Prompt snippet enabling branch-to-branch handoff.

Appended to the action-branch system prompts (control, analysis, jobs,
virtual_things, discovery) only when ``agent_handoff_enabled`` is set. It tells the model
how to continue into another branch via the ``route_to`` tool.
"""

HANDOFF_PROMPT = """\

## Continuing In Another Branch
If the task requires tools that are unavailable in this branch, call `route_to` \
with the branch that has them and stop. For example, aggregation and model \
input preparation need analysis and its run_code tool. Hand off before \
fetching large histories or repeating inspections that cannot complete the task.
When finishing one part of a request exposes follow-up work in another area, \
complete the current part, then hand off. The handoff happens automatically.
If a Thing the user names is not in the local catalog, hand off to discovery \
to find and onboard it from its source; never continue with a different Thing.

Valid intents:
- **control**: perform a Thing action or build a control panel/widget.
- **analysis**: read, explore, visualise, or compute over Thing/graph data.
- **jobs**: create, inspect, run, or debug an automation job.
- **virtual_things**: create, update, or test a computed/virtual Thing.
- **discovery**: find and onboard a Thing from a registered external source.

Only hand off when another branch's tools are needed. If the request is \
complete, finish your response.
"""
