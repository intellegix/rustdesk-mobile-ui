#!/usr/bin/env python3
"""
RustDesk Mobile UI - Relay Server (Deploy to Render.com)
Relays commands between the web frontend and your local PC.
"""

import asyncio
import json
import os
import secrets
import hashlib
import time
from datetime import datetime
from typing import Optional, Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Response, Cookie, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="RustDesk Mobile UI Relay", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Connection state - Multi-host support
class HostConnection:
    """Represents a connected host PC."""
    def __init__(self, ws: WebSocket, host_id: str, host_name: str = None):
        self.ws = ws
        self.host_id = host_id
        self.host_name = host_name or host_id
        self.platform = "Unknown"
        self.platform_version = ""
        self.capabilities = {}
        self.connected_at = datetime.now()

    def to_dict(self):
        return {
            "host_id": self.host_id,
            "host_name": self.host_name,
            "platform": self.platform,
            "platform_version": self.platform_version,
            "capabilities": self.capabilities,
            "connected_at": self.connected_at.isoformat()
        }


# Dictionary of connected hosts: host_id -> HostConnection
pc_connections: dict[str, HostConnection] = {}
# Currently selected host for each web client (session_id -> host_id)
selected_host: dict[str, str] = {}
# Legacy: single connection reference for backwards compatibility
pc_connection: Optional[WebSocket] = None
web_connections: list[WebSocket] = []
pending_requests: dict[str, asyncio.Future] = {}

# Message deduplication tracking (prevents double-sends)
recent_requests: Dict[str, float] = {}  # request_hash -> timestamp
DEDUP_WINDOW = 2.0  # 2-second deduplication window

# Shared password for both site access and relay authentication
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "Devops$@2026")
AUTH_TOKEN = SITE_PASSWORD  # Use same password for relay auth
valid_sessions: set[str] = set()


class RelayMessage(BaseModel):
    type: str  # "request", "response", "broadcast"
    request_id: Optional[str] = None
    endpoint: Optional[str] = None
    method: Optional[str] = None
    data: Optional[dict] = None


class ConnectionHealthMonitor:
    """Monitors connection health and automatically removes dead connections."""

    def __init__(self):
        self.health_checks = {}
        self.check_interval = 10.0  # 10-second health checks
        self.monitoring_task = None
        self.pending_pongs = {}  # ping_id -> timestamp

    async def start_monitoring(self):
        """Start continuous health monitoring"""
        if self.monitoring_task is None:
            self.monitoring_task = asyncio.create_task(self._monitor_loop())
            print("[HEALTH] Started connection health monitoring")

    async def stop_monitoring(self):
        """Stop health monitoring"""
        if self.monitoring_task:
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                pass
            self.monitoring_task = None

    async def _monitor_loop(self):
        """Main monitoring loop"""
        while True:
            try:
                await self._check_all_connections()
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[HEALTH] Monitor loop error: {e}")
                await asyncio.sleep(5)

    async def _check_all_connections(self):
        """Check health of all PC connections"""
        global pc_connections
        current_time = time.time()

        # Clean up old pending pongs
        expired_pongs = [ping_id for ping_id, timestamp in self.pending_pongs.items()
                        if current_time - timestamp > 10.0]
        for ping_id in expired_pongs:
            self.pending_pongs.pop(ping_id, None)

        for host_id, host_conn in list(pc_connections.items()):
            try:
                # Send health ping
                ping_id = secrets.token_hex(4)
                self.pending_pongs[ping_id] = current_time

                await host_conn.ws.send_json({
                    "type": "health_ping",
                    "ping_id": ping_id,
                    "timestamp": current_time
                })

                # Wait for pong (with timeout)
                try:
                    await asyncio.wait_for(
                        self._wait_for_pong(ping_id),
                        timeout=5.0
                    )
                    print(f"[HEALTH] Host {host_id} healthy")

                except asyncio.TimeoutError:
                    print(f"[HEALTH] Host {host_id} unresponsive - removing")
                    await self._remove_dead_connection(host_id)

            except Exception as e:
                print(f"[HEALTH] Health check failed for {host_id}: {e}")
                await self._remove_dead_connection(host_id)

    async def _wait_for_pong(self, ping_id: str):
        """Wait for a specific pong response"""
        start_time = time.time()
        while ping_id in self.pending_pongs:
            if time.time() - start_time > 5.0:
                raise asyncio.TimeoutError()
            await asyncio.sleep(0.1)

    async def handle_health_pong(self, data: dict):
        """Handle received health pong"""
        ping_id = data.get("ping_id")
        if ping_id and ping_id in self.pending_pongs:
            server_timestamp = self.pending_pongs.pop(ping_id)
            latency = (time.time() - server_timestamp) * 1000
            print(f"[HEALTH] Received pong for {ping_id} (latency: {latency:.2f}ms)")

    async def _remove_dead_connection(self, host_id: str):
        """Remove a dead connection"""
        global pc_connections, pc_connection

        if host_id in pc_connections:
            try:
                await pc_connections[host_id].ws.close()
            except:
                pass
            pc_connections.pop(host_id, None)

            # Update legacy reference if needed
            if pc_connection and getattr(pc_connection, 'host_id', None) == host_id:
                pc_connection = None

            print(f"[HEALTH] Removed dead connection: {host_id}")

# Global health monitor
health_monitor = ConnectionHealthMonitor()


def is_authenticated(session_id: str) -> bool:
    """Check if session is authenticated."""
    return session_id in valid_sessions


LOGIN_PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>RustDesk Mobile UI - Login</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .login-container {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 16px;
            padding: 40px;
            width: 100%;
            max-width: 400px;
            backdrop-filter: blur(10px);
        }
        h1 {
            color: #f97316;
            font-size: 24px;
            margin-bottom: 8px;
            text-align: center;
        }
        .subtitle {
            color: #9ca3af;
            font-size: 14px;
            text-align: center;
            margin-bottom: 32px;
        }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            color: #d1d5db;
            font-size: 14px;
            display: block;
            margin-bottom: 8px;
        }
        input[type="password"] {
            width: 100%;
            padding: 14px 16px;
            background: rgba(0, 0, 0, 0.3);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 8px;
            color: #fff;
            font-size: 16px;
            outline: none;
            transition: border-color 0.2s;
        }
        input[type="password"]:focus {
            border-color: #f97316;
        }
        button {
            width: 100%;
            padding: 14px;
            background: #f97316;
            border: none;
            border-radius: 8px;
            color: #fff;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.2s;
        }
        button:hover {
            background: #ea580c;
        }
        .error {
            background: rgba(239, 68, 68, 0.2);
            border: 1px solid rgba(239, 68, 68, 0.3);
            color: #fca5a5;
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 20px;
            font-size: 14px;
            text-align: center;
        }
        .icon {
            text-align: center;
            font-size: 48px;
            margin-bottom: 16px;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <div class="icon">🔐</div>
        <h1>RustDesk Mobile UI</h1>
        <p class="subtitle">Enter password to access</p>
        {error}
        <form method="POST" action="/login">
            <div class="form-group">
                <label for="password">Password</label>
                <input type="password" id="password" name="password" required autofocus>
            </div>
            <button type="submit">Login</button>
        </form>
    </div>
</body>
</html>
"""


@app.get("/login", response_class=HTMLResponse)
async def login_page(error: str = ""):
    """Show login page."""
    error_html = f'<div class="error">{error}</div>' if error else ""
    return LOGIN_PAGE.replace("{error}", error_html)


@app.post("/login")
async def login(response: Response, password: str = Form(...)):
    """Handle login."""
    if password == SITE_PASSWORD:
        session_id = secrets.token_hex(32)
        valid_sessions.add(session_id)
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie(key="session", value=session_id, httponly=True, max_age=86400)
        return resp
    return RedirectResponse(url="/login?error=Invalid+password", status_code=303)


@app.get("/logout")
async def logout(response: Response, session: str = Cookie(None)):
    """Handle logout."""
    if session in valid_sessions:
        valid_sessions.discard(session)
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(key="session")
    return resp


@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "pc_connected": len(pc_connections) > 0,
        "hosts_count": len(pc_connections),
        "web_clients": len(web_connections)
    }


@app.get("/api/status")
async def relay_status():
    """Get relay connection status."""
    return {
        "pc_connected": len(pc_connections) > 0,
        "web_clients": len(web_connections),
        "hosts_count": len(pc_connections)
    }


@app.get("/api/hosts")
async def list_hosts():
    """List all connected host PCs."""
    return {
        "hosts": [host.to_dict() for host in pc_connections.values()]
    }


@app.post("/api/hosts/cleanup")
async def cleanup_hosts():
    """Remove all stale/zombie host connections."""
    global pc_connection, pc_connections

    # Find hosts without capabilities (zombie connections)
    zombie_ids = [
        host_id for host_id, host in pc_connections.items()
        if not host.capabilities
    ]

    # Remove zombies
    for host_id in zombie_ids:
        pc_connections.pop(host_id, None)

    # Reset pc_connection to a valid host if needed
    if pc_connections:
        # Select the first host with capabilities
        for host in pc_connections.values():
            if host.capabilities:
                pc_connection = host.ws
                break
    else:
        pc_connection = None

    return {
        "removed": len(zombie_ids),
        "remaining": len(pc_connections)
    }


@app.post("/api/hosts/select")
async def select_host(request: Request):
    """Select a host to control."""
    data = await request.json()
    host_id = data.get("host_id")

    if not host_id:
        raise HTTPException(status_code=400, detail="host_id required")

    if host_id not in pc_connections:
        raise HTTPException(status_code=404, detail="Host not connected")

    # For now, use a global selected host (can be per-session later)
    global pc_connection
    pc_connection = pc_connections[host_id].ws

    return {"status": "selected", "host_id": host_id}


def generate_request_hash(endpoint: str, method: str, data: dict) -> str:
    """Generate deterministic hash for request deduplication"""
    content = f"{endpoint}:{method}:{json.dumps(data, sort_keys=True) if data else 'null'}"
    return hashlib.md5(content.encode()).hexdigest()

async def relay_to_pc_deduplicated(endpoint: str, method: str = "GET", data: dict = None) -> dict:
    """Relay a request to the connected PC with deduplication to prevent double-sends"""
    global recent_requests

    # Check for duplicate request
    req_hash = generate_request_hash(endpoint, method, data)
    current_time = time.time()

    # Clean expired entries
    expired = [h for h, t in recent_requests.items() if current_time - t > DEDUP_WINDOW]
    for h in expired:
        recent_requests.pop(h, None)

    # Check for recent duplicate
    if req_hash in recent_requests:
        print(f"[DEDUP] Ignoring duplicate request: {endpoint}")
        return {"status": "deduplicated", "original_time": recent_requests[req_hash]}

    # Record request and proceed
    recent_requests[req_hash] = current_time
    return await relay_to_pc_reliable(endpoint, method, data)

async def relay_to_pc_reliable(endpoint: str, method: str = "GET", data: dict = None) -> dict:
    """Relay a request to the connected PC with retry logic for reliability"""
    max_retries = 3
    base_delay = 1.0

    for attempt in range(max_retries):
        try:
            return await relay_to_pc(endpoint, method, data)
        except (asyncio.TimeoutError, ConnectionError) as e:
            if attempt == max_retries - 1:
                raise HTTPException(status_code=504, detail=f"Request failed after {max_retries} attempts")

            delay = base_delay * (2 ** attempt)  # 1s, 2s, 4s
            print(f"[RETRY] Attempt {attempt + 1} failed for {endpoint}, retrying in {delay}s")
            await asyncio.sleep(delay)

async def relay_to_pc(endpoint: str, method: str = "GET", data: dict = None) -> dict:
    """Relay a request to the connected PC."""
    global pc_connection, pending_requests

    if pc_connection is None:
        raise HTTPException(status_code=503, detail="PC not connected")

    request_id = secrets.token_hex(8)
    future = asyncio.get_event_loop().create_future()
    pending_requests[request_id] = future

    try:
        await pc_connection.send_json({
            "type": "request",
            "request_id": request_id,
            "endpoint": endpoint,
            "method": method,
            "data": data
        })

        # Wait for response with timeout
        response = await asyncio.wait_for(future, timeout=30.0)
        return response
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Request timed out")
    finally:
        pending_requests.pop(request_id, None)


# Proxy all API endpoints to the PC
@app.get("/api/apps")
async def get_apps():
    return await relay_to_pc_deduplicated("/api/apps", "GET")


@app.post("/api/apps")
async def save_apps(request: Request):
    data = await request.json()
    return await relay_to_pc_deduplicated("/api/apps", "POST", data)


@app.post("/api/launch/{app_id}")
async def launch_app(app_id: str):
    return await relay_to_pc_deduplicated(f"/api/launch/{app_id}", "POST")


@app.post("/api/launch-custom")
async def launch_custom(request: Request):
    data = await request.json()
    return await relay_to_pc_deduplicated("/api/launch-custom", "POST", data)


@app.get("/api/windows")
async def get_windows():
    return await relay_to_pc_deduplicated("/api/windows", "GET")


@app.post("/api/windows/{window_id}/focus")
async def focus_window(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/focus", "POST")


@app.post("/api/windows/{window_id}/close")
async def close_window(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/close", "POST")


@app.post("/api/windows/{window_id}/minimize")
async def minimize_window(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/minimize", "POST")


@app.post("/api/windows/{window_id}/maximize")
async def maximize_window(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/maximize", "POST")


@app.get("/api/system/volume")
async def get_volume():
    return await relay_to_pc_deduplicated("/api/system/volume", "GET")


@app.post("/api/system/volume")
async def set_volume(request: Request):
    data = await request.json()
    return await relay_to_pc_deduplicated("/api/system/volume", "POST", data)


@app.post("/api/system/volume/mute")
async def toggle_mute():
    return await relay_to_pc_deduplicated("/api/system/volume/mute", "POST")


@app.get("/api/system/brightness")
async def get_brightness():
    return await relay_to_pc_deduplicated("/api/system/brightness", "GET")


@app.post("/api/system/brightness")
async def set_brightness(request: Request):
    data = await request.json()
    return await relay_to_pc_deduplicated("/api/system/brightness", "POST", data)


@app.get("/api/clipboard")
async def get_clipboard():
    return await relay_to_pc_deduplicated("/api/clipboard", "GET")


@app.post("/api/clipboard")
async def set_clipboard(request: Request):
    data = await request.json()
    return await relay_to_pc_deduplicated("/api/clipboard", "POST", data)


@app.post("/api/paste-image")
async def paste_image(request: Request):
    """Paste an image to the desktop clipboard and simulate Ctrl+V."""
    data = await request.json()
    return await relay_to_pc_deduplicated("/api/paste-image", "POST", data)


@app.get("/api/rustdesk/status")
async def get_rustdesk_status():
    return await relay_to_pc_deduplicated("/api/rustdesk/status", "GET")


@app.get("/api/rustdesk/devices")
async def get_rustdesk_devices():
    """Get saved RustDesk devices."""
    return await relay_to_pc_deduplicated("/api/rustdesk/devices", "GET")


@app.post("/api/rustdesk/connect")
async def rustdesk_connect(request: Request):
    """Connect to a RustDesk device."""
    data = await request.json()
    return await relay_to_pc_deduplicated("/api/rustdesk/connect", "POST", data)


@app.get("/api/system/info")
async def get_system_info():
    return await relay_to_pc_deduplicated("/api/system/info", "GET")


@app.post("/api/action/lock")
async def action_lock():
    return await relay_to_pc_deduplicated("/api/action/lock", "POST")


@app.post("/api/action/sleep")
async def action_sleep():
    return await relay_to_pc_deduplicated("/api/action/sleep", "POST")


@app.post("/api/action/screenshot")
async def action_screenshot():
    return await relay_to_pc_deduplicated("/api/action/screenshot", "POST")


# Window streaming endpoints
@app.get("/api/windows/{window_id}/info")
async def get_window_info(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/info", "GET")


@app.get("/api/windows/{window_id}/snapshot")
async def get_window_snapshot(window_id: str, quality: int = 60, max_width: int = 800):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/snapshot", "GET", {"quality": quality, "max_width": max_width})


# Chrome control endpoints
@app.post("/api/windows/{window_id}/chrome/navigate")
async def chrome_navigate(window_id: str, request: Request):
    data = await request.json()
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/chrome/navigate", "POST", data)


@app.post("/api/windows/{window_id}/chrome/back")
async def chrome_back(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/chrome/back", "POST")


@app.post("/api/windows/{window_id}/chrome/forward")
async def chrome_forward(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/chrome/forward", "POST")


@app.post("/api/windows/{window_id}/chrome/refresh")
async def chrome_refresh(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/chrome/refresh", "POST")


@app.post("/api/windows/{window_id}/chrome/new-tab")
async def chrome_new_tab(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/chrome/new-tab", "POST")


@app.post("/api/windows/{window_id}/chrome/close-tab")
async def chrome_close_tab(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/chrome/close-tab", "POST")


@app.post("/api/windows/{window_id}/chrome/next-tab")
async def chrome_next_tab(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/chrome/next-tab", "POST")


@app.post("/api/windows/{window_id}/chrome/prev-tab")
async def chrome_prev_tab(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/chrome/prev-tab", "POST")


# Window snap/split endpoints
@app.post("/api/windows/{window_id}/restore")
async def window_restore(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/restore", "POST")


@app.post("/api/windows/{window_id}/snap/left")
async def window_snap_left(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/snap/left", "POST")


@app.post("/api/windows/{window_id}/snap/right")
async def window_snap_right(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/snap/right", "POST")


@app.post("/api/windows/{window_id}/snap/top-left")
async def window_snap_top_left(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/snap/top-left", "POST")


@app.post("/api/windows/{window_id}/snap/top-right")
async def window_snap_top_right(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/snap/top-right", "POST")


@app.post("/api/windows/{window_id}/snap/bottom-left")
async def window_snap_bottom_left(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/snap/bottom-left", "POST")


@app.post("/api/windows/{window_id}/snap/bottom-right")
async def window_snap_bottom_right(window_id: str):
    return await relay_to_pc_deduplicated(f"/api/windows/{window_id}/snap/bottom-right", "POST")


# Folders API endpoints
@app.post("/api/folders/search")
async def folders_search(request: Request):
    data = await request.json()
    return await relay_to_pc_deduplicated("/api/folders/search", "POST", data)


@app.post("/api/folders/open")
async def folders_open(request: Request):
    data = await request.json()
    return await relay_to_pc_deduplicated("/api/folders/open", "POST", data)


@app.websocket("/ws/pc")
async def pc_websocket(websocket: WebSocket):
    """WebSocket endpoint for the PC client."""
    global pc_connection

    # Accept connection first (required by FastAPI before any interaction)
    await websocket.accept()

    # Check auth token
    token = websocket.query_params.get("token")
    if token != AUTH_TOKEN:
        await websocket.close(code=4001, reason="Invalid token")
        return

    # Require explicit host_id - no random fallback to prevent ghost connections
    host_id = websocket.query_params.get("host_id")
    if not host_id:
        print("PC connection rejected: missing host_id")
        await websocket.close(code=4002, reason="host_id required")
        return

    # Deduplication: Close old connection if same host_id reconnects
    if host_id in pc_connections:
        old_host = pc_connections[host_id]
        print(f"Host {host_id} reconnecting - closing old connection")
        try:
            await old_host.ws.close(code=4003, reason="Reconnected from another session")
        except:
            pass
        pc_connections.pop(host_id, None)
        # Clear pc_connection if it was the old one
        if pc_connection == old_host.ws:
            pc_connection = None

    # Create host connection (but don't announce yet - wait for host_register)
    host = HostConnection(websocket, host_id)
    host_registered = False  # Track if host has sent registration

    print(f"PC connected: {host_id} from {websocket.client.host} (awaiting registration)")

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")

            # Handle host registration message - this officially adds the host
            if msg_type == "host_register":
                host.host_name = data.get("host_name", host_id)
                host.platform = data.get("platform", "Unknown")
                host.platform_version = data.get("platform_version", "")
                host.capabilities = data.get("capabilities", {})

                # Now officially register the host
                if not host_registered:
                    pc_connections[host_id] = host
                    host_registered = True

                    # Set as active connection if no other hosts connected
                    if pc_connection is None:
                        pc_connection = websocket

                    print(f"Host registered: {host.host_name} ({host.platform})")
                    print(f"Total hosts connected: {len(pc_connections)}")

                    # Start health monitoring if this is the first host
                    if len(pc_connections) == 1:
                        await health_monitor.start_monitoring()

                    # Notify web clients about new host (only after registration)
                    for wc in web_connections:
                        try:
                            await wc.send_json({
                                "type": "host_connected",
                                "host": host.to_dict(),
                                "hosts_count": len(pc_connections)
                            })
                        except:
                            pass
                else:
                    # Host already registered, just update info
                    print(f"Host updated: {host.host_name} ({host.platform})")
                    for wc in web_connections:
                        try:
                            await wc.send_json({
                                "type": "host_updated",
                                "host": host.to_dict()
                            })
                        except:
                            pass
                continue

            if msg_type == "health_pong":
                # Handle health check response
                await health_monitor.handle_health_pong(data)
                continue

            if msg_type == "response":
                # Handle response to a pending request
                request_id = data.get("request_id")
                if request_id in pending_requests:
                    pending_requests[request_id].set_result(data.get("data", {}))

            elif msg_type == "broadcast":
                # Broadcast to all web clients
                for wc in web_connections:
                    try:
                        await wc.send_json(data)
                    except:
                        pass

            elif msg_type == "stream_frame":
                # Forward stream frame to all web clients
                for wc in web_connections:
                    try:
                        await wc.send_json(data)
                    except:
                        pass

            elif msg_type == "stream_error":
                # Forward stream error to all web clients
                for wc in web_connections:
                    try:
                        await wc.send_json(data)
                    except:
                        pass

            elif msg_type == "stream_status":
                # Forward stream status to all web clients
                for wc in web_connections:
                    try:
                        await wc.send_json(data)
                    except:
                        pass

            elif msg_type == "terminal_output":
                # Forward terminal output to all web clients
                for wc in web_connections:
                    try:
                        await wc.send_json(data)
                    except:
                        pass

    except WebSocketDisconnect:
        print(f"PC disconnected: {host_id} (was registered: {host_registered})")

        # Only cleanup if host was actually registered
        if host_registered:
            # Remove from connections
            pc_connections.pop(host_id, None)

            # If this was the active connection, switch to another or None
            if pc_connection == websocket:
                if pc_connections:
                    # Switch to first available host
                    first_host = next(iter(pc_connections.values()))
                    pc_connection = first_host.ws
                    print(f"Switched to host: {first_host.host_id}")
                else:
                    pc_connection = None

            print(f"Remaining hosts: {len(pc_connections)}")

            # Notify web clients about disconnected host
            for wc in web_connections:
                try:
                    await wc.send_json({
                        "type": "host_disconnected",
                        "host_id": host_id,
                        "hosts_count": len(pc_connections)
                    })
                except:
                    pass
        else:
            print(f"Unregistered connection closed (no cleanup needed)")


@app.websocket("/ws")
async def web_websocket(websocket: WebSocket):
    """WebSocket endpoint for web clients."""
    await websocket.accept()
    web_connections.append(websocket)

    # Send initial status with list of hosts
    await websocket.send_json({
        "type": "pc_status",
        "connected": len(pc_connections) > 0,
        "hosts": [host.to_dict() for host in pc_connections.values()],
        "hosts_count": len(pc_connections)
    })

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")

            # Log all non-ping messages
            if msg_type != "ping":
                print(f"[WS] Received from web: {msg_type} - {data}")

            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})

            elif msg_type in ("stream_start", "stream_stop", "stream_adjust"):
                # Forward stream control messages to PC
                if pc_connection:
                    try:
                        await pc_connection.send_json(data)
                    except:
                        await websocket.send_json({
                            "type": "stream_error",
                            "window_id": data.get("window_id"),
                            "error": "PC not connected"
                        })
                else:
                    await websocket.send_json({
                        "type": "stream_error",
                        "window_id": data.get("window_id"),
                        "error": "PC not connected"
                    })

            elif msg_type in ("terminal_start", "terminal_input", "terminal_stop", "terminal_keystroke", "terminal_command", "terminal_key"):
                # Forward terminal control messages to PC
                print(f"[RELAY] Terminal message: {msg_type}, window: {data.get('window_id', 'N/A')}, cmd: {data.get('command', data.get('key', 'N/A'))}")
                if pc_connection:
                    try:
                        await pc_connection.send_json(data)
                        print(f"[RELAY] Forwarded to PC successfully")
                    except Exception as e:
                        print(f"[RELAY] Failed to forward: {e}")
                        await websocket.send_json({
                            "type": "terminal_output",
                            "session_id": data.get("session_id", "default"),
                            "text": f"Failed to send to PC: {e}\n"
                        })
                else:
                    print(f"[RELAY] PC not connected, cannot forward terminal message")
                    await websocket.send_json({
                        "type": "terminal_output",
                        "session_id": data.get("session_id", "default"),
                        "text": "PC not connected\n"
                    })

            elif msg_type == "remote_mouse":
                # Forward mouse operations for text selection
                print(f"[RELAY] Mouse: {data.get('action')} at ({data.get('x')}, {data.get('y')})")
                if pc_connection:
                    try:
                        await pc_connection.send_json(data)
                    except Exception as e:
                        print(f"[RELAY] Failed to forward mouse: {e}")

            elif msg_type == "remote_scroll":
                # Forward scroll operations to PC
                print(f"[RELAY] Scroll: delta_y={data.get('delta_y')}")
                if pc_connection:
                    try:
                        await pc_connection.send_json(data)
                    except Exception as e:
                        print(f"[RELAY] Failed to forward scroll: {e}")

    except WebSocketDisconnect:
        if websocket in web_connections:
            web_connections.remove(websocket)


# Serve frontend
@app.get("/", response_class=HTMLResponse)
async def serve_frontend(session: str = Cookie(None)):
    """Serve the frontend (requires authentication)."""
    # Check authentication
    if not is_authenticated(session):
        return RedirectResponse(url="/login", status_code=303)

    # Try to find index.html in the same directory
    import os
    script_dir = os.path.dirname(os.path.abspath(__file__))
    index_path = os.path.join(script_dir, "index.html")

    if os.path.exists(index_path):
        return FileResponse(index_path)

    # Return a simple status page if no frontend
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head><title>RustDesk Mobile UI Relay</title></head>
    <body style="font-family: sans-serif; padding: 40px; text-align: center;">
        <h1>RustDesk Mobile UI Relay</h1>
        <p>PC Connected: <strong>{"Yes" if pc_connection else "No"}</strong></p>
        <p>Web Clients: <strong>{len(web_connections)}</strong></p>
        <p style="color: #666; margin-top: 40px;">
            Place index.html in the same directory to serve the mobile UI.
        </p>
        <p><a href="/logout">Logout</a></p>
    </body>
    </html>
    """)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8765))
    print(f"Starting relay server on port {port}")
    print(f"Auth token: {AUTH_TOKEN}")
    uvicorn.run(app, host="0.0.0.0", port=port)
