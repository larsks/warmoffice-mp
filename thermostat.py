import ds18x20
import machine
import micropython
import ntptime
import onewire
import time
import uasyncio as asyncio
import urequests as requests

from collections import namedtuple

micropython.alloc_emergency_exception_buf(100)

STATE_INIT = 0
STATE_IDLE = 1
STATE_TRACKING = 2
STATE_ACTIVE = 3
STATE_PREWARM = 4
STATE_OFF = 5


def time_as_minutes(t):
    return (t[3] * 60) + t[4]


class Schedule(namedtuple("Schedule", ["state", "temp", "hour", "minute"])):
    def as_minutes(self):
        return (self.hour * 60) + self.minute

    def __str__(self):
        return "{}:{} T:{} S:{}".format(self.hour, self.minute, self.temp, self.state)


def make_logger(name, color):
    """Return a logger that prefixes each line with a colored name tag"""

    # ANSI color codes
    colors = {
        "black": "\u001b[30m",
        "red": "\u001b[31m",
        "green": "\u001b[32m",
        "yellow": "\u001b[33m",
        "blue": "\u001b[34m",
        "magenta": "\u001b[35m",
        "cyan": "\u001b[36m",
        "white": "\u001b[37m",
        "reset": "\u001b[0m",
    }

    if color not in colors:
        raise ValueError(color)

    def _logger(msg):
        print("[{}{}{}]: {}".format(colors[color], name, colors["reset"], msg))

    return _logger


class Temperature:
    """A class for reading DS18B20 temperature sensors.

    Reads sensors every read_interval seconds, makes the
    values available in self.values for consumers.
    """

    def __init__(self, pin, read_interval=30):
        self.ds = ds18x20.DS18X20(onewire.OneWire(machine.Pin(pin)))
        self.values = []
        self.valid_read_flag = asyncio.Event()
        self.read_interval = read_interval
        self.logger = make_logger("temp", "yellow")

        self.discover()

    def discover(self):
        while True:
            self.roms = self.ds.scan()
            self.values = [None] * len(self.roms)
            self.logger("found {} sensors".format(len(self.roms)))

            if len(self.roms) > 0:
                break

            time.sleep(5)

    async def loop(self):
        self.logger("start temperature loop ({})".format(len(self.roms)))
        while True:
            self.ds.convert_temp()

            # We have to wait 750ms before reading the temperature; see
            # https://datasheets.maximintegrated.com/en/ds/DS18B20.pdf
            await asyncio.sleep_ms(750)
            for i, rom in enumerate(self.roms):
                self.values[i] = self.ds.read_temp(rom)

            self.logger("read temperature: {}".format(self.values))
            self.valid_read_flag.set()
            await asyncio.sleep(self.read_interval)


class Switch:
    """Interface to a Tasmota [1] switch

    [1]: https://tasmota.github.io/docs/
    """

    def __init__(self, addr):
        self.url = "http://{addr}/cm".format(addr=addr)
        self.logger = make_logger("switch@{}".format(addr), "blue")

    def turn_on(self):
        self.logger("turn on")
        requests.get("{url}?cmnd=Power%20On".format(url=self.url))

    def turn_off(self):
        self.logger("turn off")
        requests.get("{url}?cmnd=Power%20Off".format(url=self.url))

    def is_on(self):
        res = requests.get("{url}?cmnd=Power%20Status".format(url=self.url))
        data = res.json()
        self.logger("current status = {}".format(data["POWER"]))
        return data["power"] == "ON"


class Presence:
    """Converts motion detection events into presence detection.

    To detect "present", in detect_interval seconds there must be
    at least min_detects second during which motion is detected.
    """

    def __init__(self, motion, min_detect=20, detect_interval=120):
        self.present = False
        self.min_detect = min_detect
        self.detect_interval = detect_interval
        self.samples = bytearray(detect_interval)
        self.index = 0
        self.motion = motion
        self.logger = make_logger("presence", "green")

    async def loop(self):
        mindetects = self.detect_interval
        maxdetects = 0

        self.logger("start presence loop")
        while True:
            self.samples[self.index] = self.motion.motion

            self.index += 1
            if self.index >= len(self.samples):
                self.index = 0
                mindetects = len(self.samples)
                maxdetects = 0

            detections = sum(self.samples)
            if detections > maxdetects:
                maxdetects = detections
            if detections < mindetects:
                mindetects = detections

            self.present = detections > self.min_detect
            self.logger(
                "detections {} {} {} present {}".format(
                    mindetects, detections, maxdetects, self.present
                )
            )

            await asyncio.sleep(1)


class Motion:
    """Respond to events from a standard IR motion detector"""

    def __init__(self, pin):
        self.pin = machine.Pin(pin)

        # motion is 1 when the motion detector is indicating motion,
        # 0 when not
        self.motion = 0

        # motion_persist is set to 1 when the motion detect indicates
        # motion, and is only set to 0 when someone calls the check method.
        # This allows you to ask, "has any motion been detected since I
        # last checked?"
        self.motion_persist = 0
        self.logger = make_logger("motion", "cyan")

    def start_motion_sensor(self):
        self.pin.irq(
            handler=self.motion_detected,
            trigger=machine.Pin.IRQ_RISING | machine.Pin.IRQ_FALLING,
        )

    def stop_motion_sensor(self):
        self.pin.irq(handler=None)

    def motion_detected(self, pin):
        self.motion = pin.value()
        if self.motion:
            self.logger("motion detected")
            self.motion_persist = 1
        else:
            self.logger("no motion detected")

    def check(self):
        res = self.motion_persist
        self.motion_persist = 0
        return res


class Thermostat:
    """Control a remote switch in response to a temperature sensor"""

    def __init__(self, temp, switch, max_delta=1.0, min_delta=0.5):
        self.temp = temp
        self.switch = switch
        self.target_temp = None
        self.target_temp_flag = asyncio.Event()

        # self.active tracks whether or not we are actively managing
        # the remote switch
        self.active = False

        # self.heating indicates whether or not the remote switch is on
        self.heating = False

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
        self.logger("waiting for temperature sensor and configuration")
        await asyncio.gather(
            self.target_temp_flag.wait(), self.temp.valid_read_flag.wait()
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
                if not self.heating and delta > self.max_delta:
                    self.heat_on()
                elif self.heating and delta < self.min_delta:
                    self.heat_off()
            elif self.heating:
                self.heat_off()

            await asyncio.sleep(10)

    def heat_on(self):
        self.logger("FLAME ON")
        self.heating = True
        self.switch.turn_on()

    def heat_off(self):
        self.logger("FLAME OFF")
        self.heating = False
        self.switch.turn_off()

    def control_activate(self):
        self.logger("heat control on")
        self.active = True

    def control_deactivate(self):
        self.logger("heat control off")
        self.active = False

    def set_target_temp(self, target_temp):
        self.target_temp = target_temp
        self.target_temp_flag.set()


class Controller:
    """Stiches together schedules, sensors, and the thermostat

    The controller runs in one of the following states:

    OFF - thermostat is not active, not responding to presence events
    IDLE - thermostat is not active, but presence will trigger transition
        to TRACKING
    TRACKING - thermostat is active, presence must be detected consistently
        for max_presence_wait seconds to transition to ACTIVE, otherwise
        transition back to IDLE
    ACTIVE - thermostat is active, presence must be detected within
        max_idle_wait seconds or we transition back to IDLE
    PREWARM - thermostat is active for prewarm_wait seconds, after
        which we transition to ACTIVE if presence is detected, otherwise
        IDLE
    """

    def __init__(
        self,
        switch_addr,
        max_idle_wait=1800,
        max_presence_wait=300,
        prewarm_wait=5400,
        motion_pin=4,
        temp_pin=5,
        schedules=(
            Schedule(STATE_PREWARM, 18, 10, 30),
            Schedule(STATE_ACTIVE, 22, 12, 00),
            Schedule(STATE_OFF, 0, 4, 0),
        ),
    ):
        self.temp = Temperature(temp_pin)
        self.switch = Switch(switch_addr)
        self.therm = Thermostat(self.temp, self.switch)

        self.motion = Motion(motion_pin)
        self.presence = Presence(self.motion)
        self.last_present = 0.0

        # start in state OFF; transition to any other state happens
        # via the schedule
        self.state = STATE_OFF
        self.state_start = 0.0

        self.max_presence_wait = max_presence_wait
        self.max_idle_wait = max_idle_wait
        self.prewarm_wait = prewarm_wait

        self.time_valid = asyncio.Event()
        self.schedules = schedules
        self.lock = asyncio.Lock()

        self.logger = make_logger("control", "white")

    # change state and record the time of the transition (we use this
    # e.g. in PREWARM so that we know how long we've been running in
    # the current state)
    def change_state(self, new):
        self.logger("state {} -> {}".format(self.state, new))
        self.state = new
        self.state_start = time.time()

    # occasionally set the clock via ntp
    async def clockset(self):
        while True:
            self.logger("setting time")
            try:
                ntptime.settime()
            except OSError:
                self.logger("failed to set time")
                await asyncio.sleep(10)
            else:
                self.time_valid.set()
                await asyncio.sleep(14400)

    async def scheduler(self):
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
                self.change_state(schedule.state)
                self.therm.set_target_temp(schedule.temp)

            current += 1
            schedule = self.schedules[current]

            now_comp = time_as_minutes(time.gmtime())
            next_comp = schedule.as_minutes()
            delta = (next_comp - now_comp) % 1440
            self.logger("minutes until next schedule ({}): {}".format(schedule, delta))
            await asyncio.sleep(delta * 60)

    async def loop(self):
        asyncio.create_task(self.clockset())
        asyncio.create_task(self.temp.loop())
        asyncio.create_task(self.therm.loop())
        asyncio.create_task(self.presence.loop())

        # we require a valid time for the scheduler to work correctly
        # so wait until we're able to set it successfully (or maybe
        # someday we'll spend the big bucks for an RTC)
        self.logger("waiting for valid time")
        await self.time_valid.wait()
        asyncio.create_task(self.scheduler())

        prev_state = STATE_INIT

        while True:
            async with self.lock:
                self.logger("state = {}, time= {}".format(self.state, time.localtime()))
                state_at_loop_start = self.state

                if self.state == STATE_IDLE:
                    if prev_state != STATE_IDLE:
                        self.therm.control_deactivate()

                    if self.motion.check():
                        self.change_state(STATE_TRACKING)
                elif self.state == STATE_TRACKING:
                    if prev_state != STATE_TRACKING:
                        self.therm.control_activate()

                    if time.time() - self.state_start > self.max_presence_wait:
                        self.change_state(STATE_IDLE)
                    elif self.presence.present:
                        self.change_state(STATE_ACTIVE)
                elif self.state == STATE_ACTIVE:
                    if prev_state != STATE_ACTIVE:
                        self.therm.control_activate()

                        # This is a NOOP if we got here from STATE_TRACKING
                        # or from STATE_PREWARM. The only other way to get
                        # here is via a schedule, in which case we don't
                        # want to turn off immediately.
                        self.last_present = time.time()

                    if self.presence.present:
                        self.last_present = time.time()
                    else:
                        if time.time() - self.last_present > self.max_idle_wait:
                            self.change_state(STATE_IDLE)
                elif self.state == STATE_OFF:
                    if prev_state != STATE_OFF:
                        self.therm.control_deactivate()
                elif self.state == STATE_PREWARM:
                    if prev_state != STATE_PREWARM:
                        self.therm.control_activate()

                    if time.time() - self.state_start > self.prewarm_wait:
                        self.change_state(STATE_IDLE)
                    elif self.presence.present:
                        self.change_state(STATE_ACTIVE)

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
            self.switch.turn_off()
