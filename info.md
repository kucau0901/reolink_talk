## Reolink Talk (Two-Way Audio)

Adds a `media_player` per configured Reolink camera so you can play MP3/WAV/TTS to the camera speaker.

**v0.3.0 is fully standalone:** it talks to the camera directly over the native Baichuan protocol (no `reolink_aio`). Works on its own with manual camera credentials, or with the official Reolink integration for one-click setup. Existing installs auto-migrate with no reconfiguration and keep their entity IDs.

**Confirmed working:** Reolink Video Doorbell series, Reolink RLC-811A (firmware `v3.1.0.4695_2504301440`).

[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start?domain=reolink_talk)
