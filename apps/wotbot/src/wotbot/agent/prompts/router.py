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
Thing, prefer analysis over chat. \
Analysis covers data reachable through Things already in the catalog; asking \
what an external source offers before anything is onboarded is **discovery**.
- **jobs**: Create, list, inspect, run, debug, delete, or explain automation jobs. \
This includes time-based jobs, event-based jobs, prompt jobs, analysis jobs, \
job status, job run history, job "last result" questions, and user-facing \
notifications, reminders, or actions that should happen when a condition is met.
- **virtual_things**: Create, update, delete, disable, debug, or test standalone \
computed/synthetic/virtual Things, including computed properties, computed \
actions, emitted events, reusable threshold-crossing virtual events, and handler bindings.
- **discovery**: Create a new W3C Thing Description (TD) from user-provided \
information, or find and onboard candidates from an external source. \
This includes: describing a new Thing's properties/actions/events to build and \
store the TD; providing an API spec to be turned into a Thing; asking to "add", \
"create", or "register" something not yet in the catalog; searching external \
sources such as data portals, service catalogs, or remote registries; and \
asking what a registered external source contains or offers, such as browsing \
or listing its datasets, endpoints, or assets. \
Use this intent when the user explicitly mentions an external source or URL, \
including a question *about* such a source rather than a request to add from \
it, or when you have tried \
things_search locally and found no results and the user wants to look further \
afield. \
Do NOT use for virtual/computed Things (use virtual_things) or for searching \
existing Things (any other intent can search the local catalog).

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
