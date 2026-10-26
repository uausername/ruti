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
    """One notch down, over the card's text. The wheel scrolls only what is under the
    pointer: the bottom rows are the status line, where it scrolls nothing (seen on a
    live card, 2026-10-26). The card's `↓ N more` row marks its scroll area."""
    info = ec.CSBI()
    ec.k32.GetConsoleScreenBufferInfo(out, ctypes.byref(info))
    lines = ec.screen_tail(out)
    marks = [i for i, line in enumerate(lines) if "↓" in line and "more" in line]
    # screen_tail ends at the window's bottom row.
    from_bottom = len(lines) - 1 - marks[-1] + 2 if marks else len(lines) // 2
    record = (ec.INPUT_RECORD * 1)()
    record[0].EventType = MOUSE_EVENT
    mouse = ctypes.cast(ctypes.byref(record[0].Event), ctypes.POINTER(MOUSE_EVENT_RECORD)).contents
    mouse.dwMousePosition = ec.COORD(20, max(info.srWindow.Top, info.srWindow.Bottom - from_bottom))
    mouse.dwButtonState = (-120 & 0xFFFF) << 16  # high word: wheel delta, negative = down
    mouse.dwEventFlags = MOUSE_WHEELED
    written = ec.wt.DWORD()
    return bool(ec.k32.WriteConsoleInputW(inp, record, 1, ctypes.byref(written)))


def wait_for(out, test, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if test(ec.screen_tail(out)):
            return True
        time.sleep(ec.POLL)
    return False


# Any of these on screen means a dialog may hold the keys (a permission prompt, a
# question, the effort dialog): Enter would answer it with its default, an arrow would
# move its selection. Nothing is pressed while one shows.
DIALOG_MARKS = ("Do you want", "❯ 1.", "1. Yes", "Esc to cancel", "Allow ",
                "Change effort level?", "Enter to select", "Enter to confirm")


def dialog_up(lines) -> bool:
    return any(mark in line for line in lines for mark in DIALOG_MARKS)


def composer_text(lines) -> bool:
    """The composer holds text: its prompt row (`>` / `❯` in the input box) is not empty."""
    for line in lines:
        row = line.strip().strip("│").strip()
        for mark in ("> ", "❯ "):
            if row.startswith(mark) and row[len(mark):].strip():
                return True
    return False


def teaser(lines): return any(all(t in line for t in TEASER) for line in lines)
def card(lines): return any(CARD in line for line in lines)


LOG = os.path.join(os.path.expanduser("~"), ".claude", "dev-mods", "ysk_forward.log")
WATCH_S = 60.0     # a card's fork may finish after the turn that started it


def log(pid, what: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} claude {pid}: {what}\n")
    except OSError:
        pass


def card_up(lines) -> bool:
    """Some card is drawn: its buttons, or its header / scroll mark while they are hidden."""
    return teaser(lines) or card(lines) or any(
        line.lstrip().startswith("✦") or ("↓" in line and "more" in line) for line in lines)


def composer_empty(lines) -> bool:
    return any(line.strip().strip("│").strip() in (">", "❯") for line in lines)


def scroll_to(inp, out, test) -> bool:
    """The buttons sit below the card's own scroll area and a hidden one does not take
    its key: scroll down a notch at a time until `test` sees them. The wheel only: an
    arrow key would move the selection of any dialog that came up meanwhile."""
    for _ in range(SCROLL_STEPS):
        lines = ec.screen_tail(out)
        if test(lines):
            return True
        if dialog_up(lines):
            return False
        wheel_down(inp, out)
        time.sleep(0.25)
    return test(ec.screen_tail(out))


def forward(inp, out) -> str:
    """What it did, or why it stopped; "sent" only when Enter went in."""
    if not wait_for(out, card_up, WATCH_S):
        return "no card"
    if not card(ec.screen_tail(out)):
        if not scroll_to(inp, out, teaser):
            return "teaser buttons not found"
        lines = ec.screen_tail(out)
        if dialog_up(lines):
            return "a dialog is up"
        if not press(inp, "1", 0x31):
            return "pressing 1 failed"
        # The explanation is written once "One moment..." is gone from the screen.
        if not wait_for(out, lambda lines: not any("One moment" in l for l in lines)
                        and not teaser(lines), EXPLAIN_S):
            return "the explanation did not come"
        if not scroll_to(inp, out, card):
            return "card buttons not found"
    lines = ec.screen_tail(out)
    if dialog_up(lines):
        return "a dialog is up"
    # The card's text goes into the composer: never on top of what the user is typing,
    # and only then is "text in the composer" proof that it was the card's.
    if not composer_empty(lines):
        return "the composer holds the user's text"
    if not press(inp, "2", 0x32):
        return "pressing 2 failed"
    time.sleep(1.5)
    lines = ec.screen_tail(out)
    # Enter only for the card's text in the composer, and with no dialog up: a blind
    # Enter could approve whatever permission prompt came up meanwhile.
    if card(lines) or dialog_up(lines) or not composer_text(lines):
        return "2 did not put the card in the composer"
    return "sent" if press(inp, chr(13), 0x0D) else "pressing Enter failed"


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
    # One watcher per session: every turn end starts one, and two would press twice.
    lock = os.path.join(os.environ.get("TEMP", "."), f"ysk_forward-{pid}.lock")
    try:
        if time.time() - os.path.getmtime(lock) < WATCH_S + EXPLAIN_S + 30:
            return 0
    except OSError:
        pass
    open(lock, "w").close()
    ec.k32.FreeConsole()
    try:
        if not ec.k32.AttachConsole(pid):
            log(pid, "attach failed")
            return 1
        out = ec.k32.CreateFileW("CONOUT$", ec.GENERIC_RW, ec.SHARE_RW, None, ec.OPEN_EXISTING, 0, None)
        inp = ec.k32.CreateFileW("CONIN$", ec.GENERIC_RW, ec.SHARE_RW, None, ec.OPEN_EXISTING, 0, None)
        if out in (None, ec.INVALID) or inp in (None, ec.INVALID):
            log(pid, "no console handles")
            return 1
        what = forward(inp, out)
        if what != "no card":
            log(pid, what)
        return 0 if what == "sent" else 1
    finally:
        ec.k32.FreeConsole()
        try:
            os.remove(lock)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
