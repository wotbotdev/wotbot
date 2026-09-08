"""Worker-only tool for storing validated structured records from prompt jobs."""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from wotbot.agent.tools.contracts import tool
from wotbot.jobs.records import VirtualRecordStore


@tool
async def submit_job_record(
    data: dict[str, Any],
    config: RunnableConfig,
    raw_input: str | None = None,
    confidence: float | None = None,
) -> dict[str, Any]:
    """Validate and store one structured record for the current background job run."""
    configurable = config.get("configurable", {})
    job_id = str(configurable.get("job_id") or "")
    run_id = str(configurable.get("run_id") or "")
    virtual_thing_id = str(configurable.get("virtual_thing_id") or "")
    if not job_id or not run_id or not virtual_thing_id:
        return {
            "ok": False,
            "repairable": False,
            "error": "submit_job_record requires job_id, run_id, and virtual_thing_id",
        }

    try:
        record = VirtualRecordStore().submit_record(
            thing_id=virtual_thing_id,
            source_job_id=job_id,
            source_run_id=run_id,
            data=data,
            raw_input=raw_input,
            confidence=confidence,
        )
    except ValueError as exc:
        return {"ok": False, "repairable": True, "error": str(exc)}
    except KeyError as exc:
        return {"ok": False, "repairable": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "repairable": False, "error": str(exc)}

    return {
        "ok": True,
        "status": "record_submitted",
        "record": record,
        "job_id": job_id,
        "run_id": run_id,
        "virtual_thing_id": virtual_thing_id,
    }
