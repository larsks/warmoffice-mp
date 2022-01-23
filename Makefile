PY_FILES = main.py thermostat.py
MPY_FILES = thermostat.mpy

%.mpy: %.py
	mpy-cross -o $@ $<

all: $(MPY_FILES)
