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
- **Loudness normalization (v0.3.1+):** audio is loudness-normalized (EBU R128 `loudnorm` +
  peak limiter) during transcoding so quiet TTS plays **loud and consistent** through the small
  camera speaker. The volume slider then attenuates from that loud baseline (`1.0` = full loud).
- **Automatic talk-busy recovery (v0.3.2+, fast in-place clear in v0.3.3):** if the camera
  wedges in a "talk busy" state (Baichuan status `421`/`422`) — e.g. after a talk session was
  interrupted without a clean stop — the client clears it **in software, no camera reboot**.
  As of **v0.3.3** it does this the cheap way the official Reolink app does: on a `421`/`422`
  it sends a `TALKRESET` (stop, cmd 11) **on the same connection** and retries the config
  immediately (~tens of ms). A wedged speaker therefore stays **in sync** with the other
  speakers in a `media_player` group instead of lagging seconds behind. A full reconnect +
  re-login (up to 3 cycles) remains only as a fallback for a deep wedge, and a stop is always
  sent before closing every session so talkback self-heals **without babysitting**.

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

### Talkback "talk busy" (status `421`/`422`) — and why a camera sometimes needs a reboot

If talkback suddenly stops and `TalkConfig` returns Baichuan status `421`/`422` ("talk busy"), there are **two different cases** — and they have different causes:

**1. Shallow wedge — self-healing (handled automatically since v0.3.2 / v0.3.3).**
A previous talk session didn't release the camera's talk lock cleanly (e.g. the connection dropped before a stop was sent). The component clears this *in software* — on `421`/`422` it sends a `TALKRESET` (cmd `11`) and retries on the same connection, falling back to reconnect + re-login if needed. **No action required.**

**2. Deep wedge — caused by *another client*, not by reolink_talk.**
Signature: every `TalkConfig` variant returns `421`/`422`, `TALKRESET` (cmd `11`) is itself **rejected with `400`**, and the lock **survives a full reconnect + re-login** — only a **camera reboot** clears it. This is **not** something reolink_talk's own clean operation can cause (we could only ever reproduce the shallow case from this component). It means **another client is holding the camera's single two-way-audio channel** — Reolink cameras allow only **one** talk session at a time, and the lock is tied to that other client's connection, which is why this component's own reconnect cannot release it (a reboot drops *all* connections, so it works).

**Most common culprit: go2rtc / Frigate holding the RTSP backchannel open.**
If a go2rtc stream source for the camera does **not** disable the backchannel, go2rtc negotiates and keeps a `sendonly` audio track (the talk input) open for the *entire* time it is streaming — permanently monopolizing the talk channel so reolink_talk can never acquire it.

*Diagnose:* call the go2rtc API `GET /api/streams`; if the camera's producer `medias` lists an `audio, sendonly, …` track, go2rtc is holding the backchannel.

*Fix:* add `#backchannel=0` to **every** go2rtc source for that camera (you don't need go2rtc's backchannel if you use reolink_talk for talk):

```yaml
streams:
  my_gate:
    - rtsp://USER:PASS@CAMERA_IP/Preview_01_main#backchannel=0
```

In a real deployment, a gate camera that deep-wedged roughly **once a day** stopped wedging **completely** after `#backchannel=0` was set (verified 2+ days). If your go2rtc runs embedded inside Frigate, the streams live in Frigate's config under `go2rtc:` → restart Frigate after editing.

**Other clients that can hold the same channel** (check these if it still wedges):
- The **Reolink mobile app's** push-to-talk — avoid using it on the same camera, or close the app after use.
- The **official Reolink HA integration** keeps a persistent Baichuan connection; it does not drive talk itself, but it (and any extra RTSP/ONVIF viewer) consumes the camera's limited connection slots. Disabling it if unused reduces contention.

Rule of thumb: **keep reolink_talk the only client touching the camera's two-way-audio channel.**

## License

MIT. See `LICENSE`.
