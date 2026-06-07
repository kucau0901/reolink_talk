from __future__ import annotations

import asyncio
import logging
import shutil
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlparse

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import get_url

_LOGGER = logging.getLogger(__name__)


BC_MESSAGE_CLASS_1464: Final[bytes] = bytes.fromhex("00001464")  # status_code=0, class=1464


@dataclass(frozen=True)
class TalkAbility:
    duplex: str
    audio_stream_mode: str
    audio_type: str
    priority: int | None
    sample_rate: int
    sample_precision: int
    length_per_encoder: int
    sound_track: str


def _first_text(root: ET.Element, path: str) -> str | None:
    el = root.find(path)
    if el is None or el.text is None:
        return None
    return el.text.strip()

def _all_texts(root: ET.Element, path: str) -> list[str]:
    out: list[str] = []
    for el in root.findall(path):
        if el is None or el.text is None:
            continue
        t = el.text.strip()
        if t:
            out.append(t)
    return out


def parse_talk_ability(xml: str) -> TalkAbility:
    root = ET.fromstring(xml)
    ta = root.find(".//TalkAbility")
    if ta is None:
        raise ValueError("TalkAbility not found in response")

    # Prefer "best" settings when lists are present:
    # - FDX: full duplex is typically what we want for talkback
    # - mixAudioStream: avoids dependency on the live video stream audio mode
    duplex_list = _all_texts(ta, ".//duplexList/duplex")
    stream_mode_list = _all_texts(ta, ".//audioStreamModeList/audioStreamMode")

    duplex = _first_text(ta, ".//duplex") or ""
    if "FDX" in duplex_list:
        duplex = "FDX"
    if not duplex:
        duplex = duplex_list[0] if duplex_list else "FDX"

    audio_stream_mode = _first_text(ta, ".//audioStreamMode") or ""
    if "mixAudioStream" in stream_mode_list:
        audio_stream_mode = "mixAudioStream"
    if not audio_stream_mode:
        audio_stream_mode = stream_mode_list[0] if stream_mode_list else "followVideoStream"

    ac = ta.find(".//audioConfig")
    if ac is None:
        raise ValueError("audioConfig not found in TalkAbility")

    audio_type = _first_text(ac, ".//audioType") or "adpcm"
    prio_txt = _first_text(ac, ".//priority")
    priority = int(prio_txt) if prio_txt and prio_txt.isdigit() else None
    sample_rate = int(_first_text(ac, ".//sampleRate") or "16000")
    sample_precision = int(_first_text(ac, ".//samplePrecision") or "16")
    length_per_encoder = int(_first_text(ac, ".//lengthPerEncoder") or "1024")
    sound_track = _first_text(ac, ".//soundTrack") or "mono"

    return TalkAbility(
        duplex=duplex,
        audio_stream_mode=audio_stream_mode,
        audio_type=audio_type,
        priority=priority,
        sample_rate=sample_rate,
        sample_precision=sample_precision,
        length_per_encoder=length_per_encoder,
        sound_track=sound_track,
    )


def build_talk_config_xml(channel: int, ability: TalkAbility) -> str:
    # Match the XML shapes documented by neolink.
    prio = f"<priority>{ability.priority}</priority>\n" if ability.priority is not None else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>\n'
        "<body>\n"
        '<TalkConfig version="1.1">\n'
        f"<channelId>{channel}</channelId>\n"
        f"<duplex>{ability.duplex}</duplex>\n"
        f"<audioStreamMode>{ability.audio_stream_mode}</audioStreamMode>\n"
        "<audioConfig>\n"
        + prio
        + f"<audioType>{ability.audio_type}</audioType>\n"
        + f"<sampleRate>{ability.sample_rate}</sampleRate>\n"
        + f"<samplePrecision>{ability.sample_precision}</samplePrecision>\n"
        + f"<lengthPerEncoder>{ability.length_per_encoder}</lengthPerEncoder>\n"
        + f"<soundTrack>{ability.sound_track}</soundTrack>\n"
        + "</audioConfig>\n"
        + "</TalkConfig>\n"
        + "</body>\n"
    )


def build_talk_config_variants(channel: int, ability: TalkAbility) -> list[str]:
    """Return a small set of TalkConfig XML variants for firmware quirks."""
    full = build_talk_config_xml(channel, ability)
    variants: list[str] = [full]

    # Some firmwares appear picky about the XML header.
    if full.lstrip().startswith("<?xml"):
        try:
            _, rest = full.split("\n", 1)
            variants.append(rest)
        except ValueError:
            pass

    # Some firmwares expect just the TalkConfig element (no <body> wrapper).
    start = full.find("<TalkConfig")
    end = full.rfind("</TalkConfig>")
    if start != -1 and end != -1:
        tc = full[start : end + len("</TalkConfig>")] + "\n"
        if tc not in variants:
            variants.append(tc)

    return variants


def bcmedia_adpcm_packet(block: bytes) -> bytes:
    # Port of neolink bcmedia_adpcm() + padding rules.
    # block must be: 4 bytes predictor state + N bytes adpcm payload.
    if len(block) < 5:
        raise ValueError("ADPCM block too small")
    payload_len = len(block) + 4  # + magic u16 + blocksize u16
    # Neolink format: "block size without header, halved" (DVI-4 payload bytes / 2).
    block_size = ((len(block) - 4) // 2)
    header = struct.pack(
        "<IHHHH",
        0x62773130,  # MAGIC_HEADER_BCMEDIA_ADPCM
        payload_len,
        payload_len,
        0x0100,  # MAGIC_HEADER_BCMEDIA_ADPCM_DATA
        block_size,
    )
    pad_len = (-len(block)) % 8
    return header + block + (b"\x00" * pad_len)


def talk_binary_payload(adpcm_bytes: bytes, full_block_size: int, blocks_per_payload: int = 4) -> list[tuple[bytes, int]]:
    # Returns list of (binary_payload, blocks_in_payload).
    out: list[tuple[bytes, int]] = []
    blocks = [adpcm_bytes[i : i + full_block_size] for i in range(0, len(adpcm_bytes), full_block_size)]
    # Drop incomplete trailing block (if any)
    if blocks and len(blocks[-1]) != full_block_size:
        blocks = blocks[:-1]
    for i in range(0, len(blocks), blocks_per_payload):
        group = blocks[i : i + blocks_per_payload]
        payload = b"".join(bcmedia_adpcm_packet(b) for b in group)
        out.append((payload, len(group)))
    return out


async def fetch_bytes(hass: HomeAssistant, url: str) -> bytes:
    session = async_get_clientsession(hass)
    fetch_url = url
    # If it's a Home Assistant local URL (TTS/media proxy), we need to sign it,
    # because we are fetching server-side (no browser cookies / auth headers).
    try:
        parsed = urlparse(url)
        if url.startswith("/"):
            path_q = url
        elif parsed.scheme in ("http", "https"):
            path_q = parsed.path + (("?" + parsed.query) if parsed.query else "")
            base = get_url(hass, allow_internal=True)
            base_netloc = urlparse(base).netloc
            if parsed.netloc != base_netloc:
                path_q = ""
        else:
            path_q = ""

        if path_q:
            from homeassistant.components.http.auth import async_sign_path

            base = get_url(hass, allow_internal=True)
            signed = async_sign_path(hass, path_q)
            fetch_url = f"{base}{signed}"
    except Exception:
        # Best-effort; fall back to raw URL.
        pass

    async with session.get(fetch_url, allow_redirects=True) as resp:
        resp.raise_for_status()
        return await resp.read()


# IMA/DVI ADPCM encoder tables (standard IMA ADPCM).
_IMA_INDEX_TABLE: Final[list[int]] = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]
_IMA_STEP_TABLE: Final[list[int]] = [
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    16,
    17,
    19,
    21,
    23,
    25,
    28,
    31,
    34,
    37,
    41,
    45,
    50,
    55,
    60,
    66,
    73,
    80,
    88,
    97,
    107,
    118,
    130,
    143,
    157,
    173,
    190,
    209,
    230,
    253,
    279,
    307,
    337,
    371,
    408,
    449,
    494,
    544,
    598,
    658,
    724,
    796,
    876,
    963,
    1060,
    1166,
    1282,
    1411,
    1552,
    1707,
    1878,
    2066,
    2272,
    2499,
    2749,
    3024,
    3327,
    3660,
    4026,
    4428,
    4871,
    5358,
    5894,
    6484,
    7132,
    7845,
    8630,
    9493,
    10442,
    11487,
    12635,
    13899,
    15289,
    16818,
    18500,
    20350,
    22385,
    24623,
    27086,
    29794,
    32767,
]


# Loudness defaults (v0.3.1+). Camera talk speakers are small and often outdoors,
# and TTS audio frequently arrives quiet (soft prosody / low-level synthesis), so we
# normalize every clip to a consistent, loud target with EBU R128 loudnorm and catch
# peaks with a limiter. `volume` (the HA volume slider, 0..1) then attenuates from
# that loud baseline, so 1.0 = full loud and lower = quieter.
DEFAULT_LOUDNORM_I = -14.0   # integrated loudness target (LUFS)
DEFAULT_LOUDNORM_TP = -1.5   # true-peak ceiling (dBFS)
DEFAULT_LOUDNORM_LRA = 11.0  # loudness range
DEFAULT_GAIN_DB = 6.0        # extra makeup gain on top of loudnorm (limiter-protected)


def _build_af_chain(
    volume: float,
    *,
    loudnorm_i: float | None,
    loudnorm_tp: float,
    loudnorm_lra: float,
    gain_db: float,
    limiter: bool,
) -> str:
    """Compose the ffmpeg -af filter chain: loudnorm -> makeup gain -> volume -> limiter."""
    filters: list[str] = []
    if loudnorm_i is not None:
        filters.append(f"loudnorm=I={loudnorm_i}:TP={loudnorm_tp}:LRA={loudnorm_lra}")
    if gain_db:
        filters.append(f"volume={gain_db}dB")
    v = max(0.0, float(volume))
    if abs(v - 1.0) > 1e-6:
        filters.append(f"volume={v}")
    if limiter:
        # Pure peak limiting (level=false: do NOT auto-normalize), so the makeup gain
        # above can push hot without clipping/distorting the small camera speaker.
        filters.append("alimiter=level=false:limit=0.97")
    return ",".join(filters) if filters else "anull"


async def ffmpeg_to_pcm_s16le(
    input_bytes: bytes,
    *,
    sample_rate: int,
    volume: float = 1.0,
    loudnorm_i: float | None = DEFAULT_LOUDNORM_I,
    loudnorm_tp: float = DEFAULT_LOUDNORM_TP,
    loudnorm_lra: float = DEFAULT_LOUDNORM_LRA,
    gain_db: float = DEFAULT_GAIN_DB,
    limiter: bool = True,
) -> bytes:
    """Decode arbitrary audio to mono 16-bit PCM (little-endian) at sample_rate.

    By default (loudnorm_i set) the audio is loudness-normalized to a consistent,
    loud target and peak-limited, so quiet TTS plays loud through the camera speaker.
    Pass loudnorm_i=None for the legacy unprocessed behavior (just the volume gain).
    """
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    af = _build_af_chain(
        volume,
        loudnorm_i=loudnorm_i,
        loudnorm_tp=loudnorm_tp,
        loudnorm_lra=loudnorm_lra,
        gain_db=gain_db,
        limiter=limiter,
    )
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-af",
        af,
        "-ac",
        "1",
        "-ar",
        str(int(sample_rate)),
        "-f",
        "s16le",
        "pipe:1",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout
    out, err = await proc.communicate(input_bytes)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {err.decode('utf-8', 'ignore')}")
    return out


def _ima_encode_nibble(sample: int, predictor: int, step_index: int) -> tuple[int, int, int]:
    """Encode one PCM sample to a 4-bit IMA ADPCM nibble.

    Returns (nibble, new_predictor, new_step_index).
    """
    step = _IMA_STEP_TABLE[step_index]
    diff = sample - predictor
    sign = 0
    if diff < 0:
        sign = 8
        diff = -diff

    delta = 0
    vpdiff = step >> 3
    if diff >= step:
        delta |= 4
        diff -= step
        vpdiff += step
    if diff >= (step >> 1):
        delta |= 2
        diff -= step >> 1
        vpdiff += step >> 1
    if diff >= (step >> 2):
        delta |= 1
        vpdiff += step >> 2

    if sign:
        predictor -= vpdiff
    else:
        predictor += vpdiff

    predictor = max(-32768, min(32767, predictor))

    step_index += _IMA_INDEX_TABLE[delta | sign]
    step_index = max(0, min(88, step_index))

    return (delta | sign) & 0xF, predictor, step_index


def ima_adpcm_encode_dvi_blocks(pcm_s16le: bytes, *, full_block_size: int) -> bytes:
    """Encode PCM s16le into DVI-4 ADPCM blocks.

    Block layout expected by neolink talk:
    - 2 bytes: initial predictor sample (i16 LE)
    - 1 byte: step index
    - 1 byte: reserved (0)
    - (full_block_size - 4) bytes: packed nibbles, 2 samples per byte
    """
    if full_block_size < 8:
        raise ValueError("full_block_size too small")
    if len(pcm_s16le) % 2 != 0:
        raise ValueError("PCM length must be even (s16le)")

    payload_bytes = full_block_size - 4
    payload_samples = payload_bytes * 2

    # Convert pcm bytes to list of i16
    sample_count = len(pcm_s16le) // 2
    samples = struct.unpack("<" + ("h" * sample_count), pcm_s16le) if sample_count else ()
    if not samples:
        return b""

    # Streaming-style: each block header contains the current predictor + index
    # (the "last output" state), followed by payload_samples ADPCM-coded samples.
    predictor = int(samples[0])
    step_index = 0
    pos = 1  # first sample is used as initial predictor

    out = bytearray()
    while pos <= len(samples):
        block = bytearray()
        block += struct.pack("<hBB", predictor, step_index, 0)

        nibble_acc = None
        # Encode a fixed number of samples per block.
        for _ in range(payload_samples):
            s = int(samples[pos]) if pos < len(samples) else 0
            pos += 1
            nib, predictor, step_index = _ima_encode_nibble(s, predictor, step_index)
            if nibble_acc is None:
                nibble_acc = nib
            else:
                block.append((nibble_acc & 0xF) | ((nib & 0xF) << 4))
                nibble_acc = None
        if nibble_acc is not None:
            block.append(nibble_acc & 0xF)

        if len(block) < full_block_size:
            block.extend(b"\x00" * (full_block_size - len(block)))
        out += block[:full_block_size]

        # Stop once we've consumed all input samples and emitted at least one block.
        if pos >= len(samples):
            break

    return bytes(out)

