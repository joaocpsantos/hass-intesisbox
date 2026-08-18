# Intesis Gateway for Home Assistant

Control Intesis Air Conditioning Gateways locally via the WMP Protocol.

## Installation

### Option 1: HACS (Recommended)

1. Open HACS in your Home Assistant instance
2. Click on "Integrations"
3. Click the three dots in the top right corner
4. Select "Custom repositories"
5. Add the repository URL: `https://github.com/aroberts/hass-intesisbox`
6. Select "Integration" as the category
7. Click "Add"
8. Click "Install" on the IntesisBox card that appears
9. Restart Home Assistant
10. Go to Settings → Devices & Services
11. Click "Add Integration"
12. Search for "Intesis Gateway" and select it
13. Enter your Intesis Gateway IP address as the host

### Option 2: Manual Installation

1. Download the `intesisbox` directory from this repository
2. Copy it into your `custom_components` directory (create it if it doesn't exist)
3. Restart Home Assistant
4. Go to Settings → Devices & Services
5. Click "Add Integration"
6. Search for "Intesis Gateway" and select it
7. Enter your Intesis Gateway IP address and name it

## Configuration

**Settings → Devices & Services → Add Integration → "Intesis Gateway"**

Fan and Swing modes can be customized after setup via Gear Icon in the Intesis Gateway Integration.

### Fahrenheit Display Mode

By default the climate entity reports temperature in Celsius (the unit's native scale) and Home Assistant's Fahrenheit preference converts those values. Because indoor units only accept integer °C setpoints, round-tripping through °F produces uneven 1–2°F steps and occasional collisions (e.g. requesting 74°F lands on 73°F after the unit rounds 23.3°C to 23°C).

To fix this, enable **Display Temperature in Fahrenheit** under ⚙️ → *Options*. This makes the entity:

- Report temperature natively in Fahrenheit with 2°F steps.
- Map each °F setpoint to a single °C value using the same linear table as the Fujitsu **UTY-RNNUM** wall remote (60°F↔16°C, 68°F↔20°C, 80°F↔26°C, … up to 88°F↔30°C — one button press on the remote advances 1°C internally and 2°F on the label).
- Show the measured ambient temperature in °F with 0.1° resolution (the actual reading, not stepped).

Changing this option requires a reload of the integration (Settings → Devices & Services → Intesis Gateway → ⋮ → Reload) so the entity is re-created with the new unit.

> **Note:** The mapping is validated against the Fujitsu UTY-RNNUM remote. Other brands' wall remotes may use a different °F↔°C anchor; open an issue with a log of `SETPTEMP` values captured while stepping the remote if your unit differs.

## Troubleshooting

**Device Unavailable:**
- Verify IP address and network connectivity
- Check port 3310 is not blocked

### Debug Logging

Add the logger configuration to `configuration.yaml` and restart Home Assistant.
```yaml
  logs:
    custom_components.intesisbox: debug
```

## IntesisBox Emulator

Emulates IntesisBox WMP protocol device on TCP port 3310.

### Usage

```bash
# Run with defaults
python IntesisBoxEmulator.py

# Get help
python IntesisBoxEmulator.py --help

# Example with custom V/H vane and fan positions
python IntesisBoxEmulator.py --VUD A7S --VLR A5S --FAN A4
```

### Compact Notation Format

Pattern: `[A][X][S]`
* A = includes AUTO
* X = number of positions (1-9)
* S = includes SWING (vanes only, not for fan)
* N = disabled (LIMITS queries are ignored, no response sent)

### Allow dynamic min max temp limits per mode

--dynamic-setptemp

Enables dynamic temperature limits based on current MODE

Default: Disabled (static limits [180,300])

### Support setting and tracking time

- **CFG:DATETIME** - Get current internal date and time
  - Format: `CFG:DATETIME,DD/MM/YYYY HH:MM:SS`
  - Example response: `CFG:DATETIME,31/12/2001 19:18:31`
- **CFG:DATETIME,DD/MM/YYYY HH:MM:SS** - Set internal date and time
  - Example: `CFG:DATETIME,31/12/2025 23:59:50`
  - Response: `ACK` (or `ERR` if format invalid)

## Credits

Original: [@jnimmo](https://github.com/jnimmo)
