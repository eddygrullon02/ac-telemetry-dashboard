"""
camera.py - captures the game screen and turns it into JPEG frames
==================================================================

WHAT THIS DOES
    Grabs the game picture, shrinks each frame, encodes it as a JPEG, and keeps
    the newest frame in memory. live_server.py streams those frames to the
    browser.

    The capture thread only runs while at least one browser is WATCHING.
    When nobody is watching, nothing is captured, so there is no performance
    cost while you are just recording laps.

TWO CAPTURE MODES
    "window"  (default) Finds the Assetto Corsa window and captures only that
              area of the desktop, wherever it is. Works for a single screen,
              two monitors or triple screens, on any graphics card, because it
              uses desktop coordinates (see window_finder.py). Uses `mss`.
    "monitor" Captures one whole monitor with dxcam (device + monitor index).
              Use this if window mode shows black or frozen frames.

REQUIREMENTS
    pip install mss opencv-python          (window mode)
    pip install dxcam                      (only for monitor mode)
    If opencv-python won't install, `pip install pillow` also works as the
    JPEG encoder (a bit slower).

NOTES
    - Set the game to borderless windowed. Exclusive fullscreen usually gives
      black frames.
    - Keep other windows (like the dashboard) off the game's monitor, because
      window mode captures what is visible on screen.
"""

import io
import threading
import time
from collections import deque

# Optional libraries: we check which ones exist so we can give a clear error
# message instead of crashing at import time.
try:
    import numpy as np
except ImportError:
    np = None
try:
    import cv2
except ImportError:
    cv2 = None
try:
    from PIL import Image
except ImportError:
    Image = None
try:
    import dxcam
except ImportError:
    dxcam = None
try:
    import mss
except ImportError:
    mss = None
try:
    from window_finder import find_game_region
except Exception:                       # not Windows, or ctypes problem
    find_game_region = None


# ----------------------------------------------------------------------
# Grabbers: each one has .grab() -> BGR numpy frame or None, and .close()
# ----------------------------------------------------------------------
class _WindowGrabber:
    """Captures just the Assetto Corsa window, wherever it is on the desktop."""

    def __init__(self):
        # Newer mss versions renamed mss.mss to mss.MSS; support both.
        self.sct = (getattr(mss, "MSS", None) or mss.mss)()
        self.region = None                # {'left','top','width','height'}
        self.target = ""                  # human-readable description for the dashboard
        self.status = ""                  # message shown if the window can't be found
        self._next_find = 0.0

    def grab(self):
        now = time.perf_counter()
        if now >= self._next_find:        # re-locate the window about once a second,
            self.region = find_game_region()   # so moving the window is picked up
            self._next_find = now + 1.0
            if self.region:
                r = self.region
                self.target = f"AC window {r['width']}x{r['height']} at ({r['left']}, {r['top']})"
                self.status = ""
            else:
                self.target = ""
                self.status = ("waiting for the Assetto Corsa window "
                               "(is the game running and not minimised?)")
        if self.region is None:
            time.sleep(0.2)               # don't spin while waiting
            return None
        try:
            shot = self.sct.grab(self.region)
        except Exception:
            self.region, self._next_find = None, 0.0   # window moved or closed; look again
            return None
        frame = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        return np.ascontiguousarray(frame[:, :, :3])  # BGRA -> BGR

    def close(self):
        try:
            self.sct.close()
        except Exception:
            pass


class _MonitorGrabber:
    """Captures one whole monitor with dxcam (device = graphics card, monitor = output)."""

    def __init__(self, device, monitor):
        self.cam = dxcam.create(device_idx=device, output_idx=monitor, output_color="BGR")
        self.target = f"monitor {monitor} on graphics card {device}"
        self.status = ""

    def grab(self):
        return self.cam.grab()            # None if the screen didn't change

    def close(self):
        try:
            release = getattr(self.cam, "release", None)
            if release:
                release()
        except Exception:
            pass


# ----------------------------------------------------------------------
# The camera
# ----------------------------------------------------------------------
class ScreenCamera:
    def __init__(self, mode="window", monitor=0, device=0, center_crop_aspect=None):
        self.mode = mode                  # "window" or "monitor"
        self.device = device              # monitor mode: graphics card index
        self.monitor = monitor            # monitor mode: output index on that card
        self.center_crop_aspect = center_crop_aspect   # e.g. 16/9: keep only the centre screen of a triple

        self.fps = 15                     # target frames per second
        self.width = 960                  # frames are shrunk to this width (pixels)
        self.quality = 70                 # JPEG quality, 1-100

        self._lock = threading.Lock()     # protects _viewers and _thread
        self._viewers = 0                 # how many browsers are watching right now
        self._thread = None               # the capture thread (None when idle)

        self._jpeg = None                 # newest encoded frame (bytes)
        self._frame_id = 0                # increments every new frame
        self.error = None                 # fatal error message, if any
        self.status = ""                  # soft status, e.g. "waiting for the game window"
        self.target = ""                  # what is being captured right now

        self._encode_ms = 0.0             # smoothed encode time, for the stats panel
        self._frame_kb = 0.0              # size of the last frame
        self._times = deque(maxlen=60)    # timestamps of recent frames, for fps

    # ------------------------------------------------------------------
    # Public API (called by live_server.py)
    # ------------------------------------------------------------------
    def unavailable_reason(self):
        """Return a human-readable reason the camera can't work, or None if it can."""
        if self.mode == "window":
            if find_game_region is None:
                return "window capture needs Windows (window_finder.py could not load)"
            if mss is None or np is None:
                return "mss is not installed (run: python -m pip install mss)"
        elif self.mode == "monitor":
            if dxcam is None or np is None:
                return "dxcam is not installed (run: python -m pip install dxcam)"
        else:
            return f"unknown CAMERA_MODE '{self.mode}' (use 'window' or 'monitor')"
        if cv2 is None and Image is None:
            return "no JPEG encoder found (run: python -m pip install opencv-python)"
        return None

    def configure(self, fps, width, quality):
        """Apply settings, clamped to sensible ranges."""
        self.fps = max(5, min(30, int(fps)))
        self.width = max(320, min(1920, int(width)))
        self.quality = max(30, min(90, int(quality)))

    def acquire(self):
        """A viewer started watching. Start the capture thread if it's idle."""
        with self._lock:
            self._viewers += 1
            if self._thread is None:
                self.error = None
                self.status = ""
                self._jpeg = None          # don't show a stale frame from an old session
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def release(self):
        """A viewer stopped watching. The thread exits when nobody is left."""
        with self._lock:
            self._viewers = max(0, self._viewers - 1)

    def latest(self):
        """Return (newest JPEG bytes or None, frame id)."""
        return self._jpeg, self._frame_id

    def stats(self):
        """Numbers for the dashboard's camera panel."""
        fps = 0.0
        t = list(self._times)
        if len(t) >= 2 and time.perf_counter() - t[-1] < 2.0:
            fps = (len(t) - 1) / (t[-1] - t[0])
        return {
            "available": self.unavailable_reason() is None,
            "problem": self.unavailable_reason() or self.error or self.status or None,
            "mode": self.mode,
            "target": self.target,
            "viewers": self._viewers,
            "capture_fps": round(fps, 1),
            "encode_ms": round(self._encode_ms, 1),
            "frame_kb": round(self._frame_kb, 1),
            "backend": "opencv" if cv2 is not None else "pillow",
            "settings": {"fps": self.fps, "width": self.width, "quality": self.quality},
        }

    # ------------------------------------------------------------------
    # Capture thread
    # ------------------------------------------------------------------
    def _run(self):
        """Capture while anyone is watching, then shut down cleanly."""
        while True:
            grabber = self._open()
            if grabber is None:                  # couldn't start capture; error is set
                with self._lock:
                    self._thread = None
                return
            try:
                self._capture(grabber)           # returns when the last viewer leaves
            finally:
                grabber.close()
            with self._lock:
                if self._viewers == 0 or self.error:   # nobody left, or capture failed
                    self._thread = None
                    return
                # else: a viewer arrived while shutting down, so loop and reopen

    def _open(self):
        try:
            if self.mode == "window":
                return _WindowGrabber()
            return _MonitorGrabber(self.device, self.monitor)
        except Exception as e:
            self.error = f"Could not start screen capture: {e}"
            return None

    def _capture(self, grabber):
        next_t = time.perf_counter()
        while self._viewers > 0:
            try:
                frame = grabber.grab()
            except Exception as e:
                self.error = f"Capture failed: {e}"
                return
            self.target = grabber.target
            self.status = grabber.status
            if frame is not None:
                t0 = time.perf_counter()
                jpeg = self._encode(frame)
                if jpeg:
                    ms = (time.perf_counter() - t0) * 1000
                    self._encode_ms = ms if self._encode_ms == 0 else 0.9 * self._encode_ms + 0.1 * ms
                    self._frame_kb = len(jpeg) / 1024
                    self._jpeg = jpeg
                    self._frame_id += 1
                    self._times.append(time.perf_counter())

            # Pace the loop to the target fps (deadline-based, like the collector)
            next_t += 1.0 / self.fps
            delay = next_t - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.perf_counter()     # we're behind; don't try to catch up

    def _encode(self, frame):
        """Optionally centre-crop, shrink a BGR frame to self.width, and encode as JPEG."""
        h, w = frame.shape[:2]

        # Triple screens: the game window can be very wide. Keep only the centre part.
        if self.center_crop_aspect:
            target_w = int(h * self.center_crop_aspect)
            if w > target_w * 1.05:
                x0 = (w - target_w) // 2
                frame = np.ascontiguousarray(frame[:, x0:x0 + target_w])
                h, w = frame.shape[:2]

        if cv2 is not None:
            if w > self.width:
                frame = cv2.resize(frame, (self.width, round(h * self.width / w)),
                                   interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
            return buf.tobytes() if ok else None

        # Pillow fallback (slower)
        img = Image.fromarray(frame[:, :, ::-1].copy())   # BGR -> RGB
        if img.width > self.width:
            img = img.resize((self.width, round(img.height * self.width / img.width)),
                             Image.BILINEAR)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=self.quality)
        return out.getvalue()
