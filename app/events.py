"""Alarm event bus: in-memory queue by default, Kafka or Google Pub/Sub in production.

Each message is one alarm (JSON). The consumer stores it and opens an incident for major and
critical alarms that are not already part of an open incident.
"""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable

from app.config import get_settings
from app.observability import log, metrics

Handler = Callable[[dict], Awaitable[None]]


class InMemoryBus:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict] = asyncio.Queue()

    async def publish(self, alarm: dict) -> None:
        await self.queue.put(alarm)

    async def consume(self, handler: Handler) -> None:
        while True:
            alarm = await self.queue.get()
            try:
                await handler(alarm)
            finally:
                self.queue.task_done()


class KafkaBus:
    """Kafka consumer/producer (aiokafka). Messages are JSON alarms on ALARM_TOPIC."""

    def __init__(self, bootstrap: str, topic: str) -> None:
        self.bootstrap, self.topic = bootstrap, topic

    async def publish(self, alarm: dict) -> None:
        from aiokafka import AIOKafkaProducer

        producer = AIOKafkaProducer(bootstrap_servers=self.bootstrap)
        await producer.start()
        try:
            await producer.send_and_wait(self.topic, json.dumps(alarm).encode())
        finally:
            await producer.stop()

    async def consume(self, handler: Handler) -> None:
        from aiokafka import AIOKafkaConsumer

        consumer = AIOKafkaConsumer(self.topic, bootstrap_servers=self.bootstrap, group_id="assurance-agent",
                                    enable_auto_commit=False)
        await consumer.start()
        try:
            async for msg in consumer:
                await handler(json.loads(msg.value))
                await consumer.commit()  # commit only after the alarm was handled
        finally:
            await consumer.stop()


class PubSubBus:
    """Google Cloud Pub/Sub. Uses a streaming pull subscription named <topic>-agent."""

    def __init__(self, project: str, topic: str) -> None:
        self.project, self.topic = project, topic

    async def publish(self, alarm: dict) -> None:
        from google.cloud import pubsub_v1

        publisher = pubsub_v1.PublisherClient()
        publisher.publish(publisher.topic_path(self.project, self.topic), json.dumps(alarm).encode()).result()

    async def consume(self, handler: Handler) -> None:
        from google.cloud import pubsub_v1

        loop = asyncio.get_running_loop()
        sub = pubsub_v1.SubscriberClient()
        path = sub.subscription_path(self.project, f"{self.topic}-agent")

        def callback(message) -> None:
            fut = asyncio.run_coroutine_threadsafe(handler(json.loads(message.data)), loop)
            fut.result()
            message.ack()

        future = sub.subscribe(path, callback=callback)
        await asyncio.to_thread(future.result)


def make_bus():
    s = get_settings()
    if s.event_bus == "kafka":
        return KafkaBus(s.kafka_bootstrap, s.alarm_topic)
    if s.event_bus == "pubsub":
        return PubSubBus(s.vertex_project or "", s.alarm_topic)
    return InMemoryBus()


def alarm_handler(service, store) -> Handler:
    async def handle(alarm: dict) -> None:
        metrics.inc("alarms_received", severity=alarm.get("severity", "unknown"))
        if not store.get_alarm(alarm["id"]):
            store.insert_alarm(alarm)
        if alarm.get("severity") in {"critical", "major"} and alarm["id"] not in service.by_alarm:
            try:
                await service.open_incident(alarm["id"])
            except Exception as exc:  # keep consuming; the failure is visible in logs and metrics
                metrics.inc("incident_failures")
                log.error(json.dumps({"event": "incident_failed", "alarm": alarm["id"], "error": str(exc)}))

    return handle
