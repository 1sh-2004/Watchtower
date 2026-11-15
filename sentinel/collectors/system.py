"""Local hardware and OS metrics: CPU, RAM, disk usage and I/O, network, GPU,
and the heaviest processes."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

import psutil

GB = 1024 ** 3
_IDLE_NAMES = {"System Idle Process", "Idle"}


def rate(prev: Optional[float], cur: Optional[float], dt: float) -> Optional[float]:
    """Per-second rate between two monotonically increasing counters.

    Returns None when a rate cannot be computed, and 0 on counter reset.
    """
    if prev is None or cur is None or dt <= 0:
        return None
    delta = cur - prev
    return 0.0 if delta < 0 else delta / dt


def parse_nvidia_smi(output: str) -> List[Dict[str, Any]]:
    """Parse `nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu
    --format=csv,noheader,nounits`. Unsupported fields ('[N/A]') become None."""

    def num(text: str) -> Optional[float]:
        try:
            return float(text)
        except ValueError:
            return None

    gpus = []
    for line in output.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        gpus.append({
            "name": parts[0],
            "util_percent": num(parts[1]),
            "mem_used_mb": num(parts[2]),
            "mem_total_mb": num(parts[3]),
            "temp_c": num(parts[4]),
        })
    return gpus


def read_gpus(timeout: float = 3.0) -> List[Dict[str, Any]]:
    """NVIDIA GPU metrics via nvidia-smi. Returns [] when unavailable."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=timeout, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    return parse_nvidia_smi(out)


class SystemCollector:
    """Stateful collector: keeps the previous I/O counters to compute rates."""

    def __init__(self, disk_path: Optional[str] = None) -> None:
        self.disk_path = disk_path or os.path.abspath(os.sep)
        self._last_t: Optional[float] = None
        self._last_disk = None
        self._last_net = None
        psutil.cpu_percent(None)  # prime so the first real sample is meaningful

    def sample(self) -> Dict[str, Any]:
        now = time.time()
        dt = (now - self._last_t) if self._last_t else 0.0

        ram = psutil.virtual_memory()
        disk = psutil.disk_usage(self.disk_path)
        dio = psutil.disk_io_counters()  # None inside some containers/VMs
        nio = psutil.net_io_counters()

        read_bps = write_bps = up_bps = down_bps = None
        if self._last_disk is not None and dio is not None:
            read_bps = rate(self._last_disk.read_bytes, dio.read_bytes, dt)
            write_bps = rate(self._last_disk.write_bytes, dio.write_bytes, dt)
        if self._last_net is not None and nio is not None:
            up_bps = rate(self._last_net.bytes_sent, nio.bytes_sent, dt)
            down_bps = rate(self._last_net.bytes_recv, nio.bytes_recv, dt)
        self._last_t, self._last_disk, self._last_net = now, dio, nio

        def r(v: Optional[float]) -> Optional[float]:
            return None if v is None else round(v, 1)

        return {
            "ts": int(now),
            "cpu": {"percent": round(psutil.cpu_percent(None), 1), "count": psutil.cpu_count()},
            "ram": {
                "percent": round(ram.percent, 1),
                "used_gb": round(ram.used / GB, 2),
                "total_gb": round(ram.total / GB, 2),
            },
            "disk": {
                "percent": round(disk.percent, 1),
                "used_gb": round(disk.used / GB, 2),
                "total_gb": round(disk.total / GB, 2),
                "read_bps": r(read_bps),
                "write_bps": r(write_bps),
                "io_supported": dio is not None,
            },
            "network": {"up_bps": r(up_bps), "down_bps": r(down_bps)},
            "gpus": read_gpus(),
            "uptime": int(now - psutil.boot_time()),
        }

    def top_processes(self, n: int = 10) -> Dict[str, Any]:
        """Heaviest processes. CPU is normalised to total capacity (0-100%) so it is
        comparable with the system CPU figure. psutil caches Process objects, so
        cpu_percent() measures since the previous call (the background loop)."""
        cores = psutil.cpu_count() or 1
        rows = []
        for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            info = proc.info
            name = info.get("name") or "?"
            if name in _IDLE_NAMES:
                continue
            cpu = (info.get("cpu_percent") or 0.0) / cores
            mem = info.get("memory_percent") or 0.0
            if cpu > 0 or mem > 0.05:
                rows.append({"pid": info["pid"], "name": name, "cpu": round(cpu, 1), "memory": round(mem, 1)})
        rows.sort(key=lambda x: (x["cpu"], x["memory"]), reverse=True)
        return {"processes": rows[:n], "total_count": len(psutil.pids())}
