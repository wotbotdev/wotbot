from wotbot.catalog.models import ThingRecord
from wotbot.catalog.store import serialize_document
from wotbot.workspaces import current_workspace_id

# Consumers that derive data from the event (search indexer, RDF store) run
# outside any request and use this field to write into the right workspace.
# It is not part of the Thing Description; normalize_thing_td_payload drops it.
WORKSPACE_EVENT_KEY = "workspaceId"


def build_change_event(event_type: str, record: ThingRecord) -> dict[str, object]:
    return {
        **serialize_document(record),
        "eventType": event_type,
        "hash": record.document_hash,
        WORKSPACE_EVENT_KEY: record.workspace_id,
    }


def build_remove_event(thing_id: str) -> dict[str, str]:
    return {
        "eventType": "remove",
        "id": thing_id,
        WORKSPACE_EVENT_KEY: current_workspace_id(),
    }
