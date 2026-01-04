#!/usr/bin/env python3
"""
RustDesk Mobile UI - Relay Server (Deploy to Render.com)
Relays commands between the web frontend and your local PC.
"""

import asyncio
import json
import os
import secrets
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
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

# Simple auth token (set via environment variable)
AUTH_TOKEN = os.environ.get("RELAY_AUTH_TOKEN", "change-me-in-production")


class RelayMessage(BaseModel):
    type: str  # "request", "response", "broadcast"
    request_id: Optional[str] = None
    endpoint: Optional[str] = None
    method: Optional[str] = None
    data: Optional[dict] = None


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

    # Check auth token
    token = websocket.query_params.get("token")
    if token != AUTH_TOKEN:
        await websocket.close(code=4001, reason="Invalid token")
        return

    await websocket.accept()
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
async def serve_frontend():
    """Serve the frontend."""
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
    </body>
    </html>
    """)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8765))
    print(f"Starting relay server on port {port}")
    print(f"Auth token: {AUTH_TOKEN}")
    uvicorn.run(app, host="0.0.0.0", port=port)
