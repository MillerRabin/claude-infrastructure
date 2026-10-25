#!/usr/bin/env python3
"""Map the ft5x06 capacitive buttons (touches below the screen) to key presses."""
import fcntl, os, struct, sys, time, tomllib

CONFIG = os.environ.get("CAPKEYS_CONFIG", "/etc/capkeys.conf")

KEYS = {
    "ESC": 1, "TAB": 15, "ENTER": 28, "LEFTCTRL": 29, "A": 30, "LEFTSHIFT": 42, "LEFTALT": 56, "SPACE": 57,
    "F4": 62, "HOME": 102, "UP": 103, "LEFT": 105, "RIGHT": 106, "DOWN": 108, "LEFTMETA": 125, "MENU": 139,
    "BACK": 158, "HOMEPAGE": 172, "APPSELECT": 580,
}

EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
ABS_MT_SLOT, ABS_MT_X, ABS_MT_Y, ABS_MT_ID = 47, 53, 54, 57

UI_SET_EVBIT, UI_SET_KEYBIT = 0x40045564, 0x40045565
UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502


def load_config():
    with open(CONFIG, "rb") as f:
        cfg = tomllib.load(f)
    zones = []
    for b in sorted(cfg["buttons"], key=lambda b: b["max_x"]):
        keys = [k if isinstance(k, int) else KEYS[k.upper()] for k in b["keys"]]
        zones.append((b["max_x"], keys))
    return {
        "device_name": cfg.get("device_name", "generic ft5x06"),
        "screen_max_y": cfg.get("screen_max_y", 1919),
        "debounce": cfg.get("debounce", 0.3),
        "zones": zones,
    }


def find_device(name):
    """Return /dev/input/eventN of the first input device whose name contains `name`."""
    with open("/proc/bus/input/devices") as f:
        for block in f.read().split("\n\n"):
            if f'Name="' in block and name in block.split('Name="', 1)[1].split('"', 1)[0]:
                for token in block.split("Handlers=", 1)[1].split():
                    if token.startswith("event"):
                        return "/dev/input/" + token
    raise SystemExit("input device %r not found" % name)


def make_keyboard(keys):
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    for k in keys:
        fcntl.ioctl(fd, UI_SET_KEYBIT, k)
    dev = struct.pack("80sHHHHi", b"capkeys", 0x06, 0x1, 0x1, 1, 0) + bytes(4 * 64 * 4)
    os.write(fd, dev)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    return fd


def emit(fd, keys, value):
    # One SYN per key so modifier combos are seen as sequential presses
    for k in keys:
        os.write(fd, struct.pack("llHHi", 0, 0, EV_KEY, k, value))
        os.write(fd, struct.pack("llHHi", 0, 0, EV_SYN, 0, 0))
        time.sleep(0.02)


def main():
    cfg = load_config()
    zones, max_y, debounce = cfg["zones"], cfg["screen_max_y"], cfg["debounce"]
    dev = find_device(cfg["device_name"])
    print("reading %s (%s)" % (dev, cfg["device_name"]), flush=True)
    ui = make_keyboard(sorted({k for _, ks in zones for k in ks}))
    ts = open(dev, "rb", buffering=0)
    slot = 0
    pos = {}       # slot -> [x, y]; last reported values persist between touches
    new = set()    # slots with a new touch awaiting SYN
    active = {}    # slot -> keys currently held
    last = {}      # zone keys -> time of last accepted press
    try:
        while True:
            data = ts.read(24)
            if len(data) < 24:
                break
            _, _, t, c, v = struct.unpack("llHHi", data)
            if t == EV_ABS:
                if c == ABS_MT_SLOT:
                    slot = v
                elif c == ABS_MT_X:
                    pos.setdefault(slot, [0, 0])[0] = v
                elif c == ABS_MT_Y:
                    pos.setdefault(slot, [0, 0])[1] = v
                elif c == ABS_MT_ID:
                    if v == -1:
                        keys = active.pop(slot, None)
                        if keys:
                            emit(ui, reversed(keys), 0)
                    else:
                        new.add(slot)
            elif t == EV_SYN and new:
                for s in new:
                    x, y = pos.get(s, [0, 0])
                    if y > max_y:
                        keys = next((ks for mx, ks in zones if x < mx), zones[-1][1])
                        now = time.monotonic()
                        if now - last.get(tuple(keys), 0) < debounce:
                            continue
                        last[tuple(keys)] = now
                        active[s] = keys
                        print("button x=%d keys=%s" % (x, keys), flush=True)
                        emit(ui, keys, 1)
                new.clear()
    finally:
        fcntl.ioctl(ui, UI_DEV_DESTROY)


if __name__ == "__main__":
    sys.exit(main())
