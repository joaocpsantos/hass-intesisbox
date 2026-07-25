"""Constants for the Intesis integration."""

DOMAIN = "intesisbox"

DEFAULT_NAME = "Intesis Gateway"

# Fan mode configuration keys
CONF_FAN_MODE_AUTO = "fan_mode_auto"
CONF_FAN_MODE_1 = "fan_mode_1"
CONF_FAN_MODE_2 = "fan_mode_2"
CONF_FAN_MODE_3 = "fan_mode_3"
CONF_FAN_MODE_4 = "fan_mode_4"
CONF_FAN_MODE_5 = "fan_mode_5"
CONF_FAN_MODE_6 = "fan_mode_6"
CONF_FAN_MODE_7 = "fan_mode_7"
CONF_FAN_MODE_8 = "fan_mode_8"
CONF_FAN_MODE_9 = "fan_mode_9"

# Default fan mode mapping
# Using numbers to match device values directly
DEFAULT_FAN_MODES = {
    "AUTO": "auto",
    "1": "1",
    "2": "2",
    "3": "3",
    "4": "4",
    "5": "5",
    "6": "6",
    "7": "7",
    "8": "8",
    "9": "9",
}

# Vertical vane configuration keys
CONF_VANE_VERTICAL_AUTO = "vane_vertical_auto"
CONF_VANE_VERTICAL_1 = "vane_vertical_1"
CONF_VANE_VERTICAL_2 = "vane_vertical_2"
CONF_VANE_VERTICAL_3 = "vane_vertical_3"
CONF_VANE_VERTICAL_4 = "vane_vertical_4"
CONF_VANE_VERTICAL_5 = "vane_vertical_5"
CONF_VANE_VERTICAL_6 = "vane_vertical_6"
CONF_VANE_VERTICAL_7 = "vane_vertical_7"
CONF_VANE_VERTICAL_8 = "vane_vertical_8"
CONF_VANE_VERTICAL_9 = "vane_vertical_9"
CONF_VANE_VERTICAL_SWING = "vane_vertical_swing"

# Default vertical vane mapping
DEFAULT_VANE_VERTICAL_MODES = {
    "AUTO": "auto",
    "1": "1",
    "2": "2",
    "3": "3",
    "4": "4",
    "5": "5",
    "6": "6",
    "7": "7",
    "8": "8",
    "9": "9",
    "SWING": "swing",
}

# Horizontal vane configuration keys
CONF_VANE_HORIZONTAL_AUTO = "vane_horizontal_auto"
CONF_VANE_HORIZONTAL_1 = "vane_horizontal_1"
CONF_VANE_HORIZONTAL_2 = "vane_horizontal_2"
CONF_VANE_HORIZONTAL_3 = "vane_horizontal_3"
CONF_VANE_HORIZONTAL_4 = "vane_horizontal_4"
CONF_VANE_HORIZONTAL_5 = "vane_horizontal_5"
CONF_VANE_HORIZONTAL_6 = "vane_horizontal_6"
CONF_VANE_HORIZONTAL_7 = "vane_horizontal_7"
CONF_VANE_HORIZONTAL_8 = "vane_horizontal_8"
CONF_VANE_HORIZONTAL_9 = "vane_horizontal_9"
CONF_VANE_HORIZONTAL_SWING = "vane_horizontal_swing"

# Default horizontal vane mapping
DEFAULT_VANE_HORIZONTAL_MODES = {
    "AUTO": "auto",
    "1": "1",
    "2": "2",
    "3": "3",
    "4": "4",
    "5": "5",
    "6": "6",
    "7": "7",
    "8": "8",
    "9": "9",
    "SWING": "swing",
}

# DateTime configuration keys
CONF_SYNC_TIME = "sync_time"
CONF_USE_LOCAL_TIME = "use_local_time"

# WMP Protocol configuration keys
CONF_ENABLE_PING = "enable_ping"

# Default DateTime configuration
DEFAULT_SYNC_TIME = False
DEFAULT_USE_LOCAL_TIME = True

# Default WMP Protocol configuration
DEFAULT_ENABLE_PING = False

# Fahrenheit display mode configuration
CONF_DISPLAY_FAHRENHEIT = "display_fahrenheit"
DEFAULT_DISPLAY_FAHRENHEIT = False

# Fujitsu UTY-RNNUM remote mapping: one button press = 1°C internal = 2°F label.
# Anchored at 20°C = 68°F. Built from the RNNUM behaviour, not naive arithmetic.
FAHRENHEIT_TO_CELSIUS = {
    60: 16,
    62: 17,
    64: 18,
    66: 19,
    68: 20,
    70: 21,
    72: 22,
    74: 23,
    76: 24,
    78: 25,
    80: 26,
    82: 27,
    84: 28,
    86: 29,
    88: 30,
}
CELSIUS_TO_FAHRENHEIT = {c: f for f, c in FAHRENHEIT_TO_CELSIUS.items()}
