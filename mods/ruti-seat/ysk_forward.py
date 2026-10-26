"""Sends a "You should know" card to the main chat, for the ruti-seat mod in flow mode.

The card comes as a teaser (`1: Learn more  2: Knew this already ...`); `1` writes the
explanation (`1: Understood  2: Chat in main session  0: Dismiss`), `2` puts it in the
chat, Enter sends it. This presses exactly those keys, each only while the screen shows
the row that key answers; anything else on screen and it writes nothing.

    python -I ysk_forward.py [claude-pid]     # without a pid: relaunch detached, return

Prints one line: what it did.
"""

import ctypes
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import effort_confirm as ec  # noqa: E402

TEASER = ("1: Learn more", "2: Knew this already")
CARD = "2: Chat in main session"
LOOK_S = 4.0       # a teaser is either on screen at turn end or not
EXPLAIN_S = 90.0   # the explanation is written by a model ("One moment...")


def press(inp, char: str, vk: int) -> bool:
    records = (ec.INPUT_RECORD * 2)()
    for i, down in enumerate((True, False)):
        records[i].EventType = ec.KEY_EVENT
        key = records[i].Event.KeyEvent
        key.bKeyDown, key.wRepeatCount, key.wVirtualKeyCode = down, 1, vk
        key.uChar, key.dwControlKeyState = char, 0
    written = ec.wt.DWORD()
    return bool(ec.k32.WriteConsoleInputW(inp, records, 2, ctypes.byref(written)))


class MOUSE_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("dwMousePosition", ec.COORD), ("dwButtonState", ec.wt.DWORD),
                ("dwControlKeyState", ec.wt.DWORD), ("dwEventFlags", ec.wt.DWORD)]


MOUSE_EVENT, MOUSE_WHEELED = 0x2, 0x4
SCROLL_STEPS = 12


def wheel_down(inp, out) -> bool:
    """One notch down, over the card's area (the bottom rows, where it is drawn)."""
    info = ec.CSBI()
    ec.k32.GetConsoleScreenBufferInfo(out, ctypes.byref(info))
    record = (ec.INPUT_RECORD * 1)()
    record[0].EventType = MOUSE_EVENT
    mouse = ctypes.cast(ctypes.byref(record[0].Event), ctypes.POINTER(MOUSE_EVENT_RECORD)).contents
    mouse.dwMousePosition = ec.COORD(info.dwSize.X // 2, max(info.srWindow.Top, info.srWindow.Bottom - 4))
    mouse.dwButtonState = (-120 & 0xFFFF) << 16  # high word: wheel delta, negative = down
    mouse.dwEventFlags = MOUSE_WHEELED
    written = ec.wt.DWORD()
    return bool(ec.k32.WriteConsoleInputW(inp, record, 1, ctypes.byref(written)))


def scroll_to_card(inp, out) -> bool:
    """The card's buttons sit below its own scroll area and a hidden `2` does not take
    the key: scroll down a notch at a time (the wheel, then the arrow) until it shows."""
    for step in range(SCROLL_STEPS * 2):
        if card(ec.screen_tail(out)):
            return True
        if step < SCROLL_STEPS:
            wheel_down(inp, out)
        else:
            press(inp, chr(0), 0x28)  # VK_DOWN
        time.sleep(0.25)
    return card(ec.screen_tail(out))


def wait_for(out, test, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if test(ec.screen_tail(out)):
            return True
        time.sleep(ec.POLL)
    return False


def teaser(lines): return any(all(t in line for t in TEASER) for line in lines)
def card(lines): return any(CARD in line for line in lines)


def main() -> int:
    if len(sys.argv) < 2:
        pid = ec.claude_pid()
        if pid is None:
            print("no claude.exe above this process; nothing done")
            return 1
        # Detached: the mod's hook returns at once, this waits out the explanation.
        subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), str(pid)],
                         creationflags=0x00000008 | 0x00000200, close_fds=True)
        print(f"watching claude {pid} for a card")
        return 0
    pid = int(sys.argv[1])
    ec.k32.FreeConsole()
    if not ec.k32.AttachConsole(pid):
        return 1
    try:
        out = ec.k32.CreateFileW("CONOUT$", ec.GENERIC_RW, ec.SHARE_RW, None, ec.OPEN_EXISTING, 0, None)
        inp = ec.k32.CreateFileW("CONIN$", ec.GENERIC_RW, ec.SHARE_RW, None, ec.OPEN_EXISTING, 0, None)
        if out in (None, ec.INVALID) or inp in (None, ec.INVALID):
            return 1
        if not card(ec.screen_tail(out)):
            if not wait_for(out, teaser, LOOK_S) or not press(inp, "1", 0x31):
                return 1
            # The explanation is written once "One moment..." is gone from the screen.
            if not wait_for(out, lambda lines: not any("One moment" in l for l in lines)
                            and not teaser(lines), EXPLAIN_S):
                return 1
            if not scroll_to_card(inp, out):
                return 1
        if not press(inp, "2", 0x32):
            return 1
        time.sleep(1.5)
        if card(ec.screen_tail(out)):  # the card did not take the key: send nothing
            return 1
        return 0 if press(inp, "\r", 0x0D) else 1
    finally:
        ec.k32.FreeConsole()


if __name__ == "__main__":
    sys.exit(main())
