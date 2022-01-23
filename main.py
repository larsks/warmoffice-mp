import micropython

from thermostat import Controller

micropython.alloc_emergency_exception_buf(100)

c = Controller("192.168.1.156")
c.run()
