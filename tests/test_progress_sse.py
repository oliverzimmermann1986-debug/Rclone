"""Performance and lifecycle contracts for the shared progress SSE producer."""

from __future__ import annotations

import asyncio
import json
import threading

from app.routes.api_jobs import _ProgressBroadcaster


def test_forty_clients_share_one_expensive_snapshot_and_release_resources():
    call_count = 0
    started = threading.Event()
    release = threading.Event()

    def snapshot():
        nonlocal call_count
        call_count += 1
        started.set()
        assert release.wait(timeout=2)
        return {"running": True, "sequence": call_count}

    async def scenario() -> None:
        broadcaster = _ProgressBroadcaster(snapshot, interval=60)
        queues = [await broadcaster.subscribe() for _ in range(40)]
        assert broadcaster.subscriber_count == 40
        assert await asyncio.to_thread(started.wait, 2)
        release.set()
        payloads = await asyncio.gather(
            *(asyncio.wait_for(queue.get(), timeout=2) for queue in queues)
        )

        assert len(set(payloads)) == 1
        assert json.loads(payloads[0])["running"] is True
        assert call_count == 1

        await asyncio.gather(*(broadcaster.unsubscribe(queue) for queue in queues))
        assert broadcaster.subscriber_count == 0
        assert broadcaster._task is None

    asyncio.run(scenario())


def test_slow_client_queue_keeps_only_latest_snapshot_and_shutdown_unblocks_it():
    call_count = 0

    def snapshot():
        nonlocal call_count
        call_count += 1
        return {"sequence": call_count}

    async def scenario() -> None:
        broadcaster = _ProgressBroadcaster(snapshot, interval=0.01)
        queue = await broadcaster.subscribe()
        await asyncio.sleep(0.08)

        assert call_count > 1
        assert queue.maxsize == 1
        assert queue.qsize() == 1
        latest = json.loads(queue.get_nowait())
        assert latest["sequence"] == call_count

        waiting = asyncio.create_task(queue.get())
        await broadcaster.stop()
        assert await asyncio.wait_for(waiting, timeout=1) is None
        assert broadcaster.subscriber_count == 0
        assert broadcaster._task is None

    asyncio.run(scenario())
