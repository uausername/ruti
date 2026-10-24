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
            if not wait_for(out, card, EXPLAIN_S):
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
