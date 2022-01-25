PY_FILES = main.py thermostat.py
MPY_FILES = thermostat.mpy

%.mpy: %.py
	mpy-cross -o $@ $<

%.svg: %.dot
	dot -Tsvg -o $@ $<

all: $(MPY_FILES)
