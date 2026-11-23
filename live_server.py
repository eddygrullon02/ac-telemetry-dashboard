"""
live_server.py - streams live Assetto Corsa telemetry and game video to any browser
====================================================================================

WHAT THIS DOES
    - Streams telemetry ~30 times per second over a WebSocket (/ws) to the
      dashboard page (static/index.html).
    - Streams the game window as MJPEG video (/video), only while a browser is
      actually watching (see camera.py).
    - Runs INDEPENDENTLY of collector.py: both read AC's shared memory at the
      same time, so you can record a session and watch it live.

    LIVE MODE     AC shared memory --> sources.LiveSource  --\
                  Game window --> camera.py --> /video -------+--> browser
    REPLAY MODE   recorded Parquet --> sources.ReplaySource --/    (no game needed)

USAGE
    python live_server.py                              # live, with the game running
    python live_server.py --replay sample_data         # replay a recorded session (any OS)
    python live_server.py --port 8001                  # use a different port

    PC browser:   http://localhost:8000
    Phone:        the address printed at startup (same Wi-Fi)
"""

import argparse
import asyncio
import json
import socket
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse, StreamingResponse

from camera import ScreenCamera
from sources import LiveSource, ReplaySource

BASE_DIR = Path(__file__).resolve().parent
STREAM_HZ = 30          # telemetry updates per second sent to each browser

# --- Camera settings -------------------------------------------------------
# "window":  capture only the Assetto Corsa window, wherever it is (any monitor
#            layout, any graphics card). This is the default.
# "monitor": capture one whole monitor with dxcam. Fallback if "window" gives
#            black or frozen frames; uses CAMERA_DEVICE and CAMERA_MONITOR below.
CAMERA_MODE = "window"
CAMERA_DEVICE = 1       # monitor mode only: graphics card index
CAMERA_MONITOR = 0      # monitor mode only: monitor index on that card
# Triple screens: the game window can be very wide. Set this to 16 / 9 to show only
# the centre screen, or leave as None to show the whole width.
CAMERA_CENTER_CROP_ASPECT = None

app = FastAPI(title="AC Live Telemetry")
camera = ScreenCamera(mode=CAMERA_MODE, monitor=CAMERA_MONITOR, device=CAMERA_DEVICE,
                      center_crop_aspect=CAMERA_CENTER_CROP_ASPECT)
source = None           # LiveSource or ReplaySource, chosen in main()


def get_source():
    if source is None:
        raise HTTPException(status_code=500, detail="Start the server with: python live_server.py")
    return source


def _json_default(o):
    """Let json.dumps handle numpy numbers if they sneak in."""
    return o.item() if hasattr(o, "item") else str(o)


@app.get("/")
def index():
    """Serve the dashboard page."""
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/meta")
def meta():
    """Car / track info for the dashboard header."""
    return get_source().meta()


@app.get("/api/camera")
def camera_stats():
    """Capture rate, encode time and frame size, for the camera panel."""
    if get_source().name == "replay":
        return {"available": False, "problem": "camera is not available in replay mode"}
    return camera.stats()


@app.get("/video")
async def video(fps: int = 15, width: int = 960, quality: int = 70):
    """MJPEG stream of the game window. Works in a plain <img> tag on any device.

    Query parameters: fps (5-30), width in pixels (320-1920), quality (30-90).
    """
    if get_source().name == "replay":
        raise HTTPException(status_code=503, detail="camera is not available in replay mode")
    problem = camera.unavailable_reason()
    if problem:
        raise HTTPException(status_code=503, detail=problem)
    camera.configure(fps, width, quality)

    async def frames():
        camera.acquire()                    # starts the capture thread if idle
        last_id = -1
        try:
            while True:
                jpeg, frame_id = camera.latest()
                if camera.error and jpeg is None:
                    break                   # capture failed to start; end the stream
                if jpeg is None or frame_id == last_id:
                    await asyncio.sleep(0.01)   # wait for a new frame
                    continue
                last_id = frame_id
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
        finally:
            camera.release()                # runs when the browser disconnects

    return StreamingResponse(frames(),
                             media_type="multipart/x-mixed-replace; boundary=frame")


@app.websocket("/ws")
async def stream(ws: WebSocket):
    """Send telemetry to one browser until it leaves."""
    await ws.accept()
    try:
        async for row in get_source().stream(STREAM_HZ):
            await ws.send_text(json.dumps(row, default=_json_default))
    except Exception:
        # Browser closed the tab or lost connection; just end this stream.
        pass


def lan_ip():
    """This PC's address on the local network (what a phone should connect to)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))    # no packet is actually sent
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def parse_args():
    ap = argparse.ArgumentParser(description="Live Assetto Corsa telemetry dashboard server")
    ap.add_argument("--replay", metavar="PATH",
                    help="replay a recorded session folder (or a folder of sessions) instead of reading the game")
    ap.add_argument("--host", default="0.0.0.0",
                    help="address to listen on (0.0.0.0 = reachable from other devices on your network)")
    ap.add_argument("--port", type=int, default=8000)
    return ap.parse_args()


def main():
    global source
    args = parse_args()
    source = ReplaySource.from_path(args.replay) if args.replay else LiveSource()

    print()
    print(f"  Mode:      {'REPLAY of ' + args.replay if args.replay else 'LIVE (Assetto Corsa)'}")
    print(f"  On this PC: http://localhost:{args.port}")
    print(f"  On a phone: http://{lan_ip()}:{args.port}   (same Wi-Fi)")
    print()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
