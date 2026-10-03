"""User-run USB transfer of pico/secrets.py to an attached MicroPython Pico W.

Credentials are transmitted only to the Pico, never printed. Existing device
secrets are never read or replaced. Run only when the user chooses to do so.
"""

import argparse
import hashlib
from pathlib import Path
import time

import serial
from serial.tools import list_ports


SOURCE = Path(__file__).with_name("secrets.py")


def read_until(device, marker, seconds=8):
    deadline = time.monotonic() + seconds
    data = bytearray()
    while marker not in data:
        if time.monotonic() >= deadline or len(data) > 8192:
            raise TimeoutError("Pico response missing")
        data.extend(device.read(512))
    return bytes(data)


def execute(device, command, seconds=12):
    device.reset_input_buffer()
    device.write(command.encode("ascii") + b"\x04")
    reply = read_until(device, b"\x04\x04>", seconds)
    if not reply.startswith(b"OK"):
        raise RuntimeError("Pico command rejected")
    parts = reply[2:].split(b"\x04", 2)
    if len(parts) < 2 or parts[1]:
        raise RuntimeError("Pico command failed")
    return parts[0]


def remote_hash(device, name):
    result = execute(device,
        "import uhashlib,ubinascii;print('SHA:'+ubinascii.hexlify(uhashlib.sha256(open('%s','rb').read()).digest()).decode())" % name)
    return result.split(b"SHA:", 1)[1].split(b"\r\n", 1)[0].decode("ascii")


def main():
    parser = argparse.ArgumentParser(description="Transfer your Wi-Fi file to the Pico W")
    parser.add_argument("--port", required=True, help="MicroPython USB serial port")
    args = parser.parse_args()
    matching = [item for item in list_ports.comports() if item.device == args.port]
    if len(matching) != 1 or matching[0].vid != 0x2E8A or matching[0].pid != 0x0005:
        raise SystemExit("The requested port is not the expected MicroPython Pico")
    data = SOURCE.read_bytes()
    if not data or len(data) > 4096:
        raise SystemExit("Local pico/secrets.py is empty or too large")
    expected = hashlib.sha256(data).hexdigest()
    device = serial.Serial(args.port, 115200, timeout=0.1, write_timeout=2)
    try:
        device.write(b"\r\x03\x03")
        time.sleep(0.2)
        device.reset_input_buffer()
        device.write(b"\r\x01")
        read_until(device, b">", 5)
        listing = execute(device, "import os;print('HAS:'+str('secrets.py' in os.listdir('/') or 'secrets.py.new' in os.listdir('/')))")
        if b"HAS:True" in listing:
            raise RuntimeError("Device Wi-Fi file already exists; refusing to overwrite")
        execute(device, "open('secrets.py.new','wb').close()")
        for offset in range(0, len(data), 128):
            chunk = data[offset:offset + 128].hex()
            execute(device, "import ubinascii;f=open('secrets.py.new','ab');f.write(ubinascii.unhexlify('%s'));f.close()" % chunk)
        if remote_hash(device, "secrets.py.new") != expected:
            raise RuntimeError("USB transfer integrity check failed")
        execute(device, "import os;os.rename('secrets.py.new','secrets.py')")
        if remote_hash(device, "secrets.py") != expected:
            raise RuntimeError("Installed file integrity check failed")
        print("Pico Wi-Fi file installed and SHA-256 verified; no credential values shown")
    finally:
        try:
            device.write(b"\x02")
            time.sleep(0.1)
            device.write(b"\rimport machine;machine.reset()\r")
        except Exception:
            pass
        device.close()


if __name__ == "__main__":
    main()
