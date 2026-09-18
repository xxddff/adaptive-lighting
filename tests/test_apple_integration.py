"""Exercise the real Apple source and light controller together."""

import asyncio
import copy
from unittest.mock import MagicMock, patch

from homeassistant.components import persistent_notification
from homeassistant.components.adaptive_lighting import apple_source
from homeassistant.components.adaptive_lighting.const import (
    CONF_APPLE_CURVE_ID,
    CONF_APPLE_PROBE_URL,
    CONF_COLOR_SOURCE,
    CONF_INITIAL_TRANSITION,
    CONF_LIGHTS,
    CONF_TRANSITION,
)
from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_COLOR_TEMP_KELVIN
from homeassistant.const import CONF_NAME

from .test_apple_source import FIXTURE, Response
from .test_switch import (
    ENTITY_LIGHT_1,
    ENTITY_LIGHT_2,
    setup_lights,
    setup_lights_and_switch,
    setup_switch,
)

URL = "http://probe.example:8787"


async def _real_source(hass, snapshot):
    """Return a source on the fixture plan whose clock the test controls."""
    source = await apple_source.async_get_source(hass, URL, "ikea-matter")
    now = [snapshot["serverTimeMillis"]]
    source._now = lambda: now[0]
    return source, now


def _apple_options(**options):
    return {
        CONF_COLOR_SOURCE: "apple",
        CONF_APPLE_PROBE_URL: URL,
        CONF_APPLE_CURVE_ID: "ikea-matter",
        CONF_INITIAL_TRANSITION: 0,
        CONF_TRANSITION: 0,
        **options,
    }


async def test_real_source_fallback_and_recovery_reaches_controller(hass, hass_storage):
    """A transport failure reaches sun and recovers without resetting brightness."""
    snapshot = copy.deepcopy(FIXTURE)
    url = "http://probe.example:8787"
    source = await apple_source.async_get_source(hass, url, "ikea-matter")
    now = [snapshot["serverTimeMillis"]]
    source._now = lambda: now[0]
    session = MagicMock()
    session.get.side_effect = lambda *_args, **_kwargs: Response(snapshot)
    with (
        patch.object(apple_source, "async_get_clientsession", return_value=session),
        patch.object(
            persistent_notification,
            "async_create",
            wraps=persistent_notification.async_create,
        ) as create,
        patch.object(
            persistent_notification,
            "async_dismiss",
            wraps=persistent_notification.async_dismiss,
        ) as dismiss,
    ):
        switch, lights = await setup_lights_and_switch(
            hass,
            {
                CONF_COLOR_SOURCE: "apple",
                CONF_APPLE_PROBE_URL: url,
                CONF_APPLE_CURVE_ID: "ikea-matter",
            },
        )
        try:
            assert source.uses_apple
            assert session.get.call_count == 1
            assert switch.extra_state_attributes["color_temp_kelvin"] is None
            # Inspect the outgoing command to avoid the legacy template light's
            # mired round-trip changing the reported Kelvin by a few degrees.
            with patch.object(
                lights[0],
                "async_turn_on",
                wraps=lights[0].async_turn_on,
            ) as turn_on:
                now[0] = source._current.grace_until
                snapshot.update(phase="expired", serverTimeMillis=now[0])
                await source._async_update()
                await hass.async_block_till_done()
                assert not source.uses_apple
                assert switch.extra_state_attributes["color_temp_kelvin"] is not None
                assert create.call_count == 1
                assert turn_on.called
                notification_id = create.call_args.kwargs["notification_id"]
                assert (
                    turn_on.call_args.kwargs[ATTR_COLOR_TEMP_KELVIN]
                    == switch._settings["color_temp_kelvin"]
                )

                turn_on.reset_mock()
                now[0] += 60_000
                delta = now[0] - FIXTURE["serverTimeMillis"]
                for key in (
                    "effectiveStartMillis",
                    "validFromMillis",
                    "validUntilMillis",
                ):
                    snapshot[key] = FIXTURE[key] + delta
                snapshot.update(
                    phase="active",
                    planId="replacement",
                    serverTimeMillis=now[0],
                )
                await source._async_update()
                await hass.async_block_till_done()
                assert source.uses_apple
                assert switch.extra_state_attributes["color_temp_kelvin"] is None
                assert dismiss.call_args.args[1] == notification_id
                brightness = hass.states.get(ENTITY_LIGHT_1).attributes[ATTR_BRIGHTNESS]
                assert turn_on.call_args.kwargs[
                    ATTR_COLOR_TEMP_KELVIN
                ] == source.color_temperature(brightness * 100 / 255)
                requests = session.get.call_count
                await source._async_update()
                assert session.get.call_count == requests
        finally:
            await switch.async_turn_off()
        assert not source._listeners
        assert source._timer is None


async def test_activation_sends_one_command_per_light(hass, hass_storage):
    """The source's first publication must not duplicate the turn-on adaptation."""
    snapshot = copy.deepcopy(FIXTURE)
    source, _now = await _real_source(hass, snapshot)
    session = MagicMock()
    session.get.side_effect = lambda *_args, **_kwargs: Response(snapshot)
    with patch.object(apple_source, "async_get_clientsession", return_value=session):
        lights = await setup_lights(hass)
        with patch.object(
            lights[0],
            "async_turn_on",
            wraps=lights[0].async_turn_on,
        ) as turn_on:
            _, switch = await setup_switch(
                hass,
                _apple_options(**{CONF_LIGHTS: [ENTITY_LIGHT_1]}),
            )
            await hass.async_block_till_done()
            assert turn_on.call_count == 1
            turn_on.reset_mock()
            await switch.async_turn_off()
            await switch.async_turn_on()
            await hass.async_block_till_done()
            assert turn_on.call_count == 1
            # A profile joining the same, unchanged plan leaves other lights alone.
            turn_on.reset_mock()
            _, other = await setup_switch(
                hass,
                _apple_options(**{CONF_NAME: "other", CONF_LIGHTS: [ENTITY_LIGHT_2]}),
            )
            await hass.async_block_till_done()
            assert turn_on.call_count == 0
        assert source.uses_apple
        await other.async_turn_off()
        await switch.async_turn_off()


async def test_turn_off_during_initial_fetch_returns_cleanly(hass, hass_storage):
    """Deactivating the source mid-request must not raise out of the turn-on."""
    snapshot = copy.deepcopy(FIXTURE)
    source, now = await _real_source(hass, snapshot)
    session = MagicMock()
    session.get.side_effect = lambda *_args, **_kwargs: Response(snapshot)
    with patch.object(apple_source, "async_get_clientsession", return_value=session):
        await setup_lights(hass)
        _, switch = await setup_switch(
            hass,
            _apple_options(**{CONF_LIGHTS: [ENTITY_LIGHT_1]}),
        )
        await switch.async_turn_off()
        # Beyond the grace period only a fresh plan is usable: activation fetches.
        now[0] = source._current.grace_until
        pending = hass.loop.create_future()
        session.get.side_effect = lambda *_args, **_kwargs: Response(pending)
        activation = asyncio.create_task(switch.async_turn_on())
        for _ in range(4):
            await asyncio.sleep(0)
        assert session.get.call_count == 2
        await switch.async_turn_off()
        await activation
        assert not switch.is_on
        assert switch._apple_source is None
        assert not source._listeners
        assert source._timer is None
