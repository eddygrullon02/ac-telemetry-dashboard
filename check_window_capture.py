"""
check_window_capture.py - checks that the Assetto Corsa window can be found and captured
=======================================================================================

USAGE (venv active, game running in borderless windowed mode):
    python check_window_capture.py
Then click into the game and keep driving during the countdown and the test.

WHAT IT PRINTS
    - where the game window is (desktop coordinates)
    - how many frames it captured in 4 seconds, and how many of them changed
    - it saves window_first.jpg and window_last.jpg so you can see what was captured
"""

import time

import cv2
import mss
import numpy as np

from window_finder import find_game_region, describe_visible_windows

region = find_game_region()
print("Game window region:", region)

if region is None:
    print("\nCould not find the Assetto Corsa window. Visible windows I can see:")
    for exe, title, rect in describe_visible_windows():
        print(f"  {exe:24s} {rect['width']}x{rect['height']} at ({rect['left']}, {rect['top']})  {title[:40]}")
    raise SystemExit("\nIf the game is listed under a different .exe name, tell me what it is.")

print("\nYou have 6 seconds: click into the game and start driving, and keep driving.")
for i in range(6, 0, -1):
    print(i, end=" ", flush=True)
    time.sleep(1)
print("\nMeasuring for 4 seconds...")

sct = (getattr(mss, "MSS", None) or mss.mss)()
region = find_game_region() or region              # re-check in case you moved the window
prev, got, changed, first, last = None, 0, 0, None, None
t0 = time.perf_counter()
while time.perf_counter() - t0 < 4:
    shot = sct.grab(region)
    frame = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)[:, :, :3]
    got += 1
    if first is None:
        first = frame.copy()
    last = frame
    small = frame[::40, ::40].astype(np.int16)     # tiny sample of the frame
    if prev is not None and np.abs(small - prev).mean() > 0.5:
        changed += 1
    prev = small
elapsed = time.perf_counter() - t0
sct.close()

print(f"frames captured: {got} ({got / elapsed:.0f} per second), frames that changed: {changed}")
cv2.imwrite("window_first.jpg", np.ascontiguousarray(first))
cv2.imwrite("window_last.jpg", np.ascontiguousarray(last))
print("Saved window_first.jpg and window_last.jpg")
