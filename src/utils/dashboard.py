"""Simple web dashboard for monitoring the agent."""

import asyncio
import logging
from datetime import datetime
from typing import Dict, Any, List
import json

logger = logging.getLogger(__name__)


class Dashboard:
    """Simple web dashboard using FastAPI."""

    def __init__(self, host: str = "0.0.0.0", port: int = 18789):
        """Initialize dashboard.

        Args:
            host: Host to bind to
            port: Port to bind to
        """
        self.host = host
        self.port = port
        self.status = {
            "state": "initializing",
            "phase": "N/A",
            "progress": "0/0",
            "uptime_seconds": 0,
            "last_update": datetime.now().isoformat()
        }
        self.logs = []
        self.max_logs = 100

        try:
            from fastapi import FastAPI
            from fastapi.responses import HTMLResponse, JSONResponse
            import uvicorn

            self.FastAPI = FastAPI
            self.HTMLResponse = HTMLResponse
            self.JSONResponse = JSONResponse
            self.uvicorn = uvicorn
            self.enabled = True

            logger.info(f"Dashboard initialized on {host}:{port}")
        except ImportError:
            logger.warning("FastAPI not installed. Dashboard disabled.")
            logger.warning("Install with: pip install fastapi uvicorn")
            self.enabled = False

    def update_status(self, **kwargs):
        """Update dashboard status.

        Args:
            **kwargs: Status fields to update
        """
        self.status.update(kwargs)
        self.status["last_update"] = datetime.now().isoformat()

    def add_log(self, message: str, level: str = "info"):
        """Add log entry.

        Args:
            message: Log message
            level: Log level
        """
        self.logs.append({
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "message": message
        })

        # Keep only last N logs
        if len(self.logs) > self.max_logs:
            self.logs = self.logs[-self.max_logs:]

    async def start(self):
        """Start dashboard server."""
        if not self.enabled:
            logger.warning("Dashboard not enabled")
            return

        app = self.FastAPI(title="Autonomous Agent Dashboard")

        @app.get("/", response_class=self.HTMLResponse)
        async def root():
            """Serve dashboard HTML."""
            return self._get_dashboard_html()

        @app.get("/api/status", response_class=self.JSONResponse)
        async def get_status():
            """Get current status."""
            return self.status

        @app.get("/api/logs", response_class=self.JSONResponse)
        async def get_logs():
            """Get recent logs."""
            return {"logs": self.logs[-50:]}  # Last 50 logs

        @app.get("/health")
        async def health():
            """Health check endpoint."""
            return {"status": "healthy", "timestamp": datetime.now().isoformat()}

        # Run server
        config = self.uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="info"
        )
        server = self.uvicorn.Server(config)

        logger.info(f"Starting dashboard server on http://{self.host}:{self.port}")
        await server.serve()

    def _get_dashboard_html(self) -> str:
        """Generate dashboard HTML.

        Returns:
            HTML string
        """
        return """<!DOCTYPE html>
<html>
<head>
    <title>Autonomous Agent Dashboard</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #1e1e1e;
            color: #d4d4d4;
            padding: 20px;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        .header {
            background: #2d2d2d;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
        }
        h1 { color: #4CAF50; margin-bottom: 10px; }
        .status {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        .stat-card {
            background: #2d2d2d;
            padding: 15px;
            border-radius: 8px;
            border-left: 4px solid #4CAF50;
        }
        .stat-label { color: #888; font-size: 0.9em; margin-bottom: 5px; }
        .stat-value { font-size: 1.5em; font-weight: bold; }
        .progress-bar {
            width: 100%;
            height: 30px;
            background: #2d2d2d;
            border-radius: 15px;
            overflow: hidden;
            margin: 20px 0;
        }
        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #4CAF50, #45a049);
            transition: width 0.3s;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-weight: bold;
        }
        .logs {
            background: #000;
            padding: 15px;
            border-radius: 8px;
            height: 400px;
            overflow-y: auto;
            font-family: 'Courier New', monospace;
            font-size: 0.9em;
        }
        .log-entry {
            padding: 5px 0;
            border-bottom: 1px solid #333;
        }
        .log-time { color: #888; }
        .log-info { color: #2196F3; }
        .log-success { color: #4CAF50; }
        .log-warning { color: #ff9800; }
        .log-error { color: #f44336; }
        .refresh-btn {
            background: #4CAF50;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 5px;
            cursor: pointer;
            font-size: 1em;
        }
        .refresh-btn:hover { background: #45a049; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🤖 Autonomous Agent Dashboard</h1>
            <p>Real-time monitoring and control</p>
        </div>

        <div class="status" id="status">
            <div class="stat-card">
                <div class="stat-label">State</div>
                <div class="stat-value" id="state">-</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Current Phase</div>
                <div class="stat-value" id="phase">-</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Progress</div>
                <div class="stat-value" id="progress">-</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Uptime</div>
                <div class="stat-value" id="uptime">-</div>
            </div>
        </div>

        <div class="progress-bar">
            <div class="progress-fill" id="progress-bar">0%</div>
        </div>

        <button class="refresh-btn" onclick="fetchData()">Refresh</button>

        <h2 style="margin: 20px 0 10px 0;">📝 Recent Logs</h2>
        <div class="logs" id="logs"></div>
    </div>

    <script>
        async function fetchData() {
            try {
                // Fetch status
                const statusRes = await fetch('/api/status');
                const status = await statusRes.json();

                document.getElementById('state').textContent = status.state || 'N/A';
                document.getElementById('phase').textContent = status.phase || 'N/A';
                document.getElementById('progress').textContent = status.progress || '0/0';

                // Calculate uptime
                const uptimeMins = Math.floor(status.uptime_seconds / 60);
                const uptimeHours = Math.floor(uptimeMins / 60);
                document.getElementById('uptime').textContent =
                    `${uptimeHours}h ${uptimeMins % 60}m`;

                // Update progress bar
                const [current, total] = (status.progress || '0/0').split('/').map(Number);
                const percent = total > 0 ? Math.round((current / total) * 100) : 0;
                const progressBar = document.getElementById('progress-bar');
                progressBar.style.width = percent + '%';
                progressBar.textContent = percent + '%';

                // Fetch logs
                const logsRes = await fetch('/api/logs');
                const logsData = await logsRes.json();

                const logsDiv = document.getElementById('logs');
                logsDiv.innerHTML = logsData.logs.map(log => {
                    const levelClass = 'log-' + log.level;
                    const time = new Date(log.timestamp).toLocaleTimeString();
                    return `<div class="log-entry">
                        <span class="log-time">[${time}]</span>
                        <span class="${levelClass}">${log.message}</span>
                    </div>`;
                }).join('');

                // Scroll to bottom
                logsDiv.scrollTop = logsDiv.scrollHeight;

            } catch (error) {
                console.error('Error fetching data:', error);
            }
        }

        // Auto-refresh every 2 seconds
        setInterval(fetchData, 2000);

        // Initial fetch
        fetchData();
    </script>
</body>
</html>
"""
