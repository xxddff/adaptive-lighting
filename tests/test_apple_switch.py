"""Observable Apple color adaptation behavior using simulated HA lights."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.adaptive_lighting.adaptation_utils import (
    LightControlAttributes,
)
from homeassistant.components.adaptive_lighting.const import (
    ATTR_ADAPTIVE_LIGHTING_MANAGER,
    CONF_APPLE_CURVE_ID,
    CONF_APPLE_PROBE_URL,
    CONF_COLOR_SOURCE,
    CONF_DETECT_NON_HA_CHANGES,
    CONF_INITIAL_TRANSITION,
    CONF_INTERCEPT,
    CONF_LIGHTS,
    CONF_MIN_BRIGHTNESS,
    CONF_MULTI_LIGHT_INTERCEPT,
    CONF_SEPARATE_TURN_ON_COMMANDS,
    CONF_TAKE_OVER_CONTROL,
    CONF_USE_DEFAULTS,
    DOMAIN,
    SERVICE_CHANGE_SWITCH_SETTINGS,
    TakeOverControlMode,
)
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_NAME,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.core import Context
from homeassistant.util.color import (
    color_temperature_kelvin_to_mired,
    color_temperature_mired_to_kelvin,
    color_temperature_to_rgb,
)

from .test_switch import (
    ENTITY_LIGHT_1,
    ENTITY_LIGHT_2,
    ENTITY_LIGHT_3,
    set_light_brightness,
    setup_lights,
    setup_lights_and_switch,
    setup_switch,
)


class FakeAppleSource:
    """Keep transport out of tests of light commands and manual control."""

    uses_apple = True

    def __init__(self):
        """Track active owners and evaluator calls."""
        self.listeners = {}
        self.inputs = []

    async def async_add_listener(self, owner_id, name, callback):  # noqa: ARG002
        """Subscribe an enabled profile."""
        self.listeners[owner_id] = callback

    async def async_remove_listener(self, owner_id):
        """Unsubscribe a disabled profile."""
        self.listeners.pop(owner_id, None)

    def color_temperature(self, brightness_pct, transition=0):
        """Return a predictable brightness-dependent temperature."""
        self.inputs.append((brightness_pct, transition))
        return self.expected_kelvin(brightness_pct * 255 / 100)

    @staticmethod
    def expected_kelvin(brightness):
        """Compute the expected result in HA brightness units."""
        return round(2500 + brightness * 10)


def reported_kelvin(light, kelvin):
    """Apply the legacy template light's exact mired storage conversion."""
    if hasattr(light, "_temperature"):
        return color_temperature_mired_to_kelvin(
            color_temperature_kelvin_to_mired(kelvin),
        )
    return kelvin


@pytest.fixture
async def apple_source(hass):
    source = FakeAppleSource()
    with patch(
        "homeassistant.components.adaptive_lighting.switch.async_get_source",
        AsyncMock(return_value=source),
    ):
        yield source
    domain_data = hass.data.get(DOMAIN, {})
    for entry_data in list(domain_data.values()):
        if isinstance(entry_data, dict) and (switch := entry_data.get("switch")):
            await switch.async_turn_off()
    manager = domain_data.get(ATTR_ADAPTIVE_LIGHTING_MANAGER)
    if manager:
        for timer in manager.auto_reset_manual_control_timers.values():
            timer.cancel()
        for timer in manager.transition_timers.values():
            timer.cancel()
        for task in manager.adaptation_tasks:
            task.cancel()


async def setup_apple(hass, **options):
    return await setup_lights_and_switch(
        hass,
        {
            CONF_COLOR_SOURCE: "apple",
            CONF_APPLE_PROBE_URL: "http://probe.local:8787",
            CONF_APPLE_CURVE_ID: "yeelight",
            **options,
        },
        all_lights=True,
    )


async def test_apple_uses_planned_brightness_and_hides_shared_color(hass, apple_source):
    switch, _ = await setup_apple(hass)
    state = hass.states.get(ENTITY_LIGHT_1)
    assert state.attributes[ATTR_COLOR_TEMP_KELVIN] == apple_source.expected_kelvin(
        state.attributes[ATTR_BRIGHTNESS],
    )
    attrs = switch.extra_state_attributes
    assert attrs["color_temp_kelvin"] is None
    assert attrs["rgb_color"] is None
    assert attrs["brightness_pct"] == switch._settings["brightness_pct"]
    assert not any(key.startswith("apple") for key in attrs)
    assert switch._take_over_control_mode is TakeOverControlMode.PAUSE_CHANGED


@pytest.mark.parametrize("intercept", [True, False])
@pytest.mark.parametrize(
    ("brightness_param", "value", "brightness"),
    [
        ("brightness", 80, 80),
        ("brightness_pct", 20, 51),
        ("brightness_step", 80, 80),
        ("brightness_step_pct", 20, 51),
    ],
)
async def test_apple_explicit_turn_on_brightness_wins(
    hass,
    apple_source,
    intercept,
    brightness_param,
    value,
    brightness,
):
    switch, lights = await setup_apple(hass, **{CONF_INTERCEPT: intercept})
    # Inspect the actual light call, before any later correction can hide a flash.
    light = lights[2]
    with patch.object(light, "async_turn_on", wraps=light.async_turn_on) as turn_on:
        await hass.services.async_call(
            "light",
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: ENTITY_LIGHT_3, brightness_param: value},
            blocking=True,
        )
        if intercept:
            assert turn_on.call_args_list[0].kwargs[ATTR_BRIGHTNESS] == brightness
            assert turn_on.call_args_list[0].kwargs[
                ATTR_COLOR_TEMP_KELVIN
            ] == apple_source.expected_kelvin(brightness)
    await hass.async_block_till_done()
    await asyncio.gather(*switch.manager.adaptation_tasks)
    state = hass.states.get(ENTITY_LIGHT_3)
    assert state.attributes[ATTR_BRIGHTNESS] == brightness
    assert state.attributes[ATTR_COLOR_TEMP_KELVIN] == reported_kelvin(
        light,
        apple_source.expected_kelvin(brightness),
    )


@pytest.mark.parametrize("split", [True, False])
async def test_apple_multi_light_first_frames_use_each_brightness(
    hass,
    apple_source,
    split,
):
    switch, lights = await setup_apple(
        hass,
        **{
            CONF_INTERCEPT: True,
            CONF_MULTI_LIGHT_INTERCEPT: True,
            CONF_SEPARATE_TURN_ON_COMMANDS: split,
        },
    )
    await switch.adapt_brightness_switch.async_turn_off()
    targets = [ENTITY_LIGHT_2, ENTITY_LIGHT_3]
    for light, entity_id, brightness in zip(
        lights[1:3],
        targets,
        [60, 180],
        strict=True,
    ):
        await light.async_turn_off()
        set_light_brightness(light, brightness)
        state = hass.states.get(entity_id)
        hass.states.async_set(
            entity_id,
            "off",
            {**state.attributes, ATTR_BRIGHTNESS: brightness},
        )
    await hass.async_block_till_done()
    await hass.services.async_call(
        "light",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: targets},
        blocking=True,
    )
    await hass.async_block_till_done()
    await asyncio.gather(*switch.manager.adaptation_tasks)
    for light, entity_id, brightness in zip(
        lights[1:3],
        targets,
        [60, 180],
        strict=True,
    ):
        state = hass.states.get(entity_id)
        assert state.attributes[ATTR_COLOR_TEMP_KELVIN] == reported_kelvin(
            light,
            apple_source.expected_kelvin(brightness),
        )


async def test_apple_manual_dimming_updates_only_color(hass, apple_source):
    switch, lights = await setup_apple(hass)
    await hass.services.async_call(
        "light",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: ENTITY_LIGHT_1, ATTR_BRIGHTNESS: 95},
        blocking=True,
        context=Context(),
    )
    await hass.async_block_till_done()
    await asyncio.sleep(0.25)
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY_LIGHT_1)
    assert state.attributes[ATTR_BRIGHTNESS] == 95
    assert state.attributes[ATTR_COLOR_TEMP_KELVIN] == reported_kelvin(
        lights[0],
        apple_source.expected_kelvin(95),
    )
    assert switch.manager.last_service_data[ENTITY_LIGHT_1][
        ATTR_COLOR_TEMP_KELVIN
    ] == apple_source.expected_kelvin(95)
    assert (
        switch.manager.get_manual_control_attributes(ENTITY_LIGHT_1)
        == LightControlAttributes.BRIGHTNESS
    )
    assert not switch._apple_brightness_timers

    await hass.services.async_call(
        "light",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: ENTITY_LIGHT_1, ATTR_COLOR_TEMP_KELVIN: 4200},
        blocking=True,
        context=Context(),
    )
    await hass.async_block_till_done()
    await switch._async_apple_source_changed()
    assert hass.states.get(ENTITY_LIGHT_1).attributes[
        ATTR_COLOR_TEMP_KELVIN
    ] == reported_kelvin(lights[0], 4200)


async def test_apple_missing_brightness_skips_color_and_rgb_uses_same_temperature(
    hass,
    apple_source,
):
    switch, _ = await setup_apple(hass)
    await switch.adapt_brightness_switch.async_turn_off()
    state = hass.states.get(ENTITY_LIGHT_2)
    attrs = dict(state.attributes)
    attrs.pop(ATTR_BRIGHTNESS, None)
    hass.states.async_set(ENTITY_LIGHT_2, "off", attrs)
    assert await switch.prepare_adaptation_data(ENTITY_LIGHT_2) is None
    data = await switch.prepare_adaptation_data(
        ENTITY_LIGHT_2,
        requested_brightness=100,
        prefer_rgb_color=True,
    )
    command = await data.next_service_call_data()
    expected = tuple(
        round(value)
        for value in color_temperature_to_rgb(apple_source.expected_kelvin(100))
    )
    assert command[ATTR_RGB_COLOR] == expected


async def test_apple_sleep_and_fallback_restore_existing_global_colors(
    hass,
    apple_source,
):
    switch, _ = await setup_apple(hass)
    await switch.sleep_mode_switch.async_turn_on()
    await hass.async_block_till_done()
    await switch._async_apple_source_changed()
    assert (
        switch.extra_state_attributes["color_temp_kelvin"]
        == switch._sun_light_settings.sleep_color_temp
    )
    await switch.sleep_mode_switch.async_turn_off()
    await hass.async_block_till_done()
    apple_source.uses_apple = False
    await switch._async_apple_source_changed()
    assert (
        switch.extra_state_attributes["color_temp_kelvin"]
        == switch._settings["color_temp_kelvin"]
    )


async def test_apple_source_subscription_and_pending_dimming_stop_with_switch(
    hass,
    apple_source,
):
    switch, _ = await setup_apple(
        hass,
        **{
            CONF_TAKE_OVER_CONTROL: False,
            CONF_DETECT_NON_HA_CHANGES: False,
        },
    )
    assert len(apple_source.listeners) == 1
    await hass.services.async_call(
        "light",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: ENTITY_LIGHT_1, ATTR_BRIGHTNESS: 75},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert (
        switch.manager.get_manual_control_attributes(ENTITY_LIGHT_1)
        == LightControlAttributes.NONE
    )
    await switch.async_turn_off()
    assert not apple_source.listeners
    assert not switch._apple_brightness_timers
    await switch.async_turn_on()
    assert len(apple_source.listeners) == 1
    await switch.async_will_remove_from_hass()
    assert not apple_source.listeners


async def test_apple_dimming_does_not_turn_light_back_on(hass, apple_source):
    switch, _ = await setup_apple(hass)
    await hass.services.async_call(
        "light",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: ENTITY_LIGHT_1, ATTR_BRIGHTNESS: 100},
        blocking=True,
    )
    await hass.services.async_call(
        "light",
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: ENTITY_LIGHT_1},
        blocking=True,
    )
    await hass.async_block_till_done()
    await asyncio.sleep(0.25)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_LIGHT_1).state == "off"
    assert not switch._apple_brightness_timers


async def test_apple_physical_color_and_brightness_change_remains_manual(
    hass,
    apple_source,
):
    switch, lights = await setup_apple(hass)
    light = lights[0]
    set_light_brightness(light, 95)
    light._attr_color_temp_kelvin = 4200
    if hasattr(light, "_temperature"):
        light._temperature = color_temperature_kelvin_to_mired(4200)
    light.async_set_context(Context())
    light.async_write_ha_state()
    await hass.async_block_till_done()
    await asyncio.sleep(0.25)
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY_LIGHT_1)
    assert state.attributes[ATTR_BRIGHTNESS] == 95
    assert state.attributes[ATTR_COLOR_TEMP_KELVIN] == reported_kelvin(light, 4200)
    assert (
        switch.manager.get_manual_control_attributes(ENTITY_LIGHT_1)
        == LightControlAttributes.ALL
    )


async def test_apple_split_followup_is_cancelled_after_turn_off(hass, apple_source):
    switch, _ = await setup_apple(
        hass,
        **{
            CONF_INTERCEPT: True,
            CONF_SEPARATE_TURN_ON_COMMANDS: True,
            CONF_INITIAL_TRANSITION: 0.4,
        },
    )
    await hass.services.async_call(
        "light",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: ENTITY_LIGHT_3, ATTR_BRIGHTNESS: 80},
        blocking=True,
    )
    await hass.services.async_call(
        "light",
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: ENTITY_LIGHT_3},
        blocking=True,
    )
    await hass.async_block_till_done()
    await asyncio.gather(*switch.manager.adaptation_tasks)
    assert hass.states.get(ENTITY_LIGHT_3).state == "off"


async def test_apple_multiple_profiles_preserve_explicit_brightness(hass, apple_source):
    await setup_lights(hass)
    targets = [ENTITY_LIGHT_2, ENTITY_LIGHT_3]
    await hass.services.async_call(
        "light",
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: targets},
        blocking=True,
    )
    switches = []
    for index, entity_id in enumerate(targets):
        _, switch = await setup_switch(
            hass,
            {
                CONF_NAME: f"apple_{index}",
                CONF_COLOR_SOURCE: "apple",
                CONF_APPLE_PROBE_URL: "http://probe.local:8787",
                CONF_APPLE_CURVE_ID: "yeelight",
                CONF_LIGHTS: [entity_id],
                CONF_INTERCEPT: True,
                CONF_MULTI_LIGHT_INTERCEPT: True,
                CONF_INITIAL_TRANSITION: 0,
            },
        )
        switches.append(switch)
    await hass.services.async_call(
        "light",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: targets, ATTR_BRIGHTNESS: 80},
        blocking=True,
    )
    await hass.async_block_till_done()
    await asyncio.gather(*switches[0].manager.adaptation_tasks)
    for entity_id in targets:
        state = hass.states.get(entity_id)
        assert state.attributes[ATTR_BRIGHTNESS] == 80
        assert state.attributes[ATTR_COLOR_TEMP_KELVIN] == apple_source.expected_kelvin(
            80,
        )


async def test_apple_source_callback_cannot_adapt_after_switch_off(hass, apple_source):
    """A source callback awaiting a device cannot outlive main-switch activation."""
    switch, lights = await setup_apple(hass)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()

    with (
        patch.object(
            switch.manager,
            "update_manually_controlled_from_untracked_change",
            side_effect=blocked,
        ),
        patch.object(
            lights[0],
            "async_turn_on",
            wraps=lights[0].async_turn_on,
        ) as applied,
    ):
        pending = asyncio.create_task(switch._async_apple_source_changed())
        await entered.wait()
        await switch.async_turn_off()
        release.set()
        await pending
        applied.assert_not_awaited()


async def test_apple_initial_subscription_cannot_restart_disabled_switch(
    hass,
    apple_source,
):
    """Disabling while initial source loading waits suppresses its late adaptation."""
    switch, _ = await setup_apple(hass)
    await switch.async_turn_off()
    entered, release = asyncio.Event(), asyncio.Event()
    add_listener = apple_source.async_add_listener

    async def blocked(*args, **kwargs):
        await add_listener(*args, **kwargs)
        entered.set()
        await release.wait()

    with patch.object(apple_source, "async_add_listener", side_effect=blocked):
        pending = asyncio.create_task(switch.async_turn_on())
        await entered.wait()
        await switch.async_turn_off()
        release.set()
        await pending
        assert not switch.is_on
        assert not apple_source.listeners


async def test_apple_runtime_factory_reset_preserves_provider(hass, apple_source):
    """Resetting tunable settings cannot change a reload-only Apple provider."""
    switch, _ = await setup_apple(hass)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_CHANGE_SWITCH_SETTINGS,
        {
            ATTR_ENTITY_ID: switch.entity_id,
            CONF_USE_DEFAULTS: "factory",
            CONF_MIN_BRIGHTNESS: 7,
        },
        blocking=True,
    )
    assert switch._sun_light_settings.min_brightness == 7
    assert switch._color_source == "apple"
    assert switch._current_settings[CONF_COLOR_SOURCE] == "apple"
    assert switch._current_settings[CONF_APPLE_PROBE_URL] == "http://probe.local:8787"
    assert switch._current_settings[CONF_APPLE_CURVE_ID] == "yeelight"
    assert switch._apple_source is apple_source
    assert len(apple_source.listeners) == 1
