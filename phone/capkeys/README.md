# capkeys

Makes the three capacitive buttons under the screen work on the phone (dragonphone: Xiaomi Redmi Note 4X / mido,
postmarketOS, Phosh).

The ft5x06 touch controller does not report the buttons as keys. A press arrives as an ordinary touch with Y outside the
screen (Y = 2040 while the screen ends at 1919) and X = 200, 500 or 800 for the left, middle and right button.
`capkeys.py` reads `/dev/input/event1`, recognizes these touches and emits key presses through `uinput`.

| Button | X   | Keys         | Effect in Phosh                    |
|--------|-----|--------------|------------------------------------|
| left   | 200 | `Super+A`    | list of all apps                   |
| middle | 500 | `Super`      | overview (home)                    |
| right  | 800 | `Alt+Left`   | back (in apps that support it)     |

`Esc` and `Alt+Tab` did nothing in Phosh, so they are not used. Repeated presses of one button within 0.3 s are ignored
because the controller bounces. Change the mapping in `ZONES` at the top of `capkeys.py`.

## Installation

On the phone (needs root for `/dev/input` and `/dev/uinput`; python3 is the only dependency):

```sh
sudo sh install.sh            # copies the script, installs and starts capkeys.service
sudo systemctl restart capkeys   # after editing the script
```

Logs: `journalctl -u capkeys` (one line per recognized press). Remove: `sudo systemctl disable --now capkeys`.

`TOUCH_DEV` is `event1` on this phone; check `/proc/bus/input/devices` (`generic ft5x06`) if the number changes.
