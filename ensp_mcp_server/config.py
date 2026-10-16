"""
eNSP MCP Server Configuration Module

设备注册表管理 + 自动发现。
通过 eNSP 本地 TCP 端口直连设备（无需配置 Cloud/Telnet）。
"""
import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from copy import deepcopy
from pathlib import Path
from pydantic import BaseModel

from .exceptions import DeviceNotFoundError, ToolFailure
from .topology_context import load_current_topology
from .validation import validate_name, validate_port, validate_topology_devices, validate_topology_payload

# ==================== 日志配置 ====================

_log_level = os.environ.get("ENSP_LOG_LEVEL", "WARNING").upper()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.WARNING),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("ensp_mcp")


# ==================== 配置模型 ====================

class ServerConfig(BaseModel):
    """服务器配置"""
    server_name: str = "eNSP_NetOps_Server"
    timeout: int = 30


# 全局配置实例
config = ServerConfig()


# ==================== 设备注册表 ====================

_ROOT_DIR = Path(os.path.dirname(os.path.dirname(__file__)))
_REGISTRY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "devices.json")
_TOPOLOGY_MANIFEST_FILE = _ROOT_DIR / "topology_registry.json"

# 支持两种格式:
#   简单: { "R1": 2000 }
#   详细: { "R1": { "port": 2000, "username": "admin", "password": "xxx" } }
_device_registry: dict[str, int | dict] = {}


def _topology_manifest_template() -> dict[str, object]:
    return {"managed_names": [], "previous_entries": {}}


def _load_topology_manifest() -> dict[str, object]:
    if not _TOPOLOGY_MANIFEST_FILE.exists():
        return _topology_manifest_template()

    try:
        with _TOPOLOGY_MANIFEST_FILE.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return _topology_manifest_template()

    if not isinstance(payload, dict):
        return _topology_manifest_template()

    managed_names = payload.get("managed_names", [])
    previous_entries = payload.get("previous_entries", {})
    return {
        "managed_names": managed_names if isinstance(managed_names, list) else [],
        "previous_entries": previous_entries if isinstance(previous_entries, dict) else {},
    }


def _save_topology_manifest(managed_names: list[str], previous_entries: dict[str, int | dict]) -> None:
    _write_json_atomic(_TOPOLOGY_MANIFEST_FILE, {
        "managed_names": managed_names, "previous_entries": previous_entries,
    })


def _clear_topology_manifest() -> None:
    if _TOPOLOGY_MANIFEST_FILE.exists():
        _TOPOLOGY_MANIFEST_FILE.unlink()


def _normalize_registry_entry(entry: int | dict, default_source: str = "legacy") -> int | dict:
    if isinstance(entry, dict):
        normalized = dict(entry)
        normalized.setdefault("_source", default_source)
        return normalized
    return {"port": int(entry), "_source": default_source}


def _entry_source(entry: int | dict) -> str:
    if isinstance(entry, dict):
        return str(entry.get("_source", "legacy"))
    return "legacy"


def _build_registry_entry(
    port: int,
    *,
    source: str,
    existing: int | dict | None = None,
    username: str = "",
    password: str = "",
) -> dict[str, object]:
    entry: dict[str, object] = {"port": port, "_source": source}
    if isinstance(existing, dict):
        if existing.get("username"):
            entry["username"] = existing["username"]
        if existing.get("password"):
            entry["password"] = existing["password"]
    if username:
        entry["username"] = username
    if password:
        entry["password"] = password
    return entry


def _restore_topology_managed_entries() -> dict[str, object]:
    manifest = _load_topology_manifest()
    managed_names = [str(name).strip() for name in manifest.get("managed_names", []) if str(name).strip()]
    previous_entries = manifest.get("previous_entries", {}) if isinstance(manifest.get("previous_entries"), dict) else {}

    removed: list[str] = []
    restored: list[str] = []

    if not managed_names:
        return {"removed": removed, "restored": restored, "count": 0}

    for name in managed_names:
        if name in previous_entries:
            _device_registry[name] = previous_entries[name]
            restored.append(name)
        elif name in _device_registry:
            del _device_registry[name]
            removed.append(name)

    _save_registry()
    _clear_topology_manifest()
    return {"removed": removed, "restored": restored, "count": len(removed)}


def collect_cleanup_device_names(devices: list[dict] | None) -> list[str]:
    manifest = _load_topology_manifest()
    managed_names = [str(name).strip() for name in manifest.get("managed_names", []) if str(name).strip()]

    target_names = managed_names[:]
    if not target_names and devices:
        target_names = [
            str(item.get("name", "")).strip()
            for item in devices
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        ]

    if not target_names:
        target_names = [
            name for name, entry in _device_registry.items()
            if _entry_source(entry) == "topology_sync"
        ]

    if not target_names and not (_ROOT_DIR / "topology.json").exists():
        target_names = [
            name for name, entry in _device_registry.items()
            if _entry_source(entry) in {"topology_sync", "legacy", "auto_discover"}
        ]

    deduped: list[str] = []
    seen: set[str] = set()
    for name in target_names:
        if name and name not in seen:
            seen.add(name)
            deduped.append(name)
    return deduped


def _load_registry():
    global _device_registry
    if os.path.exists(_REGISTRY_FILE):
        try:
            with open(_REGISTRY_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                _device_registry = {
                    str(name): _normalize_registry_entry(entry)
                    for name, entry in loaded.items()
                }
            else:
                _device_registry = {}
            logger.info("设备注册表已加载: %d 台设备", len(_device_registry))
        except Exception as e:
            logger.error("加载注册表失败: %s", e)
            _device_registry = {}


def _save_registry():
    _write_json_atomic(Path(_REGISTRY_FILE), _device_registry)


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _write_json_atomic(path: Path, payload: object) -> None:
    _write_bytes_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


def _registered_name(name: str, registry: dict | None = None) -> str | None:
    """Use one case-insensitive identity while retaining the stored spelling."""
    name = validate_name(name)
    registry = _device_registry if registry is None else registry
    matches = [key for key in registry if key.casefold() == name.casefold()]
    if len(matches) > 1:
        raise ValueError(
            f"注册表存在大小写冲突的设备名: {matches!r}；请用精确名称注销重复项"
        )
    return matches[0] if matches else None


def sync_topology_devices(
    devices: list[dict] | None,
    *,
    username: str = "",
    password: str = "",
    snapshot: tuple[Path, dict] | None = None,
) -> dict[str, object]:
    """Validate completely before replacing the previous topology inventory."""
    devices = validate_topology_devices(devices)
    if snapshot is not None and validate_topology_payload(snapshot[1]) != devices:
        raise ValueError("拓扑快照与待同步设备不一致")
    original = deepcopy(_device_registry)
    merged = deepcopy(original)
    manifest = _load_topology_manifest()
    prior = manifest["previous_entries"]
    for name in manifest["managed_names"]:
        if name in prior:
            merged[name] = deepcopy(prior[name])
        else:
            merged.pop(name, None)

    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    previous_entries: dict[str, int | dict] = {}
    managed_names: list[str] = []

    for item in devices:
        name = _registered_name(item["name"], original) or _registered_name(item["name"], merged) or item["name"]
        port = item["comPort"]
        existing = original.get(name)
        existing_port = _get_device_port(existing) if existing is not None else None
        if name in merged:
            previous_entries[name] = deepcopy(merged[name])

        merged[name] = _build_registry_entry(
            port,
            source="topology_sync",
            existing=existing,
            username=username,
            password=password,
        )
        managed_names.append(name)

        if existing is None:
            created.append(name)
        elif existing_port != port:
            updated.append(name)

    # Build the replacement in memory. On a write failure, restore the files
    # and shared dictionary, including an optional uploaded snapshot.
    paths = [Path(_REGISTRY_FILE), _TOPOLOGY_MANIFEST_FILE]
    if snapshot is not None:
        paths.append(snapshot[0])
    backups = {path: path.read_bytes() if path.exists() else None for path in paths}
    _device_registry.clear()
    _device_registry.update(merged)
    try:
        _save_registry()
        _save_topology_manifest(managed_names, previous_entries)
        if snapshot is not None:
            _write_json_atomic(*snapshot)
    except Exception:
        _device_registry.clear()
        _device_registry.update(original)
        for path, content in backups.items():
            if content is None:
                path.unlink(missing_ok=True)
            elif not path.exists() or path.read_bytes() != content:
                _write_bytes_atomic(path, content)
        raise
    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "count": len(created) + len(updated),
    }


def remove_topology_devices(devices: list[dict] | None) -> dict[str, object]:
    """Remove topology-linked devices from the registry while preserving unrelated entries."""
    removed: list[str] = []
    restored: list[str] = []
    skipped: list[str] = []
    manifest = _load_topology_manifest()
    previous_entries = manifest.get("previous_entries", {}) if isinstance(manifest.get("previous_entries"), dict) else {}
    target_names = collect_cleanup_device_names(devices)

    if not target_names:
        return {"removed": removed, "restored": restored, "skipped": skipped, "count": 0, "restored_count": 0}

    for name in target_names:
        if name in previous_entries:
            _device_registry[name] = previous_entries[name]
            restored.append(name)
        elif name in _device_registry:
            del _device_registry[name]
            removed.append(name)
        else:
            skipped.append(name)

    _save_registry()
    _clear_topology_manifest()
    return {
        "removed": removed,
        "restored": restored,
        "skipped": skipped,
        "count": len(removed),
        "restored_count": len(restored),
    }


def sync_topology_devices_from_file(
    file_path: str = "",
    *,
    username: str = "",
    password: str = "",
) -> str:
    """Register devices from a saved topology.json snapshot."""
    target = Path(file_path) if file_path else Path(_REGISTRY_FILE).resolve().parent / "topology.json"
    if not target.exists():
        return ToolFailure(f"Topology file not found: {target}")

    try:
        with target.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        return ToolFailure(f"Unable to read topology file: {exc}")

    try:
        devices = validate_topology_payload(payload)
        summary = sync_topology_devices(devices, username=username, password=password)
    except Exception as exc:
        return ToolFailure(f"Topology sync failed: {exc}")
    return (
        f"Topology sync complete. Added {len(summary['created'])}, "
        f"updated {len(summary['updated'])}, skipped {len(summary['skipped'])}."
    )


def _get_device_port(entry: int | dict) -> int:
    if isinstance(entry, dict):
        return entry.get("port", 23)
    return entry


def register_device(name: str, port: int, username: str = "", password: str = "") -> str:
    try:
        name = validate_name(name)
        port = validate_port(port)
        name = _registered_name(name) or name
    except ValueError as exc:
        return ToolFailure(str(exc))
    _device_registry[name] = _build_registry_entry(
        port,
        source="manual",
        existing=_device_registry.get(name),
        username=username,
        password=password,
    )
    _save_registry()
    return f"设备 '{name}' 已注册，端口: {port}"


def unregister_device(name: str) -> str:
    try:
        name = validate_name(name)
        # Exact names also allow old, conflicting registrations to be removed.
        name = name if name in _device_registry else (_registered_name(name) or name)
    except ValueError as exc:
        return ToolFailure(str(exc))
    if name in _device_registry:
        del _device_registry[name]
        _save_registry()
        return f"设备 '{name}' 已注销"
    return f"设备 '{name}' 不存在"


def clear_registered_devices(device_names: list[str] | None = None) -> dict[str, object]:
    target_names = (
        [str(name).strip() for name in device_names if str(name).strip()]
        if device_names is not None
        else list(_device_registry.keys())
    )
    removed: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()

    for name in target_names:
        if name in seen:
            continue
        seen.add(name)
        if name in _device_registry:
            del _device_registry[name]
            removed.append(name)
        else:
            skipped.append(name)

    _save_registry()
    return {"removed": removed, "skipped": skipped, "count": len(removed)}


def list_devices() -> str:
    if not _device_registry:
        return "当前没有注册任何设备。请使用 register_device 工具注册设备。"
    lines = ["已注册设备列表:"]
    lines.append(f"{'设备名':<12} {'端口':<8}")
    lines.append("-" * 20)
    for name, entry in _device_registry.items():
        port = _get_device_port(entry)
        lines.append(f"{name:<12} {port:<8}")
    return "\n".join(lines)


def _resolve_device(device_ip: str) -> tuple[str, int]:
    """
    解析设备标识，返回 (host, port)

    - 设备名: "R1" → ("127.0.0.1", 注册表中的端口)
    - IP地址: "192.168.56.10" → ("192.168.56.10", 23)
    """
    device_ip = validate_name(device_ip)
    name = _registered_name(device_ip)
    if name is not None:
        return ("127.0.0.1", _get_device_port(_device_registry[name]))

    logger.debug("设备 '%s' 未在注册表中找到，回退到 IP 模式", device_ip)
    return (device_ip, 23)


# ==================== 自动发现 ====================

def _decode_console_bytes(raw: bytes | None) -> str:
    """Decode Windows console tool output that is in the OEM code page.

    tasklist/netstat emit bytes in the local code page (e.g. cp936 on Chinese
    Windows). Decoding them as UTF-8 fails inside subprocess's reader thread and
    leaves ``stdout`` as None, so decode manually with the OEM code page and
    never raise on undecodable bytes.
    """
    data = raw or b""
    encodings: list[str] = []
    if sys.platform == "win32":
        try:
            import ctypes

            oem = ctypes.windll.kernel32.GetOEMCP()
            if oem:
                encodings.append(f"cp{oem}")
        except Exception:
            pass
    encodings.extend(["utf-8", "latin-1"])
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _topology_console_candidates() -> dict[int, str]:
    """Return non-zero console ports from the current uploaded topology.

    The web UI stores the parsed ``.topo`` inventory in ``topology.json``.
    Keeping this fallback limited to that current snapshot avoids guessing
    which arbitrary local topology file should be used by auto discovery.
    """
    try:
        payload = load_current_topology()
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("devices"), list):
        return {}

    candidates: dict[int, str] = {}
    for item in payload["devices"]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        raw_port = item.get("comPort", item.get("port"))
        if not isinstance(name, str) or not name.strip():
            continue
        if isinstance(raw_port, str) and raw_port.strip().isdigit():
            raw_port = int(raw_port.strip())
        if type(raw_port) is not int or not 1 <= raw_port <= 65535:
            # Port 0 is a valid topology value for PC/Client/Server, but it
            # deliberately has no console to probe.
            continue
        candidates.setdefault(raw_port, name.strip())
    return candidates


def _probe_console_port(port: int, timeout: float = 2.0) -> bool:
    """Check whether a local eNSP console port accepts a TCP connection."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _discover_from_topology_ports() -> tuple[set[int], dict[int, str]]:
    """Probe current topology ``com_port`` values as discovery fallback."""
    candidates = _topology_console_candidates()
    reachable = {
        port: name
        for port, name in candidates.items()
        if _probe_console_port(port)
    }
    return set(reachable), reachable


def auto_discover_devices() -> str:
    """
    自动发现 eNSP 设备：
    1. 优先通过 eNSP_Client.exe + netstat 获取监听端口
    2. 进程枚举不可用时，回退到当前拓扑的 com_port 逐个 TCP 探测
    3. 连接获取 sysname 并合并更新注册表（保留手动注册的设备）
    """
    fallback_aliases: dict[int, str] = {}
    discovery_source = "tasklist/netstat"

    def use_topology_fallback(reason: str) -> tuple[set[int], str | None]:
        nonlocal fallback_aliases, discovery_source
        fallback_ports, fallback_aliases = _discover_from_topology_ports()
        discovery_source = "topology com_port TCP fallback"
        if not fallback_ports:
            return set(), reason
        return fallback_ports, None

    # 1. 查找 eNSP 进程 PID
    try:
        result = subprocess.run(
            ['tasklist'], capture_output=True, timeout=10
        )
    except Exception as e:
        port_set, fallback_error = use_topology_fallback(f"无法获取进程列表: {e}")
        pids: set[str] = set()
    else:
        pids = set()
        if result.returncode == 0:
            for line in _decode_console_bytes(result.stdout).split('\n'):
                parts = line.split()
                if len(parts) >= 2 and parts[0].lower() == 'ensp_client.exe' and parts[1].isdigit():
                    pids.add(parts[1])
        if not pids:
            port_set, fallback_error = use_topology_fallback(
                "未检测到 eNSP_Client.exe 进程，也没有可用的当前拓扑 com_port TCP 端口。"
            )
        else:
            # 2. 获取控制台端口
            try:
                result = subprocess.run(
                    ['netstat', '-ano'], capture_output=True, timeout=10
                )
            except Exception as e:
                port_set, fallback_error = use_topology_fallback(f"无法获取端口信息: {e}")
            else:
                if result.returncode:
                    port_set, fallback_error = use_topology_fallback(
                        f"无法获取端口信息 (exit {result.returncode}): "
                        f"{_decode_console_bytes(result.stderr).strip()}"
                    )
                else:
                    port_set = set()
                    for line in _decode_console_bytes(result.stdout).split('\n'):
                        parts = line.split()
                        if len(parts) != 5 or parts[0].upper() != 'TCP' or parts[3].upper() != 'LISTENING':
                            continue
                        if parts[4] not in pids:
                            continue
                        match = re.fullmatch(r'(?:0\.0\.0\.0|127\.0\.0\.1):([0-9]+)', parts[1])
                        if match:
                            port = int(match.group(1))
                            if 2000 <= port <= 2999:
                                port_set.add(port)
                    if not port_set:
                        port_set, fallback_error = use_topology_fallback(
                            f"找到 eNSP 进程 (PID: {', '.join(sorted(pids))})，但未检测到控制台端口；"
                            "当前拓扑 com_port TCP 探测也没有发现可用端口。"
                        )
                    else:
                        fallback_error = None

    if not port_set:
        return ToolFailure(fallback_error or "未检测到可用的 eNSP 控制台端口。")
    ports = sorted(port_set)

    # Preserve manual/topology aliases and metadata. Only replace discovered
    # entries previously owned by auto_discover at these ports.
    merged = {
        name: entry for name, entry in _device_registry.items()
        if _entry_source(entry) != "auto_discover" or _get_device_port(entry) not in port_set
    }
    existing_names = {}
    for name, entry in merged.items():
        existing_names.setdefault(_get_device_port(entry), name)
    occupied_names = {name.casefold(): _get_device_port(entry) for name, entry in merged.items()}

    # 3. 连接获取 sysname，重名设备使用端口后缀，避免字典覆盖丢失设备。
    discovered = {}
    default_count = 0
    for port in ports:
        sysname = _get_sysname_from_port(port)
        if port in existing_names:
            name = existing_names[port]
        elif port in fallback_aliases:
            name = fallback_aliases[port]
        elif sysname and sysname != "Huawei":
            name = sysname
        else:
            default_count += 1
            name = f"Device{default_count}"
        base_name = name
        suffix = 0
        while name.casefold() in occupied_names and occupied_names[name.casefold()] != port:
            suffix += 1
            name = f"{base_name}_{port}" + (f"_{suffix}" if suffix > 1 else "")
        discovered[name] = port
        occupied_names[name.casefold()] = port

    # 4. 合并注册表（保留手动注册的设备）
    for name, port in discovered.items():
        if name not in merged:
            merged[name] = _build_registry_entry(port, source="auto_discover", existing=_device_registry.get(name))

    # Other modules hold references to this dictionary (runtime / CLI tools).
    _device_registry.clear()
    _device_registry.update(merged)
    _save_registry()

    # 生成报告
    lines = [f"自动发现完成！共找到 {len(discovered)} 台设备:", f"发现方式: {discovery_source}"]
    lines.append(f"{'设备名':<12} {'端口':<8}")
    lines.append("-" * 20)
    for name, port in discovered.items():
        lines.append(f"{name:<12} {port:<8}")

    manual_kept = {k: v for k, v in merged.items() if k not in discovered}
    if manual_kept:
        lines.append(f"\n已保留 {len(manual_kept)} 台其他已注册设备:")
        for name, entry in manual_kept.items():
            lines.append(f"  {name:<12} {_get_device_port(entry):<8}")

    if default_count > 0:
        lines.append(
            f"\n提示: {default_count} 台设备使用默认主机名(Huawei)，"
            "已自动命名。建议在 eNSP 中用 'sysname R1' 设置主机名后重新发现。"
        )
    return "\n".join(lines)


def _get_sysname_from_port(port: int) -> str | None:
    """通过控制台端口连接设备，获取 sysname"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(('127.0.0.1', port))
        time.sleep(0.5)
        s.sendall(b'\r\n')
        time.sleep(1)
        s.recv(4096)
        s.sendall(b'display current-configuration | include sysname\r\n')
        time.sleep(2)
        data = s.recv(4096).decode('utf-8', errors='ignore')
        s.close()
        for line in data.split('\n'):
            stripped = line.strip()
            if stripped.startswith('sysname ') and 'display' not in stripped:
                return stripped.replace('sysname ', '')
        return None
    except Exception:
        return None


# 启动时加载注册表
_load_registry()
