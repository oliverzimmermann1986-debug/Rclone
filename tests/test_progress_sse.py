"""Performance and lifecycle contracts for the shared progress SSE producer."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
import yaml
from fastapi import Request

from app import auth, config_store
from app.config_store import Config
from app.routes import api_jobs

from app.routes.api_jobs import _ProgressBroadcaster


@pytest.mark.parametrize("invalidate", ["revocation", "expiry"])
def test_stream_stops_before_publishing_new_data_after_authority_ends(
    tmp_path, monkeypatch, invalidate
):
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "web": {
                    "username": "admin",
                    "secret_key": "stream-test-secret-at-least-32-characters",
                    "session_version": 1,
                }
            }
        ),
        encoding="utf-8",
    )
    store = Config(path)
    monkeypatch.setattr(config_store, "_config", store)
    token = auth.create_session("admin")
    request = Request(
        {
            "type": "http",
            "headers": [(b"cookie", f"{auth.SESSION_COOKIE}={token}".encode())],
        }
    )

    async def connected():
        return False

    monkeypatch.setattr(request, "is_disconnected", connected)

    async def scenario():
        queue = asyncio.Queue()
        await queue.put('{"sequence":1}')

        class Broadcaster:
            released = False

            async def subscribe(self):
                return queue

            async def unsubscribe(self, released):
                assert released is queue
                self.released = True

        broadcaster = Broadcaster()
        monkeypatch.setattr(api_jobs, "_progress_broadcaster", broadcaster)
        response = await api_jobs.progress_stream(request)
        stream = response.body_iterator
        assert '"sequence":1' in await anext(stream)
        if invalidate == "revocation":
            store.update(lambda data: data["web"].update(session_version=2))
        else:
            monkeypatch.setattr(auth, "session_max_age", lambda: -1)
        await queue.put('{"sequence":2}')
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert broadcaster.released

    asyncio.run(scenario())


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
    snapshot_lock = threading.Lock()

    def snapshot():
        nonlocal call_count
        with snapshot_lock:
            call_count += 1
            return {"sequence": call_count}

    async def scenario() -> None:
        broadcaster = _ProgressBroadcaster(snapshot, interval=0.01)
        published = asyncio.Event()
        original_publish = broadcaster._publish

        def observe_publish(payload):
            original_publish(payload)
            if json.loads(payload)["sequence"] >= 3:
                published.set()

        broadcaster._publish = observe_publish
        queue = await broadcaster.subscribe()
        await asyncio.wait_for(published.wait(), timeout=10)

        assert call_count >= 3
        assert queue.maxsize == 1
        assert queue.qsize() == 1
        latest = json.loads(queue.get_nowait())
        assert latest == json.loads(broadcaster._last_payload)
        assert latest["sequence"] >= 3

        waiting = asyncio.create_task(queue.get())
        await broadcaster.stop()
        assert await asyncio.wait_for(waiting, timeout=1) is None
        assert broadcaster.subscriber_count == 0
        assert broadcaster._task is None

    asyncio.run(scenario())
