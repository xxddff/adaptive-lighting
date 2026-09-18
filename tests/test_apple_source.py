import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.components.adaptive_lighting import apple_source

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "apple-snapshot.json").read_text(),
)


class Response:
    """Minimal asynchronous HTTP response for transport tests."""

    def __init__(self, snapshot):
        """Store a response or delayed future."""
        self.snapshot = snapshot

    async def __aenter__(self):
        """Open the response."""
        return self

    async def __aexit__(self, *_args):
        """Close the response."""
        return

    def raise_for_status(self):
        """Return an HTTP success."""
        return

    async def json(self):
        """Return the JSON document, waiting if requested."""
        if isinstance(self.snapshot, asyncio.Future):
            return await self.snapshot
        return copy.deepcopy(self.snapshot)


@pytest.fixture
async def source_env(hass):
    stored = {}

    class MemoryStore:
        def __init__(self, _hass, _version, key):
            self.key = key

        async def async_load(self):
            # Yield like the executor-backed store so activations can overlap.
            await asyncio.sleep(0)
            return copy.deepcopy(stored.get(self.key))

        async def async_save(self, data):
            stored[self.key] = copy.deepcopy(data)

    snapshot = copy.deepcopy(FIXTURE)
    session = MagicMock()
    session.get.side_effect = lambda *_args, **_kwargs: Response(snapshot)
    with (
        patch.object(apple_source, "Store", MemoryStore),
        patch.object(apple_source, "async_get_clientsession", return_value=session),
        patch.object(apple_source.persistent_notification, "async_create") as create,
        patch.object(apple_source.persistent_notification, "async_dismiss") as dismiss,
    ):
        source = await apple_source.async_get_source(
            hass,
            "http://example.test:80/",
            "ikea-matter",
        )
        now = [snapshot["serverTimeMillis"]]
        source._now = lambda: now[0]
        yield source, now, snapshot, session, create, dismiss, stored
        for shared in hass.data[apple_source.REGISTRY_KEY].values():
            for owner in list(shared._listeners):
                await shared.async_remove_listener(owner)


async def test_shared_source_valid_cache_zero_poll_and_target_clamp(hass, source_env):
    source, now, snapshot, session, create, _dismiss, _stored = source_env
    assert (
        await apple_source.async_get_source(hass, "http://example.test", "ikea-matter")
        is source
    )
    assert session.get.call_count == 0
    callback = AsyncMock()
    await source.async_add_listener("one", "Room one", callback)
    second = AsyncMock()
    await source.async_add_listener("two", "Room two", second)
    assert session.get.call_count == 1
    assert source.uses_apple
    # The first activation publishes the plan; joining an unchanged plan does
    # not republish it to anyone.
    assert callback.await_count == 1
    assert not second.called
    assert not create.called
    assert source.color_temperature(20) != source.color_temperature(90)
    assert source.color_temperature(50, 100_000) == source._current.curve.kelvin(
        7_800_000,
        50,
    )
    now[0] = snapshot["validUntilMillis"] - 1
    await source._async_update()
    assert session.get.call_count == 1
    assert source._timer is not None


async def test_expiry_retry_grace_notification_and_recovery(source_env):
    source, now, snapshot, session, create, dismiss, _stored = source_env
    await source.async_add_listener("one", "Room one", AsyncMock())
    now[0] = snapshot["validUntilMillis"]
    snapshot["serverTimeMillis"] = now[0]
    snapshot["phase"] = "expired"
    await source._async_update()
    assert session.get.call_count == 2
    assert source.uses_apple
    assert not create.called
    now[0] += 59_999
    await source._async_update()
    assert session.get.call_count == 2
    now[0] += 1
    await source._async_update()
    assert session.get.call_count == 3
    now[0] = snapshot["validUntilMillis"] + 30 * 60 * 1000
    await source._async_update()
    assert not source.uses_apple
    assert source.color_temperature(50) is None
    assert create.call_count == 1
    assert "Room one" in create.call_args.args[1]
    notification_id = create.call_args.kwargs["notification_id"]
    now[0] += 60_000
    await source._async_update()
    assert create.call_count == 1
    # A new plan uses the current server clock and restores Apple.
    delta = now[0] - FIXTURE["serverTimeMillis"]
    for key in ("effectiveStartMillis", "validFromMillis", "validUntilMillis"):
        snapshot[key] += delta
    snapshot.update(planId="new-plan", phase="active", serverTimeMillis=now[0])
    now[0] += 60_000
    snapshot["serverTimeMillis"] = now[0]
    await source._async_update()
    assert source.uses_apple
    assert dismiss.call_args.args[1] == notification_id


async def test_same_plan_cannot_extend_window_or_grace(source_env):
    source, now, snapshot, _session, create, _dismiss, stored = source_env
    await source.async_add_listener("one", "Room", AsyncMock())
    original_end = source._current.valid_until
    now[0] = original_end
    # Mimic a collector that refreshes timeMillisOffset on the same old plan.
    snapshot["serverTimeMillis"] = now[0]
    for key in ("effectiveStartMillis", "validFromMillis", "validUntilMillis"):
        snapshot[key] += 24 * 60 * 60 * 1000
    await source._async_update()
    assert source._current.valid_until == original_end
    assert source._current.grace_until == original_end + 1_800_000
    now[0] = original_end + 1_800_000
    await source._async_update()
    assert create.call_count == 1
    assert next(iter(stored.values()))["anchors"][snapshot["planId"]][2] == original_end


async def test_persistent_cache_restart_does_not_fetch_or_renew(hass, source_env):
    source, now, snapshot, session, create, _dismiss, _stored = source_env
    await source.async_add_listener("one", "Room", AsyncMock())
    original_end = source._current.valid_until
    await source.async_remove_listener("one")
    restored = apple_source.AppleSource(hass, source.url, source.curve_id)
    restored._now = lambda: now[0]
    now[0] += 10_000
    await restored.async_add_listener("restart", "Room", AsyncMock())
    assert restored.uses_apple
    assert session.get.call_count == 1
    assert restored._current.valid_until == original_end
    await restored.async_remove_listener("restart")
    now[0] = original_end + 1_800_000
    session.get.side_effect = aiohttp.ClientConnectionError("offline")
    await restored.async_add_listener("restart", "Room", AsyncMock())
    assert not restored.uses_apple
    assert create.call_count == 1
    assert restored._current.valid_until == original_end
    await restored.async_remove_listener("restart")


async def test_missing_cache_fallback_and_notification_owner_lifecycle(source_env):
    source, now, _snapshot, session, create, dismiss, _stored = source_env
    session.get.side_effect = aiohttp.ClientConnectionError("offline")
    await source.async_add_listener("one", "Room one", AsyncMock())
    await source.async_add_listener("two", "Room two", AsyncMock())
    assert not source.uses_apple
    assert session.get.call_count == 1
    assert "Room one, Room two" in create.call_args.args[1]
    assert create.call_count == 2
    notification_id = create.call_args.kwargs["notification_id"]
    await source.async_remove_listener("one")
    assert "Room one" not in create.call_args.args[1]
    assert "Room two" in create.call_args.args[1]
    now[0] += 60_000
    await source._async_update()
    assert create.call_count == 3
    await source.async_remove_listener("two")
    assert source._timer is None
    assert dismiss.call_args.args[1] == notification_id


async def test_future_plan_activates_at_start_not_on_receipt(source_env):
    source, now, snapshot, session, create, _dismiss, _stored = source_env
    shift = 3_000_000
    for key in ("effectiveStartMillis", "validFromMillis", "validUntilMillis"):
        snapshot[key] += shift
    snapshot["phase"] = "scheduled"
    await source.async_add_listener("one", "Room", AsyncMock())
    assert not source.uses_apple
    assert source._pending is not None
    assert create.call_count == 1
    now[0] = snapshot["validFromMillis"]
    await source._async_update()
    assert source.uses_apple
    assert source._pending is None
    assert session.get.call_count == 1


async def test_explicit_disable_revokes_grace_and_recovers_on_new_plan(source_env):
    source, now, snapshot, _session, create, _dismiss, _stored = source_env
    await source.async_add_listener("one", "Room", AsyncMock())
    now[0] = snapshot["validUntilMillis"]
    snapshot.update(protocolEnabled=False, phase="disabled", serverTimeMillis=now[0])
    await source._async_update()
    assert source._current is None
    assert not source.uses_apple
    assert create.call_count == 1
    assert "disabled" in create.call_args.args[1]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schemaVersion", 5),
        ("lightId", "another-light"),
        ("protocolEnabled", "yes"),
        ("serverTimeMillis", float("nan")),
        ("planId", None),
        ("validUntilMillis", 0),
        ("phase", "not-a-phase"),
        ("validFromMillis", float("inf")),
    ],
)
async def test_invalid_snapshot_uses_sun(source_env, field, value):
    source, _now, snapshot, _session, create, _dismiss, _stored = source_env
    snapshot[field] = value
    await source.async_add_listener("one", "Room", AsyncMock())
    assert not source.uses_apple
    assert create.call_count == 1


async def test_cancel_inflight_request_and_stale_result(hass, source_env):
    source, _now, snapshot, session, _create, dismiss, _stored = source_env
    pending = hass.loop.create_future()
    session.get.side_effect = lambda *_args, **_kwargs: Response(pending)
    listener = AsyncMock()
    activation = asyncio.create_task(source.async_add_listener("one", "Room", listener))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session.get.call_count == 1
    await source.async_remove_listener("one")
    # Deactivation cancels the shared request without raising into the caller.
    await activation
    assert not source.uses_apple
    assert source._timer is None
    assert not listener.called
    assert dismiss.called
    session.get.side_effect = lambda *_args, **_kwargs: Response(snapshot)
    await source.async_add_listener("two", "New room", listener)
    assert source.uses_apple
    assert listener.await_count == 1


async def test_shared_first_request_is_not_overlapped(hass, source_env):
    source, _now, snapshot, session, _create, _dismiss, _stored = source_env
    pending = hass.loop.create_future()
    session.get.side_effect = lambda *_args, **_kwargs: Response(pending)
    first = asyncio.create_task(source.async_add_listener("one", "Room", AsyncMock()))
    second = asyncio.create_task(
        source.async_add_listener("two", "Room 2", AsyncMock()),
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session.get.call_count == 1
    pending.set_result(snapshot)
    await asyncio.gather(first, second)
    assert source.uses_apple
    assert session.get.call_count == 1


async def test_server_clock_alignment_and_monotonic_runtime(hass, source_env):
    source, now, snapshot, _session, _create, _dismiss, _stored = source_env
    now[0] += 8 * 60 * 60 * 1000
    await source.async_add_listener("one", "Room", AsyncMock())
    assert (
        source._current.valid_until == snapshot["validUntilMillis"] + 8 * 60 * 60 * 1000
    )
    assert source.uses_apple
    other = apple_source.AppleSource(hass, "http://other.test", "ikea-matter")
    with (
        patch.object(
            apple_source.time,
            "monotonic",
            return_value=other._mono_origin + 10,
        ),
        patch.object(apple_source.time, "time", return_value=0),
    ):
        assert other._now() == other._wall_origin + 10_000


async def test_callback_crossing_expiry_rearms_immediate_request(source_env):
    source, now, snapshot, session, _create, _dismiss, _stored = source_env
    now[0] = snapshot["validUntilMillis"] - 100
    snapshot["serverTimeMillis"] = now[0]

    async def crosses_deadline():
        now[0] += 200

    await source.async_add_listener("one", "Room", crosses_deadline)
    # The listener crossed the plan's expiry while update was still running.
    assert source._timer is not None
    assert source._next_request == 0
    assert source._timer.when() - source.hass.loop.time() <= 0.01
    await source._async_update()
    assert session.get.call_count == 2


async def test_failed_expiry_persists_observed_time_across_clock_rollback(
    hass,
    source_env,
):
    source, now, snapshot, session, create, _dismiss, stored = source_env
    await source.async_add_listener("one", "Room", AsyncMock())
    now[0] = snapshot["validUntilMillis"] + 1_800_000
    session.get.side_effect = aiohttp.ClientConnectionError("offline")
    await source._async_update()
    assert not source.uses_apple
    assert next(iter(stored.values()))["observedAt"] == now[0]
    await source.async_remove_listener("one")
    with patch.object(
        apple_source.time,
        "time",
        return_value=FIXTURE["serverTimeMillis"] / 1000,
    ):
        restored = apple_source.AppleSource(hass, source.url, source.curve_id)
    await restored.async_add_listener("restart", "Room", AsyncMock())
    assert not restored.uses_apple
    assert restored._now() >= now[0]
    assert create.call_count == 2
    await restored.async_remove_listener("restart")


async def test_callback_crossing_future_start_rearms_immediate_activation(source_env):
    source, now, snapshot, session, _create, _dismiss, _stored = source_env
    shift = now[0] - snapshot["validFromMillis"] + 100
    for key in ("effectiveStartMillis", "validFromMillis", "validUntilMillis"):
        snapshot[key] += shift
    snapshot["phase"] = "scheduled"

    async def crosses_start():
        now[0] += 200

    await source.async_add_listener("one", "Room", crosses_start)
    assert source._timer is not None
    assert source._timer.when() - source.hass.loop.time() <= 0.01
    await source._async_update()
    assert source.uses_apple
    assert session.get.call_count == 1


async def test_network_delay_does_not_extend_fresh_plan(hass, source_env):
    source, now, snapshot, session, _create, _dismiss, _stored = source_env
    now[0] = snapshot["validUntilMillis"] - 500
    snapshot["serverTimeMillis"] = now[0]
    pending = hass.loop.create_future()
    session.get.side_effect = lambda *_args, **_kwargs: Response(pending)
    activation = asyncio.create_task(
        source.async_add_listener("one", "Room", AsyncMock()),
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    now[0] += 2_000
    pending.set_result(snapshot)
    await activation
    assert source._current.valid_until == snapshot["validUntilMillis"]
    assert not source._current.active(now[0])
    assert source.uses_apple  # Grace uses the end node immediately.
    assert source.color_temperature(50) == source._current.curve.kelvin(7_800_000, 50)


async def test_expiry_timer_fetches_without_light_update_event(source_env):
    source, now, snapshot, session, _create, _dismiss, _stored = source_env
    await source.async_add_listener("one", "Room", AsyncMock())
    now[0] = snapshot["validUntilMillis"]
    source._timer.cancel()
    source._timer_fired()
    await source._task
    assert session.get.call_count == 2
    assert source.uses_apple
    assert source._timer is not None


async def test_invalid_cached_snapshot_recovers_by_fetch(hass, source_env):
    source, _now, snapshot, session, _create, _dismiss, stored = source_env
    stored[source._store.key] = {"observedAt": 0, "current": {"broken": True}}
    await source.async_add_listener("one", "Room", AsyncMock())
    assert session.get.call_count == 1
    assert source.uses_apple
    assert stored[source._store.key]["current"]["planId"] == snapshot["planId"]


async def test_waiting_snapshot_and_first_point_in_future(source_env):
    source, now, snapshot, session, create, _dismiss, _stored = source_env
    snapshot.update(schedule=None, planId=None, phase="waiting")
    await source.async_add_listener("one", "Room", AsyncMock())
    assert not source.uses_apple
    assert "not received" in create.call_args.args[1]
    assert session.get.call_count == 1
    now[0] += 60_000
    snapshot.update(copy.deepcopy(FIXTURE))
    snapshot["serverTimeMillis"] = now[0]
    await source._async_update()
    assert source.uses_apple


async def test_cached_future_plan_survives_restart(hass, source_env):
    source, now, snapshot, _session, _create, _dismiss, _stored = source_env
    shift = now[0] - snapshot["validFromMillis"] + 500_000
    for key in ("effectiveStartMillis", "validFromMillis", "validUntilMillis"):
        snapshot[key] += shift
    snapshot["phase"] = "scheduled"
    await source.async_add_listener("one", "Room", AsyncMock())
    start = source._pending.valid_from
    await source.async_remove_listener("one")
    restored = apple_source.AppleSource(hass, source.url, source.curve_id)
    restored._now = lambda: now[0]
    await restored.async_add_listener("restart", "Room", AsyncMock())
    assert restored._pending.valid_from == start
    now[0] = start
    await restored._async_update()
    assert restored.uses_apple
    await restored.async_remove_listener("restart")


async def test_one_listener_failure_does_not_stop_other_owner_or_timer(source_env):
    source, _now, _snapshot, _session, _create, _dismiss, _stored = source_env
    good = AsyncMock()
    bad = AsyncMock(side_effect=RuntimeError("listener failed"))
    await asyncio.gather(
        source.async_add_listener("one", "Room", good),
        source.async_add_listener("two", "Room two", bad),
    )
    assert source.uses_apple
    assert good.await_count == 1
    assert bad.await_count == 1
    assert source._timer is not None


async def test_caller_cancellation_keeps_shared_request_for_other_owner(
    hass,
    source_env,
):
    source, _now, snapshot, session, _create, _dismiss, _stored = source_env
    pending = hass.loop.create_future()
    session.get.side_effect = lambda *_args, **_kwargs: Response(pending)
    first = asyncio.create_task(source.async_add_listener("one", "Room", AsyncMock()))
    listener = AsyncMock()
    second = asyncio.create_task(
        source.async_add_listener("two", "Room two", listener),
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session.get.call_count == 1
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    assert first.cancelled()
    pending.set_result(snapshot)
    await second
    assert source.uses_apple
    assert session.get.call_count == 1
    assert listener.await_count == 1


async def test_listeners_notified_only_on_observable_change(source_env):
    source, now, snapshot, session, _create, _dismiss, _stored = source_env
    listener = AsyncMock()
    await source.async_add_listener("one", "Room", listener)
    assert listener.await_count == 1
    # Expiry keeps the end node in use: retries change nothing observable.
    now[0] = snapshot["validUntilMillis"]
    snapshot.update(phase="expired", serverTimeMillis=now[0])
    await source._async_update()
    assert session.get.call_count == 2
    now[0] += 60_000
    await source._async_update()
    assert session.get.call_count == 3
    assert source.uses_apple
    assert listener.await_count == 1
    # Falling back to sun is observable, once.
    now[0] = snapshot["validUntilMillis"] + 30 * 60 * 1000
    await source._async_update()
    assert not source.uses_apple
    assert listener.await_count == 2
    now[0] += 60_000
    await source._async_update()
    assert listener.await_count == 2
    # A replacement plan is observable.
    now[0] += 60_000
    delta = now[0] - FIXTURE["serverTimeMillis"]
    for key in ("effectiveStartMillis", "validFromMillis", "validUntilMillis"):
        snapshot[key] = FIXTURE[key] + delta
    snapshot.update(planId="replacement", phase="active", serverTimeMillis=now[0])
    await source._async_update()
    assert source.uses_apple
    assert listener.await_count == 3


async def test_store_written_only_on_change_and_periodic_observation(source_env):
    source, now, snapshot, _session, _create, _dismiss, stored = source_env
    with patch.object(
        source._store,
        "async_save",
        wraps=source._store.async_save,
    ) as save:
        await source.async_add_listener("one", "Room", AsyncMock())
        assert save.call_count == 1
        # Unchanged content within the refresh interval is not rewritten.
        for _ in range(3):
            now[0] += 60_000
            await source._async_update()
        assert save.call_count == 1
        # Expiry is more than one interval later: the observation time used
        # against clock rollbacks is refreshed, then not on every retry.
        now[0] = snapshot["validUntilMillis"]
        snapshot.update(phase="expired", serverTimeMillis=now[0])
        await source._async_update()
        assert save.call_count == 2
        for _ in range(14):
            now[0] += 60_000
            await source._async_update()
        assert save.call_count == 2
        now[0] += 60_000
        await source._async_update()
        assert save.call_count == 3
        assert next(iter(stored.values()))["observedAt"] == now[0]
        # New content is written at once.
        delta = now[0] - FIXTURE["serverTimeMillis"]
        for key in ("effectiveStartMillis", "validFromMillis", "validUntilMillis"):
            snapshot[key] = FIXTURE[key] + delta
        snapshot.update(planId="replacement", phase="active", serverTimeMillis=now[0])
        now[0] += 60_000
        await source._async_update()
        assert save.call_count == 4
        assert next(iter(stored.values()))["current"]["planId"] == "replacement"


async def test_stale_anchors_are_pruned(source_env):
    source, now, snapshot, _session, _create, _dismiss, stored = source_env
    await source.async_add_listener("one", "Room", AsyncMock())
    original_end = source._current.valid_until
    day = 24 * 60 * 60 * 1000
    source._anchors["ancient"] = [now[0] - 30 * day] * 2 + [now[0] - 29 * day]
    source._anchors["recent"] = [now[0] - 2 * day] * 2 + [now[0] - day]
    now[0] += 60_000
    await source._async_update()
    anchors = next(iter(stored.values()))["anchors"]
    assert "ancient" not in anchors
    assert "recent" in anchors
    assert snapshot["planId"] in anchors
    # The plan in use keeps its window however old it is.
    now[0] = snapshot["validUntilMillis"] + 8 * day
    snapshot.update(phase="expired", serverTimeMillis=now[0])
    await source._async_update()
    assert snapshot["planId"] in next(iter(stored.values()))["anchors"]
    assert source._current.valid_until == original_end
