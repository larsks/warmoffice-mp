# thermostat.py
# Lars Kellogg-Stedman <lars@oddbit.com>
#
# A presence-aware thermostat for controlling a heater attached to
# a Tasmota-capable switch.
#
# We're using comments rather than docstrings because (a) there's no
# meaningful way to run pydoc on this file (because none of the modules
# will be available in standard Python) and (b) to avoid growing
# the size of the byte-compiled binary.

import binascii
import ds18x20
import machine
import ntptime
import onewire
import time
import uasyncio as asyncio
import urequests as requests

from collections import namedtuple


# Represents the controller state
class State:
    # fmt: off
    INIT        = 0
    IDLE1       = 1
    TRACKING    = 2
    ACTIVE      = 3
    PREWARM     = 4
    OFF         = 5
    IDLE2       = 6
    # fmt: on

    @classmethod
    def from_string(cls, name):
        try:
            return cls.__dict__[name.upper()]
        except KeyError:
            print("[state] unknown state")
            return cls.OFF

    @classmethod
    def to_string(cls, value):
        valmap = {v: k for k, v in cls.__dict__.items() if isinstance(v, int)}
        return valmap[value]


# Convert a time tuple (as returned by time.gmtime) to minutes
def time_as_minutes(t):
    return (t[3] * 60) + t[4]


# Holds a single heating schedule entry
class Schedule(namedtuple("Schedule", ["state", "temp", "hour", "minute"])):
    # Convert time to minutes (used to compare with the current time in the
    # scheduling code).
    def as_minutes(self):
        return (self.hour * 60) + self.minute

    def __str__(self):
        return "{}:{} T:{} S:{}".format(self.hour, self.minute, self.temp, self.state)


# Return a logger that prefixes each line with a colored name tag
def make_logger(name, color):

    # ANSI color codes
    # fmt: off
    colors = {
        "black":    "\u001b[30m",
        "red":      "\u001b[31m",
        "green":    "\u001b[32m",
        "yellow":   "\u001b[33m",
        "blue":     "\u001b[34m",
        "magenta":  "\u001b[35m",
        "cyan":     "\u001b[36m",
        "white":    "\u001b[37m",
        "reset":    "\u001b[0m",
    }
    # fmt: on

    if color not in colors:
        raise ValueError(color)

    def _logger(msg):
        print("[{}{}{}]: {}".format(colors[color], name, colors["reset"], msg))

    return _logger


# A class for reading DS18B20 temperature sensors.
#
# Reads sensors every read_interval seconds, makes the
# values available in self.values for consumers.
class Temperature:
    def __init__(self, pin, expected=1, read_interval=10):
        self.ds = ds18x20.DS18X20(onewire.OneWire(machine.Pin(pin)))
        self.expected = expected

        # valid_read_flag is used by client code that needs to wait
        # until a valid sensor reading is available
        self.valid_read_flag = asyncio.Event()

        self.read_interval = read_interval
        self.logger = make_logger("temp", "yellow")
        self.last_read = 0.0

    # This method waits until we're able to discover at least the
    # expected number of sensors.
    async def discover(self):
        while True:
            self.roms = self.ds.scan()
            self.logger(
                "found {} sensors want {}".format(len(self.roms), self.expected)
            )

            if len(self.roms) >= self.expected:
                self.values = [0.0] * len(self.roms)
                break

            await asyncio.sleep(5)

    async def loop(self):
        self.logger("waiting for sensors")
        await self.discover()

        self.logger("start temperature loop ({})".format(len(self.roms)))
        while True:
            try:
                self.ds.convert_temp()

                # We have to wait 750ms before reading the temperature; see
                # https://datasheets.maximintegrated.com/en/ds/DS18B20.pdf
                await asyncio.sleep_ms(750)

                for i, rom in enumerate(self.roms):
                    self.values[i] = self.ds.read_temp(rom)
                    self.logger(
                        "read temperature {} from device {}".format(
                            self.values[i],
                            binascii.hexlify(rom).decode(),
                        )
                    )

                self.valid_read_flag.set()
                self.last_read = time.time()
            except Exception as err:
                self.logger("failed read: {}".format(err))

            await asyncio.sleep(self.read_interval)

    def time_since_last_read(self):
        return time.time() - self.last_read

    async def wait_ready(self):
        await self.valid_read_flag.wait()


# Interface to a Tasmota [1] switch
#
# [1]: https://tasmota.github.io/docs/
class Switch:
    def __init__(self, addr):
        self.url = "http://{addr}/cm".format(addr=addr)
        self.lock = asyncio.Lock()
        self.log = make_logger("switch@{}".format(addr), "blue")

    async def request(self, cmnd):
        self.log("trying to acquire lock")
        async with self.lock:
            self.log("lock acquired")
            while True:
                try:
                    self.log("sending command: {}".format(cmnd))
                    requests.get(
                        "{url}?cmnd={cmnd}".format(
                            url=self.url, cmnd=cmnd.replace(" ", "%20")
                        )
                    )
                except OSError:
                    self.log("failed to communicate with switch (retrying)")
                    await asyncio.sleep(5)
                else:
                    self.log("command sent successfully")
                    break

    async def turn_on(self):
        self.log("turn on")
        await self.request("Power On")

    async def turn_off(self):
        self.log("turn off")
        await self.request("Power Off")

    async def is_on(self):
        res = await self.request("Power Status")
        data = res.json()
        self.log("current status = {}".format(data["POWER"]))
        return data["power"] == "ON"


# Converts motion detection events into presence detection.
#
# To detect "present", in detect_interval seconds there must be
# at least min_detects seconds during which motion is detected.
class Presence:
    def __init__(self, motion, min_detect=20, detect_interval=300):
        self.present = 0
        self.min_detect = min_detect
        self.detect_interval = detect_interval
        self.motion = motion
        self.logger = make_logger("presence", "green")

    async def loop(self):
        self.logger("start presence loop")
        self.samples = []
        while True:
            try:
                await asyncio.wait_for(self.motion.wait_motion(), self.detect_interval)
            except asyncio.TimeoutError:
                self.logger("timed out waiting for motion")
                self.present = 0
                continue

            self.samples.append(time.time())
            self.logger(
                "motion detected (have {}, want {})".format(
                    len(self.samples), self.min_detect
                )
            )

            if len(self.samples) >= self.min_detect:
                self.samples = self.samples[len(self.samples) - self.min_detect :]
                delta = self.samples[-1] - self.samples[0]

                if delta <= self.detect_interval:
                    self.logger("present (delta={})".format(delta))
                    self.present = 1
                else:
                    self.logger("not present (delta={})".format(delta))
                    self.present = 0

            await asyncio.sleep(1)


# Respond to events from a standard IR motion detector
class Motion:
    def __init__(self, pin):
        self.pin = machine.Pin(pin)

        # motion is 1 when the motion detector is indicating motion,
        # 0 when not
        self.motion = 0

        # motion_persist is set to 1 when the motion detect indicates
        # motion, and is only set to 0 when someone calls the was_motion method.
        # This allows you to ask, "has any motion been detected since I
        # last checked?"
        self.motion_persist = 0

        # motion_flag allows a client to await on motion instead of
        # polling
        self.motion_flag = asyncio.ThreadSafeFlag()

        self.detections = 0
        self.logger = make_logger("motion", "cyan")

    def start_motion_sensor(self):
        self.pin.irq(
            handler=self.motion_detected,
            trigger=machine.Pin.IRQ_RISING | machine.Pin.IRQ_FALLING,
        )

    def stop_motion_sensor(self):
        self.pin.irq(handler=None)

    def motion_detected(self, pin):
        self.logger("motion detected")
        self.motion_persist = 1
        self.detections += 1
        self.motion_flag.set()

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.wait_motion()

    async def wait_motion(self):
        await self.motion_flag.wait()

    def was_motion(self):
        res = self.motion_persist
        self.motion_persist = 0
        return res


# Control a remote switch in response to a temperature sensor
class Thermostat:
    def __init__(self, temp, switch, max_delta=1.0, min_delta=0.5):
        self.temp = temp
        self.switch = switch
        self.target_temp = None
        self.target_temp_flag = asyncio.Event()

        # self.active tracks whether or not we are actively managing
        # the remote switch
        self.active = 0

        # self.heating indicates whether or not the remote switch is on
        self.heating = 0

        # turn on the heat when we are max_delta below the target
        # temperature
        self.max_delta = max_delta

        # turn off the heat when we are within min_delta of the
        # target temperature
        self.min_delta = min_delta

        self.logger = make_logger("therm", "red")

    async def loop(self):
        # ensure that we have a target temperature and that there has been at
        # least one valid temperature reading before we start
        self.logger("waiting for dependencies")
        await asyncio.gather(
            self.temp.wait_ready(),
            self.wait_ready(),
        )

        self.logger("start thermostat loop")
        while True:
            delta = self.target_temp - self.temp.values[0]

            self.logger(
                "have={}, want={}, delta={}, active={}, heating={}".format(
                    self.temp.values[0],
                    self.target_temp,
                    delta,
                    self.active,
                    self.heating,
                )
            )

            if self.active:
                if self.temp.time_since_last_read() > 300:
                    # safety check -- bail out if temperature stops
                    # updating.
                    self.logger("⚠️ stale temperature reading")
                    self.control_deactivate()
                elif not self.heating and delta > self.max_delta:
                    await self.heat_on()
                elif self.heating and delta < self.min_delta:
                    await self.heat_off()
            elif self.heating:
                await self.heat_off()

            await asyncio.sleep(10)

    async def heat_on(self):
        self.logger("🔥 FLAME ON ")
        self.heating = 1
        asyncio.create_task(self.switch.turn_on())

    async def heat_off(self):
        self.logger("FLAME OFF")
        self.heating = 0
        asyncio.create_task(self.switch.turn_off())

    def control_activate(self):
        self.logger("heat control on")
        self.active = 1

    def control_deactivate(self):
        self.logger("heat control off")
        self.active = 0

    def set_target_temp(self, target_temp):
        self.target_temp = target_temp
        self.target_temp_flag.set()

    async def wait_ready(self):
        await self.target_temp_flag.wait()


# Periodically sync the system clock using ntp
class Clock:
    def __init__(self):
        self.logger = make_logger("clock", "magenta")
        self.time_valid = asyncio.Event()

    async def loop(self):
        while True:
            self.logger("setting time")
            try:
                ntptime.settime()
            except OSError:
                # on failure, retry in 10 seconds
                self.logger("failed to set time")
                await asyncio.sleep(10)
            else:
                # otherwise, retry in 4 hours
                self.logger("set time to {}".format(time.gmtime()))
                self.time_valid.set()
                await asyncio.sleep(14400)

    async def wait_ready(self):
        await self.time_valid.wait()


# Expose prometheus style metrics on port 9100
class MetricsServer:
    def __init__(self, controller, temp, therm, presence, motion):
        self.controller = controller
        self.temp = temp
        self.therm = therm
        self.presence = presence
        self.motion = motion

        self.logger = make_logger("metrics", "white")

    async def start_server(self):
        self.logger("waiting for dependencies")
        await asyncio.gather(
            self.temp.wait_ready(),
            self.therm.wait_ready(),
        )

        self.logger("starting server")
        await asyncio.start_server(self.handle_request, "0.0.0.0", 9100)

    async def handle_request(self, reader, writer):
        self.logger("handling http request")

        # read header
        while True:
            line = await reader.readline()
            if line == b"\r\n":
                break

        response = [
            "HTTP/1.1 200 OK",
            "Content-type: text/plain",
            "",
            "warmoffice_state {}".format(self.controller.state),
            "warmoffice_current_temperature {}".format(self.temp.values[0]),
            "warmoffice_target_temperature {}".format(self.therm.target_temp),
            "warmoffice_presence {}".format(self.presence.present),
            "warmoffice_motion_detected {}".format(self.motion.detections),
            "warmoffice_thermostat_active {}".format(self.therm.active),
            "warmoffice_thermostat_heating {}".format(self.therm.heating),
        ]

        for line in response:
            writer.write(line)
            writer.write("\n")
            await writer.drain()

        writer.close()
        await writer.wait_closed()


# Stiches together schedules, sensors, and the thermostat
#
# The controller runs in one of the following states:
#
# OFF - thermostat is not active, not responding to presence events
# IDLE1 - thermostat is not active, but presence will trigger transition
#     to TRACKING
# IDLE2 - thermostat is not active, but motion will trigger transition
#     to TRACKING
# TRACKING - thermostat is active, presence must be detected consistently
#     for max_presence_wait seconds to transition to ACTIVE, otherwise
#     transition back to IDLE
# ACTIVE - thermostat is active, presence must be detected within
#     max_idle_wait seconds or we transition back to IDLE
# PREWARM - thermostat is active for prewarm_wait seconds, after
#     which we transition to ACTIVE if presence is detected, otherwise
#     IDLE
class Controller:
    def __init__(
        self,
        switch_addr,
        max_idle_wait=1800,
        max_presence_wait=600,
        min_time_present=300,
        prewarm_wait=5400,
        motion_pin=4,
        temp_pin=5,
        # fmt: off
        schedules=(
            Schedule("prewarm", 18, 10, 30),
            Schedule("idle2",   20, 12, 00),
        ),
        # fmt: on
    ):
        self.temp = Temperature(temp_pin)
        self.switch = Switch(switch_addr)
        self.therm = Thermostat(self.temp, self.switch)
        self.clock = Clock()
        self.motion = Motion(motion_pin)
        self.presence = Presence(self.motion)

        self.metrics = MetricsServer(
            self,
            self.temp,
            self.therm,
            self.presence,
            self.motion,
        )

        # start in state OFF; transition to any other state happens
        # via the schedule
        self.state = State.OFF
        self.state_start = 0.0

        self.max_presence_wait = max_presence_wait
        self.min_time_present = min_time_present
        self.max_idle_wait = max_idle_wait
        self.prewarm_wait = prewarm_wait

        self.schedules = schedules

        # used to synchronize the scheduler-initiated state
        # changes with the main loop
        self.lock = asyncio.Lock()

        self.logger = make_logger("control", "white")

    # change state and record the time of the transition (we use this
    # e.g. in PREWARM so that we know how long we've been running in
    # the current state)
    def change_state(self, new):
        self.logger(
            "state {} -> {}".format(State.to_string(self.state), State.to_string(new))
        )
        self.state = new
        self.state_start = time.time()

    async def scheduler(self):
        self.logger("waiting for valid time 🕗")
        await self.clock.wait_ready()

        now_comp = time_as_minutes(time.gmtime())

        # figure out what schedule period we should be in
        # *right now* to determine our initial state
        a = (now_comp - self.schedules[0].as_minutes()) % 1440
        for i in range(len(self.schedules)):
            b = (self.schedules[i].as_minutes() - self.schedules[0].as_minutes()) % 1440
            if a < b:
                selected = i
                break
        else:
            selected = 0

        current = (selected - 1) % len(self.schedules)
        schedule = self.schedules[current]

        while True:
            self.logger("scheduler selecting {}".format(schedule))
            async with self.lock:
                self.change_state(State.from_string(schedule.state))
                self.therm.set_target_temp(schedule.temp)

            current = (current + 1) % len(self.schedules)
            schedule = self.schedules[current]

            # figure out how long until the start of the next schedule
            # and sleep an appropriate amount of time
            now_comp = time_as_minutes(time.gmtime())
            next_comp = schedule.as_minutes()
            delta = (next_comp - now_comp) % 1440
            self.logger("minutes until next schedule ({}): {}".format(schedule, delta))
            await asyncio.sleep(delta * 60)

    # simple http server that presents prometheus-style metrics
    async def loop(self):
        asyncio.create_task(self.clock.loop())
        asyncio.create_task(self.presence.loop())
        asyncio.create_task(self.temp.loop())
        asyncio.create_task(self.therm.loop())
        asyncio.create_task(self.scheduler())
        asyncio.create_task(self.metrics.start_server())

        prev_state = State.INIT
        last_log = time.time()
        last_present = 0.0
        start_presence = 0.0

        while True:
            async with self.lock:

                # log current state no more often than 1/minute
                if time.time() - last_log > 60:
                    self.logger(
                        "state = {}, state_time = {}, time = {}".format(
                            State.to_string(self.state),
                            time.time() - self.state_start,
                            time.localtime(),
                        )
                    )
                    last_log = time.time()

                state_at_loop_start = self.state

                if self.state == State.IDLE1:
                    if prev_state != State.IDLE1:
                        self.therm.control_deactivate()

                    if self.presence.present:
                        # switch to TRACKING state when presence is detected
                        self.change_state(State.TRACKING)
                elif self.state == State.IDLE2:
                    if prev_state != State.IDLE2:
                        self.therm.control_deactivate()

                        # clear persistent motion flag
                        self.motion.was_motion()

                    if self.motion.was_motion():
                        # switch to TRACKING state when any motion is detected
                        self.change_state(State.TRACKING)
                    elif time.time() - self.state_start > self.max_presence_wait:
                        # switch to IDLE1 after max_presence_wait
                        self.change_state(State.IDLE1)
                elif self.state == State.TRACKING:
                    if prev_state != State.TRACKING:
                        self.therm.control_activate()

                    if time.time() - self.state_start > self.max_presence_wait:
                        self.change_state(State.IDLE2)
                    elif self.presence.present:
                        if start_presence == 0.0:
                            start_presence = time.time()
                        elif time.time() - start_presence > self.min_time_present:
                            self.change_state(State.ACTIVE)
                    else:
                        start_presence = 0.0
                elif self.state == State.ACTIVE:
                    if prev_state != State.ACTIVE:
                        self.therm.control_activate()

                        # This is a NOOP if we got here from State.TRACKING
                        # or from State.PREWARM. The only other way to get
                        # here is via a schedule, in which case we don't
                        # want to turn off immediately.
                        last_present = time.time()

                    if self.presence.present:
                        last_present = time.time()
                    else:
                        if time.time() - last_present > self.max_idle_wait:
                            self.change_state(State.IDLE2)
                elif self.state == State.OFF:
                    if prev_state != State.OFF:
                        self.therm.control_deactivate()
                elif self.state == State.PREWARM:
                    if prev_state != State.PREWARM:
                        self.therm.control_activate()

                    if time.time() - self.state_start > self.prewarm_wait:
                        self.change_state(State.IDLE2)
                    elif self.presence.present:
                        self.change_state(State.ACTIVE)

                prev_state = state_at_loop_start

            await asyncio.sleep(1)

    def run(self):
        try:
            self.motion.start_motion_sensor()
            asyncio.run(self.loop())
        except KeyboardInterrupt:
            pass
        finally:
            self.motion.stop_motion_sensor()
            asyncio.run(self.switch.turn_off())
