"""Waveshare Pico-ePaper-2.13 V4 normal full refresh only.

Pin mapping follows the supplied V4 reference; SCK/MOSI require board check.
"""

import time

from protocol import landscape_buffer


class PanelError(Exception):
    pass


class Panel:
    def __init__(self):
        from machine import Pin, SPI
        self.rst = Pin(12, Pin.OUT)
        self.busy = Pin(13, Pin.IN, Pin.PULL_UP)
        self.cs = Pin(9, Pin.OUT)
        # Match the proven V4 reference: SPI1 first, then reclaim GP8 for DC.
        self.spi = SPI(1)
        self.spi.init(baudrate=4_000_000)
        self.dc = Pin(8, Pin.OUT)
        self.cs.value(1)

    def _send(self, command, data=None):
        self.dc.value(0)
        self.cs.value(0)
        self.spi.write(bytes((command,)))
        self.cs.value(1)
        if data is not None:
            self.dc.value(1)
            self.cs.value(0)
            self.spi.write(bytes(data))
            self.cs.value(1)

    def _wait(self, deadline):
        while self.busy.value() == 1:
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                raise PanelError("BUSY timeout")
            time.sleep_ms(10)

    def _reset(self):
        for level, delay in ((1, 20), (0, 2), (1, 20)):
            self.rst.value(level)
            time.sleep_ms(delay)

    def init(self, deadline):
        self._reset()
        time.sleep_ms(100)
        self._wait(deadline)
        self._send(0x12)
        self._wait(deadline)
        self._send(0x01, (0xf9, 0, 0))
        self._send(0x11, (0x07,))
        self._send(0x44, (0, 15))
        self._send(0x45, (0, 0, 249, 0))
        self._send(0x4e, (0,))
        self._send(0x4f, (0, 0))
        self._send(0x3c, (0x05,))
        self._send(0x21, (0, 0x80))
        self._send(0x18, (0x80,))
        self._wait(deadline)

    def display(self, wire, deadline):
        buffer = landscape_buffer(wire)
        # Reproduce the supplied Landscape driver's byte order exactly.
        ordered = bytearray(4000)
        n = 0
        for j in range(15, -1, -1):
            for i in range(250):
                ordered[n] = buffer[i + j * 250]
                n += 1
        # This V4 board's working reference toggles CS for each RAM byte.
        self._send(0x24)
        self.dc.value(1)
        one = bytearray(1)
        for value in ordered:
            one[0] = value
            self.cs.value(0)
            self.spi.write(one)
            self.cs.value(1)
        self._send(0x22, (0xf7,))
        self._send(0x20)
        self._wait(deadline)

    def sleep(self, deadline):
        self._wait(deadline)
        self._send(0x10, (0x01,))
        time.sleep_ms(100)
