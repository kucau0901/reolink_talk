DOMAIN = "reolink_talk"

CONF_REOLINK_ENTRY_IDS = "reolink_entry_ids"
CONF_CHANNEL = "channel"

# Standalone (v0.3.0+) — the component stores its OWN camera credentials so it no
# longer relies on the official Reolink integration at runtime.
CONF_CAMERAS = "cameras"  # entry.data[CONF_CAMERAS] = {key: {host, username, password, baichuan_port, channel, title}}
CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_BC_PORT = "baichuan_port"
CONF_TITLE = "title"

DEFAULT_CHANNEL = 0
DEFAULT_BC_PORT = 9000

