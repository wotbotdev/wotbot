"""Limits and capability checks shared by both ends of the validation bridge."""

MAX_READS = 20
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_DOCUMENT_MESSAGE_BYTES = 128 * 1024 * 1024
UNTESTED_OPERATIONS = [
    "writeProperty",
    "invokeAction",
    "observeProperty",
    "subscribeEvent",
    "user_interactions",
]


def request_problem(request: object, capabilities: list[dict]) -> tuple[str, str] | None:
    if not isinstance(request, dict) or not isinstance(request.get("op"), str):
        return "capability", "Invalid validation bridge request"
    op = request["op"]
    if op != "readProperty":
        return "bridge_blocked", f"{op} is untested: validation permits only property reads"
    thing_id, name = request.get("thingId"), request.get("name")
    if not isinstance(thing_id, str) or not thing_id or not isinstance(name, str) or not name:
        return "capability", "A property read requires a Thing ID and property name"
    if request.get("uriVariables") is not None and not isinstance(request["uriVariables"], dict):
        return "capability", "uriVariables must be an object"
    if not any(
        cap.get("thingId") == thing_id
        and op in cap.get("ops", [])
        and (not cap.get("affordances") or name in cap["affordances"])
        for cap in capabilities
    ):
        return "capability", f"Not permitted: {op} {thing_id} {name}"
    return None
