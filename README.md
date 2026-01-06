# Intellegix Remote Mobile

A mobile-first web interface for remote desktop control via WebSocket relay. Control your PC from any mobile device with a beautiful, responsive touch interface.

## Features

- **Real-time Screen Streaming** - View your desktop with live updates
- **Touch-optimized Controls** - Designed for mobile phones and tablets
- **Claude Code Integration** - Full keyboard shortcuts and slash commands for Claude Code CLI
- **Window Management** - Snap, resize, minimize windows remotely
- **Terminal Access** - Built-in terminal with command history
- **System Controls** - Volume, brightness, clipboard sync
- **Multi-tab Support** - Switch between terminal sessions (Ctrl+Alt+1-6)

## Architecture

```
Mobile Browser  <-->  Render.com (Relay Server)  <-->  PC (Relay Client)
    index.html         relay_server.py               relay_client.py
```

## Quick Start

### 1. Deploy Relay Server to Render

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com)

Or manually:
```bash
# Push to GitHub, then connect to Render.com
# Use render.yaml for automatic configuration
```

### 2. Run Relay Client on Your PC

```bash
# Install dependencies
pip install websockets pyautogui mss pillow pywin32 pycaw screen_brightness_control

# Run the client
python relay_client.py https://your-app.onrender.com
```

### 3. Access from Mobile

Open `https://your-app.onrender.com` on your phone.

## Claude Code Menu

The interface includes comprehensive Claude Code support:

| Tab | Contents |
|-----|----------|
| Keys | Y/N/Skip, Options 1-5, Navigation, Terminal shortcuts |
| /Cmds | 40+ slash commands organized by category |
| Vim | Full vim mode navigation and editing |
| Multi | Multiline input, quick bash commands |

## Files

| File | Description |
|------|-------------|
| `index.html` | Mobile web interface |
| `relay_server.py` | WebSocket relay server (deploy to Render) |
| `relay_client.py` | PC client that connects to relay |
| `render.yaml` | Render.com deployment config |
| `relay-24x7.bat` | Auto-restart batch file for 24/7 operation |

## Configuration

### Environment Variables (Relay Server)

| Variable | Description | Default |
|----------|-------------|---------|
| `RELAY_TOKEN` | Authentication token | `rustdesk2024` |
| `PORT` | Server port | `10000` |

### PC Client

```bash
# Basic usage
python relay_client.py https://your-server.onrender.com

# With custom token
python relay_client.py https://your-server.onrender.com --token your-token
```

## 24/7 Operation

### Windows

```batch
# Double-click to start with auto-restart
relay-24x7.bat
```

Or install as scheduled task:
```powershell
powershell -ExecutionPolicy Bypass -File install-relay-service.ps1
```

## Requirements

### Server (Render.com)
- Python 3.9+
- FastAPI
- uvicorn
- websockets

### Client (Windows PC)
- Python 3.9+
- pyautogui
- mss
- pillow
- pywin32
- pycaw
- screen_brightness_control

## License

MIT License - see [LICENSE](LICENSE)

## Author

**Intellegix** - [GitHub](https://github.com/intellegix)
