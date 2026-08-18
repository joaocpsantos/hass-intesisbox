"""Communication with an Intesisbox device."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
import time

_LOGGER = logging.getLogger(__name__)

API_DISCONNECTED = "Disconnected"
API_CONNECTING = "Connecting"
API_AUTHENTICATED = "Connected"

POWER_ON = "ON"
POWER_OFF = "OFF"
POWER_STATES = [POWER_ON, POWER_OFF]

MODE_AUTO = "AUTO"
MODE_DRY = "DRY"
MODE_FAN = "FAN"
MODE_COOL = "COOL"
MODE_HEAT = "HEAT"
MODES = [MODE_AUTO, MODE_DRY, MODE_FAN, MODE_COOL, MODE_HEAT]

FUNCTION_ONOFF = "ONOFF"
FUNCTION_MODE = "MODE"
FUNCTION_SETPOINT = "SETPTEMP"
FUNCTION_FANSP = "FANSP"
FUNCTION_VANEUD = "VANEUD"
FUNCTION_VANELR = "VANELR"
FUNCTION_AMBTEMP = "AMBTEMP"
FUNCTION_ERRSTATUS = "ERRSTATUS"
FUNCTION_ERRCODE = "ERRCODE"

NULL_VALUES = ["-32768", "32768"]

# The device closes the socket after 1 minute without traffic, so ping well
# inside that window (WMP Protocol Specification, section "Considerations
# before integrating WMP protocol").
KEEPALIVE_INTERVAL = 30
# How often to check that the device is still answering us.
WATCHDOG_INTERVAL = 10
# A device that has not answered a single poll or ping for this long is gone,
# even though the socket still looks open from our side.
RESPONSE_TIMEOUT = 45
# How long to wait for a device to acknowledge a command, and how many times
# to send it before giving up and rebuilding the connection.
SET_CONFIRM_TIMEOUT = 3
SET_ATTEMPTS = 2
# The spec requires at least 1 second between closing and reopening a socket.
RECONNECT_DELAY = 5
MAX_RECONNECT_DELAY = 300

background_tasks = set()


def clean_background_task(task):
    """Handle background task completion."""
    background_tasks.discard(task)
    if task.cancelled():
        _LOGGER.debug("Background task was cancelled")
        return
    exc = task.exception()
    if exc is not None:
        _LOGGER.error("Background task failed: %r", exc, exc_info=exc)


def ensure_background_task(coro, loop):
    """Ensure background task is running."""
    task = asyncio.ensure_future(coro, loop=loop)
    background_tasks.add(task)
    task.add_done_callback(clean_background_task)
    return task


class IntesisBox(asyncio.Protocol):
    """Handles communication with an intesisbox device via WMP."""

    def __init__(self, ip: str, port: int = 3310, loop=None):
        """Set up base state."""
        self._ip = ip
        self._port = port
        self._mac = None
        self._device: dict[str, str] = {}
        self._connectionStatus = API_DISCONNECTED
        self._transport: asyncio.BaseTransport | None = None
        self._updateCallbacks: list[Callable[[], None]] = []
        self._errorCallbacks: list[Callable[[str], None]] = []
        self._errorMessage: str | None = None
        self._controllerType = None
        self._model: str | None = None
        self._firmversion: str | None = None
        self._rssi: int | None = None
        self._eventLoop = loop

        # Traffic tracking, so a socket the device stopped answering on can be
        # told apart from an idle one.
        self._last_rx: float | None = None
        self._last_tx: float | None = None
        self._pending_sets: dict[str, tuple[str, float]] = {}

        # Reconnection handling
        self._stopped = False
        self._reconnecting = False
        self._reconnect_delay = RECONNECT_DELAY
        # Bumped for every socket, so the polling loops of a replaced
        # connection stop instead of waking up alongside the new one.
        self._session = 0

        # Limits
        self._operation_list: list[str] = []
        self._fan_speed_list: list[str] = []
        self._vertical_vane_list: list[str] = []
        self._horizontal_vane_list: list[str] = []
        self._setpoint_minimum: int | None = None
        self._setpoint_maximum: int | None = None

    def connection_made(self, transport: asyncio.BaseTransport):
        """Asyncio callback for a successful connection."""
        _LOGGER.debug("Connected to IntesisBox")
        self._transport = transport
        self._session += 1
        ensure_background_task(self.query_initial_state(self._session), self._eventLoop)

    def _session_active(self, session: int) -> bool:
        """Whether the connection a background loop was started for is still live."""
        return self.is_connected and session == self._session

    async def keep_alive(self, session: int):
        """Send a keepalive command to reset it's watchdog timer."""
        while self._session_active(session):
            _LOGGER.debug("Sending keepalive")
            self._write("PING")
            await asyncio.sleep(KEEPALIVE_INTERVAL)
        else:
            _LOGGER.debug("Not connected, skipping keepalive")

    async def watchdog(self, session: int):
        """Drop a socket the device has stopped answering on.

        A WMP gateway that goes away without closing the TCP connection leaves
        us with a socket that still accepts writes, so commands are silently
        lost. Nothing but the absence of replies gives it away.
        """
        while self._session_active(session):
            await asyncio.sleep(WATCHDOG_INTERVAL)
            if not self._session_active(session):
                break
            silence = self._silence()
            if silence is not None and silence > RESPONSE_TIMEOUT:
                _LOGGER.warning(
                    "%s: no reply for %.0fs although the socket is still open, "
                    "dropping the connection and reconnecting",
                    self._ip,
                    silence,
                )
                self._force_reconnect()
                break

    async def poll_ambtemp(self, session: int):
        """Retrieve Ambient Temperature to prevent integration timeouts."""
        while self._session_active(session):
            _LOGGER.debug("Sending AMBTEMP")
            self._write("GET,1:AMBTEMP")
            await asyncio.sleep(10)
        else:
            _LOGGER.debug("Not connected, skipping Ambient Temp Request")

    async def query_initial_state(self, session: int):
        """Fetch configuration from the device upon connection."""
        cmds = [
            "ID",
            "LIMITS:SETPTEMP",
            "LIMITS:FANSP",
            "LIMITS:MODE",
            "LIMITS:VANEUD",
            "LIMITS:VANELR",
        ]
        for cmd in cmds:
            if session != self._session or self._transport is None:
                # This socket has been replaced or closed; its setup is moot.
                return
            self._write(cmd)
            await asyncio.sleep(1)

    def _silence(self) -> float | None:
        """Seconds since the last byte was received, or None if nothing ever was."""
        if self._last_rx is None:
            return None
        return time.monotonic() - self._last_rx

    def _silence_text(self) -> str:
        silence = self._silence()
        return f"{silence:.0f}s ago" if silence is not None else "never"

    def _can_write(self, cmd) -> bool:
        """Report, loudly, when a command cannot reach the device."""
        if self._transport is None:
            _LOGGER.warning(
                "%s: dropping %r, no transport (status=%s)",
                self._ip,
                cmd,
                self._connectionStatus,
            )
            return False
        if self._transport.is_closing():
            _LOGGER.warning(
                "%s: dropping %r, transport is closing (status=%s)",
                self._ip,
                cmd,
                self._connectionStatus,
            )
            return False
        return True

    def _submit(self, coro, timeout: float | None = None):
        """Run a coroutine on the device's event loop, from any thread.

        Entity methods are called in an executor thread, and a transport may
        only be written to from the thread its loop runs in.
        """
        if self._eventLoop is None:
            _LOGGER.error("%s: no event loop to run %r on", self._ip, coro)
            coro.close()
            return None

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is self._eventLoop:
            ensure_background_task(coro, self._eventLoop)
            return None

        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._eventLoop)
        except RuntimeError:
            _LOGGER.warning("%s: event loop is gone, dropping %r", self._ip, coro)
            coro.close()
            return None

        if timeout is None:
            return None
        try:
            return future.result(timeout)
        except TimeoutError:
            _LOGGER.warning("%s: timed out waiting for %r", self._ip, coro)
        except Exception:
            _LOGGER.exception("%s: command failed", self._ip)
        return None

    def _write(self, cmd) -> bool:
        if not self._can_write(cmd):
            return False
        self._transport.write(f"{cmd}\r".encode("ascii"))
        self._last_tx = time.monotonic()
        _LOGGER.debug(f"Data sent: {cmd!r}")
        return True

    async def _writeasync(self, cmd) -> bool:
        """Async write to slow down commands and await response from units."""
        written = self._write(cmd)
        await asyncio.sleep(1)
        return written

    def data_received(self, data):
        """Asyncio callback when data is received on the socket."""
        linesReceived = data.decode("ascii").splitlines()
        statusChanged = False
        self._last_rx = time.monotonic()

        for line in linesReceived:
            _LOGGER.debug(f"Data received: {line!r}")
            cmdList = line.split(":", 1)
            cmd = cmdList[0]
            args = None
            if len(cmdList) <= 1:
                # ACK / ERR / PONG carry no colon and used to be discarded here
                # without a trace.
                bare = cmd.strip()
                if bare == "ACK":
                    # A SET that does not change the value is answered with a
                    # bare ACK and no CHN, so this is the only confirmation
                    # some commands ever get.
                    self._confirm_oldest_set()
                elif bare == "ERR":
                    _LOGGER.warning(
                        "%s: device answered ERR, it rejected the last command",
                        self._ip,
                    )
                    self._pending_sets.clear()
                elif bare:
                    _LOGGER.debug("%s: device answered %r", self._ip, bare)
            if len(cmdList) > 1:
                args = cmdList[1]
                if cmd == "ID":
                    self._parse_id_received(args)
                    self._connectionStatus = API_AUTHENTICATED
                    self._reconnect_delay = RECONNECT_DELAY
                    session = self._session
                    ensure_background_task(self.poll_status(session), self._eventLoop)
                    ensure_background_task(self.poll_ambtemp(session), self._eventLoop)
                    ensure_background_task(self.keep_alive(session), self._eventLoop)
                    ensure_background_task(self.watchdog(session), self._eventLoop)
                elif cmd == "CHN,1":
                    self._parse_change_received(args)
                    statusChanged = True
                elif cmd == "LIMITS":
                    self._parse_limits_received(args)
                    statusChanged = True

        if statusChanged:
            self._send_update_callback()

    def _confirm_oldest_set(self):
        """Mark the longest outstanding command as acknowledged.

        WMP answers on a single socket in order, so a bare ACK belongs to the
        command that has been waiting the longest.
        """
        if not self._pending_sets:
            return
        uid = min(self._pending_sets, key=lambda key: self._pending_sets[key][1])
        value, sent_at = self._pending_sets.pop(uid)
        _LOGGER.debug(
            "%s: device acknowledged %s=%s after %.2fs",
            self._ip,
            uid,
            value,
            time.monotonic() - sent_at,
        )

    def _parse_id_received(self, args):
        # ID:Model,MAC,IP,Protocol,Version,RSSI
        info = args.split(",")
        if len(info) >= 6:
            self._model = info[0]
            self._mac = info[1]
            self._firmversion = info[4]
            self._rssi = info[5]

            _LOGGER.debug(
                "Updated info: model:%s mac:%s version:%s rssi:%s",
                self._model,
                self._mac,
                self._firmversion,
                self._rssi,
            )

    def _parse_change_received(self, args):
        function = args.split(",")[0]
        value = args.split(",")[1]
        if value in NULL_VALUES:
            value = None
        self._device[function] = value

        pending = self._pending_sets.pop(function, None)
        if pending is not None:
            _LOGGER.debug(
                "%s: device confirmed %s=%s, %.2fs after the command was sent",
                self._ip,
                function,
                value,
                time.monotonic() - pending[1],
            )

        _LOGGER.debug(f"Updated state: {self._device!r}")

    def _parse_limits_received(self, args):
        split_args = args.split(",", 1)

        if len(split_args) == 2:
            function = split_args[0]
            values = split_args[1][1:-1].split(",")

            if function == FUNCTION_SETPOINT and len(values) == 2:
                self._setpoint_minimum = int(values[0]) / 10
                self._setpoint_maximum = int(values[1]) / 10
            elif function == FUNCTION_FANSP:
                self._fan_speed_list = values
            elif function == FUNCTION_MODE:
                self._operation_list = values
            elif function == FUNCTION_VANEUD:
                self._vertical_vane_list = values
            elif function == FUNCTION_VANELR:
                self._horizontal_vane_list = values

            _LOGGER.debug(
                f"Updated limits: {self._setpoint_minimum=} "
                f"{self._setpoint_maximum=} {self._fan_speed_list=} "
                f"{self._operation_list=} {self._vertical_vane_list=} "
                f"{self._horizontal_vane_list=}"
            )
        return

    def connection_lost(self, exc):
        """Asyncio callback for a lost TCP connection."""
        self._connectionStatus = API_DISCONNECTED
        self._transport = None
        self._pending_sets.clear()
        _LOGGER.warning(
            "%s: connection lost (exc=%r, last data received %s)",
            self._ip,
            exc,
            self._silence_text(),
        )
        self._send_update_callback()
        self._schedule_reconnect()

    def _force_reconnect(self):
        """Tear down a socket the device is no longer answering on."""
        transport = self._transport
        self._connectionStatus = API_DISCONNECTED
        self._transport = None
        self._pending_sets.clear()
        if transport is not None:
            # abort() rather than close(): there is nothing left to flush to a
            # device that stopped talking to us.
            transport.abort()
        self._send_update_callback()
        self._schedule_reconnect()

    def _schedule_reconnect(self):
        """Queue a reconnection attempt, backing off while the device is away."""
        if self._stopped or self._reconnecting:
            return
        self._reconnecting = True
        delay = self._reconnect_delay
        self._reconnect_delay = min(self._reconnect_delay * 2, MAX_RECONNECT_DELAY)
        _LOGGER.debug("%s: reconnecting in %ss", self._ip, delay)
        self._submit(self._reconnect_after(delay))

    async def _reconnect_after(self, delay: float):
        """Wait out the backoff, then reopen the connection."""
        try:
            await asyncio.sleep(delay)
        finally:
            self._reconnecting = False
        if not self._stopped and self.is_disconnected:
            self.connect()

    async def _open_connection(self):
        """Open the socket, scheduling another attempt if the device is away."""
        _LOGGER.debug("Opening connection to IntesisBox %s:%s", self._ip, self._port)
        try:
            await self._eventLoop.create_connection(lambda: self, self._ip, self._port)
        except OSError as err:
            self._connectionStatus = API_DISCONNECTED
            _LOGGER.warning("%s: connection attempt failed: %s", self._ip, err)
            self._send_update_callback()
            self._schedule_reconnect()

    def connect(self):
        """Public method for connecting to the IntesisBox."""
        if self._stopped:
            return
        if not self._ip or not self._port:
            _LOGGER.error("%s: missing IP address or port", self._ip)
            return
        if self._connectionStatus != API_DISCONNECTED:
            _LOGGER.debug(
                "%s: connect() ignored, already %s", self._ip, self._connectionStatus
            )
            return
        if self._reconnecting:
            _LOGGER.debug("%s: connect() ignored, a retry is already queued", self._ip)
            return
        self._connectionStatus = API_CONNECTING
        self._submit(self._open_connection())

    def stop(self):
        """Public method for shutting down connectivity with the IntesisBox."""
        self._stopped = True
        self._connectionStatus = API_DISCONNECTED
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()

    async def poll_status(self, session: int):
        """Periodically poll for updates since the controllers don't always update reliably."""
        while self._session_active(session):
            _LOGGER.debug("Polling for update")
            self._write("GET,1:*")
            await asyncio.sleep(60 * 5)  # 5 minutes
        else:
            _LOGGER.debug("Not connected, skipping poll_status()")

    def set_temperature(self, setpoint):
        """Public method for setting the temperature."""
        set_temp = int(setpoint * 10)
        self._set_value(FUNCTION_SETPOINT, set_temp)

    def set_fan_speed(self, fan_speed):
        """Public method to set the fan speed."""
        self._set_value(FUNCTION_FANSP, fan_speed)

    def set_vertical_vane(self, vane: str):
        """Public method to set the vertical vane."""
        self._set_value(FUNCTION_VANEUD, vane)

    def set_horizontal_vane(self, vane: str):
        """Public method to set the horizontal vane."""
        self._set_value(FUNCTION_VANELR, vane)

    def _set_value(self, uid: str, value: str | int) -> None:
        """Change a setting on the thermostat."""
        self._submit(self._send_set(uid, value), timeout=self._set_timeout())

    def _set_timeout(self) -> float:
        """Worst case time _send_set() needs, plus room to return."""
        return SET_ATTEMPTS * (1 + SET_CONFIRM_TIMEOUT) + 2

    async def _send_set(self, uid: str, value: str | int) -> bool:
        """Send a command and wait for the device to acknowledge it.

        The device answers a SET with an ACK, and with a CHN as well when the
        value actually changes. Silence means the command was lost, which on a
        half-open socket is the only symptom there is.
        """
        for attempt in range(1, SET_ATTEMPTS + 1):
            _LOGGER.debug(
                "%s: sending SET %s=%s (attempt %d, status=%s, last data received %s)",
                self._ip,
                uid,
                value,
                attempt,
                self._connectionStatus,
                self._silence_text(),
            )
            self._pending_sets[uid] = (str(value), time.monotonic())
            if not await self._writeasync(f"SET,1:{uid},{value}"):
                self._pending_sets.pop(uid, None)
                break

            deadline = time.monotonic() + SET_CONFIRM_TIMEOUT
            while time.monotonic() < deadline:
                if uid not in self._pending_sets:
                    return True
                await asyncio.sleep(0.2)

            _LOGGER.warning(
                "%s: SET %s=%s was not acknowledged within %ss",
                self._ip,
                uid,
                value,
                SET_CONFIRM_TIMEOUT,
            )

        self._pending_sets.pop(uid, None)
        _LOGGER.error(
            "%s: giving up on SET %s=%s, rebuilding the connection",
            self._ip,
            uid,
            value,
        )
        self._force_reconnect()
        return False

    def set_mode(self, mode):
        """Send mode and confirm change before turning on."""
        _LOGGER.debug(f"Setting MODE to {mode}.")
        if mode not in MODES:
            _LOGGER.error("%s: unsupported mode %s", self._ip, mode)
            return
        self._submit(self._apply_mode(mode), timeout=self._set_timeout() * 3)

    async def _apply_mode(self, mode) -> None:
        """Set the mode, then power on once the device reports it.

        Some units answer out of order, so the mode is read back rather than
        assumed before switching the unit on.
        """
        if not await self._send_set(FUNCTION_MODE, mode):
            return

        deadline = time.monotonic() + SET_CONFIRM_TIMEOUT
        while self.mode != mode and time.monotonic() < deadline:
            _LOGGER.debug(
                f"Waiting for MODE to return {mode}, currently {str(self.mode)}"
            )
            await self._writeasync("GET,1:MODE")

        if self.mode != mode:
            _LOGGER.error(
                "%s: device still reports MODE=%s after being set to %s, "
                "not turning it on",
                self._ip,
                self.mode,
                mode,
            )
            return

        if not self.is_on:
            _LOGGER.debug(f"MODE confirmed now {str(self.mode)}, proceed to Power On")
            await self._send_set(FUNCTION_ONOFF, POWER_ON)

    def set_mode_dry(self):
        """Public method to set device to dry asynchronously."""
        self._set_value(FUNCTION_MODE, MODE_DRY)

    def set_power_off(self):
        """Public method to turn off the device asynchronously."""
        self._set_value(FUNCTION_ONOFF, POWER_OFF)

    def set_power_on(self):
        """Public method to turn on the device asynchronously."""
        self._set_value(FUNCTION_ONOFF, POWER_ON)

    @property
    def operation_list(self) -> list[str]:
        """Supported modes."""
        return self._operation_list

    @property
    def vane_horizontal_list(self) -> list[str]:
        """Supported Horizontal Vane settings."""
        return self._horizontal_vane_list

    @property
    def vane_vertical_list(self) -> list[str]:
        """Supported Vertical Vane settings."""
        return self._vertical_vane_list

    @property
    def mode(self) -> str | None:
        """Current mode."""
        return self._device.get(FUNCTION_MODE)

    @property
    def fan_speed(self) -> str | None:
        """Current fan speed."""
        return self._device.get(FUNCTION_FANSP)

    @property
    def fan_speed_list(self) -> list[str]:
        """Supported fan speeds."""
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
        """Firmware versioon of the IntesisBox."""
        return self._firmversion

    @property
    def is_on(self) -> bool:
        """Return true if the controlled device is turned on."""
        return self._device.get(FUNCTION_ONOFF) == POWER_ON

    @property
    def has_swing_control(self) -> bool:
        """Return true if the device supports swing modes."""
        return len(self._horizontal_vane_list) > 1 or len(self._vertical_vane_list) > 1

    @property
    def setpoint(self) -> float | None:
        """Public method returns the target temperature."""
        setpoint = self._device.get(FUNCTION_SETPOINT)
        return (int(setpoint) / 10) if setpoint else None

    @property
    def ambient_temperature(self) -> float | None:
        """Public method returns the current temperature."""
        temperature = self._device.get(FUNCTION_AMBTEMP)
        return (int(temperature) / 10) if temperature else None

    @property
    def max_setpoint(self) -> float | None:
        """Maximum allowed target temperature."""
        return self._setpoint_maximum

    @property
    def min_setpoint(self) -> float | None:
        """Minimum allowed target temperature."""
        return self._setpoint_minimum

    @property
    def rssi(self) -> int | None:
        """Wireless signal strength of the IntesisBox."""
        return self._rssi

    @property
    def vertical_swing(self) -> str | None:
        """Current vertical vane setting."""
        return self._device.get(FUNCTION_VANEUD)

    @property
    def horizontal_swing(self) -> str | None:
        """Current horizontal vane setting."""
        return self._device.get(FUNCTION_VANELR)

    def _send_update_callback(self):
        """Notify all listeners that state of the thermostat has changed."""
        if not self._updateCallbacks:
            _LOGGER.debug("Update callback has not been set by client.")

        for callback in self._updateCallbacks:
            callback()

    def _send_error_callback(self, message: str):
        """Notify all listeners that an error has occurred."""
        self._errorMessage = message

        if self._errorCallbacks == []:
            _LOGGER.debug("Error callback has not been set by client.")

        for callback in self._errorCallbacks:
            callback(message)

    @property
    def is_connected(self) -> bool:
        """Returns true if the TCP connection is established."""
        return self._connectionStatus == API_AUTHENTICATED

    @property
    def error_message(self) -> str | None:
        """Returns the last error message, or None if there were no errors."""
        return self._errorMessage

    @property
    def is_disconnected(self) -> bool:
        """Returns true when the TCP connection is disconnected and idle."""
        return self._connectionStatus == API_DISCONNECTED

    def add_update_callback(self, method):
        """Public method to add a callback subscriber."""
        self._updateCallbacks.append(method)

    def add_error_callback(self, method):
        """Public method to add a callback subscriber."""
        self._errorCallbacks.append(method)
