"""
Homelab Monitor — FastAPI Backend
==================================
Provides REST + WebSocket endpoints for:
  • Docker container management (list, start, stop, restart, remove)
  • Docker Compose project management (up, down, rebuild, ps)
  • Host system metrics (CPU, RAM, Disk, Network, Uptime)
  • Real-time container log streaming via WebSocket

Runs locally on the server — accesses Docker socket directly.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import subprocess
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import docker
import psutil
from docker.errors import APIError, NotFound
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─── Docker Client ────────────────────────────────────────────────────────────

def get_docker_client() -> docker.DockerClient:
    """Create Docker client from socket."""
    return docker.from_env()


# ─── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Validate Docker connectivity on startup."""
    try:
        client = get_docker_client()
        client.ping()
        print("✓ Docker daemon connected")
        client.close()
    except Exception as e:
        print(f"✗ Docker connection failed: {e}")
        print("  Make sure Docker socket is accessible")
    yield


# ─── App Setup ────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Homelab Monitor",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("homelab")

BOOT_TIME = psutil.boot_time()

# ─── Metrics History (in-memory ring buffer) ──────────────────────────────────
# Stores last 90 data points = 30 min at 20s intervals
METRICS_HISTORY: collections.deque = collections.deque(maxlen=90)

# ─── Action Audit Log ─────────────────────────────────────────────────────────
# Stores last 200 actions performed via the panel
ACTION_LOG: collections.deque = collections.deque(maxlen=200)

def log_action(target: str, action: str, result: str = "ok", detail: str = ""):
    ACTION_LOG.appendleft({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "action": action,
        "result": result,
        "detail": detail,
    })


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"Unhandled error on {request.url.path}: {exc}\n{traceback.format_exc()}")
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc), "path": str(request.url.path)},
    )


# ─── Models ───────────────────────────────────────────────────────────────────

class ContainerAction(BaseModel):
    action: str  # start | stop | restart | remove | pause | unpause

class ComposeAction(BaseModel):
    action: str       # up | down | rebuild | pull | ps
    project_dir: str  # absolute path to docker-compose directory
    service: str | None = None  # optional: target specific service


# ═══════════════════════════════════════════════════════════════════════════════
#  SYSTEM METRICS
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/system")
async def get_system_metrics() -> dict[str, Any]:
    """Return host CPU, memory, disk, network, and uptime info."""
    cpu_freq = psutil.cpu_freq()
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    net = psutil.net_io_counters()
    temps = {}

    try:
        temp_data = psutil.sensors_temperatures()
        for name, entries in temp_data.items():
            temps[name] = [
                {"label": e.label or name, "current": e.current, "high": e.high, "critical": e.critical}
                for e in entries
            ]
    except (AttributeError, RuntimeError):
        pass

    load_1, load_5, load_15 = os.getloadavg()

    return {
        "cpu": {
            "percent": psutil.cpu_percent(interval=0.5),
            "per_cpu": psutil.cpu_percent(interval=0.1, percpu=True),
            "cores_physical": psutil.cpu_count(logical=False),
            "cores_logical": psutil.cpu_count(logical=True),
            "freq_current": round(cpu_freq.current, 0) if cpu_freq else None,
            "freq_max": round(cpu_freq.max, 0) if cpu_freq else None,
            "load_avg": {"1m": round(load_1, 2), "5m": round(load_5, 2), "15m": round(load_15, 2)},
        },
        "memory": {
            "total": mem.total,
            "used": mem.used,
            "available": mem.available,
            "percent": mem.percent,
            "swap_total": swap.total,
            "swap_used": swap.used,
            "swap_percent": swap.percent,
        },
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "percent": disk.percent,
        },
        "network": {
            "bytes_sent": net.bytes_sent,
            "bytes_recv": net.bytes_recv,
            "packets_sent": net.packets_sent,
            "packets_recv": net.packets_recv,
        },
        "temperatures": temps,
        "uptime_seconds": int(time.time() - BOOT_TIME),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  DOCKER CONTAINERS
# ═══════════════════════════════════════════════════════════════════════════════

def _container_info(c) -> dict:
    """Extract useful info from a Docker container object."""
    # Compute CPU/Memory from stats (quick snapshot)
    stats = {}
    try:
        raw = c.stats(stream=False)
        # CPU
        cpu_delta = raw["cpu_stats"]["cpu_usage"]["total_usage"] - raw["precpu_stats"]["cpu_usage"]["total_usage"]
        sys_delta = raw["cpu_stats"]["system_cpu_usage"] - raw["precpu_stats"].get("system_cpu_usage", 0)
        n_cpus = raw["cpu_stats"].get("online_cpus", 1)
        cpu_pct = (cpu_delta / sys_delta) * n_cpus * 100.0 if sys_delta > 0 else 0.0
        # Memory
        mem_usage = raw["memory_stats"].get("usage", 0)
        mem_limit = raw["memory_stats"].get("limit", 1)
        stats = {
            "cpu_percent": round(cpu_pct, 2),
            "memory_usage": mem_usage,
            "memory_limit": mem_limit,
            "memory_percent": round((mem_usage / mem_limit) * 100, 2) if mem_limit > 0 else 0,
        }
    except Exception:
        stats = {"cpu_percent": 0, "memory_usage": 0, "memory_limit": 0, "memory_percent": 0}

    ports = {}
    try:
        raw_ports = c.attrs.get("NetworkSettings", {}).get("Ports") or {}
        for container_port, bindings in raw_ports.items():
            if bindings:
                ports[container_port] = [{"HostIp": b.get("HostIp", ""), "HostPort": b.get("HostPort", "")} for b in bindings]
    except Exception:
        pass

    labels = c.labels or {}

    try:
        image_name = str(c.image.tags[0]) if c.image.tags else c.attrs.get("Config", {}).get("Image", "unknown")
    except Exception:
        image_name = c.attrs.get("Config", {}).get("Image", "unknown")

    return {
        "id": c.short_id,
        "id_full": c.id,
        "name": c.name,
        "image": image_name,
        "status": c.status,
        "state": c.attrs.get("State", {}).get("Health", {}).get("Status", c.status),
        "created": c.attrs.get("Created", ""),
        "started_at": c.attrs.get("State", {}).get("StartedAt", ""),
        "ports": ports,
        "labels": labels,
        "compose_project": labels.get("com.docker.compose.project", ""),
        "compose_service": labels.get("com.docker.compose.service", ""),
        "stats": stats,
    }


@app.get("/api/containers")
async def list_containers(all: bool = Query(True, description="Include stopped containers")):
    """List all Docker containers with stats."""
    client = get_docker_client()
    try:
        containers = client.containers.list(all=all)
        results = []
        for c in containers:
            try:
                # Image name — handle deleted/dangling images
                try:
                    image_name = str(c.image.tags[0]) if c.image.tags else c.attrs.get("Config", {}).get("Image", "unknown")
                except Exception:
                    image_name = c.attrs.get("Config", {}).get("Image", "unknown")

                # Ports — handle malformed bindings
                ports = {}
                try:
                    raw_ports = c.attrs.get("NetworkSettings", {}).get("Ports") or {}
                    for cp, bindings in raw_ports.items():
                        if bindings:
                            ports[cp] = [{"HostIp": b.get("HostIp", ""), "HostPort": b.get("HostPort", "")} for b in bindings]
                except Exception:
                    pass

                labels = c.labels or {}
                info = {
                    "id": c.short_id,
                    "id_full": c.id,
                    "name": c.name,
                    "image": image_name,
                    "status": c.status,
                    "state_health": c.attrs.get("State", {}).get("Health", {}).get("Status", ""),
                    "created": c.attrs.get("Created", ""),
                    "started_at": c.attrs.get("State", {}).get("StartedAt", ""),
                    "ports": ports,
                    "labels": labels,
                    "compose_project": labels.get("com.docker.compose.project", ""),
                    "compose_service": labels.get("com.docker.compose.service", ""),
                }
                results.append(info)
            except Exception as e:
                # Skip broken container but log it
                results.append({
                    "id": getattr(c, 'short_id', 'unknown'),
                    "id_full": getattr(c, 'id', ''),
                    "name": getattr(c, 'name', 'unknown'),
                    "image": "error",
                    "status": getattr(c, 'status', 'unknown'),
                    "state_health": "",
                    "created": "",
                    "started_at": "",
                    "ports": {},
                    "labels": {},
                    "compose_project": "",
                    "compose_service": "",
                    "_error": str(e),
                })
        return {"containers": results, "total": len(results)}
    finally:
        client.close()


@app.get("/api/containers/{container_id}/stats")
async def container_stats(container_id: str):
    """Get detailed stats for a single container."""
    client = get_docker_client()
    try:
        c = client.containers.get(container_id)
        return _container_info(c)
    except NotFound:
        raise HTTPException(404, f"Container {container_id} not found")
    finally:
        client.close()


@app.post("/api/containers/{container_id}")
async def container_action(container_id: str, body: ContainerAction):
    """Perform an action on a container: start, stop, restart, remove, pause, unpause."""
    client = get_docker_client()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        raise HTTPException(404, f"Container {container_id} not found")

    actions = {
        "start": lambda: container.start(),
        "stop": lambda: container.stop(timeout=15),
        "restart": lambda: container.restart(timeout=15),
        "remove": lambda: container.remove(force=True),
        "pause": lambda: container.pause(),
        "unpause": lambda: container.unpause(),
    }

    if body.action not in actions:
        raise HTTPException(400, f"Unknown action: {body.action}. Valid: {list(actions.keys())}")

    try:
        actions[body.action]()
        log_action(target=container.name, action=body.action, result="ok")
        return {"ok": True, "container": container_id, "action": body.action}
    except APIError as e:
        log_action(target=container.name, action=body.action, result="error", detail=str(e.explanation))
        raise HTTPException(500, f"Docker error: {e.explanation}")
    finally:
        client.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  CONTAINER LOGS (WebSocket — real-time streaming)
# ═══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/logs/{container_id}")
async def stream_logs(ws: WebSocket, container_id: str, tail: int = 100):
    """Stream container logs via WebSocket."""
    await ws.accept()
    client = get_docker_client()

    try:
        container = client.containers.get(container_id)
    except NotFound:
        await ws.send_json({"error": f"Container {container_id} not found"})
        await ws.close()
        return

    queue: asyncio.Queue = asyncio.Queue()
    stop_event = asyncio.Event()

    def _read_logs():
        """Blocking log reader — runs in a thread."""
        try:
            log_stream = container.logs(stream=True, follow=True, tail=tail, timestamps=True)
            for chunk in log_stream:
                if stop_event.is_set():
                    break
                line = chunk.decode("utf-8", errors="replace").strip()
                if line:
                    queue.put_nowait(line)
        except Exception as e:
            queue.put_nowait(f"__ERROR__:{e}")
        finally:
            queue.put_nowait("__DONE__")

    # Start blocking reader in a thread
    loop = asyncio.get_event_loop()
    reader_future = loop.run_in_executor(None, _read_logs)

    try:
        while True:
            line = await queue.get()
            if line == "__DONE__":
                break
            if line.startswith("__ERROR__:"):
                await ws.send_json({"error": line[10:]})
                break
            await ws.send_text(line)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        stop_event.set()
        client.close()


@app.get("/api/containers/{container_id}/logs")
async def get_logs(container_id: str, tail: int = Query(200, ge=1, le=5000)):
    """Get recent logs for a container (non-streaming)."""
    client = get_docker_client()
    try:
        container = client.containers.get(container_id)
        # Run blocking call in thread
        raw_logs = await asyncio.to_thread(container.logs, tail=tail, timestamps=True)
        logs = raw_logs.decode("utf-8", errors="replace")
        return {"container": container_id, "logs": logs, "lines": tail}
    except NotFound:
        raise HTTPException(404, f"Container {container_id} not found")
    finally:
        client.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  INTERACTIVE TERMINAL (WebSocket — docker exec)
# ═══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/exec/{container_id}")
async def exec_terminal(ws: WebSocket, container_id: str):
    """Interactive shell inside a container via WebSocket.

    Protocol:
        → client sends JSON: {"type": "input", "data": "ls -la\\n"}
        → client sends JSON: {"type": "resize", "cols": 120, "rows": 40}
        ← server sends JSON: {"type": "output", "data": "..."}
        ← server sends JSON: {"type": "error", "data": "..."}
        ← server sends JSON: {"type": "exit", "code": 0}
    """
    await ws.accept()
    client = get_docker_client()

    try:
        container = client.containers.get(container_id)
    except NotFound:
        await ws.send_json({"type": "error", "data": f"Container {container_id} not found"})
        await ws.close()
        return

    if container.status != "running":
        await ws.send_json({"type": "error", "data": f"Container is {container.status}, must be running"})
        await ws.close()
        return

    # Detect available shell
    shell = "/bin/sh"
    for candidate in ["/bin/bash", "/bin/zsh", "/bin/ash"]:
        try:
            test = container.exec_run(f"test -x {candidate}", demux=False)
            if test.exit_code == 0:
                shell = candidate
                break
        except Exception:
            pass

    # Create exec instance with TTY
    try:
        exec_id = client.api.exec_create(
            container.id,
            cmd=shell,
            stdin=True,
            stdout=True,
            stderr=True,
            tty=True,
            environment={"TERM": "xterm-256color"},
        )
        sock = client.api.exec_start(exec_id["Id"], socket=True, tty=True)
        # Get the underlying socket
        raw_sock = sock._sock
        raw_sock.setblocking(False)
    except APIError as e:
        await ws.send_json({"type": "error", "data": f"Exec failed: {e.explanation}"})
        await ws.close()
        return
    except Exception as e:
        await ws.send_json({"type": "error", "data": f"Exec failed: {str(e)}"})
        await ws.close()
        return

    await ws.send_json({"type": "output", "data": f"Connected to {container.name} ({shell})\r\n"})

    stop_event = asyncio.Event()

    async def _read_from_container():
        """Read output from the exec socket and send to WebSocket."""
        loop = asyncio.get_event_loop()
        while not stop_event.is_set():
            try:
                data = await loop.run_in_executor(None, lambda: raw_sock.recv(4096))
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                await ws.send_json({"type": "output", "data": text})
            except BlockingIOError:
                await asyncio.sleep(0.01)
            except OSError:
                break
            except Exception:
                break

        if not stop_event.is_set():
            # Get exit code
            try:
                inspect = client.api.exec_inspect(exec_id["Id"])
                code = inspect.get("ExitCode", -1)
                await ws.send_json({"type": "exit", "code": code})
            except Exception:
                await ws.send_json({"type": "exit", "code": -1})

    # Start reader task
    reader_task = asyncio.create_task(_read_from_container())

    try:
        while True:
            msg = await ws.receive_json()
            msg_type = msg.get("type", "")

            if msg_type == "input":
                data = msg.get("data", "")
                if data:
                    try:
                        raw_sock.sendall(data.encode("utf-8"))
                    except OSError:
                        break

            elif msg_type == "resize":
                cols = msg.get("cols", 80)
                rows = msg.get("rows", 24)
                try:
                    client.api.exec_resize(exec_id["Id"], height=rows, width=cols)
                except Exception:
                    pass

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Terminal error: {e}")
    finally:
        stop_event.set()
        reader_task.cancel()
        try:
            raw_sock.close()
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
        client.close()

@app.websocket("/ws/metrics")
async def stream_metrics(ws: WebSocket):
    """Stream system metrics every 2 seconds via WebSocket. Store history every 20s."""
    await ws.accept()
    tick = 0
    try:
        while True:
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            net = psutil.net_io_counters()
            load_1, load_5, load_15 = os.getloadavg()

            data = {
                "cpu_percent": psutil.cpu_percent(interval=0.3),
                "per_cpu": psutil.cpu_percent(interval=0.1, percpu=True),
                "memory_percent": mem.percent,
                "memory_used": mem.used,
                "memory_total": mem.total,
                "disk_percent": disk.percent,
                "disk_used": disk.used,
                "disk_total": disk.total,
                "net_sent": net.bytes_sent,
                "net_recv": net.bytes_recv,
                "load_avg": [round(load_1, 2), round(load_5, 2), round(load_15, 2)],
                "uptime_seconds": int(time.time() - BOOT_TIME),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            await ws.send_json(data)

            # Store snapshot every 20s (every 10 ticks × 2s)
            tick += 1
            if tick % 10 == 0:
                METRICS_HISTORY.append({
                    "cpu": data["cpu_percent"],
                    "mem": data["memory_percent"],
                    "disk": data["disk_percent"],
                    "net_sent": data["net_sent"],
                    "net_recv": data["net_recv"],
                    "load": data["load_avg"][0],
                    "ts": data["timestamp"],
                })

            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


@app.get("/api/metrics/history")
async def get_metrics_history():
    """Return stored metrics history (up to 30min)."""
    return {"history": list(METRICS_HISTORY), "points": len(METRICS_HISTORY)}


@app.get("/api/actions")
async def get_action_log(limit: int = Query(50, ge=1, le=200)):
    """Return recent action audit log."""
    return {"actions": list(ACTION_LOG)[:limit]}


# ═══════════════════════════════════════════════════════════════════════════════
#  DOCKER COMPOSE
# ═══════════════════════════════════════════════════════════════════════════════

def _find_compose_projects() -> list[dict]:
    """Discover docker compose projects from running containers."""
    client = get_docker_client()
    try:
        containers = client.containers.list(all=True)
        projects: dict[str, dict] = {}
        for c in containers:
            labels = c.labels or {}
            project = labels.get("com.docker.compose.project", "")
            workdir = labels.get("com.docker.compose.project.working_dir", "")
            config = labels.get("com.docker.compose.project.config_files", "")
            service = labels.get("com.docker.compose.service", "")
            if project:
                if project not in projects:
                    projects[project] = {
                        "name": project,
                        "working_dir": workdir,
                        "config_files": config,
                        "services": [],
                    }
                projects[project]["services"].append({
                    "name": service,
                    "status": c.status,
                    "container_id": c.short_id,
                })
        return list(projects.values())
    finally:
        client.close()


@app.get("/api/compose")
async def list_compose_projects():
    """List all detected Docker Compose projects."""
    return {"projects": _find_compose_projects()}


@app.post("/api/compose")
async def compose_action(body: ComposeAction):
    """Execute a Docker Compose action on a project directory."""
    project_dir = Path(body.project_dir)

    # Validate compose file exists
    compose_files = ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"]
    found = any((project_dir / f).exists() for f in compose_files)
    if not found:
        raise HTTPException(400, f"No compose file found in {project_dir}")

    base_cmd = ["docker", "compose"]
    service_args = [body.service] if body.service else []

    cmds = {
        "up":      base_cmd + ["up", "-d"] + service_args,
        "down":    base_cmd + ["down"],
        "pull":    base_cmd + ["pull"] + service_args,
        "rebuild": base_cmd + ["up", "-d", "--build", "--force-recreate"] + service_args,
        "ps":      base_cmd + ["ps", "--format", "json"],
    }

    if body.action not in cmds:
        raise HTTPException(400, f"Unknown action: {body.action}. Valid: {list(cmds.keys())}")

    try:
        result = subprocess.run(
            cmds[body.action],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
        return {
            "ok": result.returncode == 0,
            "action": body.action,
            "project_dir": str(project_dir),
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Command timed out (120s)")
    except Exception as e:
        raise HTTPException(500, str(e))


# ═══════════════════════════════════════════════════════════════════════════════
#  DOCKER INFO
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/docker/info")
async def docker_info():
    """Get Docker daemon info and version."""
    client = get_docker_client()
    try:
        info = client.info()
        version = client.version()
        return {
            "version": version.get("Version", ""),
            "api_version": version.get("ApiVersion", ""),
            "os": info.get("OperatingSystem", ""),
            "arch": info.get("Architecture", ""),
            "kernel": info.get("KernelVersion", ""),
            "containers_running": info.get("ContainersRunning", 0),
            "containers_paused": info.get("ContainersPaused", 0),
            "containers_stopped": info.get("ContainersStopped", 0),
            "images": info.get("Images", 0),
            "storage_driver": info.get("Driver", ""),
            "docker_root": info.get("DockerRootDir", ""),
        }
    finally:
        client.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  DOCKER IMAGES
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/images")
async def list_images():
    """List all Docker images."""
    client = get_docker_client()
    try:
        images = client.images.list()
        results = []
        for img in images:
            results.append({
                "id": img.short_id.replace("sha256:", ""),
                "tags": img.tags,
                "size": img.attrs.get("Size", 0),
                "created": img.attrs.get("Created", ""),
            })
        return {"images": results, "total": len(results)}
    finally:
        client.close()


@app.delete("/api/images/{image_id}")
async def remove_image(image_id: str, force: bool = Query(False)):
    """Remove a Docker image."""
    client = get_docker_client()
    try:
        client.images.remove(image_id, force=force)
        return {"ok": True, "image": image_id}
    except NotFound:
        raise HTTPException(404, f"Image {image_id} not found")
    except APIError as e:
        raise HTTPException(500, f"Docker error: {e.explanation}")
    finally:
        client.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  SERVE FRONTEND
# ═══════════════════════════════════════════════════════════════════════════════

STATIC_DIR = Path(__file__).parent / "static"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
async def serve_index():
    """Serve the frontend."""
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return JSONResponse({"error": "Frontend not found. Place index.html in /static/"}, 404)


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=9090,
        reload=False,
        log_level="info",
    )