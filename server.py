#!/usr/bin/env python3
"""
RustDesk Mobile UI - Windows Backend Server
Provides APIs for window management, system controls, app launching, and window streaming on Windows.
"""

import asyncio
import json
import os
import subprocess
import ctypes
from datetime import datetime
from typing import Optional, Dict, Set
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

import pyperclip
import psutil

# Import window capture module
try:
    from window_capture import WindowCapture, ChromeController
    HAS_CAPTURE = WindowCapture.is_available()
except ImportError:
    HAS_CAPTURE = False
    WindowCapture = None
    ChromeController = None
    print("Warning: window_capture module not found. Streaming disabled.")

# Windows-specific imports
try:
    import win32gui
    import win32con
    import win32process
    import win32api
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False
    print("Warning: pywin32 not installed. Window management disabled.")

try:
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from comtypes import CLSCTX_ALL
    HAS_PYCAW = True
except ImportError:
    HAS_PYCAW = False
    print("Warning: pycaw not installed. Volume control disabled.")

try:
    import screen_brightness_control as sbc
    HAS_SBC = True
except ImportError:
    HAS_SBC = False
    print("Warning: screen_brightness_control not installed. Brightness control disabled.")


class StreamManager:
    """Manages window streaming sessions."""

    def __init__(self):
        self.active_streams: Dict[str, asyncio.Task] = {}  # window_id -> task
        self.stream_clients: Dict[str, Set[WebSocket]] = {}  # window_id -> set of websockets
        self.frame_seq: Dict[str, int] = {}  # window_id -> sequence number

    async def start_stream(self, window_id: str, websocket: WebSocket,
                          fps: int = 8, quality: int = 60, max_width: int = 800):
        """Start streaming a window to a websocket client."""
        if not HAS_CAPTURE:
            await websocket.send_json({
                "type": "stream_error",
                "window_id": window_id,
                "error": "Window capture not available"
            })
            return

        # Add client to stream
        if window_id not in self.stream_clients:
            self.stream_clients[window_id] = set()
            self.frame_seq[window_id] = 0

        self.stream_clients[window_id].add(websocket)

        # Start capture loop if not already running
        if window_id not in self.active_streams or self.active_streams[window_id].done():
            self.active_streams[window_id] = asyncio.create_task(
                self._capture_loop(window_id, fps, quality, max_width)
            )

        # Send status
        await websocket.send_json({
            "type": "stream_status",
            "window_id": window_id,
            "status": "active"
        })

    async def stop_stream(self, window_id: str, websocket: WebSocket):
        """Stop streaming a window for a client."""
        if window_id in self.stream_clients:
            self.stream_clients[window_id].discard(websocket)

            # If no more clients, stop the capture loop
            if not self.stream_clients[window_id]:
                if window_id in self.active_streams:
                    self.active_streams[window_id].cancel()
                    del self.active_streams[window_id]
                del self.stream_clients[window_id]
                del self.frame_seq[window_id]

        await websocket.send_json({
            "type": "stream_status",
            "window_id": window_id,
            "status": "stopped"
        })

    async def _capture_loop(self, window_id: str, fps: int, quality: int, max_width: int):
        """Capture frames and broadcast to clients."""
        interval = 1.0 / fps
        hwnd = int(window_id)

        while window_id in self.stream_clients and self.stream_clients[window_id]:
            try:
                # Capture frame
                result = WindowCapture.capture_window(hwnd, quality=quality, max_width=max_width)

                if result is None:
                    # Window might be closed or minimized
                    await self._broadcast_error(window_id, "Window not available")
                    break

                b64_data, width, height = result
                self.frame_seq[window_id] += 1

                # Broadcast to all clients
                frame_msg = {
                    "type": "stream_frame",
                    "window_id": window_id,
                    "frame": b64_data,
                    "width": width,
                    "height": height,
                    "seq": self.frame_seq[window_id]
                }

                dead_clients = set()
                for client in self.stream_clients.get(window_id, set()):
                    try:
                        await client.send_json(frame_msg)
                    except:
                        dead_clients.add(client)

                # Remove dead clients
                if dead_clients and window_id in self.stream_clients:
                    self.stream_clients[window_id] -= dead_clients

                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                await self._broadcast_error(window_id, str(e))
                break

    async def _broadcast_error(self, window_id: str, error: str):
        """Broadcast error to all clients watching a window."""
        error_msg = {
            "type": "stream_error",
            "window_id": window_id,
            "error": error
        }
        for client in self.stream_clients.get(window_id, set()):
            try:
                await client.send_json(error_msg)
            except:
                pass

    def remove_client(self, websocket: WebSocket):
        """Remove a client from all streams."""
        for window_id in list(self.stream_clients.keys()):
            if websocket in self.stream_clients[window_id]:
                self.stream_clients[window_id].discard(websocket)
                if not self.stream_clients[window_id]:
                    if window_id in self.active_streams:
                        self.active_streams[window_id].cancel()
                        del self.active_streams[window_id]
                    del self.stream_clients[window_id]
                    if window_id in self.frame_seq:
                        del self.frame_seq[window_id]


# Global stream manager
stream_manager = StreamManager()

app = FastAPI(title="RustDesk Mobile UI - Windows", version="1.0.0")

# CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# WebSocket connections for real-time updates
active_connections: list[WebSocket] = []


class ClipboardData(BaseModel):
    content: str


class VolumeData(BaseModel):
    level: int  # 0-100


class BrightnessData(BaseModel):
    level: int  # 0-100


# Default app configurations for Windows
DEFAULT_APPS = [
    {
        "id": "claude-code",
        "name": "Claude Code",
        "command": "wt -w 0 nt --title \"Claude Code\" cmd /k claude",
        "icon": "terminal",
        "color": "#D97706",
        "category": "Development",
        "priority": 1
    },
    {
        "id": "vscode",
        "name": "VS Code",
        "command": "code",
        "icon": "code",
        "color": "#0078D4",
        "category": "Development",
        "priority": 2
    },
    {
        "id": "browser",
        "name": "Browser",
        "command": "start msedge",
        "icon": "globe",
        "color": "#4285F4",
        "category": "Development",
        "priority": 3
    },
    {
        "id": "file-explorer",
        "name": "Files",
        "command": "explorer",
        "icon": "folder",
        "color": "#F59E0B",
        "category": "System",
        "priority": 4
    },
    {
        "id": "terminal",
        "name": "Terminal",
        "command": "wt",
        "icon": "terminal-square",
        "color": "#1F2937",
        "category": "Development",
        "priority": 5
    },
    {
        "id": "settings",
        "name": "Settings",
        "command": "start ms-settings:",
        "icon": "settings",
        "color": "#6B7280",
        "category": "System",
        "priority": 6
    },
    {
        "id": "task-manager",
        "name": "Task Manager",
        "command": "taskmgr",
        "icon": "activity",
        "color": "#10B981",
        "category": "System",
        "priority": 7
    },
    {
        "id": "notepad",
        "name": "Notepad",
        "command": "notepad",
        "icon": "file-text",
        "color": "#8B5CF6",
        "category": "Productivity",
        "priority": 8
    }
]

# Config file path
CONFIG_DIR = Path.home() / ".config" / "rustdesk-mobile-ui"
CONFIG_FILE = CONFIG_DIR / "apps.json"

# Also check local apps.json
LOCAL_CONFIG = Path(__file__).parent / "apps.json"


def load_app_config():
    """Load app configuration from file or return defaults."""
    # First check local apps.json
    if LOCAL_CONFIG.exists():
        try:
            with open(LOCAL_CONFIG, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading local config: {e}")

    # Then check user config
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading config: {e}")

    return DEFAULT_APPS


def save_app_config(apps):
    """Save app configuration to file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(apps, f, indent=2)


# Window management functions
def get_window_list():
    """Get list of open windows using Win32 API."""
    if not HAS_WIN32:
        return []

    windows = []

    def enum_callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title and len(title) > 0:
                # Skip system windows
                if title in ["Program Manager", "Settings", "Microsoft Text Input Application"]:
                    return True

                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    process = psutil.Process(pid)
                    process_name = process.name().lower().replace('.exe', '')
                except:
                    process_name = "unknown"

                windows.append({
                    "id": str(hwnd),
                    "title": title[:50],  # Truncate long titles
                    "pid": str(pid) if 'pid' in dir() else "0",
                    "class": process_name,
                    "icon": get_icon_for_process(process_name)
                })
        return True

    try:
        win32gui.EnumWindows(enum_callback, None)
    except Exception as e:
        print(f"Error enumerating windows: {e}")

    return windows


def get_icon_for_process(process_name: str) -> str:
    """Map process name to icon name."""
    icon_map = {
        "code": "code",
        "chrome": "chrome",
        "msedge": "globe",
        "firefox": "firefox",
        "windowsterminal": "terminal",
        "cmd": "terminal",
        "powershell": "terminal",
        "pwsh": "terminal",
        "explorer": "folder",
        "slack": "message-square",
        "discord": "message-circle",
        "spotify": "music",
        "notepad": "file-text",
        "winword": "file-text",
        "excel": "table",
        "outlook": "mail",
        "teams": "users",
    }

    for key, icon in icon_map.items():
        if key in process_name:
            return icon
    return "window"


# Volume control functions
def get_volume():
    """Get current system volume level."""
    if not HAS_PYCAW:
        return {"volume": 50, "error": "pycaw not installed"}

    try:
        speakers = AudioUtilities.GetSpeakers()
        volume = speakers.EndpointVolume
        current = volume.GetMasterVolumeLevelScalar()
        return {"volume": int(current * 100)}
    except Exception as e:
        return {"volume": 50, "error": str(e)}


def set_volume(level: int):
    """Set system volume level."""
    if not HAS_PYCAW:
        return {"error": "pycaw not installed"}

    try:
        level = max(0, min(100, level))
        speakers = AudioUtilities.GetSpeakers()
        volume = speakers.EndpointVolume
        volume.SetMasterVolumeLevelScalar(level / 100, None)
        return {"volume": level}
    except Exception as e:
        return {"volume": level, "error": str(e)}


# Brightness control functions
def get_brightness():
    """Get current display brightness."""
    if not HAS_SBC:
        return {"brightness": 100, "error": "screen_brightness_control not installed"}

    try:
        brightness = sbc.get_brightness()
        if isinstance(brightness, list):
            brightness = brightness[0]
        return {"brightness": brightness}
    except Exception as e:
        return {"brightness": 100, "error": str(e)}


def set_brightness(level: int):
    """Set display brightness."""
    if not HAS_SBC:
        return {"error": "screen_brightness_control not installed"}

    try:
        level = max(5, min(100, level))  # Min 5% to prevent black screen
        sbc.set_brightness(level)
        return {"brightness": level}
    except Exception as e:
        return {"brightness": level, "error": str(e)}


# System info functions
def get_system_info():
    """Get system information."""
    info = {}

    # Hostname
    info["hostname"] = os.environ.get("COMPUTERNAME", "Windows PC")

    # Uptime
    boot_time = psutil.boot_time()
    uptime_seconds = datetime.now().timestamp() - boot_time
    hours, remainder = divmod(int(uptime_seconds), 3600)
    minutes, _ = divmod(remainder, 60)
    info["uptime"] = f"{hours}h {minutes}m"

    # Memory usage
    mem = psutil.virtual_memory()
    info["memory"] = {
        "total": f"{mem.total / (1024**3):.1f}GB",
        "used": f"{mem.used / (1024**3):.1f}GB"
    }

    # CPU usage
    cpu_percent = psutil.cpu_percent(interval=0.1)
    info["cpu_usage"] = f"{cpu_percent:.1f}%"

    return info


def get_rustdesk_status():
    """Check if RustDesk session is active."""
    rustdesk_running = False
    has_connection = False

    for proc in psutil.process_iter(['name', 'cmdline']):
        try:
            name = proc.info['name'].lower()
            if 'rustdesk' in name:
                rustdesk_running = True
                cmdline = proc.info.get('cmdline', [])
                if cmdline and any('--cm' in arg for arg in cmdline):
                    has_connection = True
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return {
        "rustdesk_running": rustdesk_running,
        "has_connection": has_connection,
        "status": "connected" if has_connection else ("ready" if rustdesk_running else "offline")
    }


# API Endpoints
@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "timestamp": datetime.now().isoformat(), "platform": "windows"}


@app.get("/api/apps")
async def get_apps():
    """Get configured applications."""
    apps = load_app_config()
    return {"apps": sorted(apps, key=lambda x: x.get("priority", 99))}


@app.post("/api/apps")
async def save_apps(apps: list[dict]):
    """Save app configurations."""
    save_app_config(apps)
    return {"status": "saved"}


@app.post("/api/launch/{app_id}")
async def launch_app(app_id: str):
    """Launch an application by ID."""
    apps = load_app_config()
    app_config = next((a for a in apps if a["id"] == app_id), None)

    if not app_config:
        raise HTTPException(status_code=404, detail="App not found")

    # Launch in background
    command = app_config["command"]
    try:
        subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
        return {"status": "launched", "app": app_config["name"]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/launch-custom")
async def launch_custom(data: dict):
    """Launch a custom command."""
    command = data.get("command", "")
    if not command:
        raise HTTPException(status_code=400, detail="No command provided")

    try:
        subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
        return {"status": "launched"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/windows")
async def get_windows():
    """Get list of open windows."""
    windows = get_window_list()
    return {"windows": windows}


@app.post("/api/windows/{window_id}/focus")
async def focus_window(window_id: str):
    """Focus/activate a window."""
    if not HAS_WIN32:
        raise HTTPException(status_code=500, detail="pywin32 not installed")

    try:
        hwnd = int(window_id)
        # Restore if minimized
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        # Bring to foreground
        win32gui.SetForegroundWindow(hwnd)
        return {"status": "focused"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/windows/{window_id}/close")
async def close_window(window_id: str):
    """Close a window."""
    if not HAS_WIN32:
        raise HTTPException(status_code=500, detail="pywin32 not installed")

    try:
        hwnd = int(window_id)
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        return {"status": "closed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/windows/{window_id}/minimize")
async def minimize_window(window_id: str):
    """Minimize a window."""
    if not HAS_WIN32:
        raise HTTPException(status_code=500, detail="pywin32 not installed")

    try:
        hwnd = int(window_id)
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        return {"status": "minimized"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/windows/{window_id}/maximize")
async def maximize_window(window_id: str):
    """Maximize a window."""
    if not HAS_WIN32:
        raise HTTPException(status_code=500, detail="pywin32 not installed")

    try:
        hwnd = int(window_id)
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        return {"status": "maximized"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Window streaming endpoints
@app.get("/api/windows/{window_id}/info")
async def get_window_info(window_id: str):
    """Get detailed information about a window."""
    if not HAS_CAPTURE:
        raise HTTPException(status_code=500, detail="Window capture not available")

    try:
        hwnd = int(window_id)
        info = WindowCapture.get_window_info(hwnd)
        if "error" in info:
            raise HTTPException(status_code=404, detail=info["error"])
        return info
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid window ID")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/windows/{window_id}/snapshot")
async def get_window_snapshot(window_id: str, quality: int = 60, max_width: int = 800):
    """Get a single snapshot of a window."""
    if not HAS_CAPTURE:
        raise HTTPException(status_code=500, detail="Window capture not available")

    try:
        hwnd = int(window_id)
        result = WindowCapture.capture_window(hwnd, quality=quality, max_width=max_width)

        if result is None:
            raise HTTPException(status_code=404, detail="Window not available or minimized")

        b64_data, width, height = result
        return {
            "frame": b64_data,
            "width": width,
            "height": height,
            "window_id": window_id
        }
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid window ID")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Chrome control endpoints
@app.post("/api/windows/{window_id}/chrome/navigate")
async def chrome_navigate(window_id: str, data: dict):
    """Navigate Chrome to a URL."""
    if not HAS_CAPTURE or ChromeController is None:
        raise HTTPException(status_code=500, detail="Chrome control not available")

    url = data.get("url", "")
    if not url:
        raise HTTPException(status_code=400, detail="URL required")

    try:
        hwnd = int(window_id)
        success = ChromeController.navigate_to_url(hwnd, url)
        return {"status": "navigated" if success else "failed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/windows/{window_id}/chrome/back")
async def chrome_back(window_id: str):
    """Go back in browser history."""
    if not HAS_CAPTURE or ChromeController is None:
        raise HTTPException(status_code=500, detail="Chrome control not available")

    try:
        hwnd = int(window_id)
        success = ChromeController.go_back(hwnd)
        return {"status": "success" if success else "failed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/windows/{window_id}/chrome/forward")
async def chrome_forward(window_id: str):
    """Go forward in browser history."""
    if not HAS_CAPTURE or ChromeController is None:
        raise HTTPException(status_code=500, detail="Chrome control not available")

    try:
        hwnd = int(window_id)
        success = ChromeController.go_forward(hwnd)
        return {"status": "success" if success else "failed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/windows/{window_id}/chrome/refresh")
async def chrome_refresh(window_id: str):
    """Refresh the current page."""
    if not HAS_CAPTURE or ChromeController is None:
        raise HTTPException(status_code=500, detail="Chrome control not available")

    try:
        hwnd = int(window_id)
        success = ChromeController.refresh(hwnd)
        return {"status": "success" if success else "failed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/windows/{window_id}/chrome/new-tab")
async def chrome_new_tab(window_id: str):
    """Open a new tab."""
    if not HAS_CAPTURE or ChromeController is None:
        raise HTTPException(status_code=500, detail="Chrome control not available")

    try:
        hwnd = int(window_id)
        success = ChromeController.new_tab(hwnd)
        return {"status": "success" if success else "failed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/windows/{window_id}/chrome/close-tab")
async def chrome_close_tab(window_id: str):
    """Close current tab."""
    if not HAS_CAPTURE or ChromeController is None:
        raise HTTPException(status_code=500, detail="Chrome control not available")

    try:
        hwnd = int(window_id)
        success = ChromeController.close_tab(hwnd)
        return {"status": "success" if success else "failed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/windows/{window_id}/chrome/next-tab")
async def chrome_next_tab(window_id: str):
    """Switch to next tab."""
    if not HAS_CAPTURE or ChromeController is None:
        raise HTTPException(status_code=500, detail="Chrome control not available")

    try:
        hwnd = int(window_id)
        success = ChromeController.next_tab(hwnd)
        return {"status": "success" if success else "failed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/windows/{window_id}/chrome/prev-tab")
async def chrome_prev_tab(window_id: str):
    """Switch to previous tab."""
    if not HAS_CAPTURE or ChromeController is None:
        raise HTTPException(status_code=500, detail="Chrome control not available")

    try:
        hwnd = int(window_id)
        success = ChromeController.prev_tab(hwnd)
        return {"status": "success" if success else "failed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/system/volume")
async def api_get_volume():
    """Get current volume level."""
    return get_volume()


@app.post("/api/system/volume")
async def api_set_volume(data: VolumeData):
    """Set volume level."""
    result = set_volume(data.level)
    await broadcast_update("volume", result)
    return result


@app.post("/api/system/volume/mute")
async def toggle_mute():
    """Toggle mute."""
    if not HAS_PYCAW:
        raise HTTPException(status_code=500, detail="pycaw not installed")

    try:
        speakers = AudioUtilities.GetSpeakers()
        volume = speakers.EndpointVolume
        current_mute = volume.GetMute()
        volume.SetMute(not current_mute, None)
        return {"status": "toggled", "muted": not current_mute}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/system/brightness")
async def api_get_brightness():
    """Get current brightness level."""
    return get_brightness()


@app.post("/api/system/brightness")
async def api_set_brightness(data: BrightnessData):
    """Set brightness level."""
    result = set_brightness(data.level)
    await broadcast_update("brightness", result)
    return result


@app.get("/api/clipboard")
async def api_get_clipboard():
    """Get clipboard content."""
    try:
        content = pyperclip.paste()
        return {"content": content[:1000]}  # Limit size
    except Exception as e:
        return {"content": "", "error": str(e)}


@app.post("/api/clipboard")
async def api_set_clipboard(data: ClipboardData):
    """Set clipboard content."""
    try:
        pyperclip.copy(data.content)
        await broadcast_update("clipboard", {"content": data.content[:100]})
        return {"status": "copied"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/rustdesk/status")
async def api_get_rustdesk_status():
    """Check if RustDesk session is active."""
    return get_rustdesk_status()


@app.get("/api/system/info")
async def api_get_system_info():
    """Get system information."""
    return get_system_info()


async def broadcast_update(event_type: str, data: dict):
    """Broadcast update to all connected WebSocket clients."""
    message = json.dumps({"type": event_type, "data": data})
    for connection in active_connections:
        try:
            await connection.send_text(message)
        except:
            pass


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates and window streaming."""
    await websocket.accept()
    active_connections.append(websocket)

    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            msg_type = message.get("type", "")

            if msg_type == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))

            elif msg_type == "get_windows":
                windows = get_window_list()
                await websocket.send_text(json.dumps({"type": "windows", "data": windows}))

            elif msg_type == "stream_start":
                # Start streaming a window
                window_id = message.get("window_id")
                options = message.get("options", {})
                fps = options.get("fps", 8)
                quality = options.get("quality", 60)
                max_width = options.get("max_width", 800)

                if window_id:
                    await stream_manager.start_stream(
                        window_id, websocket, fps=fps, quality=quality, max_width=max_width
                    )

            elif msg_type == "stream_stop":
                # Stop streaming a window
                window_id = message.get("window_id")
                if window_id:
                    await stream_manager.stop_stream(window_id, websocket)

            elif msg_type == "stream_adjust":
                # Adjust stream settings (restart with new settings)
                window_id = message.get("window_id")
                options = message.get("options", {})
                if window_id:
                    await stream_manager.stop_stream(window_id, websocket)
                    fps = options.get("fps", 8)
                    quality = options.get("quality", 60)
                    max_width = options.get("max_width", 800)
                    await stream_manager.start_stream(
                        window_id, websocket, fps=fps, quality=quality, max_width=max_width
                    )

            elif msg_type == "get_window_info":
                # Get window info via WebSocket
                window_id = message.get("window_id")
                if window_id and HAS_CAPTURE:
                    info = WindowCapture.get_window_info(int(window_id))
                    await websocket.send_json({"type": "window_info", "data": info})

    except WebSocketDisconnect:
        # Clean up stream subscriptions
        stream_manager.remove_client(websocket)
        if websocket in active_connections:
            active_connections.remove(websocket)


# Quick action endpoints
@app.post("/api/action/lock")
async def action_lock():
    """Lock the workstation."""
    try:
        ctypes.windll.user32.LockWorkStation()
        return {"status": "locked"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/action/sleep")
async def action_sleep():
    """Put the system to sleep."""
    try:
        subprocess.run(
            ["rundll32.exe", "powrprof.dll,SetSuspendState", "0", "1", "0"],
            check=True
        )
        return {"status": "sleeping"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/action/screenshot")
async def action_screenshot():
    """Take a screenshot."""
    try:
        import pyautogui
        screenshot_path = Path.home() / "Pictures" / f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        screenshot = pyautogui.screenshot()
        screenshot.save(str(screenshot_path))
        return {"status": "captured", "path": str(screenshot_path)}
    except ImportError:
        # Fallback to Windows Snipping Tool
        subprocess.Popen(["snippingtool", "/clip"])
        return {"status": "launched snipping tool"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Serve frontend
SCRIPT_DIR = Path(__file__).parent
INDEX_FILE = SCRIPT_DIR / "index.html"
PREVIEW_FILE = SCRIPT_DIR / "mobile-ui-preview.html"


@app.get("/")
async def serve_frontend():
    """Serve the frontend."""
    # Try index.html first, then mobile-ui-preview.html
    if INDEX_FILE.exists():
        return FileResponse(INDEX_FILE)
    elif PREVIEW_FILE.exists():
        return FileResponse(PREVIEW_FILE)
    return {"message": "Frontend not found. Place index.html in the same directory as server.py"}


if __name__ == "__main__":
    import uvicorn

    print("=" * 50)
    print("RustDesk Mobile UI - Windows Backend")
    print("=" * 50)
    print(f"pywin32 (window management): {'OK' if HAS_WIN32 else 'MISSING'}")
    print(f"pycaw (volume control): {'OK' if HAS_PYCAW else 'MISSING'}")
    print(f"screen_brightness_control: {'OK' if HAS_SBC else 'MISSING'}")
    print("=" * 50)
    print("Starting server on http://0.0.0.0:8765")
    print("Access from your phone: http://<your-pc-ip>:8765")
    print("=" * 50)

    uvicorn.run(app, host="0.0.0.0", port=8765)
