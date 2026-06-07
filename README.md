# Reolink Talk (Two-Way Audio) for Home Assistant

![Reolink Talk](docs/banner.png)

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)

Expose Reolink cameras that support **two-way audio** as `media_player` entities, so you can play:

- MP3/WAV files (local media or URLs)
- Home Assistant TTS output (anything that resolves to audio)

It talks to the camera directly and **does not depend on go2rtc, Frigate, or Docker** for talkback.

## v0.3.0 — fully standalone

Starting with **v0.3.0**, this integration is fully standalone. It stores its own camera
credentials and communicates directly with the camera using the **native Baichuan protocol**,
so it no longer relies on the official Reolink integration at runtime.

- The official **Reolink integration is now optional**. If it is installed, setup can import
  your camera credentials with one click. If it is not installed, you can enter credentials
  manually.
- Existing users **upgrade with no reconfiguration** — see
  [Upgrading from an earlier version](#upgrading-from-an-earlier-version).

## How it works

- Audio is sent over the **native Baichuan protocol** (TCP port **9000** by default), the same
  channel the official Reolink app uses for talkback.
- The Baichuan client is a **dependency-free reimplementation** — it does **not** use
  `reolink_aio` and does **not** read the Reolink integration at runtime. This keeps Reolink Talk
  isolated from changes in the Reolink library or integration.
- Incoming audio is transcoded to the camera's required format with **ffmpeg**, encoded as
  **IMA ADPCM** (DVI-4 variant), and encrypted with **AES** (via `pycryptodome`) before transmission.

## Install (HACS — custom repository)

This integration is not in the default HACS index. Install it as a **custom repository**:

1. In Home Assistant, open **HACS**.
2. Click the **⋮ menu** (top right) → **Custom repositories**.
3. Paste this URL into the **Repository** field:
   ```
   https://github.com/kucau0901/reolink_talk
   ```
   Set **Type** to **Integration**, then click **Add**.
4. Search HACS for **Reolink Talk (Two-Way Audio)** and click **Download**.
5. **Restart Home Assistant.**
6. Go to **Settings → Devices & Services → Add Integration**, and pick **Reolink Talk (Two-Way Audio)**.

[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start?domain=reolink_talk)

> Updates will arrive automatically through HACS whenever this repo's `main` branch changes.

## Requirements

- `ffmpeg` available in your Home Assistant environment (used to decode/transcode audio before sending).
- `pycryptodome` (used for AES encryption). It is listed in the integration's manifest and is
  installed automatically by Home Assistant — no manual step needed.
- The **official Reolink integration is optional**. It is recommended only because it enables a
  one-click credential import during setup. If you don't have it, use the manual setup path below.

## Setup

Only a single instance of Reolink Talk is supported; it manages all of your talk-capable cameras.
There are two setup paths, chosen automatically based on whether the Reolink integration is present.

### Path 1 — With the Reolink integration (one-click)

If the official Reolink integration is already configured, the setup completes in one click:
Reolink Talk imports each Reolink camera's credentials into its own storage and creates the
`media_player` entities. You can later refine which cameras and which channel are used from the
integration's **Configure** (options) dialog.

### Path 2 — Manual setup (no Reolink integration required)

If the Reolink integration is not installed, you'll be shown a short form to enter the camera
details directly:

- **Host** — camera IP or hostname
- **Username** — default `admin`
- **Password**
- **Channel** — default `0`
- **Baichuan port** — default `9000`

The credentials are validated against the camera using the native Baichuan login before the entry
is saved. (Each manual setup adds one camera.)

## Usage

After setup, you will get one `media_player` per configured camera (imported from Reolink or
entered manually):

- `media_player.<something>` (shown in UI as "Reolink Talk <camera title>")

You can:

- Use the media browser to pick local files from `media/`.
- Call `media_player.play_media` from automations/scripts.
- Use the volume slider for **software volume** control (applied during audio transcoding). The
  native Baichuan client does **not** use the camera's hardware speak-volume API, so volume
  behavior is consistent across all supported models.

## Upgrading from an earlier version

If you installed v0.1 or v0.2 (which read credentials from the Reolink integration at runtime),
upgrading to v0.3.0 is seamless:

- On the first restart, your configuration is **migrated automatically** — the camera credentials
  are copied from the previously-selected Reolink entries into Reolink Talk's own storage.
- **No reconfiguration is required**, and your **entity IDs are preserved byte-for-byte**
  (the unique ID is keyed by the original Reolink entry ID and channel), so existing automations
  keep working unchanged. The migration is one-time and idempotent.
- Because Reolink Talk no longer reads the Reolink integration at runtime, you can **remove the
  Reolink integration afterward** if you only kept it for talkback. (Keep it if you use it for
  streams, motion, or other entities.)

## Compatible Cameras

This integration only works for cameras that expose Reolink **TalkAbility** with `audioType=adpcm`
via the Baichuan protocol (that is what the official Reolink app uses for talkback).

### Confirmed Working

- Reolink **Video Doorbell series** (tested on a doorbell in this Home Assistant setup)
- Reolink **RLC-811A** on firmware `v3.1.0.4695_2504301440` (tested on Home Assistant 2026.5.2; requires the rspCode 421 retry-after-stop fix shipped in this fork)

### Expected To Work (Needs Community Confirmation)

In general, models that support **Two-Way Audio** in the official Reolink app/client are good candidates, as long as they are set up as standalone devices in Home Assistant (not behind an NVR/Home Hub limitation) and expose ADPCM TalkAbility.

Reolink maintains an official list of models that support Two-Way Audio:

- [Which Reolink Cameras Support Two-Way Audio](https://support.reolink.com/hc/en-us/articles/360003764334-Which-Reolink-Cameras-Support-Two-Way-Audio/)

Important caveats from Reolink:

- If a camera is connected to an NVR, two-way audio may not be usable in some configurations. See: [Introduction to Two-Way Audio](https://support.reolink.com/hc/en-us/articles/900000600906-Introduction-to-Two-Way-Audio/).

If you test a model successfully, please open a GitHub issue/PR and add it to the “Confirmed Working” list (include your model name and whether it is PoE/WiFi/battery).

## Stability / Compatibility Notes

- Cameras are only usable for talkback if the device reports `TalkAbility` with `audioType=adpcm`.
- Firmware differences exist. This integration tries to pick `FDX` + `mixAudioStream` automatically when supported.
- If a camera is offline during startup, it may still show as available; the definitive check happens when you actually play media.

## Troubleshooting

This repo includes two debug scripts (optional):

- `scripts/reolink_talk_debug.py`: send a sine tone or a file to a specific camera using the native Baichuan client.
- `scripts/reolink_talk_e2e_capture_test.py`: capture RTSP audio while sending talk to confirm speaker output is present.

## License

MIT. See `LICENSE`.
