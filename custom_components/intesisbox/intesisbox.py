"""Communication with an Intesisbox device."""

import asyncio
from collections.abc import Callable
import logging

_LOGGER = logging.getLogger(__name__)

# Connection states
API_DISCONNECTED = "Disconnected"
API_CONNECTING = "Connecting"
API_AUTHENTICATED = "Connected"

# Power states
POWER_ON = "ON"
POWER_OFF = "OFF"
POWER_STATES = [POWER_ON, POWER_OFF]

# Operation modes
MODE_AUTO = "AUTO"
MODE_DRY = "DRY"
MODE_FAN = "FAN"
MODE_COOL = "COOL"
MODE_HEAT = "HEAT"
MODES = [MODE_AUTO, MODE_DRY, MODE_FAN, MODE_COOL, MODE_HEAT]

# Function identifiers
FUNCTION_ONOFF = "ONOFF"
FUNCTION_MODE = "MODE"
FUNCTION_SETPOINT = "SETPTEMP"
FUNCTION_FANSP = "FANSP"
FUNCTION_VANEUD = "VANEUD"
FUNCTION_VANELR = "VANELR"
FUNCTION_AMBTEMP = "AMBTEMP"
FUNCTION_ERRSTATUS = "ERRSTATUS"
FUNCTION_ERRCODE = "ERRCODE"
FUNCTION_DATETIME = "DATETIME"

# Null/invalid values returned by the device
NULL_VALUES = ["-32768", "32768"]

# Timing constants (configurable)
# Strategy: Initial 1-second delay after connection to let device settle,
# then 250ms minimum spacing between all commands during normal operation
KEEPALIVE_INTERVAL = 60  # seconds between PING commands (keeps device watchdog happy)
AMBIENT_TEMP_POLL_INTERVAL = 10  # seconds between temperature requests
STATUS_POLL_INTERVAL = 300  # seconds (5 minutes) between full status polls
COMMAND_DELAY = 1  # seconds delay before first command after connection
INTER_COMMAND_DELAY = 0.25  # seconds (250ms) minimum between any commands
MODE_SET_TIMEOUT = 20  # seconds to wait for mode change confirmation
AMBTEMP_TIMEOUT = 30  # seconds without AMBTEMP response = disconnected


class IntesisBox(asyncio.Protocol):
    """Handles communication with an IntesisBox device via WMP protocol."""

    def __init__(
        self,
        ip: str,
        port: int = 3310,
        loop: asyncio.AbstractEventLoop | None = None,
        name: str | None = None,
        enable_ping: bool = False,
    ):
        """Set up base state."""
        self._ip = ip
        self._port = port
        self._name = name
        self._enable_ping = enable_ping
        self._mac: str | None = None
        self._device: dict[str, str] = {}
        self._connectionStatus = API_DISCONNECTED
        self._transport: asyncio.Transport | None = None
        self._updateCallbacks: list[Callable[[], None]] = []
        self._errorCallbacks: list[Callable[[str], None]] = []
        self._errorMessage: str | None = None
        self._model: str | None = None
        self._firmversion: str | None = None
        self._rssi: str | None = None
        self._eventLoop = loop or asyncio.get_event_loop()

        # Background task tracking
        self._keepalive_task: asyncio.Task | None = None
        self._poll_temp_task: asyncio.Task | None = None
        self._poll_status_task: asyncio.Task | None = None
        self._init_query_task: asyncio.Task | None = None
        self._last_ambtemp_time: float = 0.0
        self._last_command_time: float = 0.0

        # Device limits
        self._operation_list: list[str] = []
        self._fan_speed_list: list[str] = []
        self._vertical_vane_list: list[str] = []
        self._horizontal_vane_list: list[str] = []
        self._setpoint_minimum: float | None = None
        self._setpoint_maximum: float | None = None

        # Device datetime
        self._device_datetime: str | None = None

        # Command retry tracking
        self._command_retry_counts: dict[str, int] = {}

        # Disconnect event for clean shutdown
        self._disconnect_event: asyncio.Event = asyncio.Event()
        self._disconnect_event.set()  # Initially set (not connected)

    def connection_made(self, transport: asyncio.BaseTransport) -> None:  # type: ignore[override]
        """Asyncio callback for a successful connection."""
        _LOGGER.info(
            "%s Connection made callback triggered - transport established",
            self._log_prefix,
        )
        self._transport = transport  # type: ignore[assignment]
        _LOGGER.debug(
            "%s Transport stored, scheduling initial state query", self._log_prefix
        )

        # Clear disconnect event (connection is now active)
        self._disconnect_event.clear()

        # Clear any pending retry counts since connection is now established
        self._command_retry_counts.clear()

        # Initialize connection health tracking (AMBTEMP only)
        self._last_ambtemp_time = asyncio.get_event_loop().time()
        self._last_command_time = 0.0

        # Schedule initial query - track the task so we can cancel it if needed
        self._init_query_task = self._eventLoop.create_task(self._query_initial_state())

    async def _query_initial_state(self) -> None:
        """Fetch configuration from the device upon connection."""
        _LOGGER.debug(
            "%s Starting initial state query, transport available: %s",
            self._log_prefix,
            self._transport is not None,
        )

        cmds = [
            "ID",
            "LIMITS:SETPTEMP",
            "LIMITS:FANSP",
            "LIMITS:MODE",
            "LIMITS:VANEUD",
            "LIMITS:VANELR",
        ]
        for i, cmd in enumerate(cmds, 1):
            if not self._transport:
                _LOGGER.warning(
                    "%s Transport lost during initial query at command: %s",
                    self._log_prefix,
                    cmd,
                )
                break
            _LOGGER.info(
                "%s Sending initialization command %d/%d: %s",
                self._log_prefix,
                i,
                len(cmds),
                cmd,
            )
            await self._write_async(cmd, delay=COMMAND_DELAY)

    def _write(self, cmd: str) -> None:
        """Send a command to the device."""
        if not self._transport:
            _LOGGER.error(
                "%s Cannot send command, transport not available: %s",
                self._log_prefix,
                cmd,
            )
            return

        if self._transport.is_closing():
            _LOGGER.warning(
                "%s Cannot send command, transport is closing: %s",
                self._log_prefix,
                cmd,
            )
            return

        try:
            self._transport.write(f"{cmd}\r".encode("ascii"))
            self._last_command_time = asyncio.get_event_loop().time()
            _LOGGER.debug("%s Data sent: %r", self._log_prefix, cmd)
        except Exception as err:
            _LOGGER.error(
                "%s Failed to send command %s: %s", self._log_prefix, cmd, err
            )

    async def _write_async(self, cmd: str, delay: float = COMMAND_DELAY) -> None:
        """Send a command to the device with rate limiting."""
        # Enforce minimum interval between commands
        now = asyncio.get_event_loop().time()
        time_since_last = now - self._last_command_time
        if self._last_command_time > 0 and time_since_last < INTER_COMMAND_DELAY:
            wait_time = INTER_COMMAND_DELAY - time_since_last
            _LOGGER.debug(
                "%s Waiting %.3fs before command (rate limiting)",
                self._log_prefix,
                wait_time,
            )
            await asyncio.sleep(wait_time)

        self._write(cmd)
        await asyncio.sleep(delay)

    def data_received(self, data: bytes) -> None:
        """Asyncio callback when data is received on the socket."""
        try:
            lines_received = data.decode("ascii").splitlines()
        except UnicodeDecodeError as err:
            _LOGGER.error(
                "%s Failed to decode received data: %s", self._log_prefix, err
            )
            return

        status_changed = False

        for line in lines_received:
            _LOGGER.debug("%s Data received: %r", self._log_prefix, line)

            cmd_list = line.split(":", 1)
            if len(cmd_list) < 2:
                _LOGGER.debug(
                    "%s Ignoring line without colon separator: %s",
                    self._log_prefix,
                    line,
                )
                continue

            cmd = cmd_list[0]
            args = cmd_list[1]

            if cmd == "ID":
                self._parse_id_received(args)
                self._connectionStatus = API_AUTHENTICATED
                self._start_background_tasks()
                # Notify HA that connection is restored so entity becomes available
                self._send_update_callback()
            elif cmd == "PONG":
                # Just log PONG for debugging, not used for connection health
                _LOGGER.debug("%s PONG received, RSSI: %s", self._log_prefix, args)
            elif cmd == "CHN,1":
                self._parse_change_received(args)
                status_changed = True
            elif cmd == "LIMITS":
                self._parse_limits_received(args)
                status_changed = True
            elif cmd == "CFG":
                self._parse_cfg_received(args)

        if status_changed:
            self._send_update_callback()

    def _parse_id_received(self, args: str) -> None:
        """Parse ID response: Model,MAC,IP,Protocol,Version,RSSI."""
        info = args.split(",")
        if len(info) >= 6:
            self._model = info[0]
            self._mac = info[1]
            self._firmversion = info[4]
            self._rssi = info[5]

            _LOGGER.debug(
                "%s Updated info: model=%s mac=%s version=%s rssi=%s",
                self._log_prefix,
                self._model,
                self._mac,
                self._firmversion,
                self._rssi,
            )

    def _parse_change_received(self, args: str) -> None:
        """Parse status change notification."""
        parts = args.split(",", 1)
        if len(parts) != 2:
            _LOGGER.warning(
                "%s Invalid change notification format: %s", self._log_prefix, args
            )
            return

        function = parts[0]
        value: str | None = parts[1]

        if value in NULL_VALUES:
            value = None

        # Track AMBTEMP responses for connection health monitoring
        if function == FUNCTION_AMBTEMP:
            self._last_ambtemp_time = asyncio.get_event_loop().time()

        # Check if MODE is changing (after initial setup)
        # Some devices have different temperature limits for different modes
        if function == FUNCTION_MODE and FUNCTION_MODE in self._device:
            old_mode = self._device.get(FUNCTION_MODE)
            if old_mode != value and value is not None:
                _LOGGER.info(
                    "%s Mode changed from %s to %s, re-querying temperature limits",
                    self._log_prefix,
                    old_mode,
                    value,
                )
                # Query temperature limits after mode change
                # Some devices have different min/max temps for different modes
                self._schedule_task(
                    self._write_async("LIMITS:SETPTEMP", delay=COMMAND_DELAY)
                )

        self._device[function] = value  # type: ignore[assignment]
        _LOGGER.debug("%s Updated state: %r", self._log_prefix, self._device)

    def _parse_limits_received(self, args: str) -> None:
        """Parse device capability limits."""
        split_args = args.split(",", 1)

        if len(split_args) != 2:
            _LOGGER.warning("%s Invalid limits format: %s", self._log_prefix, args)
            return

        function = split_args[0]
        # Remove brackets from values list
        values_str = split_args[1]
        if values_str.startswith("[") and values_str.endswith("]"):
            values_str = values_str[1:-1]
        values = values_str.split(",")

        _LOGGER.info(
            "%s Received LIMITS for %s: %s", self._log_prefix, function, values
        )

        if function == FUNCTION_SETPOINT and len(values) == 2:
            try:
                self._setpoint_minimum = int(values[0]) / 10
                self._setpoint_maximum = int(values[1]) / 10
            except ValueError as err:
                _LOGGER.error(
                    "%s Failed to parse setpoint limits: %s", self._log_prefix, err
                )
        elif function == FUNCTION_FANSP:
            self._fan_speed_list = values
        elif function == FUNCTION_MODE:
            self._operation_list = values
        elif function == FUNCTION_VANEUD:
            self._vertical_vane_list = values
        elif function == FUNCTION_VANELR:
            self._horizontal_vane_list = values

    def _parse_cfg_received(self, args: str) -> None:
        """Parse CFG responses (currently only DATETIME)."""
        split_args = args.split(",", 1)

        if len(split_args) < 1:
            _LOGGER.warning("%s Invalid CFG format: %s", self._log_prefix, args)
            return

        function = split_args[0]

        if function == FUNCTION_DATETIME:
            if len(split_args) == 2:
                # CFG:DATETIME,DD/MM/YYYY HH:MM:SS response
                datetime_value = split_args[1]
                self._device_datetime = datetime_value
                _LOGGER.info("%s Device datetime: %s", self._log_prefix, datetime_value)
            else:
                _LOGGER.warning(
                    "%s CFG:DATETIME response missing datetime value", self._log_prefix
                )
            _LOGGER.info(
                "%s Horizontal vane list populated: %s",
                self._log_prefix,
                self._horizontal_vane_list,
            )

        _LOGGER.debug(
            "%s Updated limits: setpoint_min=%s setpoint_max=%s fan_speeds=%s "
            "operations=%s vane_vertical=%s vane_horizontal=%s",
            self._log_prefix,
            self._setpoint_minimum,
            self._setpoint_maximum,
            self._fan_speed_list,
            self._operation_list,
            self._vertical_vane_list,
            self._horizontal_vane_list,
        )

    def _start_background_tasks(self) -> None:
        """Start background polling tasks."""
        if self._enable_ping:
            if not self._keepalive_task or self._keepalive_task.done():
                self._keepalive_task = asyncio.run_coroutine_threadsafe(  # type: ignore[assignment]
                    self._keep_alive(), self._eventLoop
                )
        if not self._poll_temp_task or self._poll_temp_task.done():
            self._poll_temp_task = asyncio.run_coroutine_threadsafe(  # type: ignore[assignment]
                self._poll_ambtemp(), self._eventLoop
            )
        if not self._poll_status_task or self._poll_status_task.done():
            self._poll_status_task = asyncio.run_coroutine_threadsafe(  # type: ignore[assignment]
                self._poll_status(), self._eventLoop
            )

    def _cancel_background_tasks(self) -> None:
        """Cancel all background tasks."""
        for task in [
            self._keepalive_task,
            self._poll_temp_task,
            self._poll_status_task,
            self._init_query_task,
        ]:
            if task and not task.done():
                task.cancel()

    async def _keep_alive(self) -> None:
        """Send periodic keepalive commands to reset device watchdog timer."""
        try:
            while self.is_connected:
                _LOGGER.debug("%s Sending keepalive", self._log_prefix)
                await self._write_async("PING", delay=0)
                await asyncio.sleep(KEEPALIVE_INTERVAL)
        except asyncio.CancelledError:
            _LOGGER.debug("%s Keepalive task cancelled", self._log_prefix)
        except Exception as err:
            _LOGGER.error("%s Keepalive task error: %s", self._log_prefix, err)

    async def _poll_ambtemp(self) -> None:
        """Periodically request ambient temperature updates."""
        try:
            while self.is_connected:
                _LOGGER.debug("%s Requesting ambient temperature", self._log_prefix)
                await self._write_async("GET,1:AMBTEMP", delay=0)
                await asyncio.sleep(AMBIENT_TEMP_POLL_INTERVAL)

                # Check if we've received AMBTEMP response recently
                if self.is_connected and self._last_ambtemp_time > 0:
                    now = asyncio.get_event_loop().time()
                    time_since_ambtemp = now - self._last_ambtemp_time

                    if time_since_ambtemp > AMBTEMP_TIMEOUT:
                        _LOGGER.error(
                            "%s No AMBTEMP response for %.1fs (timeout: %ds), closing connection",
                            self._log_prefix,
                            time_since_ambtemp,
                            AMBTEMP_TIMEOUT,
                        )
                        if self._transport:
                            self._transport.close()
                        return
        except asyncio.CancelledError:
            _LOGGER.debug("%s Ambient temp polling task cancelled", self._log_prefix)
        except Exception as err:
            _LOGGER.error("%s Ambient temp polling error: %s", self._log_prefix, err)

    async def _poll_status(self) -> None:
        """Periodically poll for all status updates."""
        try:
            while self.is_connected:
                _LOGGER.debug("%s Polling for status update", self._log_prefix)
                await self._write_async("GET,1:*", delay=0)
                await asyncio.sleep(STATUS_POLL_INTERVAL)
        except asyncio.CancelledError:
            _LOGGER.debug("%s Status polling task cancelled", self._log_prefix)
        except Exception as err:
            _LOGGER.error("%s Status polling error: %s", self._log_prefix, err)

    def connection_lost(self, exc: Exception | None) -> None:
        """Asyncio callback for a lost TCP connection."""
        self._connectionStatus = API_DISCONNECTED
        self._cancel_background_tasks()

        # Signal that connection is now fully closed
        self._disconnect_event.set()

        if exc:
            _LOGGER.warning("%s Connection lost with error: %s", self._log_prefix, exc)
        else:
            _LOGGER.info("%s Connection closed by server", self._log_prefix)

        self._send_update_callback()

    def connect(self) -> None:
        """Connect to the IntesisBox device."""
        if self._connectionStatus != API_DISCONNECTED:
            _LOGGER.debug(
                "%s connect() called but already connecting/connected (status: %s)",
                self._log_prefix,
                self._connectionStatus,
            )
            return

        self._connectionStatus = API_CONNECTING

        if not self._ip or not self._port:
            _LOGGER.error("%s Missing IP address or port", self._log_prefix)
            self._connectionStatus = API_DISCONNECTED
            return

        _LOGGER.info(
            "%s Initiating connection to IntesisBox at %s:%s",
            self._log_prefix,
            self._ip,
            self._port,
        )

        try:
            # Create the connection coroutine
            coro = self._eventLoop.create_connection(lambda: self, self._ip, self._port)

            # Schedule it on the event loop
            _ = asyncio.run_coroutine_threadsafe(coro, self._eventLoop)
            _LOGGER.debug("%s Connection coroutine scheduled", self._log_prefix)

        except Exception as err:
            _LOGGER.error(
                "%s Failed to schedule connection: %s",
                self._log_prefix,
                err,
                exc_info=True,
            )
            self._connectionStatus = API_DISCONNECTED

    def disconnect(self) -> None:
        """Force disconnect and reset connection state."""
        _LOGGER.debug(
            "%s Forcing disconnect, current status: %s",
            self._log_prefix,
            self._connectionStatus,
        )
        self._connectionStatus = API_DISCONNECTED
        self._cancel_background_tasks()

        if self._transport:
            self._transport.close()
            self._transport = None

    def stop(self) -> None:
        """Shutdown the connection and cleanup."""
        _LOGGER.info("%s Stopping IntesisBox connection", self._log_prefix)
        self._connectionStatus = API_DISCONNECTED
        self._cancel_background_tasks()

        if self._transport:
            try:
                self._transport.close()
            except Exception as err:
                _LOGGER.error("%s Error closing transport: %s", self._log_prefix, err)
            finally:
                self._transport = None

    async def wait_for_disconnect(self, timeout: float = 5.0) -> bool:
        """Wait for connection to fully close.

        Args:
            timeout: Maximum time to wait in seconds

        Returns:
            True if disconnect completed, False if timeout

        """
        try:
            await asyncio.wait_for(self._disconnect_event.wait(), timeout=timeout)
            _LOGGER.debug("%s Disconnect completed", self._log_prefix)
            return True
        except TimeoutError:
            _LOGGER.warning(
                "%s Disconnect did not complete within %ss timeout",
                self._log_prefix,
                timeout,
            )
            return False

    def _schedule_task(self, coro):
        """Schedule a coroutine on the event loop (thread-safe)."""
        return asyncio.run_coroutine_threadsafe(coro, self._eventLoop)

    def set_temperature(self, setpoint: float) -> None:
        """Set the target temperature."""
        set_temp = int(setpoint * 10)
        self._schedule_task(self._set_value_async(FUNCTION_SETPOINT, set_temp))

    def set_fan_speed(self, fan_speed: str) -> None:
        """Set the fan speed."""
        self._schedule_task(self._set_value_async(FUNCTION_FANSP, fan_speed))

    def set_vertical_vane(self, vane: str) -> None:
        """Set the vertical vane position."""
        self._schedule_task(self._set_value_async(FUNCTION_VANEUD, vane))

    def set_horizontal_vane(self, vane: str) -> None:
        """Set the horizontal vane position."""
        self._schedule_task(self._set_value_async(FUNCTION_VANELR, vane))

    async def _set_value_async(self, uid: str, value: str | int) -> None:
        """Send a SET command to the device asynchronously."""
        try:
            await self._write_async(f"SET,1:{uid},{value}", delay=0)
        except Exception as err:
            _LOGGER.error(
                "%s Failed to set %s to %s: %s", self._log_prefix, uid, value, err
            )
            raise

    async def set_mode_async(self, mode: str) -> None:
        """Set the operation mode with proper sequencing."""
        if mode not in MODES:
            _LOGGER.error("%s Invalid mode: %s", self._log_prefix, mode)
            return

        _LOGGER.debug("%s Setting MODE to %s", self._log_prefix, mode)

        # Send mode command
        await self._set_value_async(FUNCTION_MODE, mode)

        # If device is off, wait for mode to be confirmed before turning on
        # This is critical because device responds with CHN,1:ONOFF before CHN,1:MODE
        # If we power on before mode changes, device will turn on in the OLD mode
        if not self.is_on:
            _LOGGER.debug(
                "%s Device is off, waiting for mode confirmation before power on",
                self._log_prefix,
            )

            # Wait for mode to be set (with timeout)
            for _retry in range(MODE_SET_TIMEOUT):
                # Wait FIRST, then check (give device time to process SET and send CHN)
                await asyncio.sleep(1)

                if self.mode == mode:
                    _LOGGER.debug(
                        "%s Mode confirmed as %s, powering on", self._log_prefix, mode
                    )
                    await self._set_value_async(FUNCTION_ONOFF, POWER_ON)
                    return

            # Timeout reached - mode never changed
            _LOGGER.error(
                "%s Timeout waiting for mode to change to %s (current: %s), not powering on",
                self._log_prefix,
                mode,
                self.mode,
            )

    def set_mode(self, mode: str) -> None:
        """Set the operation mode (non-blocking wrapper)."""
        self._schedule_task(self.set_mode_async(mode))

    def set_power_off(self) -> None:
        """Turn off the device."""
        self._schedule_task(self._set_value_async(FUNCTION_ONOFF, POWER_OFF))

    def set_power_on(self) -> None:
        """Turn on the device."""
        self._schedule_task(self._set_value_async(FUNCTION_ONOFF, POWER_ON))

    def query_datetime(self) -> None:
        """Query the device's current datetime (non-blocking)."""
        self._schedule_task(
            self._write_async("CFG:DATETIME", delay=INTER_COMMAND_DELAY)
        )

    async def set_datetime_async(self, datetime_str: str) -> None:
        """Set the device's datetime.

        Args:
            datetime_str: DateTime in format "DD/MM/YYYY HH:MM:SS"

        """
        _LOGGER.debug(
            "%s Setting device datetime to: %s", self._log_prefix, datetime_str
        )
        await self._write_async(
            f"CFG:DATETIME,{datetime_str}", delay=INTER_COMMAND_DELAY
        )

    # Properties
    @property
    def _log_prefix(self) -> str:
        """Return formatted log prefix with name and entity_id."""
        # Use entity_id format (climate.deviceid) once we have the MAC
        if self._mac:
            entity_id = f"climate.{self._mac.lower()}"
            if self._name:
                return f"[{self._name}({entity_id})]"
            return f"[{entity_id}]"
        # Fall back to IP during initial connection before ID is received
        return f"[{self._ip}]"

    @property
    def operation_list(self) -> list[str]:
        """List of supported operation modes."""
        return self._operation_list

    @property
    def vane_horizontal_list(self) -> list[str]:
        """List of supported horizontal vane settings."""
        return self._horizontal_vane_list

    @property
    def vane_vertical_list(self) -> list[str]:
        """List of supported vertical vane settings."""
        return self._vertical_vane_list

    @property
    def mode(self) -> str | None:
        """Current operation mode."""
        return self._device.get(FUNCTION_MODE)

    @property
    def fan_speed(self) -> str | None:
        """Current fan speed."""
        return self._device.get(FUNCTION_FANSP)

    @property
    def fan_speed_list(self) -> list[str]:
        """List of supported fan speeds."""
        return self._fan_speed_list

    @property
    def device_mac_address(self) -> str | None:
        """MAC address of the IntesisBox."""
        return self._mac

    @property
    def device_model(self) -> str | None:
        """Model of the IntesisBox."""
        return self._model

    @property
    def firmware_version(self) -> str | None:
        """Firmware version of the IntesisBox."""
        return self._firmversion

    @property
    def device_datetime(self) -> str | None:
        """Device datetime (format: DD/MM/YYYY HH:MM:SS)."""
        return self._device_datetime

    @property
    def is_on(self) -> bool:
        """Return True if the controlled device is turned on."""
        return self._device.get(FUNCTION_ONOFF) == POWER_ON

    @property
    def has_swing_control(self) -> bool:
        """Return True if the device supports swing modes."""
        return len(self._horizontal_vane_list) > 1 or len(self._vertical_vane_list) > 1

    @property
    def setpoint(self) -> float | None:
        """Current target temperature."""
        setpoint = self._device.get(FUNCTION_SETPOINT)
        if setpoint:
            try:
                return int(setpoint) / 10
            except (ValueError, TypeError):
                _LOGGER.error(
                    "%s Invalid setpoint value: %s", self._log_prefix, setpoint
                )
        return None

    @property
    def ambient_temperature(self) -> float | None:
        """Current ambient temperature."""
        temperature = self._device.get(FUNCTION_AMBTEMP)
        if temperature:
            try:
                return int(temperature) / 10
            except (ValueError, TypeError):
                _LOGGER.error(
                    "%s Invalid temperature value: %s", self._log_prefix, temperature
                )
        return None

    @property
    def max_setpoint(self) -> float | None:
        """Maximum target temperature."""
        return self._setpoint_maximum

    @property
    def min_setpoint(self) -> float | None:
        """Minimum target temperature."""
        return self._setpoint_minimum

    @property
    def rssi(self) -> str | None:
        """Current wireless signal strength."""
        return self._rssi

    def vertical_swing(self) -> str | None:
        """Return current vertical vane setting."""
        return self._device.get(FUNCTION_VANEUD)

    def horizontal_swing(self) -> str | None:
        """Return current horizontal vane setting."""
        return self._device.get(FUNCTION_VANELR)

    @property
    def is_connected(self) -> bool:
        """Return True if the TCP connection is established and authenticated."""
        return self._connectionStatus == API_AUTHENTICATED

    @property
    def is_disconnected(self) -> bool:
        """Return True when the TCP connection is disconnected and idle."""
        return self._connectionStatus == API_DISCONNECTED

    @property
    def error_message(self) -> str | None:
        """Return the last error message, or None if there were no errors."""
        return self._errorMessage

    def add_update_callback(self, method: Callable[[], None]) -> None:
        """Add a callback to be called when device status updates."""
        self._updateCallbacks.append(method)

    def add_error_callback(self, method: Callable[[str], None]) -> None:
        """Add a callback to be called when errors occur."""
        self._errorCallbacks.append(method)

    def _send_update_callback(self) -> None:
        """Notify all update callback subscribers."""
        if not self._updateCallbacks:
            _LOGGER.debug("%s No update callbacks registered", self._log_prefix)

        for callback in self._updateCallbacks:
            try:
                callback()
            except Exception as err:
                _LOGGER.error("%s Error in update callback: %s", self._log_prefix, err)

    def _send_error_callback(self, message: str) -> None:
        """Notify all error callback subscribers."""
        self._errorMessage = message

        if not self._errorCallbacks:
            _LOGGER.debug("%s No error callbacks registered", self._log_prefix)

        for callback in self._errorCallbacks:
            try:
                callback(message)
            except Exception as err:
                _LOGGER.error("%s Error in error callback: %s", self._log_prefix, err)
