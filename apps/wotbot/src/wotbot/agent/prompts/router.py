ROUTER_PROMPT = """\
Classify the user's message into exactly one intent.

- **chat**: Greetings, general questions, small talk, help requests. Only use \
this when the user is NOT asking about the state, data, or capabilities of a \
registered Thing or resource, and is NOT asking to build any kind of \
interface. Building a UI is never chat.
- **control**: Perform an action on a Thing (start/stop, set value, trigger), \
OR build an interactive control panel, UI, widget, or interface to operate one \
or more Things (e.g. "build a control panel for the conveyor", "give me buttons to \
operate the test rig", "make a widget to configure the service").
- **analysis**: Read, explore, visualise, or understand any data from Things, \
registered SPARQL endpoints, or external knowledge graphs. \
This includes simple current-value questions like "what is the machine status", \
"where is the shipment", or "what is the API quota", as well as historical \
exploration, charts, piping data between Things, and building a live dashboard or \
monitoring interface to view Thing data. If the user asks to find or use a SPARQL endpoint \
Thing, RDF graph, knowledge graph, or RDF entity, classify as analysis. \
If the user is asking about operational or real-world state represented by a \
Thing, prefer analysis over chat.
- **jobs**: Create, list, inspect, run, debug, delete, or explain automation jobs. \
This includes time-based jobs, event-based jobs, prompt jobs, analysis jobs, \
job status, job run history, job "last result" questions, and user-facing \
notifications, reminders, or actions that should happen when a condition is met.
- **virtual_things**: Create, update, delete, disable, debug, or test standalone \
computed/synthetic/virtual Things, including computed properties, computed \
actions, emitted events, reusable threshold-crossing virtual events, and handler bindings.

If a request mixes immediate Thing control with creating an automation for later, \
classify as **jobs**.
If a request asks for a durable computed/synthetic/virtual Thing rather than a \
scheduled automation job, classify as **virtual_things**.
If the user says "notify me", "alert me", "remind me", or asks to run a real \
action when a condition is met, classify as **jobs**. If the user asks to expose \
that condition as a reusable virtual event/property Thing, classify as \
**virtual_things**.
Any request to build, create, or make a UI, panel, dashboard, widget, or \
interface backed by registered Things is **control** (to operate Things) or \
**analysis** (to view data) — never chat.
"""
