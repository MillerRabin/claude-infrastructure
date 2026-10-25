#!/usr/bin/env python3
"""Map the ft5x06 capacitive buttons (touches with Y > 1919) to key presses."""
import fcntl, os, struct, sys, time

TOUCH_DEV = "/dev/input/event1"
SCREEN_MAX_Y = 1919
KEY_A, KEY_LEFT, KEY_LEFTALT, KEY_LEFTMETA = 30, 105, 56, 125
# (max X of zone, keys pressed together)
ZONES = [(350, [KEY_LEFTMETA, KEY_A]), (650, [KEY_LEFTMETA]), (9999, [KEY_LEFTALT, KEY_LEFT])]
DEBOUNCE = 0.3  # seconds; ignore repeated presses of the same button

EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
ABS_MT_SLOT, ABS_MT_X, ABS_MT_Y, ABS_MT_ID = 47, 53, 54, 57

UI_SET_EVBIT, UI_SET_KEYBIT = 0x40045564, 0x40045565
UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502


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
    all_keys = sorted({k for _, ks in ZONES for k in ks})
    ui = make_keyboard(all_keys)
    ts = open(TOUCH_DEV, "rb", buffering=0)
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
                    if y > SCREEN_MAX_Y:
                        keys = next(ks for mx, ks in ZONES if x < mx)
                        now = time.monotonic()
                        if now - last.get(tuple(keys), 0) < DEBOUNCE:
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
