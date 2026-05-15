# Reolink Talk (Two-Way Audio) for Home Assistant

![Reolink Talk](docs/banner.png)

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)

Expose Reolink cameras that support **two-way audio** as `media_player` entities, so you can play:

- MP3/WAV files (local media or URLs)
- Home Assistant TTS output (anything that resolves to audio)

This integration piggybacks on the **official Reolink integration** for credentials and device selection, but it **does not depend on go2rtc, Frigate, or Docker** for talkback.

## Install (HACS)

1. Add this repository to HACS as a **custom repository** (category: Integration).
2. Install **Reolink Talk (Two-Way Audio)**.
3. Restart Home Assistant.
4. Add the integration in Settings -> Devices & Services.

[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start?domain=reolink_talk)

## Requirements

- Home Assistant with the **official Reolink integration** configured.
- `ffmpeg` available in your Home Assistant environment (used to decode/transcode audio before sending).

## Usage

After setup, you will get one `media_player` per selected Reolink config entry:

- `media_player.<something>` (shown in UI as "Reolink Talk <camera title>")

You can:

- Use the media browser to pick local files from `media/`.
- Call `media_player.play_media` from automations/scripts.
- Use the volume slider (software volume; some camera models may also support hardware speak volume).

## Compatible Cameras

This integration only works for cameras that expose Reolink **TalkAbility** with `audioType=adpcm` via the Baichuan protocol (that is what the official Reolink app uses for talkback).

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

- `scripts/reolink_talk_debug.py`: send a sine tone or a file to a specific camera using the same stored credentials as HA.
- `scripts/reolink_talk_e2e_capture_test.py`: capture RTSP audio while sending talk to confirm speaker output is present.

## License

MIT. See `LICENSE`.
