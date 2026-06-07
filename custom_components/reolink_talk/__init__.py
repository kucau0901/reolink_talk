from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

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

_LOGGER = logging.getLogger(__name__)


def _import_reolink_cameras(hass: HomeAssistant, reolink_entry_ids: list[str], channel: int) -> dict:
    """Copy host/user/pass from the named Reolink config entries into our own
    `cameras` dict, keyed by the Reolink entry_id (so entity unique_ids/entity_ids
    are preserved across the migration)."""
    cameras: dict[str, dict] = {}
    for eid in reolink_entry_ids:
        re_entry = hass.config_entries.async_get_entry(eid)
        if re_entry is None or re_entry.domain != "reolink":
            continue
        d = re_entry.data
        if not d.get("host") or not d.get("username"):
            continue
        cameras[eid] = {
            CONF_HOST: d["host"],
            CONF_USERNAME: d["username"],
            CONF_PASSWORD: d.get("password", ""),
            CONF_BC_PORT: int(d.get("baichuan_port", DEFAULT_BC_PORT)),
            CONF_CHANNEL: int(channel),
            CONF_TITLE: re_entry.title or eid,
        }
    return cameras


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """v1 (reads Reolink entries at runtime) -> v2 (stores own credentials).

    One-time: copy the creds from the previously-selected Reolink entries into our
    own entry.data so we no longer depend on the Reolink integration at runtime.
    """
    if entry.version >= 2:
        return True
    channel = int(entry.options.get(CONF_CHANNEL, DEFAULT_CHANNEL))
    reolink_entry_ids = entry.options.get(CONF_REOLINK_ENTRY_IDS) or [
        e.entry_id for e in hass.config_entries.async_entries("reolink")
    ]
    cameras = _import_reolink_cameras(hass, reolink_entry_ids, channel)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_CAMERAS: cameras},
        version=2,
    )
    _LOGGER.info("reolink_talk migrated to v2 (standalone): imported %s camera(s)", len(cameras))
    return True

PLATFORMS: list[str] = ["media_player"]

OLD_WEBRTC_ENTITY_IDS: tuple[str, ...] = (
    "media_player.reolink_poortdeur_low_webrtc_old",
    "media_player.reolink_achtertuin_low_webrtc_old",
    "media_player.reolink_deurbel_webrtc_old",
)


async def async_setup(_hass: HomeAssistant, _config: dict) -> bool:
    return True


def _remove_old_webrtc_entities(hass: HomeAssistant) -> int:
    """Remove legacy WebRTC YAML media_player entities from the entity registry."""
    from homeassistant.helpers import entity_registry as er

    reg = er.async_get(hass)
    removed = 0
    for eid in OLD_WEBRTC_ENTITY_IDS:
        if reg.async_get(eid) is None:
            continue
        reg.async_remove(eid)
        removed += 1
    return removed


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Provide a one-shot cleanup service so the user can always remove the legacy
    # YAML/webrtc entities without editing storage files.
    if not hass.services.has_service(DOMAIN, "cleanup_old_webrtc_entities"):

        async def _svc_cleanup(_call) -> None:
            removed = _remove_old_webrtc_entities(hass)
            _LOGGER.info("cleanup_old_webrtc_entities removed %s entities", removed)

        hass.services.async_register(DOMAIN, "cleanup_old_webrtc_entities", _svc_cleanup)

    # First-ever setup (fresh install via the one-click flow): seed our own
    # `cameras` from whatever Reolink entries exist, so we are standalone from
    # the start. (Migration handles upgrades from v1.)
    if not entry.data.get(CONF_CAMERAS):
        reolink_entry_ids = entry.options.get(CONF_REOLINK_ENTRY_IDS) or [
            e.entry_id for e in hass.config_entries.async_entries("reolink")
        ]
        cameras = _import_reolink_cameras(hass, reolink_entry_ids, int(entry.options.get(CONF_CHANNEL, DEFAULT_CHANNEL)))
        if cameras:
            hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_CAMERAS: cameras})

    # Best-effort: remove legacy entities at startup so they don't re-appear in UI.
    removed = _remove_old_webrtc_entities(hass)
    if removed:
        _LOGGER.info("Removed %s legacy webrtc media_player entities", removed)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entities when options change (e.g. re-import / channel change)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
