from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from homeassistant.components.media_player import MediaPlayerEntity
from homeassistant.components.media_player.const import (
    MediaPlayerEntityFeature,
    MediaType,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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
from .baichuan_talk import BaichuanTalkClient
from .talk import (
    build_talk_config_variants,
    ffmpeg_to_pcm_s16le,
    fetch_bytes,
    ima_adpcm_encode_dvi_blocks,
    parse_talk_ability,
    talk_binary_payload,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReolinkTarget:
    host: str
    http_port: int | None
    use_https: bool | None
    port: int
    username: str
    password: str
    title: str
    channel: int


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    entities: list[ReolinkTalkPlayer] = []

    # STANDALONE (v0.3.0+): credentials are stored in our OWN entry.data["cameras"],
    # keyed by the original Reolink entry_id so unique_ids / entity_ids are preserved.
    cameras: dict = entry.data.get(CONF_CAMERAS) or {}
    if cameras:
        for key, cam in cameras.items():
            try:
                target = ReolinkTarget(
                    host=cam[CONF_HOST],
                    http_port=None,
                    use_https=None,
                    port=int(cam.get(CONF_BC_PORT, DEFAULT_BC_PORT)),
                    username=cam[CONF_USERNAME],
                    password=cam.get(CONF_PASSWORD, ""),
                    title=cam.get(CONF_TITLE, cam[CONF_HOST]),
                    channel=int(cam.get(CONF_CHANNEL, DEFAULT_CHANNEL)),
                )
            except KeyError as err:
                _LOGGER.warning("reolink_talk: skipping camera %s (missing field %s)", key, err)
                continue
            entities.append(ReolinkTalkPlayer(hass, key, target, f"Reolink Talk {target.title}"))
        async_add_entities(entities, update_before_add=False)
        return

    # LEGACY FALLBACK (only before migration/import has populated `cameras`):
    # read credentials live from the Reolink config entries.
    reolink_entry_ids: list[str] = entry.options.get(CONF_REOLINK_ENTRY_IDS, [])
    if not reolink_entry_ids:
        reolink_entry_ids = [e.entry_id for e in hass.config_entries.async_entries("reolink")]
    channel: int = int(entry.options.get(CONF_CHANNEL, DEFAULT_CHANNEL))
    reolink_entries = {e.entry_id: e for e in hass.config_entries.async_entries("reolink")}
    for reolink_entry_id in reolink_entry_ids:
        re_entry = reolink_entries.get(reolink_entry_id)
        if re_entry is None:
            continue
        data = re_entry.data
        try:
            target = ReolinkTarget(
                host=data["host"],
                http_port=data.get("port"),
                use_https=data.get("use_https"),
                port=int(data.get("baichuan_port", 9000)),
                username=data["username"],
                password=data["password"],
                title=re_entry.title,
                channel=channel,
            )
        except KeyError:
            continue
        entities.append(ReolinkTalkPlayer(hass, reolink_entry_id, target, f"Reolink Talk {re_entry.title or reolink_entry_id}"))

    async_add_entities(entities, update_before_add=False)


class ReolinkTalkPlayer(MediaPlayerEntity):
    # Add BROWSE_MEDIA because some frontend pickers filter to "browsable"
    # players even though we only need PLAY_MEDIA for TTS/MP3 playback.
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.BROWSE_MEDIA
        | getattr(MediaPlayerEntityFeature, "MEDIA_ANNOUNCE", 0)
    )
    _attr_media_content_type = None
    _attr_icon = "mdi:cctv"
    _attr_state = MediaPlayerState.IDLE
    # Some UI target pickers filter media_players to "speaker"-like devices.
    # Older HA versions model this as a plain string, not an enum.
    _attr_device_class = "speaker"

    def __init__(self, hass: HomeAssistant, reolink_entry_id: str, target: ReolinkTarget, mp_name: str) -> None:
        self.hass = hass
        self._reolink_entry_id = reolink_entry_id
        self._target = target
        # Keep entity_id stable and automation-friendly by controlling the base name.
        # This is intentionally aligned to the user's previous YAML media_player names.
        self._attr_name = mp_name
        self._attr_unique_id = f"{DOMAIN}:{reolink_entry_id}:{target.channel}"
        self._attr_volume_level = 1.0
        self._lock = asyncio.Lock()
        self._last_ability = None

    async def async_added_to_hass(self) -> None:
        # Lightweight, best-effort probe to decide if the camera supports talk.
        # We keep the entity available even if the probe fails (camera offline),
        # but if the camera explicitly reports a non-ADPCM talk type, we mark it
        # unavailable to avoid confusion.
        try:
            await self._probe_ability(timeout_s=3.0)
        except Exception:
            # Offline or transient failure; do not mark unavailable.
            return

    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        """Expose the standard HA media-source tree (includes TTS providers)."""
        from homeassistant.components import media_source

        # No custom filter here: we explicitly want to keep TTS providers visible
        # in the media browser for this player.
        return await media_source.async_browse_media(self.hass, media_content_id)

    async def async_set_volume_level(self, volume: float) -> None:
        # Software volume only. As of v0.3.1 the audio is loudness-normalized
        # (EBU R128 loudnorm + limiter) during ffmpeg transcoding so quiet TTS plays
        # loud and consistent; this slider then attenuates from that loud baseline
        # (1.0 = full loud). The camera-side "speak volume" API is intentionally not
        # used (the native client is dependency-free, no reolink_aio).
        volume = max(0.0, min(1.0, float(volume)))
        self._attr_volume_level = volume
        self.async_write_ha_state()

    async def async_play_media(self, media_type: str, media_id: str, **kwargs) -> None:
        # Resolve HA media-source URLs if needed.
        from homeassistant.components.media_player import async_process_play_media_url
        from homeassistant.components.media_source import async_resolve_media

        async with self._lock:
            try:
                self._attr_state = MediaPlayerState.PLAYING
                self.async_write_ha_state()
                media_bytes = await self._resolve_media_bytes(media_type, media_id)
                await self._play_bytes(media_bytes)
            except Exception:
                _LOGGER.exception("play_media failed for %s (media_id=%s)", self.entity_id, media_id)
                raise
            finally:
                self._attr_state = MediaPlayerState.IDLE
                self.async_write_ha_state()

    async def _resolve_media_bytes(self, media_type: str, media_id: str) -> bytes:
        """Return media bytes for play_media.

        We handle local media-source items ourselves because HA's local_source
        identifier parsing can vary per version/config, and we want this
        integration to be resilient.
        """
        from homeassistant.components.media_player import async_process_play_media_url
        from homeassistant.components.media_source import async_resolve_media

        # 1) Local media source (Home Assistant `media_dirs`), expected patterns:
        # - media-source://media_source/local/<dir_id>/<path>
        # - media-source://media_source/local/<path> (fallback to dir_id="media")
        local_prefix = "media-source://media_source/local/"
        if media_id.startswith(local_prefix):
            rest = media_id[len(local_prefix) :]
            rest = rest.lstrip("/")
            parts = rest.split("/", 1)
            if len(parts) == 1:
                dir_id, rel_path = "media", parts[0]
            else:
                dir_id, rel_path = parts[0], parts[1]

            # Resolve base dir from runtime config if available.
            base_dir = None
            try:
                base_dir = self.hass.config.media_dirs.get(dir_id)  # type: ignore[attr-defined]
            except Exception:
                base_dir = None

            # Hard fallback: most setups map "media" -> "/config/media".
            if not base_dir and dir_id == "media":
                base_dir = "/config/media"

            if not base_dir:
                raise ValueError(f"Unknown local media dir_id={dir_id!r} in {media_id!r}")

            # Prevent path traversal.
            base_dir = os.path.abspath(str(base_dir))
            abs_path = os.path.abspath(os.path.join(base_dir, rel_path))
            if not abs_path.startswith(base_dir + os.sep) and abs_path != base_dir:
                raise ValueError("Invalid media path")

            def _read() -> bytes:
                with open(abs_path, "rb") as f:
                    return f.read()

            return await self.hass.async_add_executor_job(_read)

        # 2) Other media sources: ask HA to resolve to a URL.
        if media_id.startswith("media-source://"):
            resolved = await async_resolve_media(self.hass, media_id, media_type)
            media_url = resolved.url
        else:
            media_url = media_id

        media_url = async_process_play_media_url(self.hass, media_url)
        return await fetch_bytes(self.hass, media_url)

    async def _play_bytes(self, media_bytes: bytes) -> None:
        # NATIVE v0.2.0: talk directly over Baichuan (TCP :9000) — NO reolink_aio.
        cli = BaichuanTalkClient(
            self._target.host,
            self._target.username,
            self._target.password,
            port=int(self._target.port),
        )
        try:
            await cli.connect()
            await cli.login()

            # 1) TalkAbility (cached) -> ADPCM parameters
            ability = self._last_ability
            if ability is None:
                ability_xml = await cli.get_talk_ability(self._target.channel)
                ability = parse_talk_ability(ability_xml)
                self._last_ability = ability
            if ability.audio_type.lower() != "adpcm":
                raise RuntimeError(f"Unsupported Reolink talk audioType={ability.audio_type}")

            # 2) Transcode input -> PCM -> DVI-4 ADPCM blocks.
            # full_block_size = (lengthPerEncoder / 2) + 4 (NOT WAV block_align).
            full_block_size = (int(ability.length_per_encoder) // 2) + 4
            pcm = await ffmpeg_to_pcm_s16le(
                media_bytes,
                sample_rate=ability.sample_rate,
                volume=float(self._attr_volume_level or 1.0),
            )
            adpcm_bytes = ima_adpcm_encode_dvi_blocks(pcm, full_block_size=full_block_size)
            payloads = talk_binary_payload(adpcm_bytes, full_block_size, blocks_per_payload=4)
            variants = build_talk_config_variants(self._target.channel, ability)

            # 3) TalkConfig (201, AES->BC + variant + best-effort-stop fallback)
            #    -> stream cmd-202 audio (per-packet ACK) -> stop (11).
            await cli.talk(self._target.channel, payloads, ability.sample_rate, full_block_size, variants)
        finally:
            await cli.close()

    async def _probe_ability(self, *, timeout_s: float = 5.0):
        # Cache within the entity instance to avoid extra round-trips.
        if self._last_ability is not None:
            return self._last_ability

        cli = BaichuanTalkClient(
            self._target.host,
            self._target.username,
            self._target.password,
            port=int(self._target.port),
        )
        try:
            async with asyncio.timeout(timeout_s):
                await cli.connect()
                await cli.login()
                ability_xml = await cli.get_talk_ability(self._target.channel)
            ability = parse_talk_ability(ability_xml)
            self._last_ability = ability

            if ability.audio_type.lower() != "adpcm":
                self._attr_available = False
                self.async_write_ha_state()
            return ability
        finally:
            await cli.close()
