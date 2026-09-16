"""Config flow for Adaptive Lighting integration."""

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries, data_entry_flow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import (  # pylint: disable=unused-import
    APPLE_CURVE_IDS,
    APPLE_OPTIONS,
    BASIC_OPTIONS,
    CONF_APPLE_CURVE_ID,
    CONF_APPLE_PROBE_URL,
    CONF_COLOR_SOURCE,
    CONF_LIGHTS,
    DEFAULT_APPLE_CURVE_ID,
    DOMAIN,
    EXTRA_VALIDATION,
    NONE_STR,
    VALIDATION_TUPLES,
    normalize_apple_probe_url,
    validate_apple_settings,
)
from .switch import validate

_LOGGER = logging.getLogger(__name__)

OPTIONS_FLOW_DESCRIPTION_PLACEHOLDERS = {
    "webapp_url": "https://basnijholt.github.io/adaptive-lighting",
    "docs_url": "https://github.com/basnijholt/adaptive-lighting#readme",
}
ADVANCED_OPTIONS_SECTION = "advanced"


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Adaptive Lighting."""

    VERSION = 1

    source_options: dict[str, Any] | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Handle the initial step."""
        if user_input is None and self._async_current_entries():
            return await self.async_step_menu()
        return await self.async_step_wait_for_name(user_input)

    async def async_step_menu(self, user_input: dict[str, Any] | None = None):
        """Handle the menu step."""
        if user_input is not None:
            if user_input["action"] != "new":
                entry_id = user_input["action"]
                entry = self.hass.config_entries.async_get_entry(entry_id)
                if entry:
                    self.source_options = dict(entry.options)
            return await self.async_step_wait_for_name()

        entries = self._async_current_entries()
        options = {"new": "Create new instance"}
        for entry in entries:
            options[entry.entry_id] = f"Duplicate '{entry.title}'"

        return self.async_show_form(
            step_id="menu",
            data_schema=vol.Schema(
                {vol.Required("action", default="new"): vol.In(options)},
            ),
        )

    async def async_step_wait_for_name(self, user_input: dict[str, Any] | None = None):
        """Handle the name step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_NAME])
            self._abort_if_unique_id_configured()
            options = self.source_options
            return self.async_create_entry(
                title=user_input[CONF_NAME],
                data=user_input,
                options=options,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_NAME): str}),
            errors=errors,
        )

    async def async_step_import(self, user_input: dict[str, Any] | None = None):
        """Handle configuration by YAML file."""
        if user_input is None:
            return self.async_abort(reason="no_data")

        try:
            user_input = validate_apple_settings(dict(user_input))
        except vol.Invalid:
            return self.async_abort(reason="invalid_apple_config")

        await self.async_set_unique_id(user_input[CONF_NAME])
        # Keep a list of switches that are configured via YAML
        data = self.hass.data.setdefault(DOMAIN, {})
        data.setdefault("__yaml__", set()).add(self.unique_id)

        for entry in self._async_current_entries():
            if entry.unique_id == self.unique_id:
                self.hass.config_entries.async_update_entry(entry, data=user_input)
                self._abort_if_unique_id_configured()

        return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,  # noqa: ARG004
    ) -> "OptionsFlowHandler":
        """Get the options flow for this handler."""
        return OptionsFlowHandler()


def validate_options(user_input: dict[str, Any], errors: dict[str, str]) -> None:
    """Validate the options in the OptionsFlow.

    This is an extra validation step because the validators
    in `EXTRA_VALIDATION` cannot be serialized to json.
    """
    for key, (_validate, _) in EXTRA_VALIDATION.items():
        # these are unserializable validators
        value = user_input.get(key)
        try:
            if value is not None and value != NONE_STR:
                _validate(value)
        except vol.Invalid:
            _LOGGER.exception("Configuration option %s=%s is incorrect", key, value)
            errors["base"] = "option_error"


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle a option flow for Adaptive Lighting."""

    _pending_options: dict[str, Any] | None = None

    def _flatten_section_input(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Flatten section input by merging nested 'advanced' dict into top level."""
        flat_input: dict[str, Any] = {}
        for key, value in user_input.items():
            if key == ADVANCED_OPTIONS_SECTION and isinstance(value, dict):
                flat_input.update(value)
            else:
                flat_input[key] = value
        return flat_input

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        """Handle options flow with collapsible sections."""
        conf = self.config_entry
        data = validate(conf)
        form_data = {**conf.data, **conf.options}
        if conf.source == config_entries.SOURCE_IMPORT:
            return self.async_show_form(
                step_id="init",
                data_schema=None,
                description_placeholders=OPTIONS_FLOW_DESCRIPTION_PLACEHOLDERS,
            )
        errors: dict[str, str] = {}
        if user_input is not None:
            flat_input = {
                **conf.options,
                **{key: form_data[key] for key in APPLE_OPTIONS if key in form_data},
                **self._flatten_section_input(user_input),
            }
            validate_options(flat_input, errors)
            if not errors:
                if flat_input.get(CONF_COLOR_SOURCE, "sun") == "apple":
                    self._pending_options = flat_input
                    return await self.async_step_apple()
                return self.async_create_entry(title="", data=flat_input)
            data.update(flat_input)
            form_data.update(flat_input)

        # Validate that all configured lights still exist
        all_lights = set(self.hass.states.async_entity_ids("light"))
        for configured_light in data[CONF_LIGHTS]:
            if configured_light not in all_lights:
                errors[CONF_LIGHTS] = "entity_missing"
                _LOGGER.error(
                    "%s: light entity %s is configured, but was not found",
                    data[CONF_NAME],
                    configured_light,
                )

        to_replace: dict[str, Any] = {
            CONF_LIGHTS: EntitySelector(
                EntitySelectorConfig(
                    domain="light",
                    multiple=True,
                ),
            ),
        }

        basic_schema: dict[vol.Marker, Any] = {}
        advanced_schema: dict[vol.Marker, Any] = {}
        for name, default, validation in VALIDATION_TUPLES:
            if name in APPLE_OPTIONS:
                continue
            key = vol.Optional(name, default=form_data.get(name, default))
            schema = basic_schema if name in BASIC_OPTIONS else advanced_schema
            schema[key] = to_replace.get(name, validation)

        full_schema = {
            **basic_schema,
            vol.Required(ADVANCED_OPTIONS_SECTION): data_entry_flow.section(
                vol.Schema(advanced_schema),
                data_entry_flow.SectionConfig(collapsed=True),
            ),
        }

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(full_schema),
            errors=errors,
            description_placeholders=OPTIONS_FLOW_DESCRIPTION_PLACEHOLDERS,
        )

    async def async_step_apple(self, user_input: dict[str, Any] | None = None):
        """Choose a probe and an explicit curve without requiring it to be online."""
        assert self._pending_options is not None
        data = dict(self._pending_options)
        errors: dict[str, str] = {}
        if user_input is not None:
            data.update(user_input)
            try:
                data[CONF_APPLE_PROBE_URL] = normalize_apple_probe_url(
                    data.get(CONF_APPLE_PROBE_URL, ""),
                )
            except vol.Invalid:
                errors[CONF_APPLE_PROBE_URL] = "invalid_probe_url"
            if data.get(CONF_APPLE_CURVE_ID) not in APPLE_CURVE_IDS:
                errors[CONF_APPLE_CURVE_ID] = "invalid_curve_id"
            if not errors:
                return self.async_create_entry(title="", data=data)
        return self.async_show_form(
            step_id="apple",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_APPLE_PROBE_URL,
                        default=data.get(CONF_APPLE_PROBE_URL, ""),
                    ): str,
                    vol.Required(
                        CONF_APPLE_CURVE_ID,
                        default=data.get(CONF_APPLE_CURVE_ID, DEFAULT_APPLE_CURVE_ID),
                    ): vol.In(APPLE_CURVE_IDS),
                },
            ),
            errors=errors,
        )
