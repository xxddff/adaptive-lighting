"""Shared, persistent Apple plan cache with expiry-only HTTP refresh."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiohttp
from homeassistant.components import persistent_notification
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .apple_curve import AppleCurve, finite_number
from .const import normalize_apple_probe_url

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
REGISTRY_KEY = "adaptive_lighting_apple_sources"
RETRY_SECONDS = 60
GRACE_SECONDS = 30 * 60
REQUEST_TIMEOUT_SECONDS = 10
Listener = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class ApplePlan:
    """A snapshot with immutable deadlines expressed on HA's local clock."""

    snapshot: dict[str, Any]
    curve: AppleCurve
    start: float
    valid_from: float
    valid_until: float

    @property
    def grace_until(self) -> float:
        """Return the fixed end of the expiry grace period."""
        return self.valid_until + GRACE_SECONDS * 1000

    def active(self, now: float) -> bool:
        """Whether the plan is within its original validity window."""
        return self.valid_from <= now < self.valid_until

    def available(self, now: float) -> bool:
        """Whether the plan is executable, including the end-node grace period."""
        return self.valid_from <= now < self.grace_until


async def async_get_source(
    hass: HomeAssistant,
    url: str,
    curve_id: str,
) -> AppleSource:
    """Get a shared source without loading storage or making a request."""
    url = normalize_apple_probe_url(url)
    registry: dict[tuple[str, str], AppleSource] = hass.data.setdefault(
        REGISTRY_KEY,
        {},
    )
    key = (url, curve_id)
    if key not in registry:
        registry[key] = AppleSource(hass, url, curve_id)
    return registry[key]


class AppleSource:
    """Own the cache, one request, deadline timers and one notification per source."""

    def __init__(self, hass: HomeAssistant, url: str, curve_id: str) -> None:
        """Initialize the shared source; I/O starts with its first listener."""
        self.hass = hass
        self.url = url
        self.curve_id = curve_id
        self._key = hashlib.sha256(f"{url}\n{curve_id}".encode()).hexdigest()[:24]
        self._store: Store = Store(hass, 1, f"adaptive_lighting.apple.{self._key}")
        self._listeners: dict[str, tuple[str, Listener]] = {}
        self._loaded = False
        self._anchors: dict[str, list[float]] = {}
        self._current: ApplePlan | None = None
        self._pending: ApplePlan | None = None
        self._task: asyncio.Task | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._generation = 0
        self._next_request = 0.0
        self._wall_origin = time.time() * 1000
        self._mono_origin = time.monotonic()
        self._reason = "No usable Apple plan is available."
        self._notification: str | None = None

    def _now(self) -> float:
        """Use wall time only as an origin; runtime clock jumps cannot renew plans."""
        return self._wall_origin + (time.monotonic() - self._mono_origin) * 1000

    @property
    def uses_apple(self) -> bool:
        """Whether callers should compute per-light Apple color now."""
        return self._current is not None and self._current.available(self._now())

    def color_temperature(
        self,
        brightness_pct: float,
        transition: float = 0,
    ) -> int | None:
        """Evaluate at the fade target, never running beyond the final node."""
        now = self._now()
        plan = self._current
        if plan is None or not plan.available(now):
            return None
        target = min(now + max(0, transition) * 1000, plan.valid_until)
        return plan.curve.kelvin(target - plan.start, brightness_pct)

    async def async_add_listener(
        self,
        owner_id: str,
        name: str,
        callback: Listener,
    ) -> None:
        """Activate a configuration and initialize its shared cached plan."""
        self._listeners[owner_id] = (name, callback)
        if self._task is None or self._task.done():
            self._task = self.hass.async_create_task(self._async_update())
        await asyncio.shield(self._task)

    async def async_remove_listener(self, owner_id: str) -> None:
        """Remove a configuration and stop all work when no owners remain."""
        self._listeners.pop(owner_id, None)
        if self._listeners:
            self._update_notification()
            return
        self._generation += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        task = self._task
        self._task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # The next activation may retry immediately if the previous request failed.
        self._next_request = 0
        self._update_notification()

    def _read_plan(self, snapshot: Any, now: float) -> ApplePlan:
        """Validate a snapshot and bind its first-seen clock mapping permanently."""
        if not isinstance(snapshot, dict):
            msg = "Apple snapshot must be an object"
            raise TypeError(msg)
        if (
            snapshot.get("schemaVersion") != 1
            or snapshot.get("lightId") != self.curve_id
        ):
            msg = "Apple snapshot version or light ID does not match"
            raise ValueError(msg)
        plan_id = snapshot.get("planId")
        if not isinstance(plan_id, str) or not plan_id or len(plan_id) > 256:
            msg = "Apple snapshot has no valid plan ID"
            raise ValueError(msg)
        curve = AppleCurve.from_snapshot(snapshot)
        server_now = finite_number(
            snapshot.get("serverTimeMillis"),
            "serverTimeMillis",
            0,
        )
        start = finite_number(
            snapshot.get("effectiveStartMillis"),
            "effectiveStartMillis",
            0,
        )
        valid_from = finite_number(
            snapshot.get("validFromMillis"),
            "validFromMillis",
            start,
        )
        until = finite_number(
            snapshot.get("validUntilMillis"),
            "validUntilMillis",
            valid_from,
        )
        if (
            abs(valid_from - start - curve.first_offset) > 1
            or abs(until - start - curve.end_offset) > 1
            or until <= valid_from
        ):
            msg = "Apple snapshot window does not match its transition curve"
            raise ValueError(msg)
        if plan_id in self._anchors:
            local_start, local_from, local_until = self._anchors[plan_id]
        else:
            offset = now - server_now
            local_start, local_from, local_until = (
                start + offset,
                valid_from + offset,
                until + offset,
            )
            self._anchors[plan_id] = [local_start, local_from, local_until]
        return ApplePlan(snapshot, curve, local_start, local_from, local_until)

    async def _async_load(self, generation: int) -> None:
        """Restore fixed deadlines without contacting the collector."""
        try:
            data = await self._store.async_load()
            if generation != self._generation:
                return
            if data:
                # A clock rollback between restarts must not make the last
                # observed point of an already-expired plan young again.
                observed = finite_number(data.get("observedAt", 0), "observedAt", 0)
                self._wall_origin += max(0, observed - self._now())
                for plan_id, anchor in data.get("anchors", {}).items():
                    if (
                        not isinstance(plan_id, str)
                        or not isinstance(anchor, list)
                        or len(anchor) != 3
                    ):
                        continue
                    start = finite_number(anchor[0], "cached start", 0)
                    valid_from = finite_number(anchor[1], "cached validFrom", start)
                    until = finite_number(anchor[2], "cached validUntil", valid_from)
                    self._anchors[plan_id] = [start, valid_from, until]
                if data.get("current"):
                    self._current = self._read_plan(data["current"], self._now())
                if data.get("pending"):
                    self._pending = self._read_plan(data["pending"], self._now())
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            _LOGGER.warning("Cannot restore Apple curve cache for %s", self.curve_id)
            self._current = self._pending = None
        self._loaded = True

    async def _async_save(self) -> None:
        """Persist both queued and current plans with their original local windows."""
        await self._store.async_save(
            {
                "observedAt": self._now(),
                "anchors": self._anchors,
                "current": self._current.snapshot if self._current else None,
                "pending": self._pending.snapshot if self._pending else None,
            },
        )

    def _accept_snapshot(self, snapshot: Any, observed_at: float | None = None) -> None:
        """Apply a validated protocol state without replacing grace with old data."""
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("schemaVersion") != 1
            or snapshot.get("lightId") != self.curve_id
            or not isinstance(snapshot.get("protocolEnabled"), bool)
            or snapshot.get("phase")
            not in {"waiting", "scheduled", "active", "expired", "disabled"}
        ):
            msg = "Invalid Apple snapshot envelope"
            raise ValueError(msg)
        finite_number(snapshot.get("serverTimeMillis"), "serverTimeMillis", 0)
        if not snapshot["protocolEnabled"]:
            self._current = self._pending = None
            self._reason = "The collector has disabled its Apple plan."
            return
        if snapshot.get("schedule") is None:
            self._reason = "The collector has not received an Apple plan."
            return
        now = self._now()
        plan = self._read_plan(snapshot, now if observed_at is None else observed_at)
        if plan.valid_from > now:
            self._pending = plan
            self._reason = "The next Apple plan has not started yet."
        elif self._current is None or plan.valid_until >= self._current.valid_until:
            self._current = plan
            if self._pending and self._pending.valid_until <= plan.valid_until:
                self._pending = None
            self._reason = "The Apple plan has expired."

    def _advance_pending(self) -> None:
        if self._pending is not None and self._pending.valid_from <= self._now():
            self._current = self._pending
            self._pending = None

    async def _async_request(self, generation: int) -> None:
        """Perform one bounded request and discard responses after deactivation."""
        endpoint = f"{self.url}/api/lights/{quote(self.curve_id, safe='')}/snapshot"
        request_started = self._now()
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(
                endpoint,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
            ) as response:
                response.raise_for_status()
                snapshot = await response.json()
            if generation != self._generation or not self._listeners:
                return
            # Anchor new plans to request start conservatively: response latency
            # must not extend their validity window. Already-seen IDs retain
            # their persisted mapping regardless of later network delays.
            self._accept_snapshot(snapshot, request_started)
        except (
            aiohttp.ClientError,
            TimeoutError,
            ValueError,
            TypeError,
            OSError,
        ) as err:
            self._reason = "The collector is unreachable or returned an invalid plan."
            _LOGGER.debug("Apple plan request failed for %s: %s", self.curve_id, err)
        finally:
            now = self._now()
            self._next_request = (
                0
                if self._current and self._current.active(now)
                else now + RETRY_SECONDS * 1000
            )

    async def _async_update(self) -> None:
        """Refresh only without a valid plan, publish changes and schedule a deadline."""
        generation = self._generation
        if not self._loaded:
            await self._async_load(generation)
        if generation != self._generation or not self._listeners:
            return
        self._advance_pending()
        now = self._now()
        if (
            not (self._current and self._current.active(now))
            and now >= self._next_request
        ):
            await self._async_request(generation)
        if generation != self._generation or not self._listeners:
            return
        self._advance_pending()
        try:
            await self._async_save()
        except OSError:
            _LOGGER.warning("Cannot persist Apple curve cache for %s", self.curve_id)
        if generation != self._generation or not self._listeners:
            return
        self._update_notification()
        was_apple = self.uses_apple
        callbacks = [callback() for _, callback in tuple(self._listeners.values())]
        results = await asyncio.gather(*callbacks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                _LOGGER.error("Apple plan listener failed: %s", result)
        if generation == self._generation and self._listeners:
            if was_apple != self.uses_apple:
                self._next_request = min(self._next_request, self._now())
            self._schedule()

    def _schedule(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        now = self._now()
        deadlines = []
        if self._current and self._current.active(now):
            deadlines.append(self._current.valid_until)
        else:
            deadlines.append(max(now, self._next_request))
            if self._current and self._current.available(now):
                deadlines.append(self._current.grace_until)
        if self._pending is not None:
            deadlines.append(max(now, self._pending.valid_from))
        delay = max(0.001, (min(deadlines) - now) / 1000)
        self._timer = self.hass.loop.call_later(delay, self._timer_fired)

    def _timer_fired(self) -> None:
        self._timer = None
        if self._listeners and (self._task is None or self._task.done()):
            self._task = self.hass.async_create_task(self._async_update())

    def _update_notification(self) -> None:
        notification_id = f"adaptive_lighting_apple_{self._key}"
        if not self._listeners or self.uses_apple:
            persistent_notification.async_dismiss(self.hass, notification_id)
            self._notification = None
            return
        names = ", ".join(sorted({name for name, _ in self._listeners.values()}))
        message = (
            f"Apple curve `{self.curve_id}` is unavailable. Adaptive Lighting has "
            f"fallen back to the sun color algorithm for: {names}.\n\n{self._reason}\n\n"
            "A new plan will be checked every 60 seconds. This notification will "
            "be cleared when Apple lighting resumes."
        )
        if message != self._notification:
            persistent_notification.async_create(
                self.hass,
                message,
                title="Adaptive Lighting: Apple curve unavailable",
                notification_id=notification_id,
            )
            self._notification = message
