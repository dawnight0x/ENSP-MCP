"""
eNSP CLI 工具集

提供 CLI 命令执行、配置下发、保存、运行配置查看、
Ping/Traceroute 诊断、健康检查等核心功能。
"""
import logging
import socket
from typing import Optional

from ..config import _resolve_device, _device_registry, _get_device_port
from ..console import HuaweiConsole, has_cli_error as _has_cli_error
from ..exceptions import ToolFailure
from ..validation import validate_commands, validate_name

logger = logging.getLogger("ensp_mcp.cli")


def _connect(device_ip: str, timeout: int = 15) -> HuaweiConsole:
    """根据设备名/IP 创建 HuaweiConsole 实例"""
    host, port = _resolve_device(device_ip)
    if port <= 0 or port > 65535:
        raise ValueError(_console_port_error(device_ip, port))
    return HuaweiConsole(host, port, timeout=timeout)


def _console_port_error(device_ip: str, port: int) -> str:
    """Return an explanation when a device has no usable console port."""
    return (
        f"设备 {device_ip} 没有控制台端口 (port={port})。"
        "PC/Client/Server 等终端设备在 eNSP 中不提供 CLI，无法通过控制台管理；"
        "请对路由器/交换机设备操作，或用 register_device 指定正确的控制台端口。"
    )


def _command_result(heading: str, output: str) -> str:
    result = f"{heading}\n{output}"
    return ToolFailure(result) if _has_cli_error(output) else result


# ==================== 核心工具 ====================

def execute_cli(device_ip: str, command: str, timeout: int = 30) -> str:
    """在设备上执行单条查询/显示命令"""
    try:
        device_ip = validate_name(device_ip)
        validate_commands([command])
        with _connect(device_ip, timeout=timeout) as c:
            output = c.send_command(command, timeout=timeout)
            return _command_result(f"[{device_ip}] 执行 '{command}' 结果:", output)
    except Exception as e:
        return ToolFailure(f"无法连接设备 {device_ip} 或执行失败: {e}")


def push_config(device_ip: str, commands: list[str]) -> str:
    """向设备下发一组配置命令"""
    try:
        device_ip = validate_name(device_ip)
        commands = validate_commands(commands)
        with _connect(device_ip) as c:
            output = c.send_config_set(commands)
            return _command_result(f"[{device_ip}] 配置下发结果:", output)
    except Exception as e:
        return ToolFailure(f"配置下发失败 [{device_ip}]: {e}")


def multi_device_push_config(devices: list[dict]) -> str:
    """Validate every batch, then stop all further devices on the first failure."""
    try:
        if not isinstance(devices, list) or not devices:
            raise ValueError("devices 必须是非空设备列表")
        batches = []
        for index, entry in enumerate(devices):
            if not isinstance(entry, dict):
                raise ValueError(f"devices[{index}] 必须是设备对象")
            batches.append((validate_name(entry.get("device_ip")), validate_commands(entry.get("commands"))))
    except ValueError as exc:
        return ToolFailure(f"批量配置参数无效，尚未执行任何设备: {exc}")
    results = []
    for index, (device_ip, commands) in enumerate(batches):
        result = push_config(device_ip, commands)
        results.append(result)
        if isinstance(result, ToolFailure):
            remaining = [name for name, _ in batches[index + 1:]]
            skipped_message = f"未执行设备: {remaining!r}。" if remaining else ""
            results.append(f"批量配置已停止。{skipped_message}已执行命令不会自动回滚。")
            return ToolFailure("\n\n".join(results))
    return "\n\n".join(results)


def save_config(device_ip: str) -> str:
    """保存配置，处理确认和默认文件名，并验证保存完成。"""
    try:
        device_ip = validate_name(device_ip)
        with _connect(device_ip) as c:
            output = c.save_config()
            return _command_result(f"[{device_ip}] 配置保存结果:", output)
    except Exception as e:
        return ToolFailure(f"保存配置失败 [{device_ip}]: {e}")


def get_running_config(device_ip: str, section: Optional[str] = None) -> str:
    """获取设备当前运行配置，可选 section 过滤"""
    try:
        with _connect(device_ip, timeout=30) as c:
            if section:
                command = f"display current-configuration | section {section}"
            else:
                command = "display current-configuration"
            output = c.send_command(command, timeout=30)
            if section and _has_cli_error(output):
                return ToolFailure(
                    f"[{device_ip}] section 过滤命令或表达式被设备拒绝。"
                    f"可不传 section 获取完整配置；include 仅过滤匹配行，不能等价替代配置段。\n{output}"
                )
            return _command_result(f"[{device_ip}] 运行配置:", output)
    except Exception as e:
        return ToolFailure(f"获取运行配置失败 [{device_ip}]: {e}")


# ==================== 诊断工具 ====================

def ping_from_device(
    device_ip: str,
    target_ip: str,
    count: int = 5,
    source_ip: Optional[str] = None,
) -> str:
    """从设备发起 Ping 测试"""
    try:
        # VRP's -t timeout is in milliseconds; 2 would be too short for
        # ordinary eNSP links and makes healthy targets look unreachable.
        command = f"ping -c {count} -t 2000"
        if source_ip:
            command += f" -a {source_ip}"
        command += f" {target_ip}"

        timeout = count * 2 + 10
        with _connect(device_ip, timeout=timeout) as c:
            output = c.send_command(command, timeout=timeout)
            return _command_result(f"[{device_ip}] Ping {target_ip} 结果:", output)
    except Exception as e:
        return ToolFailure(f"Ping 测试失败 [{device_ip}]: {e}")


def traceroute_from_device(
    device_ip: str,
    target_ip: str,
    source_ip: Optional[str] = None,
) -> str:
    """从设备发起 Traceroute"""
    try:
        command = "tracert"
        if source_ip:
            command += f" -a {source_ip}"
        command += f" {target_ip}"

        with _connect(device_ip, timeout=60) as c:
            output = c.send_command(command, timeout=60)
            return _command_result(f"[{device_ip}] Traceroute 到 {target_ip}:", output)
    except Exception as e:
        return ToolFailure(f"Traceroute 失败 [{device_ip}]: {e}")


# ==================== 健康检查 ====================

def health_check(device_ip: str) -> str:
    """单台设备健康检查：TCP 端口检测 + 轻量命令测试"""
    host, port = _resolve_device(device_ip)
    results = [f"[{device_ip}] 健康检查报告:"]

    if port <= 0 or port > 65535:
        results.append(f"  {_console_port_error(device_ip, port)}")
        return ToolFailure("\n".join(results))

    # 1. TCP 端口检测
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect((host, port))
        s.close()
        results.append(f"  TCP 连接 ({host}:{port}): OK")
    except Exception as e:
        results.append(f"  TCP 连接 ({host}:{port}): FAIL - {e}")
        return ToolFailure("\n".join(results))

    # 2. 命令测试
    try:
        with HuaweiConsole(host, port, timeout=10) as c:
            output = c.send_command("display clock", timeout=10)
            if _has_cli_error(output):
                results.append(f"  CLI 命令测试: FAIL - {output.strip()}")
                return ToolFailure("\n".join(results))
            results.append(f"  CLI 命令测试: OK")
            results.append(f"  设备时间: {output.strip()}")
    except Exception as e:
        results.append(f"  CLI 命令测试: FAIL - {e}")
        return ToolFailure("\n".join(results))

    return "\n".join(results)


def multi_health_check(device_names: list[str] | None = None) -> str:
    """批量健康检查"""
    if not device_names:
        if not _device_registry:
            return "没有注册任何设备，请先注册设备。"
        device_names = list(_device_registry.keys())

    results = [f"批量健康检查（{len(device_names)} 台设备）:"]
    results.append("=" * 50)

    healthy = 0
    for name in device_names:
        report = health_check(name)
        if not isinstance(report, ToolFailure) and "FAIL" not in report:
            healthy += 1
        results.append(report)
        results.append("")

    results.append(f"汇总: {healthy}/{len(device_names)} 台设备健康")
    output = "\n".join(results)
    return ToolFailure(output) if healthy != len(device_names) else output
