"""Test Adaptive Lighting config flow."""

import json
from unittest.mock import MagicMock, patch

import aiohttp
import pytest
import voluptuous as vol

try:
    from probatio import to_field_list
except ImportError:
    from voluptuous_serialize import convert as to_field_list
from homeassistant.components.adaptive_lighting import apple_source
from homeassistant.components.adaptive_lighting.config_flow import (
    describe_apple_light,
)
from homeassistant.components.adaptive_lighting.const import (
    _DOMAIN_SCHEMA,
    APPLE_OPTIONS,
    BASIC_OPTIONS,
    CONF_APPLE_CURVE_ID,
    CONF_APPLE_PROBE_URL,
    CONF_COLOR_SOURCE,
    CONF_EXPAND_LIGHT_GROUPS,
    CONF_INITIAL_TRANSITION,
    CONF_MANUAL_CONTROL_ON_EXTERNAL_TURN_ON,
    CONF_SUNRISE_TIME,
    CONF_SUNSET_TIME,
    DEFAULT_MANUAL_CONTROL_ON_EXTERNAL_TURN_ON,
    DEFAULT_NAME,
    DOMAIN,
    NONE_STR,
    VALIDATION_TUPLES,
    change_switch_settings_schema,
    normalize_apple_curve_id,
    normalize_apple_probe_url,
)
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import FlowResultType, section
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import SelectSelector

from tests.common import MockConfigEntry

from .test_apple_source import Response

CATALOG = {
    "lights": [
        {
            "id": "living-room",
            "name": "Apple Curve Probe 客厅吊灯",
            "label": "客厅吊灯",
            "paired": True,
            "minKelvin": 2700,
            "maxKelvin": 6500,
        },
        {
            "id": "ikea-zigbee",
            "name": "Apple Curve Probe ikea-zigbee",
            "label": "ikea-zigbee",
            "paired": False,
            "minKelvin": 2202,
            "maxKelvin": 4000,
        },
    ],
}

DEFAULT_DATA = {key: default for key, default, _ in VALIDATION_TUPLES}

# Split DEFAULT_DATA into basic and advanced for section-based input
BASIC_DATA = {key: value for key, value in DEFAULT_DATA.items() if key in BASIC_OPTIONS}
ADVANCED_DATA = {
    key: value
    for key, value in DEFAULT_DATA.items()
    if key not in BASIC_OPTIONS | APPLE_OPTIONS
}


def _schema_defaults(schema: vol.Schema) -> dict[str, object]:
    """Return the defaults from a voluptuous schema."""
    return {
        key.schema: key.default() if callable(key.default) else key.default
        for key in schema.schema
    }


def _advanced_section(result) -> section:
    """Return the advanced options section from a flow result."""
    advanced = result["data_schema"].schema["advanced"]
    assert isinstance(advanced, section)
    return advanced


async def test_flow_manual_configuration(hass):
    """Test that config flow works."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["handler"] == "adaptive_lighting"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_NAME: "living room"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "living room"


async def test_import_success(hass):
    """Test import step is successful."""
    data = DEFAULT_DATA.copy()
    data[CONF_NAME] = DEFAULT_NAME
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "import"},
        data=data,
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == DEFAULT_NAME
    for key, value in data.items():
        assert result["data"][key] == value


async def test_options(hass):
    """Test updating options with collapsible sections."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data={CONF_NAME: DEFAULT_NAME},
        options={},
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    # Build input with advanced options nested in "advanced" section
    advanced_data = ADVANCED_DATA.copy()
    advanced_data[CONF_INITIAL_TRANSITION] = 23
    advanced_data[CONF_EXPAND_LIGHT_GROUPS] = False
    advanced_data[CONF_SUNRISE_TIME] = NONE_STR
    advanced_data[CONF_SUNSET_TIME] = NONE_STR
    basic_data = {**BASIC_DATA, "min_brightness": 12}
    user_input = {
        **basic_data,
        "advanced": advanced_data,
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=user_input,
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY

    # Verify flattened data is saved correctly
    expected_data = {**basic_data, **advanced_data}
    for key, value in expected_data.items():
        assert result["data"][key] == value

    assert "advanced" not in result["data"]

    # Starting the flow again must load the saved flat options into both parts
    # of the sectioned form.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _schema_defaults(result["data_schema"])["min_brightness"] == 12
    assert (
        _schema_defaults(_advanced_section(result).schema)[CONF_INITIAL_TRANSITION]
        == 23
    )


async def test_options_schema_has_each_setting_once(hass):
    """Test that basic and advanced options partition all settings."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data={CONF_NAME: DEFAULT_NAME, "interval": 120, "min_brightness": 7},
        options={"min_brightness": 12},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"].schema
    advanced = _advanced_section(result)

    assert advanced.options == {"collapsed": True}
    assert (
        _schema_defaults(advanced.schema)[CONF_MANUAL_CONTROL_ON_EXTERNAL_TURN_ON]
        is DEFAULT_MANUAL_CONTROL_ON_EXTERNAL_TURN_ON
    )
    assert {key.schema for key in schema if key.schema != "advanced"} == BASIC_OPTIONS
    assert {key.schema for key in advanced.schema.schema} == set(
        DEFAULT_DATA,
    ) - BASIC_OPTIONS - APPLE_OPTIONS
    assert _schema_defaults(result["data_schema"])["interval"] == 120
    assert _schema_defaults(result["data_schema"])["min_brightness"] == 12

    serialized_schema = to_field_list(
        result["data_schema"],
        custom_serializer=cv.custom_serializer,
    )
    json.dumps(serialized_schema)
    serialized_advanced = next(
        field for field in serialized_schema if field["name"] == "advanced"
    )
    assert serialized_advanced["type"] == "expandable"
    assert serialized_advanced["expanded"] is False
    assert {field["name"] for field in serialized_advanced["schema"]} == set(
        DEFAULT_DATA,
    ) - BASIC_OPTIONS - APPLE_OPTIONS


@pytest.mark.parametrize("lights", [[], ["light.missing"]])
async def test_incorrect_options(hass, lights):
    """Test updating incorrect options in advanced section."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data={CONF_NAME: DEFAULT_NAME},
        options={},
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    # Build input with invalid advanced options nested in section
    advanced_data = ADVANCED_DATA.copy()
    advanced_data[CONF_SUNRISE_TIME] = "yolo"
    advanced_data[CONF_SUNSET_TIME] = "yolo"
    basic_data = {**BASIC_DATA, "min_brightness": 12, "lights": lights}
    user_input = {
        **basic_data,
        "advanced": advanced_data,
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=user_input,
    )
    # Should show form with errors
    assert result["type"] == FlowResultType.FORM
    expected_errors = {"base": "option_error"}
    if lights:
        expected_errors["lights"] = "entity_missing"
    assert result["errors"] == expected_errors
    assert _schema_defaults(result["data_schema"])["lights"] == lights
    assert _schema_defaults(result["data_schema"])["min_brightness"] == 12
    assert (
        _schema_defaults(_advanced_section(result).schema)[CONF_SUNRISE_TIME] == "yolo"
    )


async def test_import_twice(hass):
    """Test importing twice."""
    data = DEFAULT_DATA.copy()
    data[CONF_NAME] = DEFAULT_NAME
    for _ in range(2):
        _ = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "import"},
            data=data,
        )


async def test_options_flow_for_yaml_import(hass):
    """Test that options flow for YAML-imported entries shows empty form.

    When a config entry is imported from YAML (source=SOURCE_IMPORT),
    the options flow should show an empty form since the user should
    modify the YAML configuration directly, not through the UI.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data={CONF_NAME: DEFAULT_NAME},
        source=SOURCE_IMPORT,
        options={},
    )
    entry.add_to_hass(hass)

    # For YAML imports, the switch setup requires the unique_id to be in
    # hass.data[DOMAIN]["__yaml__"], otherwise it deletes the entry.
    # This simulates what async_step_import does.
    hass.data.setdefault(DOMAIN, {}).setdefault("__yaml__", set()).add(entry.unique_id)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)

    # For YAML imports, the options flow shows an empty form (data_schema=None)
    # This is intentional - users should modify YAML, not UI
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result.get("data_schema") is None
    assert result["description_placeholders"] == {
        "docs_url": "https://github.com/basnijholt/adaptive-lighting#readme",
        "webapp_url": "https://basnijholt.github.io/adaptive-lighting",
    }


async def test_menu_shown_when_entries_exist(hass):
    """Test that menu step is shown when existing entries exist."""
    # Create an existing entry
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="existing",
        data={CONF_NAME: "existing"},
        options={"min_brightness": 10},
    )
    entry.add_to_hass(hass)

    # Start a new config flow - should show menu
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "menu"


async def test_menu_create_new_instance(hass):
    """Test creating a new instance through the menu."""
    # Create an existing entry
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="existing",
        data={CONF_NAME: "existing"},
        options={"min_brightness": 10},
    )
    entry.add_to_hass(hass)

    # Start config flow - shows menu
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )
    assert result["step_id"] == "menu"

    # Choose to create new instance
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"action": "new"},
    )

    # Should show name form
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    # Enter name
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_NAME: "new instance"},
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "new instance"
    # New instance should have no options (not duplicated)
    assert result["options"] == {}


async def test_menu_duplicate_instance(hass):
    """Test duplicating an existing instance through the menu."""
    # Create an existing entry with custom options
    source_options = {"min_brightness": 20, "max_brightness": 80}
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="source",
        data={CONF_NAME: "source"},
        options=source_options,
    )
    entry.add_to_hass(hass)

    # Start config flow - shows menu
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )
    assert result["step_id"] == "menu"

    # Choose to duplicate existing entry
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={"action": entry.entry_id},
    )

    # Should show name form
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    # Enter name for duplicated instance
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_NAME: "duplicated"},
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "duplicated"
    # Duplicated instance should have copied options
    assert result["options"] == source_options


def _probe_session(reply):
    """Return an aiohttp-like session whose GET answers with reply or raises it."""
    session = MagicMock()
    if isinstance(reply, BaseException):
        session.get.side_effect = reply
    else:
        session.get.side_effect = lambda *_args, **_kwargs: Response(reply)
    return session


async def _start_apple_options(hass, options=None, session=None):
    """Open the options flow, choose the Apple source and reach the address step."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data={CONF_NAME: DEFAULT_NAME},
        options=options or {},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert CONF_APPLE_PROBE_URL not in _schema_defaults(result["data_schema"])
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_COLOR_SOURCE: "apple", "advanced": {}},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "apple"
    assert set(result["data_schema"].schema) == {CONF_APPLE_PROBE_URL}
    if session is not None:
        assert not session.get.called
    return result


def _curve_selector(result):
    """Return the curve ID field's validator from an apple_curve form."""
    assert result["step_id"] == "apple_curve"
    assert set(result["data_schema"].schema) == {CONF_APPLE_CURVE_ID}
    return next(iter(result["data_schema"].schema.values()))


async def test_apple_options_list_probe_lights_to_choose_from(hass):
    """The address step loads the probe's lights and offers them as a dropdown."""
    session = _probe_session(CATALOG)
    with patch.object(apple_source, "async_get_clientsession", return_value=session):
        result = await _start_apple_options(
            hass,
            {"min_brightness": 12, "transition": 17},
            session,
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_APPLE_PROBE_URL: "http://Probe.Example:80/"},
        )
    assert session.get.call_args.args[0] == "http://probe.example/api/catalog"
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {}
    assert result["description_placeholders"] == {"url": "http://probe.example"}
    selector = _curve_selector(result)
    assert isinstance(selector, SelectSelector)
    assert selector.config["custom_value"] is True
    assert selector.config["options"] == [
        {
            "value": "living-room",
            "label": "客厅吊灯 (living-room) \u00b7 2700\u20136500 K",
        },
        {
            "value": "ikea-zigbee",
            "label": "ikea-zigbee \u00b7 2202\u20134000 K \u00b7 not paired",
        },
    ]
    # Nothing is preselected before a curve was ever chosen.
    assert _schema_defaults(result["data_schema"]) == {
        CONF_APPLE_CURVE_ID: vol.UNDEFINED,
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_APPLE_CURVE_ID: "living-room"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_COLOR_SOURCE] == "apple"
    assert result["data"][CONF_APPLE_PROBE_URL] == "http://probe.example"
    assert result["data"][CONF_APPLE_CURVE_ID] == "living-room"
    assert result["data"]["min_brightness"] == 12
    assert result["data"]["transition"] == 17
    assert session.get.call_count == 1


async def test_apple_options_preselect_saved_curve_and_accept_unlisted_id(hass):
    """A saved ID stays selected, and an ID missing from the list is still accepted."""
    session = _probe_session(CATALOG)
    with patch.object(apple_source, "async_get_clientsession", return_value=session):
        result = await _start_apple_options(
            hass,
            {
                CONF_COLOR_SOURCE: "apple",
                CONF_APPLE_PROBE_URL: "http://probe.example:8787",
                CONF_APPLE_CURVE_ID: "ikea-zigbee",
            },
        )
        assert _schema_defaults(result["data_schema"]) == {
            CONF_APPLE_PROBE_URL: "http://probe.example:8787",
        }
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_APPLE_PROBE_URL: "http://probe.example:8787"},
        )
    assert _schema_defaults(result["data_schema"]) == {
        CONF_APPLE_CURVE_ID: "ikea-zigbee",
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_APPLE_CURVE_ID: " bedroom-new "},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_APPLE_CURVE_ID] == "bedroom-new"


async def test_apple_options_invalid_curve_id_can_be_corrected(hass):
    """An ID the probe would reject keeps the curve step with the typed value."""
    session = _probe_session(CATALOG)
    with patch.object(apple_source, "async_get_clientsession", return_value=session):
        result = await _start_apple_options(hass)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_APPLE_PROBE_URL: "http://probe.example:8787"},
        )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_APPLE_CURVE_ID: "Living Room"},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_APPLE_CURVE_ID: "invalid_curve_id"}
    assert isinstance(_curve_selector(result), SelectSelector)
    assert _schema_defaults(result["data_schema"]) == {
        CONF_APPLE_CURVE_ID: "Living Room",
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_APPLE_CURVE_ID: "living-room"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_APPLE_CURVE_ID] == "living-room"
    assert session.get.call_count == 1


async def test_apple_options_empty_catalog_falls_back_to_text_entry(hass):
    """A probe without lights shows a notice and a plain text field."""
    session = _probe_session({"lights": []})
    with patch.object(apple_source, "async_get_clientsession", return_value=session):
        result = await _start_apple_options(hass)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_APPLE_PROBE_URL: "http://probe.example:8787"},
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "catalog_empty"}
    assert _curve_selector(result) is str
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_APPLE_CURVE_ID: "kitchen"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_APPLE_CURVE_ID] == "kitchen"


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (aiohttp.ClientError("refused"), "connection failed"),
        (TimeoutError(), "timed out"),
    ],
)
async def test_apple_options_offline_probe_allows_manual_id(hass, reply, error):
    """An unreachable probe offers a menu; the ID can still be typed and saved."""
    session = _probe_session(reply)
    with patch.object(apple_source, "async_get_clientsession", return_value=session):
        result = await _start_apple_options(hass, {"min_brightness": 25})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_APPLE_PROBE_URL: "http://probe.example:8787"},
        )
    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "apple_offline"
    assert result["menu_options"] == ["apple", "apple_curve"]
    assert result["description_placeholders"] == {
        "url": "http://probe.example:8787",
        "error": error,
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"next_step_id": "apple_curve"},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {}
    assert _curve_selector(result) is str
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_APPLE_CURVE_ID: "ikea-matter"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_APPLE_PROBE_URL] == "http://probe.example:8787"
    assert result["data"][CONF_APPLE_CURVE_ID] == "ikea-matter"
    assert result["data"]["min_brightness"] == 25


async def test_apple_options_offline_menu_returns_to_address_step(hass):
    """Choosing to fix the address shows it again and retries loading the lights."""
    session = _probe_session(CATALOG)
    session.get.side_effect = [
        aiohttp.ClientError("refused"),
        Response(CATALOG),
    ]
    with patch.object(apple_source, "async_get_clientsession", return_value=session):
        result = await _start_apple_options(hass)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_APPLE_PROBE_URL: "http://wrong.example:8787"},
        )
        assert result["type"] == FlowResultType.MENU
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"next_step_id": "apple"},
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "apple"
        assert _schema_defaults(result["data_schema"]) == {
            CONF_APPLE_PROBE_URL: "http://wrong.example:8787",
        }
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_APPLE_PROBE_URL: "http://probe.example:8787"},
        )
    assert session.get.call_count == 2
    assert session.get.call_args.args[0] == "http://probe.example:8787/api/catalog"
    assert result["type"] == FlowResultType.FORM
    assert isinstance(_curve_selector(result), SelectSelector)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_APPLE_CURVE_ID: "living-room"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_APPLE_PROBE_URL] == "http://probe.example:8787"


async def test_apple_options_invalid_url_can_be_corrected(hass):
    """An invalid address keeps the address step without contacting anything."""
    session = _probe_session(CATALOG)
    with patch.object(apple_source, "async_get_clientsession", return_value=session):
        result = await _start_apple_options(hass, {"min_brightness": 25})
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_APPLE_PROBE_URL: "not-a-url"},
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "apple"
        assert result["errors"] == {CONF_APPLE_PROBE_URL: "invalid_probe_url"}
        assert _schema_defaults(result["data_schema"]) == {
            CONF_APPLE_PROBE_URL: "not-a-url",
        }
        assert not session.get.called
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={CONF_APPLE_PROBE_URL: "http://probe.example:8787"},
        )
    assert result["step_id"] == "apple_curve"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_APPLE_CURVE_ID: "yeelight"},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["min_brightness"] == 25


def test_describe_apple_light_labels():
    """Dropdown labels carry the name, ID, Kelvin range and pairing state."""
    light = apple_source.AppleCatalogLight("hall", "hall", True, None, None)
    assert describe_apple_light(light) == "hall"
    light = apple_source.AppleCatalogLight("hall", "Hallway", False, 2000, 6500)
    assert (
        describe_apple_light(light)
        == "Hallway (hall) \u00b7 2000\u20136500 K \u00b7 not paired"
    )


async def test_sun_options_preserve_hidden_apple_settings(hass):
    """Switching to sun does not discard the address and explicit curve choice."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=DEFAULT_NAME,
        data={CONF_NAME: DEFAULT_NAME},
        options={
            CONF_COLOR_SOURCE: "apple",
            CONF_APPLE_PROBE_URL: "http://probe.example:8787",
            CONF_APPLE_CURVE_ID: "ikea-zigbee",
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_COLOR_SOURCE: "sun", "advanced": {}},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_COLOR_SOURCE] == "sun"
    assert result["data"][CONF_APPLE_CURVE_ID] == "ikea-zigbee"
    assert result["data"][CONF_APPLE_PROBE_URL] == "http://probe.example:8787"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "localhost:8787",
        "ftp://probe",
        "http://u:p@probe",
        "http://probe?x=1",
        "http://probe/#fragment",
        "http://probe:0",
        "http://probe:99999",
        "http://pro be",
    ],
)
def test_apple_url_validation(url):
    """Reject malformed or ambiguous probe addresses before runtime."""
    with pytest.raises(vol.Invalid):
        normalize_apple_probe_url(url)


@pytest.mark.parametrize(
    ("curve_id", "expected"),
    [
        ("a", "a"),
        ("living-room", "living-room"),
        (" ikea-zigbee\n", "ikea-zigbee"),
        ("a" * 32, "a" * 32),
    ],
)
def test_apple_curve_id_normalization(curve_id, expected):
    """Accept exactly the identifiers the probe accepts, ignoring surrounding space."""
    assert normalize_apple_curve_id(curve_id) == expected


@pytest.mark.parametrize(
    "curve_id",
    ["", "   ", "Living", "living room", "living_room", "客厅", "a" * 33, None, 5],
)
def test_apple_curve_id_validation(curve_id):
    """Reject identifiers the probe could never publish."""
    with pytest.raises(vol.Invalid):
        normalize_apple_curve_id(curve_id)


def test_apple_yaml_and_runtime_configuration_boundaries():
    """YAML requires the Apple address and ID; runtime changes remain reload-only."""
    assert _DOMAIN_SCHEMA({})[CONF_COLOR_SOURCE] == "sun"
    assert _DOMAIN_SCHEMA({})[CONF_APPLE_CURVE_ID] == ""
    with pytest.raises(vol.Invalid):
        _DOMAIN_SCHEMA({CONF_COLOR_SOURCE: "apple"})
    with pytest.raises(vol.Invalid):
        _DOMAIN_SCHEMA(
            {CONF_COLOR_SOURCE: "apple", CONF_APPLE_PROBE_URL: "http://probe"},
        )
    result = _DOMAIN_SCHEMA(
        {
            CONF_COLOR_SOURCE: "apple",
            CONF_APPLE_PROBE_URL: "http://[::1]:8787/",
            CONF_APPLE_CURVE_ID: " bedroom-2 ",
        },
    )
    assert result[CONF_APPLE_PROBE_URL] == "http://[::1]:8787"
    assert result[CONF_APPLE_CURVE_ID] == "bedroom-2"
    with pytest.raises(vol.Invalid):
        _DOMAIN_SCHEMA(
            {
                CONF_COLOR_SOURCE: "apple",
                CONF_APPLE_PROBE_URL: "http://probe",
                CONF_APPLE_CURVE_ID: "Bedroom 2",
            },
        )
    service_fields = {key.schema for key in change_switch_settings_schema()}
    assert not service_fields.intersection(APPLE_OPTIONS | {CONF_COLOR_SOURCE})
