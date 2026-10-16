"""
eNSP topology helpers.
"""

from __future__ import annotations

import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from ..exceptions import ToolFailure
from ..topology_context import load_current_topology, summarize_topology

logger = logging.getLogger("ensp_mcp.topology")

_XML_DECL_RE = re.compile(r'\A<\?xml\s+[^?]*\?>')
_XML_DECL_ENCODING_RE = re.compile(r'\s+encoding\s*=\s*(["\'])(?P<encoding>[^"\']+)\1', re.IGNORECASE)


def _parse_topo_xml(file_path: str) -> ET.Element:
    """Parse an eNSP .topo file, tolerating its legacy encoding declaration.

    eNSP writes ``<?xml version="1.0" encoding="UNICODE" ?>``. Python's XML
    parser cannot resolve "UNICODE" and raises ``LookupError: unknown encoding:
    UNICODE``. Detect byte order before decoding; only edit the XML declaration,
    preserving attributes and text in the document body.
    """
    raw = Path(file_path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    elif raw[:3] == b"\xef\xbb\xbf":
        text = raw.decode("utf-8-sig")
    elif len(raw) >= 4 and raw[0] == 0 and raw[2] == 0 and raw[1] in b"< \t\r\n":
        text = raw.decode("utf-16-be")
    elif len(raw) >= 4 and raw[1] == 0 and raw[3] == 0 and raw[0] in b"< \t\r\n":
        text = raw.decode("utf-16-le")
    else:
        # For ASCII-compatible encodings, consult the declaration, then the
        # encodings used by eNSP exports. eNSP commonly labels GBK files as
        # ``UNICODE``, so assuming UTF-8 loses Chinese device names.
        header = _XML_DECL_RE.match((raw.partition(b"?>")[0] + b"?>").decode("ascii", errors="replace"))
        declared = _XML_DECL_ENCODING_RE.search(header.group()) if header else None
        encoding = declared.group("encoding") if declared else ""
        candidates: list[str] = []
        if encoding and encoding.upper() != "UNICODE":
            candidates.append(encoding)
        candidates.extend(["utf-8", "gb18030", "utf-16"])
        text = None
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                text = raw.decode(candidate)
                break
            except (LookupError, UnicodeDecodeError) as exc:
                last_error = exc
        if text is None:
            raise last_error if last_error is not None else ValueError("无法解码 .topo 文件")
    declaration = _XML_DECL_RE.match(text)
    if declaration:
        header = _XML_DECL_ENCODING_RE.sub("", declaration.group(), count=1)
        text = header + text[declaration.end():]
    return ET.fromstring(text)


def discover_topology(file_path: str) -> str:
    """
    解析 eNSP .topo 文件，提取拓扑信息

    Args:
        file_path: .topo 文件的完整路径

    Returns:
        拓扑描述（设备列表、接口、链路关系）
    """
    if not os.path.exists(file_path):
        return ToolFailure(f"文件不存在: {file_path}")

    suffix = Path(file_path).suffix.lower()
    if suffix == ".json":
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            return ToolFailure(f"无法读取 topology.json: {exc}")

        if not isinstance(payload, dict):
            return ToolFailure(f"不是有效的 topology.json 文件: {file_path}")
        return _describe_topology_payload(payload, file_path, "网页上传的 topology 快照")

    if suffix != ".topo":
        return ToolFailure(f"不是 .topo 或 topology.json 文件: {file_path}")

    try:
        root = _parse_topo_xml(file_path)
    except ET.ParseError as e:
        return ToolFailure(f"XML 解析失败: {e}")
    except (LookupError, UnicodeError, OSError) as e:
        return ToolFailure(f"无法读取 .topo 文件: {e}")

    devices = []
    links = []
    id_to_name: dict[str, str] = {}

    # 灵活查找设备节点（兼容不同 eNSP 版本：真实 .topo 用 devices/dev）
    device_nodes = _find_elements(root, ["devices/dev", "devices/device", "device", "node", "nodes/node"])
    for dev in device_nodes:
        info = _extract_device_info(dev)
        if info:
            devices.append(info)
            if info.get("id"):
                id_to_name[info["id"]] = info["name"]

    # 灵活查找链路节点（真实 .topo 用 lines/line）
    link_nodes = _find_elements(root, ["lines/line", "links/link", "link", "connection", "connections/connection"])
    for link in link_nodes:
        for info in _extract_link_infos(link):
            info["src_device"] = id_to_name.get(info["src_device"], info["src_device"])
            info["dst_device"] = id_to_name.get(info["dst_device"], info["dst_device"])
            links.append(info)

    return _describe_parsed_topology(file_path, devices, links)


def find_topo_files(search_dir: str = "") -> str:
    """
    搜索 .topo 文件

    Args:
        search_dir: 搜索目录，为空则搜索常见位置

    Returns:
        找到的 .topo 文件列表
    """
    uploaded_payload = load_current_topology()
    uploaded_summary = summarize_topology(uploaded_payload) if uploaded_payload else None

    search_dirs = []
    if search_dir:
        search_dirs.append(search_dir)
    else:
        # 常见 eNSP 项目目录
        home = os.path.expanduser("~")
        search_dirs.extend([
            os.path.join(home, "Desktop"),
            os.path.join(home, "Documents"),
            os.path.join(home, "Documents", "eNSP"),
            r"C:\Program Files\Huawei\eNSP",
            r"C:\eNSP",
        ])

    found = []
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for dirpath, directories, filenames in os.walk(d):
            for f in filenames:
                if f.lower().endswith(".topo"):
                    found.append(os.path.join(dirpath, f))
            # 限制递归深度为 3 层
            depth = len(Path(os.path.relpath(dirpath, d)).parts)
            if depth >= 3:
                directories[:] = []

    lines: list[str] = []
    if uploaded_summary:
        lines.extend(
            [
                "已检测到网页上传的当前拓扑，这就是当前应优先使用的拓扑来源：",
                f"  {uploaded_summary['path']}",
                f"  devices={uploaded_summary['device_count']}, links={uploaded_summary['link_count']}",
                "优先调用 get_topology_context 或读取资源 ensp://topology/current。",
                "如果必须传文件路径给 discover_topology，可直接使用上面的 topology.json 路径。",
            ]
        )

    if not found:
        if lines:
            lines.append("本地搜索未找到额外的 .topo 文件。")
            return "\n".join(lines)
        return "未找到 .topo 文件，且当前也没有网页上传的 topology.json。请指定 search_dir 参数或先在网页上传 TOPO。"

    if lines:
        lines.append("")
        lines.append(f"另外找到 {len(found)} 个本地 .topo 文件:")
    else:
        lines.append(f"找到 {len(found)} 个 .topo 文件:")

    for f in found[:20]:
        lines.append(f"  {f}")
    if len(found) > 20:
        lines.append(f"  ... 还有 {len(found) - 20} 个")
    return "\n".join(lines)


def _describe_topology_payload(payload: dict[str, object], file_path: str, source_label: str) -> str:
    summary = summarize_topology(payload)
    lines = [
        f"{source_label}: {Path(file_path).name}",
        f"路径: {file_path}",
        f"设备数量: {summary['device_count']}",
        f"链路数量: {summary['link_count']}",
        "这些设备名和本地端口应视为当前 MCP 操作的事实来源。",
    ]

    devices = summary.get("devices", [])
    if devices:
        lines.append("\n=== 设备列表 ===")
        for device in devices:
            name = device.get("name") or "<unknown>"
            model = device.get("model") or "unknown-model"
            port = device.get("port") if device.get("port") not in ("", None) else "missing-port"
            lines.append(f"  {name} | model={model} | local_port={port}")
    else:
        lines.append("\n=== 设备列表 ===")
        lines.append("  未解析到设备")

    return "\n".join(lines)


def _describe_parsed_topology(file_path: str, devices: list[dict], links: list[dict]) -> str:
    lines = [f"拓扑文件: {os.path.basename(file_path)}"]
    lines.append(f"设备数量: {len(devices)}")
    lines.append(f"链路数量: {len(links)}")

    if devices:
        lines.append("\n=== 设备列表 ===")
        lines.append(f"{'名称':<15} {'类型':<12} {'型号':<20} {'控制端口':<8}")
        lines.append("-" * 58)
        for d in devices:
            lines.append(f"{d['name']:<15} {d['type']:<12} {d['model']:<20} {d.get('com_port', ''):<8}")
            if d.get("interfaces"):
                for iface in d["interfaces"]:
                    lines.append(f"  └─ {iface}")

    if links:
        lines.append("\n=== 链路连接 ===")
        lines.append("  数字端口为 .topo 接口索引；CLI 接口名请通过设备接口查询确认。")
        for l in links:
            lines.append(f"  {l['src_device']}:{l['src_port']} ←→ {l['dst_device']}:{l['dst_port']}")

    return "\n".join(lines)


def _find_elements(root: ET.Element, paths: list[str]) -> list[ET.Element]:
    """尝试多种路径查找元素"""
    for path in paths:
        elements = root.findall(path)
        if elements:
            return elements
    # 也尝试递归查找
    tag = paths[-1].split("/")[-1] if paths else "device"
    return list(root.iter(tag))


def _extract_device_info(elem: ET.Element) -> dict | None:
    """从 XML 元素提取设备信息"""
    name = (
        elem.get("name")
        or elem.findtext("name")
        or elem.get("id")
        or elem.findtext("id")
    )
    if not name:
        return None

    dev_type = (
        elem.get("type")
        or elem.findtext("type")
        or elem.get("deviceType")
        or elem.findtext("deviceType")
        or ""
    )

    model = (
        elem.get("model")
        or elem.findtext("model")
        or elem.get("deviceModel")
        or elem.findtext("deviceModel")
        or ""
    )

    # 真实 .topo 无 type 字段，用型号推断设备类别以保持输出可读
    if not dev_type:
        dev_type = _infer_device_type(model)

    # 提取接口列表
    interfaces = []
    for iface_tag in ["interface", "port", "interfaces/interface", "ports/port"]:
        for iface in elem.findall(iface_tag):
            iname = iface.get("name") or iface.findtext("name") or iface.text
            if iname:
                interfaces.append(iname.strip())
        if interfaces:
            break

    # eNSP stores interface groups under slots: e.g. GE count=2 and Ethernet
    # count=8. Preserve that inventory without guessing model-specific CLI
    # numbering (slot17 is an internal topology slot identifier).
    for slot in elem.findall("slot") + elem.findall("slots/slot"):
        slot_name = slot.get("number") or slot.get("name") or "未命名插槽"
        for iface in slot.findall("interface"):
            iname = (iface.get("interfacename") or iface.get("name")
                     or iface.findtext("name") or iface.get("sztype") or "未知接口类型")
            count = iface.get("count")
            description = f"{slot_name} / {iname}"
            if count is not None:
                description += f" (数量: {count})"
            interfaces.append(description)

    return {
        "id": elem.get("id") or elem.findtext("id") or "",
        "name": name,
        "type": dev_type,
        "model": model,
        "com_port": elem.get("com_port") or elem.findtext("com_port") or "",
        "interfaces": interfaces,
    }


def _infer_device_type(model: str) -> str:
    """从 eNSP 型号推断设备类别（.topo 不含 type 字段）。"""
    lowered = str(model or "").strip().lower()
    if not lowered:
        return "unknown"
    if lowered in {"pc", "client", "server"}:
        return lowered
    if lowered.startswith(("ar", "router")):
        return "router"
    if lowered.startswith(("s", "lsw", "switch")) or lowered in {"s5700", "s3700"} or lowered.startswith("ce"):
        return "switch"
    if lowered.startswith("usg") or lowered.startswith("fw"):
        return "firewall"
    if lowered.startswith("ap"):
        return "ap"
    return "unknown"


def _extract_link_infos(elem: ET.Element) -> list[dict]:
    """从一个 ``line`` 提取每个 ``interfacePair`` 对应的物理链路。"""
    src_device = (
        elem.get("srcDeviceID") or elem.get("srcDevice") or elem.get("src_device")
        or elem.findtext("srcDevice") or elem.findtext("source/device") or ""
    )
    dst_device = (
        elem.get("destDeviceID") or elem.get("dstDevice") or elem.get("dst_device")
        or elem.findtext("dstDevice") or elem.findtext("destination/device") or ""
    )
    if not (src_device and dst_device):
        return []

    pairs = elem.findall("interfacePair")
    if not pairs:
        return [{
            "src_device": src_device,
            "src_port": (
                elem.get("srcPort") or elem.get("src_port")
                or elem.findtext("srcPort") or elem.findtext("source/port") or ""
            ),
            "dst_device": dst_device,
            "dst_port": (
                elem.get("dstPort") or elem.get("dst_port")
                or elem.findtext("dstPort") or elem.findtext("destination/port") or ""
            ),
        }]

    return [{
        "src_device": src_device,
        "src_port": pair.get("srcIndex", ""),
        "dst_device": dst_device,
        "dst_port": pair.get("tarIndex", pair.get("destIndex", "")),
    } for pair in pairs]


def _extract_link_info(elem: ET.Element) -> dict | None:
    """兼容旧调用：返回第一条链路。新代码使用 :func:`_extract_link_infos`。"""
    infos = _extract_link_infos(elem)
    return infos[0] if infos else None
