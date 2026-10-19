"""Answers Claude Code's "Change effort level?" dialog for the ruti-seat mod -- and nothing else.

`/effort` run from a mod brings up the dialog a typed one does: "This conversation is
cached for the current effort level. Switching to <level> means the full history gets
re-read". On Opus 5.5 and Sonnet 5.5 that is not so -- the transcripts show the whole
history read from cache on the request after the switch -- so the question has one
answer, and the mod should not wait on a person for it.

The mod starts this just before it runs `/effort <level>`. It attaches to the console of
the claude.exe it descends from, reads the screen until the dialog is up, and only when
the screen shows that dialog, for that level, with "Yes" under the cursor, writes the key
`1` into that console's input. Anything else on screen -- a permission prompt, another
dialog, the composer -- and it writes nothing. It gives up after a timeout.

    python -I effort_confirm.py <level> [timeout-seconds]

Prints one line: what it did. Exit 0 when it answered, 1 when it did not.
"""

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

LEVELS = ("low", "medium", "high", "xhigh", "max")
TITLE = "Change effort level?"
POLL = 0.15
# The dialog is drawn at the bottom; this many screen lines are searched for it.
TAIL = 15


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                ("szExeFile", wt.WCHAR * 260)]


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class CSBI(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD), ("wAttributes", wt.WORD),
                ("srWindow", SMALL_RECT), ("dwMaximumWindowSize", COORD)]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wt.BOOL), ("wRepeatCount", wt.WORD),
                ("wVirtualKeyCode", wt.WORD), ("wVirtualScanCode", wt.WORD),
                ("uChar", wt.WCHAR), ("dwControlKeyState", wt.DWORD)]


class _EVENT(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_pad", ctypes.c_byte * 16)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wt.WORD), ("Event", _EVENT)]


KEY_EVENT = 0x1
GENERIC_RW = 0xC0000000
SHARE_RW = 0x3
OPEN_EXISTING = 3
INVALID = ctypes.c_void_p(-1).value

k32.CreateFileW.restype = wt.HANDLE


def claude_pid() -> int | None:
    """The nearest claude.exe above this process: the session the mod runs in."""
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    procs = {}
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        procs[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile.lower())
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    pid = os.getpid()
    for _ in range(20):
        if pid not in procs:
            return None
        parent, exe = procs[pid]
        if exe == "claude.exe":
            return pid
        pid = parent
    return None


def screen_tail(out) -> list[str]:
    info = CSBI()
    if not k32.GetConsoleScreenBufferInfo(out, ctypes.byref(info)):
        return []
    win = info.srWindow
    width = info.dwSize.X
    lines = []
    for y in range(max(win.Top, win.Bottom - TAIL + 1), win.Bottom + 1):
        buf = ctypes.create_unicode_buffer(width)
        got = wt.DWORD()
        k32.ReadConsoleOutputCharacterW(out, buf, width, COORD(0, y), ctypes.byref(got))
        lines.append(buf.value[:got.value].rstrip())
    return lines


def dialog_for(lines: list[str], level: str) -> bool:
    """The effort dialog, for this level, with the cursor on its "Yes" row."""
    if not any(line.strip() == TITLE for line in lines):
        return False
    yes = f"1. Yes, switch to {level}"
    # The cursor mark (`❯`) on the Yes row: the row `1` answers is the one we read.
    return any(yes in line and line.lstrip().startswith("❯") for line in lines)


def press_one(inp) -> bool:
    records = (INPUT_RECORD * 2)()
    for i, down in enumerate((True, False)):
        records[i].EventType = KEY_EVENT
        key = records[i].Event.KeyEvent
        key.bKeyDown = down
        key.wRepeatCount = 1
        key.wVirtualKeyCode = 0x31  # VK_1
        key.wVirtualScanCode = 0x02
        key.uChar = "1"
        key.dwControlKeyState = 0
    written = wt.DWORD()
    return bool(k32.WriteConsoleInputW(inp, records, 2, ctypes.byref(written))) and written.value == 2


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in LEVELS:
        print(f"usage: effort_confirm.py <{'|'.join(LEVELS)}> [timeout]")
        return 2
    level = sys.argv[1]
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
    pid = claude_pid()
    if pid is None:
        print("no claude.exe above this process; nothing done")
        return 1
    k32.FreeConsole()
    if not k32.AttachConsole(pid):
        print(f"AttachConsole({pid}) failed: {ctypes.get_last_error()}")
        return 1
    try:
        out = k32.CreateFileW("CONOUT$", GENERIC_RW, SHARE_RW, None, OPEN_EXISTING, 0, None)
        inp = k32.CreateFileW("CONIN$", GENERIC_RW, SHARE_RW, None, OPEN_EXISTING, 0, None)
        if out in (None, INVALID) or inp in (None, INVALID):
            print(f"console handles not opened: {ctypes.get_last_error()}")
            return 1
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if dialog_for(screen_tail(out), level):
                if not press_one(inp):
                    print(f"dialog seen, WriteConsoleInput failed: {ctypes.get_last_error()}")
                    return 1
                # Confirm it took: the dialog goes away.
                gone_by = time.monotonic() + 3.0
                while time.monotonic() < gone_by:
                    time.sleep(POLL)
                    if not dialog_for(screen_tail(out), level):
                        print(f"answered: switch to {level}")
                        return 0
                print("pressed 1, but the dialog is still up")
                return 1
            time.sleep(POLL)
        print(f"no effort dialog for {level} within {timeout:.0f}s; nothing pressed")
        return 1
    finally:
        k32.FreeConsole()


if __name__ == "__main__":
    sys.exit(main())
