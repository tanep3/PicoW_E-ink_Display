"""Operator-run USB transfer for the PUSH Pico program.

Backs up only program files. Never opens, copies or changes secrets.py.
Requires an explicit backup directory and a MicroPython Pico USB serial port.
"""

import argparse
import hashlib
import os
from pathlib import Path
import time

import serial
from serial.tools import list_ports

from upload_secrets import execute, read_until, remote_hash


SOURCE = Path(__file__).resolve().parent
FILES = ("protocol.py", "panel_v4.py", "config.py", "push_receiver.py", "main.py")


def device_names(device):
    result = execute(device, "import os;print('NAMES:'+repr(os.listdir('/')))")
    import ast
    names = ast.literal_eval(result.split(b"NAMES:", 1)[1].split(b"\r\n", 1)[0].decode("ascii"))
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise RuntimeError("Pico file list invalid")
    return names


def read_file(device, name):
    result = execute(device, "import os;print('SIZE:'+str(os.stat('%s')[6]))" % name)
    size = int(result.split(b"SIZE:", 1)[1].split(b"\r\n", 1)[0])
    if size < 0 or size > 65536:
        raise RuntimeError("Pico program size out of range: " + name)
    result = bytearray()
    for offset in range(0, size, 256):
        command = ("import ubinascii;f=open('%s','rb');f.seek(%d);"
                   "print('DATA:'+ubinascii.hexlify(f.read(256)).decode());f.close()") % (name, offset)
        output = execute(device, command, 20)
        result.extend(bytes.fromhex(output.split(b"DATA:", 1)[1].split(b"\r\n", 1)[0].decode()))
    if len(result) != size or hashlib.sha256(result).hexdigest() != remote_hash(device, name):
        raise RuntimeError("Pico backup integrity mismatch: " + name)
    return bytes(result)


def write_file(device, name, data):
    execute(device, "open('%s','wb').close()" % name)
    for offset in range(0, len(data), 128):
        chunk = data[offset:offset + 128].hex()
        execute(device, "import ubinascii;f=open('%s','ab');f.write(ubinascii.unhexlify('%s'));f.close()" %
                (name, chunk))
    if remote_hash(device, name) != hashlib.sha256(data).hexdigest():
        raise RuntimeError("Pico staged hash mismatch: " + name)


def run(port, backup_dir):
    matches = [item for item in list_ports.comports() if item.device == port]
    if len(matches) != 1 or matches[0].vid != 0x2E8A or matches[0].pid != 0x0005:
        raise RuntimeError("Expected one MicroPython Pico at the specified port")
    if backup_dir.exists():
        raise RuntimeError("Choose a new, empty private backup directory")
    backup_dir.mkdir(mode=0o700, parents=True)
    os.chmod(backup_dir, 0o700)
    local = {name: (SOURCE / name).read_bytes() for name in FILES}
    for name, data in local.items():
        compile(data, name, "exec")
    device = serial.Serial(port, 115200, timeout=0.1, write_timeout=2)
    changed = []
    names = []
    try:
        device.write(b"\r\x03\x03")
        time.sleep(0.2)
        device.reset_input_buffer()
        device.write(b"\r\x01")
        read_until(device, b">", 5)
        names = device_names(device)
        if not set(FILES[:-2] + ("main.py",)).issubset(names):
            raise RuntimeError("Expected installed Pico program files missing")
        for name in FILES:
            if name + ".new" in names or name + ".prepush" in names:
                raise RuntimeError("Pico staging or rollback file already exists: " + name)
        # Complete every backup before changing any executable file.
        for name in FILES:
            if name not in names:
                continue
            data = read_file(device, name)
            path = backup_dir / name
            with path.open("xb") as stream:
                stream.write(data)
            os.chmod(path, 0o600)
            print("Backed up", name, len(data), hashlib.sha256(data).hexdigest())
        for name in FILES:
            data = local[name]
            staged = name + ".new"
            write_file(device, staged, data)
            if name in names:
                execute(device, "import os;os.rename('%s','%s')" % (name, name + ".prepush"))
            try:
                execute(device, "import os;os.rename('%s','%s')" % (staged, name))
            except Exception:
                if name in names:
                    execute(device, "import os;os.rename('%s','%s')" % (name + ".prepush", name))
                raise
            changed.append(name)
            if remote_hash(device, name) != hashlib.sha256(data).hexdigest():
                raise RuntimeError("Pico installed hash mismatch: " + name)
            execute(device, "compile(open('%s').read(),'%s','exec')" % (name, name))
            print("Installed", name, len(data), hashlib.sha256(data).hexdigest())
        print("PUSH code transferred; old program copies retained as *.prepush")
    except Exception:
        # Best effort rollback while USB REPL remains available. The private
        # backup directory also supports manual recovery if USB is lost.
        for name in reversed(changed):
            try:
                if name in names:
                    execute(device, "import os;os.remove('%s');os.rename('%s','%s')" %
                            (name, name + ".prepush", name))
                else:
                    execute(device, "import os;os.remove('%s')" % name)
            except Exception:
                print("Manual rollback required for", name)
        raise
    finally:
        try:
            device.write(b"\x02")
            time.sleep(0.1)
            device.write(b"\rimport machine;machine.reset()\r")
        except Exception:
            pass
        device.close()


def main():
    parser = argparse.ArgumentParser(description="Back up and transfer Pico PUSH program")
    parser.add_argument("--port", required=True)
    parser.add_argument("--backup-dir", required=True, type=Path)
    args = parser.parse_args()
    run(args.port, args.backup_dir)


if __name__ == "__main__":
    main()
