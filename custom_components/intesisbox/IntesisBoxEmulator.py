"""Emulates an IntesisBox device on TCP port 3310."""

import argparse
import asyncio
from datetime import datetime, timedelta
import random
import time

# Timing constants for realistic device behavior
RESPONSE_DELAY_MIN = 0.1  # Minimum response delay in seconds
RESPONSE_DELAY_MAX = 0.25  # Maximum response delay for normal commands
ONOFF_DELAY_MAX = 5.0  # ONOFF can take up to 5 seconds
SOCKET_IDLE_TIMEOUT = 60.0  # Device closes socket after 60 seconds of inactivity
MIN_RECONNECT_INTERVAL = 1.0  # Minimum time between connection cycles

# Temperature limits (in tenths of degrees C, e.g., 180 = 18.0°C)
SETPTEMP_DEFAULT_MIN = 180
SETPTEMP_DEFAULT_MAX = 300
SETPTEMP_AUTO_MIN = 180
SETPTEMP_AUTO_MAX = 300
SETPTEMP_HEAT_MIN = 200
SETPTEMP_HEAT_MAX = 300
SETPTEMP_COOL_MIN = 180
SETPTEMP_COOL_MAX = 250
SETPTEMP_DRY_MIN = 180
SETPTEMP_DRY_MAX = 250
SETPTEMP_FAN_MIN = 180
SETPTEMP_FAN_MAX = 300

MODE_AUTO = "AUTO"
MODE_HEAT = "HEAT"
MODE_DRY = "DRY"
MODE_FAN = "FAN"
MODE_COOL = "COOL"

FUNCTION_ONOFF = "ONOFF"
FUNCTION_MODE = "MODE"
FUNCTION_SETPOINT = "SETPTEMP"
FUNCTION_FANSP = "FANSP"
FUNCTION_VANEUD = "VANEUD"
FUNCTION_VANELR = "VANELR"
FUNCTION_AMBTEMP = "AMBTEMP"
FUNCTION_ERRSTATUS = "ERRSTATUS"
FUNCTION_ERRCODE = "ERRCODE"

RW_FUNCTIONS = [
    FUNCTION_ONOFF,
    FUNCTION_MODE,
    FUNCTION_SETPOINT,
    FUNCTION_VANELR,
    FUNCTION_VANEUD,
    FUNCTION_FANSP,
]


class IntesisBoxEmulator(asyncio.Protocol):
    """Dummy device, for testing."""

    # Class variable to track last disconnect across all connections
    _last_disconnect_time = None

    # Class variable to store persistent device state across connections
    _device_state = None

    # Internal clock - starts at 1 Jan 2001 00:00:00
    _internal_datetime = None  # Stored as datetime object
    _internal_clock_start = None  # Real time (monotonic) when internal clock was set
    _clock_initialized = False

    def __init__(
        self,
        vaneud_limits=None,
        vanelr_limits=None,
        fansp_limits=None,
        dynamic_setptemp=False,
    ):
        """Build an emulator, not much to see here."""
        self.mode = "AUTO"
        self.setpoint = "210"
        self.power = "OFF"

        # Store limits (None means disabled/N notation)
        self.vaneud_limits = vaneud_limits
        self.vanelr_limits = vanelr_limits
        self.fansp_limits = fansp_limits
        self.dynamic_setptemp = dynamic_setptemp

        # Timing tracking for protocol compliance
        self.connection_time = None
        self.last_activity_time = None

        # Queue for delayed CHN responses
        self.pending_notifications = []

        # Initialize device state only once (persistent across connections)
        if IntesisBoxEmulator._device_state is None:
            IntesisBoxEmulator._device_state = {
                "1": {
                    FUNCTION_MODE: MODE_AUTO,
                    FUNCTION_SETPOINT: "210",
                    FUNCTION_ONOFF: "OFF",
                    FUNCTION_FANSP: "AUTO",
                    FUNCTION_AMBTEMP: "180",
                    FUNCTION_VANEUD: "AUTO",
                    FUNCTION_VANELR: "AUTO",
                    FUNCTION_ERRSTATUS: "OK",
                    FUNCTION_ERRCODE: "",
                }
            }
            print("✓ Initialized device state (ONOFF=OFF)")

        # Initialize internal clock only once (persistent across connections)
        if not IntesisBoxEmulator._clock_initialized:
            IntesisBoxEmulator._internal_datetime = datetime(2001, 1, 1, 0, 0, 0)
            IntesisBoxEmulator._internal_clock_start = time.monotonic()
            IntesisBoxEmulator._clock_initialized = True
            print("✓ Initialized internal clock (01/01/2001 00:00:00)")

        # Reference the shared state
        self.devices = IntesisBoxEmulator._device_state

    def connection_made(self, transport):
        """Store connection when setup."""
        self.transport = transport
        peername = transport.get_extra_info("peername")

        current_time = time.time()
        self.connection_time = current_time
        self.last_activity_time = current_time

        # Check for rapid reconnection (protocol violation)
        if IntesisBoxEmulator._last_disconnect_time:
            reconnect_interval = current_time - IntesisBoxEmulator._last_disconnect_time
            if reconnect_interval < MIN_RECONNECT_INTERVAL:
                print(f"⚠️  WARNING: Connection from {peername} violated protocol!")
                print(
                    f"   Reconnected after {reconnect_interval:.3f}s (min required: {MIN_RECONNECT_INTERVAL}s)"
                )

        print(f"✓ Connection established from {peername}")

    @classmethod
    def get_internal_datetime(cls):
        """Get current internal datetime based on elapsed time since clock was set."""
        if cls._internal_datetime is None or cls._internal_clock_start is None:
            # Fallback to default if not initialized
            return datetime(2001, 1, 1, 0, 0, 0)

        # Calculate elapsed time since clock was set
        elapsed_seconds = time.monotonic() - cls._internal_clock_start

        # Add elapsed time to stored datetime
        return cls._internal_datetime + timedelta(seconds=elapsed_seconds)

    @classmethod
    def set_internal_datetime(cls, new_datetime):
        """Set the internal clock to a new datetime."""
        cls._internal_datetime = new_datetime
        cls._internal_clock_start = time.monotonic()

    def connection_lost(self, exc):
        """Track disconnection time."""
        current_time = time.time()
        IntesisBoxEmulator._last_disconnect_time = current_time

        if self.connection_time:
            duration = current_time - self.connection_time
            print(f"✗ Connection closed (duration: {duration:.1f}s)")

    def data_received(self, data):
        """Process received data."""
        current_time = time.time()

        # Check for idle timeout warning
        if self.last_activity_time:
            idle_time = current_time - self.last_activity_time
            if idle_time > SOCKET_IDLE_TIMEOUT:
                print(
                    f"⚠️  WARNING: Socket was idle for {idle_time:.1f}s (timeout: {SOCKET_IDLE_TIMEOUT}s)"
                )

        self.last_activity_time = current_time

        linesReceived = data.decode("ascii").splitlines()

        # Collect all CHN notifications that need to be sent with delay
        chn_responses = []

        for line in linesReceived:
            request = line.rstrip().split(",")
            immediate_response = ""

            if request[0] == "ID":
                immediate_response = (
                    "ID:IS-IR-WMP-1,001DC9A2C911,192.168.100.246,ASCII,v0.0.1,-44"
                )

            elif request[0] == "GET":
                acNum = request[1].split(":")[0]
                function = request[1].split(":")[1]
                if acNum in self.devices and function == "*":
                    for function, value in self.devices[acNum].items():
                        immediate_response += f"CHN,{acNum}:{function},{value}\r\n"
                elif acNum in self.devices and function in self.devices[acNum]:
                    current_value = self.devices[acNum][function]
                    immediate_response = f"CHN,{acNum}:{function},{current_value}"
                else:
                    immediate_response = "ERR"

            elif request[0] == "SET":
                acNum = request[1].split(":")[0]
                function = request[1].split(":")[1]
                if (
                    acNum in self.devices
                    and function in RW_FUNCTIONS
                    and len(request) >= 3
                ):
                    value = request[2]
                    if self.devices[acNum][function] != value:
                        self.devices[acNum][function] = value
                        # Send ACK immediately
                        immediate_response = "ACK"

                        # Queue CHN response with delay
                        # ONOFF takes longer (up to 5s), others are faster (up to 0.25s)
                        if function == FUNCTION_ONOFF:
                            delay = random.uniform(RESPONSE_DELAY_MIN, ONOFF_DELAY_MAX)
                        else:
                            delay = random.uniform(
                                RESPONSE_DELAY_MIN, RESPONSE_DELAY_MAX
                            )

                        chn_msg = f"CHN,{acNum}:{function},{value}"
                        chn_responses.append((delay, chn_msg, function))
                    else:
                        immediate_response = "ACK"
                else:
                    immediate_response = "ERR"

            elif request[0].split(":")[0] == "LIMITS":
                limit = request[0].split(":")[1]
                if limit == "FANSP":
                    if self.fansp_limits:
                        fansp_str = ",".join(self.fansp_limits)
                        immediate_response = f"LIMITS:FANSP,[{fansp_str}]"
                    # else: no response (feature disabled - device ignores the command)
                elif limit == "VANEUD":
                    if self.vaneud_limits:
                        vaneud_str = ",".join(self.vaneud_limits)
                        immediate_response = f"LIMITS:VANEUD,[{vaneud_str}]"
                    # else: no response (feature disabled - device ignores the command)
                elif limit == "VANELR":
                    if self.vanelr_limits:
                        vanelr_str = ",".join(self.vanelr_limits)
                        immediate_response = f"LIMITS:VANELR,[{vanelr_str}]"
                    # else: no response (feature disabled - device ignores the command)
                elif limit == "SETPTEMP":
                    if self.dynamic_setptemp:
                        # Get current MODE from device state
                        current_mode = self.devices.get("1", {}).get(
                            FUNCTION_MODE, "AUTO"
                        )
                        if current_mode == MODE_AUTO:
                            immediate_response = f"LIMITS:SETPTEMP,[{SETPTEMP_AUTO_MIN},{SETPTEMP_AUTO_MAX}]"
                        elif current_mode == MODE_HEAT:
                            immediate_response = f"LIMITS:SETPTEMP,[{SETPTEMP_HEAT_MIN},{SETPTEMP_HEAT_MAX}]"
                        elif current_mode == MODE_COOL:
                            immediate_response = f"LIMITS:SETPTEMP,[{SETPTEMP_COOL_MIN},{SETPTEMP_COOL_MAX}]"
                        elif current_mode == MODE_DRY:
                            immediate_response = f"LIMITS:SETPTEMP,[{SETPTEMP_DRY_MIN},{SETPTEMP_DRY_MAX}]"
                        elif current_mode == MODE_FAN:
                            immediate_response = f"LIMITS:SETPTEMP,[{SETPTEMP_FAN_MIN},{SETPTEMP_FAN_MAX}]"
                        else:
                            # Fallback to default for unknown modes
                            immediate_response = f"LIMITS:SETPTEMP,[{SETPTEMP_DEFAULT_MIN},{SETPTEMP_DEFAULT_MAX}]"
                    else:
                        # Static limits when dynamic mode is disabled
                        immediate_response = f"LIMITS:SETPTEMP,[{SETPTEMP_DEFAULT_MIN},{SETPTEMP_DEFAULT_MAX}]"
                elif limit == "MODE":
                    immediate_response = "LIMITS:MODE,[AUTO,HEAT,DRY,COOL,FAN]"

            elif request[0].split(":")[0] == "CFG":
                if len(request[0].split(":")) > 1:
                    config_item = request[0].split(":")[1]
                    if config_item == "DATETIME":
                        # Check if this is a SET (has value) or GET (no value)
                        if len(request) > 1:
                            # SET: CFG:DATETIME,DD/MM/YYYY HH:MM:SS
                            try:
                                datetime_str = request[1]
                                # Parse the datetime string
                                new_dt = datetime.strptime(
                                    datetime_str, "%d/%m/%Y %H:%M:%S"
                                )
                                # Set the internal clock
                                IntesisBoxEmulator.set_internal_datetime(new_dt)
                                immediate_response = "ACK"
                                print(f"⏰ Internal clock set to: {datetime_str}")
                            except ValueError:
                                immediate_response = "ERR"
                                print(f"⚠️  Invalid datetime format: {request[1]}")
                        else:
                            # GET: CFG:DATETIME
                            # Return current internal datetime in DD/MM/YYYY HH:MM:SS format
                            current_dt = IntesisBoxEmulator.get_internal_datetime()
                            datetime_str = current_dt.strftime("%d/%m/%Y %H:%M:%S")
                            immediate_response = f"CFG:DATETIME,{datetime_str}"

            # Send immediate response
            if immediate_response:
                immediate_response += "\r\n"
                self.transport.write(immediate_response.encode("ascii"))

        # Shuffle CHN responses to simulate real device behavior
        # Real devices often respond out of order
        if chn_responses:
            random.shuffle(chn_responses)

            # Schedule delayed CHN responses
            for delay, msg, function in chn_responses:
                asyncio.create_task(self._send_delayed_response(delay, msg, function))

    async def _send_delayed_response(self, delay, message, function):
        """Send a CHN response after a delay."""
        await asyncio.sleep(delay)
        response = f"{message}\r\n"
        self.transport.write(response.encode("ascii"))
        print(f"  → {message} (delayed {delay:.3f}s, function: {function})")


async def main(
    host, port, vaneud_limits, vanelr_limits, fansp_limits, dynamic_setptemp
):
    """Set up and run the emulator."""
    loop = asyncio.get_running_loop()

    # Create a factory function that passes the limits
    def protocol_factory():
        return IntesisBoxEmulator(
            vaneud_limits, vanelr_limits, fansp_limits, dynamic_setptemp
        )

    server = await loop.create_server(protocol_factory, host, port)
    print("=" * 70)
    print(f"IntesisBox Emulator running on {host}:{port}")
    print(f"VANEUD limits: {vaneud_limits if vaneud_limits else 'Disabled'}")
    print(f"VANELR limits: {vanelr_limits if vanelr_limits else 'Disabled'}")
    print(f"FANSP limits:  {fansp_limits if fansp_limits else 'Disabled'}")
    if dynamic_setptemp:
        print("SETPTEMP: Dynamic by MODE")
        print(f"  AUTO: [{SETPTEMP_AUTO_MIN},{SETPTEMP_AUTO_MAX}]")
        print(f"  HEAT: [{SETPTEMP_HEAT_MIN},{SETPTEMP_HEAT_MAX}]")
        print(f"  COOL: [{SETPTEMP_COOL_MIN},{SETPTEMP_COOL_MAX}]")
        print(f"  DRY:  [{SETPTEMP_DRY_MIN},{SETPTEMP_DRY_MAX}]")
        print(f"  FAN:  [{SETPTEMP_FAN_MIN},{SETPTEMP_FAN_MAX}]")
    else:
        print(f"SETPTEMP: Static [{SETPTEMP_DEFAULT_MIN},{SETPTEMP_DEFAULT_MAX}]")
    print()
    print("Timing Configuration:")
    print(f"  Response delay: {RESPONSE_DELAY_MIN}s - {RESPONSE_DELAY_MAX}s")
    print(f"  ONOFF delay:    {RESPONSE_DELAY_MIN}s - {ONOFF_DELAY_MAX}s")
    print(f"  Idle timeout:   {SOCKET_IDLE_TIMEOUT}s")
    print(f"  Min reconnect:  {MIN_RECONNECT_INTERVAL}s")
    print()
    print("Protocol behavior:")
    print("  - ACK sent immediately for SET commands")
    print("  - CHN notifications delayed and possibly shuffled")
    print("  - LIMITS queries for disabled features return nothing")
    print("  - Device state persists across disconnect/reconnect")
    print()
    print("Protocol violations will be logged to console.")
    print("=" * 70)
    await server.serve_forever()


def parse_compact_notation(notation, allow_swing=True):
    """Parse compact notation into a list of options.

    Format: [A][X][S]
    - A = includes AUTO
    - X = number of positions (1-9)
    - S = includes SWING (only if allow_swing=True)

    Examples:
    - "A7S" -> ["AUTO", "1", "2", "3", "4", "5", "6", "7", "SWING"]
    - "3S" -> ["1", "2", "3", "SWING"]
    - "4" -> ["1", "2", "3", "4"]
    - "N" -> None
    - "A3" -> ["AUTO", "1", "2", "3"]

    """
    if not notation or notation.upper() == "N":
        return None

    notation = notation.upper()
    options = []

    # Check for AUTO
    has_auto = notation.startswith("A")
    if has_auto:
        options.append("AUTO")
        notation = notation[1:]  # Remove 'A'

    # Check for SWING at the end
    has_swing = False
    if allow_swing and notation.endswith("S"):
        has_swing = True
        notation = notation[:-1]  # Remove 'S'

    # Parse the number
    if notation:
        try:
            num_positions = int(notation)
            if num_positions < 1 or num_positions > 9:
                raise ValueError(
                    f"Number of positions must be 1-9, got {num_positions}"
                )

            # Add numbered positions
            options.extend([str(i) for i in range(1, num_positions + 1)])
        except ValueError as e:
            if "invalid literal" in str(e):
                raise ValueError(
                    "Invalid notation format. Expected format: [A][1-9][S]"
                )
            raise

    # Add SWING at the end if specified
    if has_swing:
        options.append("SWING")

    return options if options else None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="IntesisBox WMP Protocol Emulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,  # Disable default help to add custom ones
        epilog="""
Compact Notation Format:
  [A][X][S] where:
    A = includes AUTO
    X = number of positions (1-9)
    S = includes SWING (vanes only)
    N = none/disabled

Examples:
  python IntesisBoxEmulator.py
    Default: VANEUD=A3, VANELR=A3, FANSP=A3

  python IntesisBoxEmulator.py --VUD A7S --VLR A5S --FAN A4
    VANEUD: AUTO,1-7,SWING
    VANELR: AUTO,1-5,SWING
    FANSP:  AUTO,1-4

  python IntesisBoxEmulator.py --VUD 4 --VLR 3S
    VANEUD: 1-4 (no AUTO, no SWING)
    VANELR: 1-3,SWING (no AUTO)

  python IntesisBoxEmulator.py --VUD A9S --VLR A9S --FAN 5
    VANEUD: AUTO,1-9,SWING
    VANELR: AUTO,1-9,SWING
    FANSP:  1-5 (no AUTO)

  python IntesisBoxEmulator.py --VUD N --VLR N --FAN N
    All limits disabled (LIMITS queries are ignored, no response sent)
        """,
    )

    # Add help arguments (both --help and --?)
    parser.add_argument(
        "-h", "--help", "--?", action="help", help="Show this help message and exit"
    )

    parser.add_argument(
        "--VUD",
        dest="vaneud",
        help="Vertical vane (up/down). Format: [A][1-9][S]. Examples: A7S, 3S, 4, N. Default: A3",
        default="A3",
    )

    parser.add_argument(
        "--VLR",
        dest="vanelr",
        help="Horizontal vane (left/right). Format: [A][1-9][S]. Examples: A5S, 3S, 4, N. Default: A3",
        default="A3",
    )

    parser.add_argument(
        "--FAN",
        dest="fansp",
        help="Fan speed. Format: [A][1-9]. Examples: A4, 5, N. Default: A3",
        default="A3",
    )

    parser.add_argument(
        "--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)"
    )

    parser.add_argument(
        "--port", type=int, default=3310, help="Port to listen on (default: 3310)"
    )

    parser.add_argument(
        "--dynamic-setptemp",
        action="store_true",
        help="Enable dynamic SETPTEMP limits based on MODE (AUTO: [180,300], HEAT: [200,300], COOL: [180,250], DRY: [180,250], FAN: [180,300])",
    )

    args = parser.parse_args()

    # Parse compact notation
    try:
        vaneud_limits = parse_compact_notation(args.vaneud, allow_swing=True)
        vanelr_limits = parse_compact_notation(args.vanelr, allow_swing=True)
        fansp_limits = parse_compact_notation(args.fansp, allow_swing=False)
    except ValueError as e:
        parser.error(str(e))

    asyncio.run(
        main(
            args.host,
            args.port,
            vaneud_limits,
            vanelr_limits,
            fansp_limits,
            args.dynamic_setptemp,
        )
    )
