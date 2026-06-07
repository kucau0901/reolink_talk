#!/usr/bin/env python3
"""
Native, dependency-free Reolink Baichuan two-way-audio (talk) client.

Implements the Baichuan TCP protocol (port 9000) directly using only the
standard library + pycryptodome (Cryptodome) — NO reolink_aio. This makes the
HA component immune to reolink_aio internal API changes.

Build order / self-test:
  python3 baichuan_talk.py --host H --user U --password P            # login only
  python3 baichuan_talk.py --host H --user U --password P --sine 1000 --duration 2

The pure talk-framing helpers (TalkAbility parsing, TalkConfig XML, ADPCM
encoder, BcMedia framing) are vendored below — this module has ZERO external
deps beyond the standard library + pycryptodome.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import math
import shutil
import struct
import subprocess
from dataclasses import dataclass
from typing import Final

from Cryptodome.Cipher import AES  # pycryptodome (bundled by HA + reolink)


_LOG = logging.getLogger("baichuan_talk")

# ----------------------------------------------------------------------------
# Constants (reolink_aio/baichuan/util.py)
# ----------------------------------------------------------------------------
HEADER_MAGIC = bytes.fromhex("f0debc0a")
AES_IV = b"0123456789abcdef"
XML_KEY = [0x1F, 0x2D, 0x3C, 0x4B, 0x5A, 0x69, 0x78, 0xFF]
DEFAULT_BC_PORT = 9000
TIMEOUT = 30

_XMLHDR = '<?xml version="1.0" encoding="UTF-8" ?>\n'
LOGIN_XML = (
    _XMLHDR
    + "<body>\n<LoginUser version=\"1.1\">\n<userName>{user}</userName>\n"
    + "<password>{password}</password>\n<userVer>1</userVer>\n</LoginUser>\n"
    + "<LoginNet version=\"1.1\">\n<type>LAN</type>\n<udpPort>0</udpPort>\n</LoginNet>\n</body>\n"
)
CHANNEL_EXT_XML = (
    _XMLHDR + "<Extension version=\"1.1\">\n<channelId>{channel}</channelId>\n</Extension>\n"
)



# ============================================================================
# Vendored pure talk helpers (ADPCM/BcMedia framing, TalkConfig XML, TalkAbility)
# Copied from scripts/reolink_talk_debug.py — pure Python, NO reolink_aio.
# ============================================================================
BC_MESSAGE_CLASS_1464 = bytes.fromhex("00001464")
LOG = _LOG  # helpers below log via LOG

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
    full = build_talk_config_xml(channel, ability)
    variants: list[str] = [full]

    if full.lstrip().startswith("<?xml"):
        try:
            _, rest = full.split("\n", 1)
            variants.append(rest)
        except ValueError:
            pass

    start = full.find("<TalkConfig")
    end = full.rfind("</TalkConfig>")
    if start != -1 and end != -1:
        tc = full[start : end + len("</TalkConfig>")] + "\n"
        if tc not in variants:
            variants.append(tc)

    return variants


def _riff_chunks(wav: bytes):
    if len(wav) < 12 or wav[0:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise ValueError("Not a RIFF/WAVE file")
    off = 12
    while off + 8 <= len(wav):
        cid = wav[off : off + 4]
        size = int.from_bytes(wav[off + 4 : off + 8], "little")
        data_off = off + 8
        data_end = data_off + size
        truncated = data_end > len(wav)
        if truncated:
            data_end = len(wav)
        yield cid, wav[data_off:data_end]
        if truncated:
            break
        off = data_end + (size % 2)


def extract_wav_fmt_and_data(wav: bytes) -> tuple[dict, bytes]:
    fmt: dict = {}
    data: bytes | None = None
    for cid, payload in _riff_chunks(wav):
        if cid == b"fmt ":
            if len(payload) < 16:
                raise ValueError("Invalid fmt chunk")
            fmt["audio_format"] = int.from_bytes(payload[0:2], "little")
            fmt["channels"] = int.from_bytes(payload[2:4], "little")
            fmt["sample_rate"] = int.from_bytes(payload[4:8], "little")
            fmt["byte_rate"] = int.from_bytes(payload[8:12], "little")
            fmt["block_align"] = int.from_bytes(payload[12:14], "little")
            fmt["bits_per_sample"] = int.from_bytes(payload[14:16], "little")
        elif cid == b"data":
            data = payload
    if not fmt or data is None:
        raise ValueError("WAV missing fmt or data chunk")
    return fmt, data


def bcmedia_adpcm_packet(block: bytes) -> bytes:
    if len(block) < 5:
        raise ValueError("ADPCM block too small")
    payload_len = len(block) + 4
    # Default interpretation seems to be "samples per block", but some firmwares
    # might interpret this differently. We allow overriding via CLI.
    samples_per_block = (len(block) - 4) * 2 + 1
    header = struct.pack(
        "<IHHHH",
        0x62773130,
        payload_len,
        payload_len,
        0x0100,
        samples_per_block,
    )
    pad_len = (-len(block)) % 8
    return header + block + (b"\x00" * pad_len)


def talk_binary_payload(adpcm_bytes: bytes, full_block_size: int, blocks_per_payload: int = 4) -> list[tuple[bytes, int]]:
    out: list[tuple[bytes, int]] = []
    blocks = [adpcm_bytes[i : i + full_block_size] for i in range(0, len(adpcm_bytes), full_block_size)]
    if blocks and len(blocks[-1]) != full_block_size:
        blocks = blocks[:-1]
    for i in range(0, len(blocks), blocks_per_payload):
        group = blocks[i : i + blocks_per_payload]
        payload = b"".join(bcmedia_adpcm_packet(b) for b in group)
        out.append((payload, len(group)))
    return out


def talk_binary_payload_custom(
    adpcm_bytes: bytes,
    *,
    full_block_size: int,
    blocks_per_payload: int,
    bcmedia_mode: str,
) -> list[tuple[bytes, int]]:
    def _packet(block: bytes) -> bytes:
        if len(block) < 5:
            raise ValueError("ADPCM block too small")
        payload_len = len(block) + 4

        if bcmedia_mode == "samples":
            val = (len(block) - 4) * 2 + 1
        elif bcmedia_mode == "bytes_half":
            # Older guess: bytes (excluding 4-byte wav header) / 2
            val = ((len(block) - 4) // 2)
        elif bcmedia_mode == "bytes":
            val = (len(block) - 4)
        else:
            raise ValueError(f"Unknown bcmedia_mode={bcmedia_mode!r}")

        header = struct.pack("<IHHHH", 0x62773130, payload_len, payload_len, 0x0100, int(val))
        pad_len = (-len(block)) % 8
        return header + block + (b"\x00" * pad_len)

    out: list[tuple[bytes, int]] = []
    blocks = [adpcm_bytes[i : i + full_block_size] for i in range(0, len(adpcm_bytes), full_block_size)]
    if blocks and len(blocks[-1]) != full_block_size:
        blocks = blocks[:-1]
    for i in range(0, len(blocks), blocks_per_payload):
        group = blocks[i : i + blocks_per_payload]
        payload = b"".join(_packet(b) for b in group)
        out.append((payload, len(group)))
    return out


async def ffmpeg_to_adpcm_wav(input_bytes: bytes, *, sample_rate: int, block_size: int, volume: float = 1.0) -> bytes:
    raise RuntimeError("ffmpeg_to_adpcm_wav is deprecated; use ffmpeg_to_pcm_s16le + ima_adpcm_encode_dvi_blocks")


async def ffmpeg_to_pcm_s16le(input_bytes: bytes, *, sample_rate: int, volume: float = 1.0) -> bytes:
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-af",
        f"volume={max(0.0, float(volume))}",
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


def _ima_encode_nibble(sample: int, predictor: int, step_index: int) -> tuple[int, int, int]:
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

    predictor = predictor - vpdiff if sign else predictor + vpdiff
    predictor = max(-32768, min(32767, predictor))

    step_index += _IMA_INDEX_TABLE[delta | sign]
    step_index = max(0, min(88, step_index))

    return (delta | sign) & 0xF, predictor, step_index


def ima_adpcm_encode_dvi_blocks(pcm_s16le: bytes, *, full_block_size: int) -> bytes:
    """Encode PCM s16le into DVI-4 ADPCM blocks (streaming-state).

    Block layout:
    - 2 bytes: predictor (i16 LE)
    - 1 byte: step index
    - 1 byte: reserved (0)
    - (full_block_size - 4) bytes: packed IMA ADPCM nibbles
    """
    if full_block_size < 8:
        raise ValueError("full_block_size too small")
    if len(pcm_s16le) % 2 != 0:
        raise ValueError("PCM length must be even (s16le)")

    payload_bytes = full_block_size - 4
    payload_samples = payload_bytes * 2

    sample_count = len(pcm_s16le) // 2
    samples = struct.unpack("<" + ("h" * sample_count), pcm_s16le) if sample_count else ()
    if not samples:
        return b""

    predictor = int(samples[0])
    step_index = 0
    pos = 1  # predictor sample is implied by the block header

    out = bytearray()
    while pos <= len(samples):
        block = bytearray()
        block += struct.pack("<hBB", predictor, step_index, 0)

        nibble_acc = None
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

        if pos >= len(samples):
            break

    return bytes(out)



def generate_sine_wav(*, freq_hz: int, duration_s: float, sample_rate: int = 16000) -> bytes:
    """Generate a mono 16-bit PCM WAV containing a sine wave."""
    import math

    n = int(sample_rate * duration_s)
    amp = 0.25  # avoid clipping

    pcm = bytearray()
    for i in range(n):
        t = i / sample_rate
        v = amp * math.sin(2 * math.pi * freq_hz * t)
        s = int(max(-1.0, min(1.0, v)) * 32767.0)
        pcm += struct.pack("<h", s)

    # RIFF/WAVE header
    num_channels = 1
    bits = 16
    byte_rate = sample_rate * num_channels * (bits // 8)
    block_align = num_channels * (bits // 8)
    data_len = len(pcm)

    out = bytearray()
    out += b"RIFF"
    out += struct.pack("<I", 36 + data_len)
    out += b"WAVE"
    out += b"fmt "
    out += struct.pack("<I", 16)  # PCM fmt chunk size
    out += struct.pack("<HHIIHH", 1, num_channels, sample_rate, byte_rate, block_align, bits)
    out += b"data"
    out += struct.pack("<I", data_len)
    out += pcm
    return bytes(out)



# ----------------------------------------------------------------------------
# Crypto primitives
# ----------------------------------------------------------------------------
def md5_modern(s: str) -> str:
    return hashlib.md5(s.encode("utf8")).hexdigest()[0:31].upper()


def derive_aes_key(nonce: str, password: str) -> bytes:
    return md5_modern(f"{nonce}-{password}")[0:16].encode("utf8")


def aes_encrypt(key: bytes, body: bytes) -> bytes:
    if not body:
        return b""
    return AES.new(key=key, mode=AES.MODE_CFB, iv=AES_IV, segment_size=128).encrypt(body)


def aes_decrypt(key: bytes, data: bytes) -> bytes:
    if not data:
        return b""
    return AES.new(key=key, mode=AES.MODE_CFB, iv=AES_IV, segment_size=128).decrypt(data)


def bc_crypt(buf, offset: int) -> bytes:
    """Symmetric Baichuan XOR cipher (encrypt == decrypt)."""
    if isinstance(buf, str):
        buf = buf.encode("utf8")
    o = offset % 256
    return bytes((b ^ XML_KEY[(o + i) % 8] ^ o) for i, b in enumerate(buf))


# ----------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------
class BaichuanApiError(Exception):
    def __init__(self, rsp_code: int, cmd_id: int):
        self.rsp_code = rsp_code
        self.cmd_id = cmd_id
        super().__init__(f"Baichuan status {rsp_code} from cmd_id {cmd_id}")


# ----------------------------------------------------------------------------
# asyncio Protocol: frame buffering + response matching
# ----------------------------------------------------------------------------
class _BaichuanProtocol(asyncio.Protocol):
    def __init__(self):
        self._buf = b""
        self.futures: dict[tuple[int, int], asyncio.Future] = {}
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def data_received(self, data: bytes):
        if data[:4] == HEADER_MAGIC:
            self._buf = data
        elif self._buf:
            self._buf += data
        else:
            return  # junk before any magic; ignore
        self._parse()

    def _parse(self):
        d = self._buf
        while len(d) >= 20 and d[:4] == HEADER_MAGIC:
            cmd_id = int.from_bytes(d[4:8], "little")
            len_body = int.from_bytes(d[8:12], "little")
            full_mess_id = int.from_bytes(d[12:16], "little")
            mclass = d[18:20].hex()
            if mclass in ("1464", "0000"):
                if len(d) < 24:
                    break
                len_header = 24
                status = int.from_bytes(d[16:18], "little")
                payload_offset = int.from_bytes(d[20:24], "little")
            elif mclass == "1466":
                len_header = 20
                status = None
                payload_offset = 0
            else:
                # 1465/legacy or unknown -> cannot parse; drop buffer
                self._buf = b""
                return
            if (len(d) - len_header) < len_body:
                break  # incomplete; wait for more
            if payload_offset == 0:
                payload_offset = len_body
            len_chunk = len_body + len_header
            len_message = payload_offset + len_header
            chunk = d[:len_message]
            payload = d[len_message:len_chunk]
            fut = self.futures.get((cmd_id, full_mess_id))
            if len_header == 24 and status is not None and status not in (200, 201, 300):
                if fut and not fut.done():
                    fut.set_exception(BaichuanApiError(status, cmd_id))
            elif fut and not fut.done():
                fut.set_result((chunk, len_header, payload))
            # else: unsolicited / push -> drop
            d = d[len_chunk:]
        self._buf = d if d[:4] == HEADER_MAGIC else b""

    def connection_lost(self, exc):
        for f in self.futures.values():
            if not f.done():
                f.set_exception(ConnectionError("Baichuan connection lost"))


# ----------------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------------
class BaichuanTalkClient:
    def __init__(self, host: str, username: str, password: str, port: int = DEFAULT_BC_PORT):
        self.host = host
        self.user = username
        self.pw = password
        self.port = port
        self.proto: _BaichuanProtocol | None = None
        self.transport = None
        self.aes_key: bytes | None = None
        self.nonce: str | None = None
        self.logged_in = False
        self._mess_id = 0
        self._wlock = asyncio.Lock()

    async def connect(self):
        loop = asyncio.get_event_loop()
        self.transport, self.proto = await loop.create_connection(
            _BaichuanProtocol, self.host, self.port
        )

    def _next_mess_id(self) -> int:
        self._mess_id = (self._mess_id + 1) % 16777216
        return self._mess_id

    def _build(self, cmd_id, ch_id, enc_type, body="", ext="", message_class="1464"):
        body_b = body.encode("utf8")
        ext_b = ext.encode("utf8")
        mess_len = len(ext_b) + len(body_b)
        payload_offset = len(ext_b)
        mid = self._next_mess_id()
        mess_id_bytes = bytes([ch_id]) + mid.to_bytes(3, "little")
        full_mess_id = int.from_bytes(mess_id_bytes, "little")
        header = HEADER_MAGIC + cmd_id.to_bytes(4, "little") + mess_len.to_bytes(4, "little") + mess_id_bytes
        if message_class == "1465":
            header += bytes.fromhex("12dc" + "1465")
        else:
            header += bytes.fromhex("0000" + "1464") + payload_offset.to_bytes(4, "little")
        enc = b""
        if mess_len > 0:
            if enc_type == "bc":
                enc = bc_crypt(ext_b, ch_id) + bc_crypt(body_b, ch_id)
            else:
                enc = aes_encrypt(self.aes_key, ext_b) + aes_encrypt(self.aes_key, body_b)
        return header + enc, cmd_id, full_mess_id

    def _decrypt_response(self, chunk: bytes, len_header: int) -> str:
        msg = chunk[len_header:]
        if not msg:
            return ""
        ch_id = chunk[12]
        if len_header == 24:
            return aes_decrypt(self.aes_key, msg).decode("utf8", "ignore")
        enc = chunk[16:18].hex()
        if enc == "00dd":
            return msg.decode("utf8", "ignore")
        if enc in ("01dd", "12dd"):
            return bc_crypt(msg, ch_id).decode("utf8", "ignore")
        if enc in ("02dd", "03dd"):
            return aes_decrypt(self.aes_key, msg).decode("utf8", "ignore")
        return msg.decode("utf8", "ignore")

    async def _send(self, cmd_id, *, channel=None, body="", ext="", enc_type="aes",
                    message_class="1464", timeout=TIMEOUT, expect_response=True):
        ch_id = 250 if channel is None else channel + 1
        if channel is not None:
            ext = CHANNEL_EXT_XML.format(channel=channel)
        packet, cid, fmid = self._build(cmd_id, ch_id, enc_type, body, ext, message_class)
        loop = asyncio.get_event_loop()
        fut = loop.create_future() if expect_response else None
        if fut is not None:
            self.proto.futures[(cid, fmid)] = fut
        try:
            async with self._wlock:
                self.transport.write(packet)
            if fut is None:
                return None
            chunk, len_header, payload = await asyncio.wait_for(fut, timeout)
        finally:
            if fut is not None:
                self.proto.futures.pop((cid, fmid), None)
        if _LOG.isEnabledFor(logging.DEBUG):
            _LOG.debug("RESP cmd=%s len_header=%s class=%s flag16=%s ch_id=%s bodylen=%s payloadlen=%s",
                       cmd_id, len_header, chunk[18:20].hex(), chunk[16:18].hex(), chunk[12],
                       len(chunk) - len_header, len(payload))
        return self._decrypt_response(chunk, len_header)

    async def get_talk_ability(self, channel: int = 0) -> str:
        """cmd 10 GetTalkAbility (AES). Returns decrypted XML."""
        return await self._send(10, channel=channel, enc_type="aes")

    async def send_talk_config(self, channel, config_xml, enc_type="aes"):
        return await self._send(201, channel=channel, body=config_xml, enc_type=enc_type)

    async def stop_talk(self, channel, enc_type="aes"):
        return await self._send(11, channel=channel, enc_type=enc_type)

    async def send_talk_audio(self, channel, binary_payload: bytes, enc_type="aes"):
        """cmd 202: encrypted Extension XML (binaryData/channelId) + raw BcMedia payload + best-effort ACK."""
        ch_id = channel + 1
        ext = (
            _XMLHDR
            + '<Extension version="1.1">\n<binaryData>1</binaryData>\n'
            + f"<channelId>{channel}</channelId>\n</Extension>\n"
        )
        ext_b = ext.encode("utf8")
        enc_ext = bc_crypt(ext_b, ch_id) if enc_type == "bc" else aes_encrypt(self.aes_key, ext_b)
        payload_offset = len(enc_ext)
        mess_len = payload_offset + len(binary_payload)
        mid = self._next_mess_id()
        mess_id_bytes = bytes([ch_id]) + mid.to_bytes(3, "little")
        full_mess_id = int.from_bytes(mess_id_bytes, "little")
        header = (
            HEADER_MAGIC + (202).to_bytes(4, "little") + mess_len.to_bytes(4, "little")
            + mess_id_bytes + bytes.fromhex("0000" + "1464") + payload_offset.to_bytes(4, "little")
        )
        await self._send_raw_ack(header + enc_ext + binary_payload, 202, full_mess_id, timeout=5.0)

    async def talk(self, channel, adpcm_payloads, sample_rate, full_block, variants):
        """High-level: TalkConfig (AES->BC + variant fallback, best-effort stop) -> audio loop -> stop."""
        enc_used = None
        last_err = None
        for cfg in variants:
            for enc in ("aes", "bc"):
                try:
                    await self.send_talk_config(channel, cfg, enc_type=enc)
                    enc_used = enc
                    last_err = None
                    break
                except BaichuanApiError as e:
                    last_err = e
                    if e.rsp_code in (400, 421, 422):
                        for se in ("aes", "bc"):
                            try:
                                await self.stop_talk(channel, enc_type=se)
                            except Exception:
                                pass
                        continue
                    raise
            if enc_used is not None:
                break
        if enc_used is None:
            raise last_err or RuntimeError("TalkConfig rejected for all variants/encryptions")
        _LOG.info("TalkConfig accepted (enc=%s). streaming %d payloads...", enc_used, len(adpcm_payloads))
        for payload, blocks in adpcm_payloads:
            await self.send_talk_audio(channel, payload, enc_type=enc_used)
            adpcm_len = full_block * blocks
            samples = (adpcm_len - 4 * blocks) * 2 + blocks
            await asyncio.sleep(samples / float(sample_rate))
        try:
            await self.stop_talk(channel, enc_type=enc_used)
        except Exception:
            pass

    async def _send_raw_ack(self, packet: bytes, cmd_id: int, full_mess_id: int, timeout=5.0):
        """Write a pre-built packet (talk binary) and best-effort await the ACK."""
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self.proto.futures[(cmd_id, full_mess_id)] = fut
        try:
            async with self._wlock:
                self.transport.write(packet)
            try:
                await asyncio.wait_for(fut, timeout)
            except Exception:
                pass
        finally:
            self.proto.futures.pop((cmd_id, full_mess_id), None)

    async def login(self):
        # Step 1: get nonce (cmd 1, BC, class 1465, header-only)
        resp = await self._send(1, enc_type="bc", message_class="1465", body="")
        self.nonce = _xml_value(resp, "nonce")
        if not self.nonce:
            raise RuntimeError(f"no nonce in login step-1 response: {resp[:200]!r}")
        self.aes_key = derive_aes_key(self.nonce, self.pw)
        # Step 2: modern login (cmd 1, BC, body=LOGIN_XML with hashed creds)
        user_hash = md5_modern(f"{self.user}{self.nonce}")
        pass_hash = md5_modern(f"{self.pw}{self.nonce}")
        body = LOGIN_XML.format(user=user_hash, password=pass_hash)
        resp2 = await self._send(1, enc_type="bc", body=body)
        self.logged_in = True
        return resp2

    async def logout(self):
        try:
            body = (
                _XMLHDR
                + "<body>\n<LoginUser version=\"1.1\">\n<userName>{u}</userName>\n"
                + "<password>{p}</password>\n<userVer>1</userVer>\n</LoginUser>\n</body>\n"
            ).format(u=self.user, p=self.pw)
            await self._send(2, body=body, timeout=5)
        except Exception:
            pass

    async def close(self):
        try:
            if self.transport:
                self.transport.close()
        except Exception:
            pass


def _xml_value(xml: str, key: str):
    try:
        el = ET.fromstring(xml)
    except Exception:
        # find the start of xml if there is a BOM/garbage prefix
        i = xml.find("<?xml")
        if i < 0:
            i = xml.find("<")
        try:
            el = ET.fromstring(xml[i:]) if i >= 0 else None
        except Exception:
            return None
    if el is None:
        return None
    found = el.find(f".//{key}")
    return found.text if found is not None else None


# ----------------------------------------------------------------------------
# Self-test
# ----------------------------------------------------------------------------
async def _main(args):
    cli = BaichuanTalkClient(args.host, args.user, args.password, args.port)
    _LOG.info("connecting to %s:%s ...", args.host, args.port)
    await cli.connect()
    _LOG.info("connected. logging in ...")
    resp = await cli.login()
    _LOG.info("LOGIN OK. nonce=%s aes_key=%s", cli.nonce, cli.aes_key)
    ability_xml = await cli.get_talk_ability(args.channel)
    ab = parse_talk_ability(ability_xml)
    _LOG.info("TalkAbility: audioType=%s sampleRate=%s lengthPerEncoder=%s duplex=%s streamMode=%s",
              ab.audio_type, ab.sample_rate, ab.length_per_encoder, ab.duplex, ab.audio_stream_mode)
    if args.sine is not None:
        # RLC-811A working framing: mixAudioStream, full_block=(lpe//2)+4, bytes_half, bpp=1
        ab = TalkAbility(
            duplex=ab.duplex, audio_stream_mode="mixAudioStream", audio_type=ab.audio_type,
            priority=ab.priority, sample_rate=ab.sample_rate, sample_precision=ab.sample_precision,
            length_per_encoder=ab.length_per_encoder, sound_track=ab.sound_track,
        )
        full_block = (int(ab.length_per_encoder) // 2) + 4
        wav = generate_sine_wav(freq_hz=int(args.sine), duration_s=float(args.duration), sample_rate=16000)
        pcm = await ffmpeg_to_pcm_s16le(wav, sample_rate=ab.sample_rate, volume=10.0)
        adpcm = ima_adpcm_encode_dvi_blocks(pcm, full_block_size=full_block)
        payloads = talk_binary_payload_custom(
            adpcm, full_block_size=full_block, blocks_per_payload=1, bcmedia_mode="bytes_half")
        variants = build_talk_config_variants(args.channel, ab)
        _LOG.info("NATIVE talk: full_block=%s payloads=%s sine=%dHz %.1fs", full_block, len(payloads), args.sine, args.duration)
        await cli.talk(args.channel, payloads, ab.sample_rate, full_block, variants)
        _LOG.info("NATIVE talk DONE.")
    await cli.logout()
    await cli.close()
    return 0


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--port", type=int, default=DEFAULT_BC_PORT)
    ap.add_argument("--channel", type=int, default=0)
    ap.add_argument("--sine", type=int, default=None)
    ap.add_argument("--duration", type=float, default=2.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
