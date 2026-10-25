"""A synchronous stand-in for the Celery worker: executes recorded tasks through the real jobs."""

from __future__ import annotations

import uuid

from app.db.models import BackgroundTask
from app.services import dispatcher as d
from app.services.dispatcher import RecordingDispatcher
from app.workers import jobs


async def drain(dispatcher: RecordingDispatcher, sessionmaker, store, *, max_steps: int = 200) -> list[str]:
    """Run queued tasks (and the tasks they enqueue) until the queue is empty. Returns executed names."""
    executed: list[str] = []
    steps = 0
    while dispatcher.sent and steps < max_steps:
        name, kwargs, _ = dispatcher.sent.pop(0)
        steps += 1
        executed.append(name)
        try:
            if name == d.SYNC_ACCOUNT:
                await jobs.run_sync_job(kwargs["task_id"], session_factory=sessionmaker, store=store)
            elif name == d.ANALYZE_EMAIL:
                if "email_id" in kwargs:
                    await jobs.run_analysis_job(kwargs["email_id"], force=kwargs.get("force", False),
                                                session_factory=sessionmaker)
                else:
                    async with sessionmaker() as s:
                        task = await s.get(BackgroundTask, uuid.UUID(kwargs["task_id"]))
                        email_id, force = task.payload["email_id"], task.payload.get("force", True)
                    await jobs.run_analysis_job(email_id, force=force, task_id=kwargs["task_id"],
                                                session_factory=sessionmaker)
            elif name == d.GENERATE_DRAFT:
                await jobs.run_generate_draft_job(kwargs["task_id"], session_factory=sessionmaker)
            elif name == d.DELIVER_NOTIFICATION:
                await jobs.run_delivery_job(kwargs["delivery_id"], session_factory=sessionmaker)
            elif name == d.CLEANUP:
                await jobs.run_cleanup_job(session_factory=sessionmaker)
        except jobs.RetryableJobError:
            executed.append(f"{name}:retry")
    return executed
