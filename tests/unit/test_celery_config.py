"""Celery wiring: tasks registered, queues routed distinctly, safe serialization."""

from app.services import dispatcher as d
from app.workers.celery_app import celery_app


def test_all_dispatched_tasks_are_registered():
    for name in (d.SYNC_ACCOUNT, d.ANALYZE_EMAIL, d.GENERATE_DRAFT, d.DELIVER_NOTIFICATION, d.CLEANUP):
        assert name in celery_app.tasks


def test_each_queue_has_its_own_routing_key():
    # Regression: queues sharing the default routing key receive every message (duplicate execution).
    keys = [(q.exchange.name, q.routing_key) for q in celery_app.conf.task_queues]
    assert len(keys) == len(set(keys))
    assert {q.name for q in celery_app.conf.task_queues} >= set(d.QUEUES.values())


def test_json_only_serialization_and_late_acks():
    assert celery_app.conf.accept_content == ["json"]
    assert celery_app.conf.task_acks_late and celery_app.conf.task_reject_on_worker_lost
