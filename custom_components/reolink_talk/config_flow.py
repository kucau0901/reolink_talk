from __future__ import annotations

import asyncio

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BC_PORT,
    CONF_CAMERAS,
    CONF_CHANNEL,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_REOLINK_ENTRY_IDS,
    CONF_TITLE,
    CONF_USERNAME,
    DEFAULT_BC_PORT,
    DEFAULT_CHANNEL,
    DOMAIN,
)


def _import_cam(re_entry, channel: int) -> dict:
    d = re_entry.data
    return {
        CONF_HOST: d["host"],
        CONF_USERNAME: d["username"],
        CONF_PASSWORD: d.get("password", ""),
        CONF_BC_PORT: int(d.get("baichuan_port", DEFAULT_BC_PORT)),
        CONF_CHANNEL: int(channel),
        CONF_TITLE: re_entry.title or re_entry.entry_id,
    }


async def _native_login_ok(host: str, user: str, password: str, port: int) -> bool:
    """Validate credentials with the native Baichuan client (no reolink_aio)."""
    from .baichuan_talk import BaichuanTalkClient

    cli = BaichuanTalkClient(host, user, password, port=port)
    try:
        async with asyncio.timeout(8):
            await cli.connect()
            await cli.login()
        return True
    except Exception:
        return False
    finally:
        try:
            await cli.close()
        except Exception:
            pass


class ReolinkTalkConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    async def async_step_user(self, user_input=None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        # If the official Reolink integration is present, offer a one-click import;
        # otherwise collect credentials manually (fully standalone).
        if self.hass.config_entries.async_entries("reolink"):
            # The empty entry is populated from Reolink at async_setup_entry time and
            # stored in our OWN data["cameras"], so we are standalone afterwards.
            return self.async_create_entry(title="Reolink Talk", data={})
        return await self.async_step_manual()

    async def async_step_manual(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            user = user_input[CONF_USERNAME].strip()
            pw = user_input[CONF_PASSWORD]
            port = int(user_input.get(CONF_BC_PORT, DEFAULT_BC_PORT))
            channel = int(user_input.get(CONF_CHANNEL, DEFAULT_CHANNEL))
            if not await _native_login_ok(host, user, pw, port):
                errors["base"] = "cannot_connect"
            else:
                cam = {
                    CONF_HOST: host,
                    CONF_USERNAME: user,
                    CONF_PASSWORD: pw,
                    CONF_BC_PORT: port,
                    CONF_CHANNEL: channel,
                    CONF_TITLE: host,
                }
                return self.async_create_entry(
                    title=f"Reolink Talk {host}", data={CONF_CAMERAS: {host: cam}}
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_USERNAME, default="admin"): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_CHANNEL, default=DEFAULT_CHANNEL): vol.Coerce(int),
                vol.Optional(CONF_BC_PORT, default=DEFAULT_BC_PORT): vol.Coerce(int),
            }
        )
        return self.async_show_form(step_id="manual", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return ReolinkTalkOptionsFlowHandler(config_entry)


class ReolinkTalkOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        hass = self.hass
        reolink_entries = {e.entry_id: e for e in hass.config_entries.async_entries("reolink")}

        if user_input is not None:
            channel = int(user_input.get(CONF_CHANNEL, DEFAULT_CHANNEL))
            selected = user_input.get(CONF_REOLINK_ENTRY_IDS, [])
            # Re-import (refresh) credentials from the chosen Reolink entries into our
            # OWN data["cameras"], keyed by entry_id so entity_ids are preserved.
            cameras: dict[str, dict] = {}
            for eid in selected:
                re_entry = reolink_entries.get(eid)
                if re_entry is None or not re_entry.data.get("host"):
                    continue
                cameras[eid] = _import_cam(re_entry, channel)
            # Preserve any manually-added (non-Reolink) cameras.
            for key, cam in (self._config_entry.data.get(CONF_CAMERAS) or {}).items():
                if key not in reolink_entries and key not in cameras:
                    cameras[key] = cam
            hass.config_entries.async_update_entry(
                self._config_entry, data={**self._config_entry.data, CONF_CAMERAS: cameras}
            )
            return self.async_create_entry(
                title="", data={CONF_CHANNEL: channel, CONF_REOLINK_ENTRY_IDS: selected}
            )

        existing = self._config_entry.data.get(CONF_CAMERAS) or {}
        default_sel = [k for k in existing if k in reolink_entries] or list(reolink_entries.keys())
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_REOLINK_ENTRY_IDS, default=default_sel
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=eid, label=e.title or eid)
                            for eid, e in reolink_entries.items()
                        ],
                        multiple=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_CHANNEL,
                    default=int(self._config_entry.options.get(CONF_CHANNEL, DEFAULT_CHANNEL)),
                ): vol.Coerce(int),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
