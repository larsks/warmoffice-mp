import ds18x20
import machine
import micropython
import ntptime
import onewire
import time
import uasyncio as asyncio
import urequests as requests

micropython.alloc_emergency_exception_buf(100)

STATE_INIT = 0
STATE_IDLE = 1
STATE_TRACKING = 2
STATE_ACTIVE = 3
STATE_PREWARM = 4
STATE_OFF = 5

# These are used to generate pretty log output
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


# Return a logger that prefixes each line with a colored name tag
def make_logger(name, color):

    if color not in colors:
        raise ValueError(color)

    def _logger(msg):
        print("[{}{}{}]: {}".format(colors[color], name, colors["reset"], msg))

    return _logger


class Temperature:
    def __init__(self, pin, read_interval=30):
        self.ds = ds18x20.DS18X20(onewire.OneWire(machine.Pin(pin)))
        self.values = []
        self.read_interval = read_interval
        self.logger = make_logger("temp", "yellow")

        self.discover()

    def discover(self):
        self.roms = self.ds.scan()
        self.values = [None] * len(self.roms)
        self.logger("found {} sensors".format(len(self.roms)))

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
            await asyncio.sleep(self.read_interval)


class Switch:
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
    def __init__(self, motion):
        self.present = False
        self.samples = bytearray(120)
        self.index = 0
        self.motion = motion
        self.logger = make_logger("presence", "green")

    async def loop(self):
        mindetects = 120
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

            self.logger(
                "detections: {} {} {}".format(mindetects, detections, maxdetects)
            )
            self.present = detections > 20

            await asyncio.sleep(1)


class Motion:
    def __init__(self, pin):
        self.pin = machine.Pin(pin)
        self.motion = 0
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
        self.logger("motion detected")
        self.motion = pin.value()
        if self.motion:
            self.motion_persist = 1

    def check(self):
        res = self.motion_persist
        self.motion_persist = 0
        return res


class Thermostat:
    def __init__(self, temp, switch, target_temp, max_delta=1.0, min_delta=0.5):
        self.temp = temp
        self.switch = switch
        self.target_temp = target_temp
        self.active = False
        self.heating = False

        self.max_delta = max_delta
        self.min_delta = min_delta

        self.logger = make_logger("therm", "red")

    async def loop(self):
        self.logger("waiting for temperature")
        while self.temp.values[0] is None:
            await asyncio.sleep(1)

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


class Controller:
    def __init__(
        self,
        switch_addr,
        max_idle_wait=1800,
        max_presence_wait=300,
        prewarm_wait=5400,
        target_temp=22.0,
        motion_pin=4,
        temp_pin=5,
        schedule=(
            (STATE_PREWARM, 10, 30),
            (STATE_ACTIVE, 12, 00),
            (STATE_OFF, 4, 0),
        ),
    ):
        self.temp = Temperature(temp_pin)
        self.switch = Switch(switch_addr)
        self.therm = Thermostat(self.temp, self.switch, target_temp)

        self.motion = Motion(motion_pin)
        self.presence = Presence(self.motion)
        self.last_present = 0.0

        self.running = False
        self.state = STATE_OFF
        self.state_start = 0.0

        self.max_presence_wait = max_presence_wait
        self.max_idle_wait = max_idle_wait
        self.prewarm_wait = prewarm_wait

        self.time_valid = asyncio.Event()
        self.schedule = schedule
        self.lock = asyncio.Lock()

        self.logger = make_logger("control", "white")

    def change_state(self, new):
        self.logger("state {} -> {}".format(self.state, new))
        self.state = new
        self.state_start = time.time()

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
                await asyncio.sleep(7200)

    async def scheduler(self):
        now = time.gmtime()
        now_comp = (now[3] * 60) + now[4]
        mindelta = 1440
        selected = -1

        for i, event in enumerate(self.schedule):
            event_comp = (event[1] * 60) + event[2]
            delta = (event_comp - now_comp) % 1440
            if delta < mindelta:
                mindelta = delta
                selected = i

        current_schedule = self.schedule[(selected - 1) % len(self.schedule)]
        self.logger("current schedule: {}".format(current_schedule))
        async with self.lock:
            self.change_state(current_schedule[0])

        while True:
            now = time.gmtime()
            now_comp = (now[3] * 60) + now[4]
            next_schedule = self.schedule[selected]
            next_comp = (next_schedule[1] * 60) + next_schedule[2]
            delta = (next_comp - now_comp) % 1440
            self.logger("minutes until next event: {}".format(delta))
            await asyncio.sleep(delta * 60)
            async with self.lock:
                self.change_state(next_schedule[0])
                selected = (selected + 1) % len(self.schedule)

    async def loop(self):
        asyncio.create_task(self.clockset())
        asyncio.create_task(self.scheduler())
        asyncio.create_task(self.temp.loop())
        asyncio.create_task(self.therm.loop())
        asyncio.create_task(self.presence.loop())

        prev_state = STATE_INIT

        self.logger("waiting for valid time")
        await self.time_valid.wait()

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


c = Controller("192.168.1.156")
