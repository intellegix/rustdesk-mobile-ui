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
from datetime import datetime
from typing import Optional

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

# Connection state
pc_connection: Optional[WebSocket] = None
web_connections: list[WebSocket] = []
pending_requests: dict[str, asyncio.Future] = {}

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
        "pc_connected": pc_connection is not None,
        "web_clients": len(web_connections)
    }


@app.get("/api/status")
async def relay_status():
    """Get relay connection status."""
    return {
        "pc_connected": pc_connection is not None,
        "web_clients": len(web_connections)
    }


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
    return await relay_to_pc("/api/apps", "GET")


@app.post("/api/apps")
async def save_apps(request: Request):
    data = await request.json()
    return await relay_to_pc("/api/apps", "POST", data)


@app.post("/api/launch/{app_id}")
async def launch_app(app_id: str):
    return await relay_to_pc(f"/api/launch/{app_id}", "POST")


@app.post("/api/launch-custom")
async def launch_custom(request: Request):
    data = await request.json()
    return await relay_to_pc("/api/launch-custom", "POST", data)


@app.get("/api/windows")
async def get_windows():
    return await relay_to_pc("/api/windows", "GET")


@app.post("/api/windows/{window_id}/focus")
async def focus_window(window_id: str):
    return await relay_to_pc(f"/api/windows/{window_id}/focus", "POST")


@app.post("/api/windows/{window_id}/close")
async def close_window(window_id: str):
    return await relay_to_pc(f"/api/windows/{window_id}/close", "POST")


@app.post("/api/windows/{window_id}/minimize")
async def minimize_window(window_id: str):
    return await relay_to_pc(f"/api/windows/{window_id}/minimize", "POST")


@app.post("/api/windows/{window_id}/maximize")
async def maximize_window(window_id: str):
    return await relay_to_pc(f"/api/windows/{window_id}/maximize", "POST")


@app.get("/api/system/volume")
async def get_volume():
    return await relay_to_pc("/api/system/volume", "GET")


@app.post("/api/system/volume")
async def set_volume(request: Request):
    data = await request.json()
    return await relay_to_pc("/api/system/volume", "POST", data)


@app.post("/api/system/volume/mute")
async def toggle_mute():
    return await relay_to_pc("/api/system/volume/mute", "POST")


@app.get("/api/system/brightness")
async def get_brightness():
    return await relay_to_pc("/api/system/brightness", "GET")


@app.post("/api/system/brightness")
async def set_brightness(request: Request):
    data = await request.json()
    return await relay_to_pc("/api/system/brightness", "POST", data)


@app.get("/api/clipboard")
async def get_clipboard():
    return await relay_to_pc("/api/clipboard", "GET")


@app.post("/api/clipboard")
async def set_clipboard(request: Request):
    data = await request.json()
    return await relay_to_pc("/api/clipboard", "POST", data)


@app.get("/api/rustdesk/status")
async def get_rustdesk_status():
    return await relay_to_pc("/api/rustdesk/status", "GET")


@app.get("/api/system/info")
async def get_system_info():
    return await relay_to_pc("/api/system/info", "GET")


@app.post("/api/action/lock")
async def action_lock():
    return await relay_to_pc("/api/action/lock", "POST")


@app.post("/api/action/sleep")
async def action_sleep():
    return await relay_to_pc("/api/action/sleep", "POST")


@app.post("/api/action/screenshot")
async def action_screenshot():
    return await relay_to_pc("/api/action/screenshot", "POST")


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
    pc_connection = websocket
    print(f"PC connected from {websocket.client.host}")

    # Notify web clients
    for wc in web_connections:
        try:
            await wc.send_json({"type": "pc_status", "connected": True})
        except:
            pass

    try:
        while True:
            data = await websocket.receive_json()

            if data.get("type") == "response":
                # Handle response to a pending request
                request_id = data.get("request_id")
                if request_id in pending_requests:
                    pending_requests[request_id].set_result(data.get("data", {}))

            elif data.get("type") == "broadcast":
                # Broadcast to all web clients
                for wc in web_connections:
                    try:
                        await wc.send_json(data)
                    except:
                        pass

    except WebSocketDisconnect:
        print("PC disconnected")
        pc_connection = None
        # Notify web clients
        for wc in web_connections:
            try:
                await wc.send_json({"type": "pc_status", "connected": False})
            except:
                pass


@app.websocket("/ws")
async def web_websocket(websocket: WebSocket):
    """WebSocket endpoint for web clients."""
    await websocket.accept()
    web_connections.append(websocket)

    # Send initial status
    await websocket.send_json({
        "type": "pc_status",
        "connected": pc_connection is not None
    })

    try:
        while True:
            data = await websocket.receive_json()

            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

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
