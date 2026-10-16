"""
Lightweight Huawei console client for eNSP.
"""

from __future__ import annotations

import logging
import re
import socket
import threading
import time
import weakref

from .runtime import publish, resolve_device_label
from .exceptions import ConfigBatchError
from .validation import validate_commands

logger = logging.getLogger("ensp_mcp.console")

_MORE_RE = re.compile(r"----\s*More\s*----", re.IGNORECASE)
_MORE_TAIL_RE = re.compile(r"----\s*More\s*----\s*$", re.IGNORECASE)
_PROMPT_SETTLE_SECONDS = 0.15
_PROMPT_RE = re.compile(
    r"^(?:<(?P<user>[A-Za-z][A-Za-z0-9_.:/-]{0,127})>"
    r"|\[(?P<system>[~*]?[A-Za-z][A-Za-z0-9_.:/-]{0,127})\])$"
)
_SYSNAME_RE = re.compile(r"^sysname\s+([A-Za-z][A-Za-z0-9_.:-]{0,127})$", re.IGNORECASE)
_ANSI_CSI_RE = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(rb"\x1b\][^\x07]*(?:\x07|\x1b\\)")
_IAC = 0xFF
_CLI_ERROR_RE = re.compile(r"^\s*(?:Error\s*:|错误\s*[:：])", re.IGNORECASE | re.MULTILINE)
_SAVE_CONFIRM_RE = re.compile(
    r"(?:\[\s*y\s*/\s*n\s*\]|\(\s*y\s*/\s*n\s*\))"
    r"(?:\s*\[\s*[ynYN]\s*\])?"
    r"\s*[:：]?\s*$",
    re.IGNORECASE,
)
_SAVE_FILENAME_RE = re.compile(
    r"(?:input|enter|输入)[^\r\n]*(?:file\s*name|filename|文件名)[^\r\n]*"
    r"\[[^\[\]\r\n]+\.(?:cfg|zip)\]\s*[:：]?\s*$", re.IGNORECASE
)
_SAVE_SUCCESS_RE = re.compile(
    r"^\s*(?:Info\s*:\s*)?(?:"
    r"Save\s+(?:the\s+)?(?:current\s+)?configuration\s+successfully[.!]?"
    r"|(?:The\s+)?(?:current\s+)?configuration\s+(?:was\s+|has\s+been\s+)?saved\s+(?:to\s+the\s+device\s+)?successfully[.!]?"
    r"|Configuration\s+file\s+had\s+been\s+saved\s+successfully[.!]?"
    r"|Successfully\s+saved\s+(?:the\s+)?(?:current\s+)?configuration[.!]?"
    r"|配置(?:文件)?保存成功[。.!]?"
    r")\s*$", re.IGNORECASE | re.MULTILINE
)
_ACTIVE_CONSOLES: "weakref.WeakSet[HuaweiConsole]" = weakref.WeakSet()
_ACTIVE_CONSOLES_LOCK = threading.RLock()


def _strip_iac(data: bytes) -> bytes:
    """Remove Telnet IAC negotiation bytes."""
    result = bytearray()
    i = 0
    while i < len(data):
        if data[i] == _IAC and i + 2 < len(data):
            i += 3
        else:
            result.append(data[i])
            i += 1
    return bytes(result)


def _strip_terminal_escapes(data: bytes) -> bytes:
    data = _ANSI_OSC_RE.sub(b"", data)
    data = _ANSI_CSI_RE.sub(b"", data)
    return data.replace(b"\x1b", b"")


def _normalize_terminal_bytes(data: bytes) -> bytes:
    data = _strip_iac(data)
    data = _strip_terminal_escapes(data)
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _tail_text(data: bytes, limit: int = 768) -> str:
    return _normalize_terminal_bytes(data[-limit:]).decode("utf-8", errors="ignore")


def _clean_text(data: bytes) -> str:
    text = _normalize_terminal_bytes(data).decode("utf-8", errors="ignore")
    text = _MORE_RE.sub("", text)
    lines = [re.sub(r" {8,}", " ", line).rstrip() for line in text.splitlines()]
    return "\n".join(lines)


def has_cli_error(output: str) -> bool:
    """A VRP rejection can still end with a valid console prompt."""
    return _CLI_ERROR_RE.search(output) is not None


class HuaweiConsole:
    """Very small socket client for eNSP control ports."""

    def __init__(self, host: str, port: int, timeout: int = 15):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._learned_prompt: str | None = None
        self._pending_prompt: str | None = None

    @property
    def device_label(self) -> str:
        return resolve_device_label(self.host, self.port) or f"{self.host}:{self.port}"

    def connect(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._sock.settimeout(self.timeout)
            self._sock.connect((self.host, self.port))
            time.sleep(0.3)
            self._sock.sendall(b"\r\n")
            self._read_until_prompt(timeout=5, discover_prompt=True)
        except Exception:
            self.disconnect()
            raise
        with _ACTIVE_CONSOLES_LOCK:
            _ACTIVE_CONSOLES.add(self)
        publish(
            "device_connect",
            device=self.device_label,
            text=f"Connected to {self.device_label}",
        )
        logger.debug("Connected to %s:%d", self.host, self.port)

    def _prompt_tokens(self) -> tuple[str, ...]:
        candidates: list[str] = []
        for value in (self._learned_prompt, self._pending_prompt, self.device_label, self.host, f"{self.host}:{self.port}"):
            token = str(value or "").strip()
            if token and token not in candidates:
                candidates.append(token)
        return tuple(candidates)

    @staticmethod
    def _prompt_inner(line: str) -> str | None:
        stripped = str(line or "").strip()
        match = _PROMPT_RE.fullmatch(stripped)
        if match is None:
            return None
        return (match.group("user") or match.group("system")).lstrip("~*")

    def _is_prompt_line(self, line: str, *, discover_prompt: bool = False) -> bool:
        inner = self._prompt_inner(line)
        if inner is None:
            return False
        for token in self._prompt_tokens():
            if inner == token or inner.startswith(f"{token}-") or inner.startswith(f"{token}/"):
                return True
        return discover_prompt and self._learned_prompt is None

    def _has_prompt(self, data: bytes, *, discover_prompt: bool = False) -> bool:
        # TCP may coalesce the prompt and subsequent asynchronous logs into
        # one receive. Inspect every line, including a split prompt completed
        # by this receive, instead of just the last line / last 768 bytes.
        lines = _normalize_terminal_bytes(data).decode("utf-8", errors="ignore").splitlines()
        line = next((line for line in reversed(lines)
                     if self._is_prompt_line(line, discover_prompt=discover_prompt)), None)
        if line is None:
            return False
        inner = self._prompt_inner(line)
        assert inner is not None
        if discover_prompt and self._learned_prompt is None:
            self._learned_prompt = inner
        if self._pending_prompt is not None:
            if inner == self._pending_prompt or inner.startswith(f"{self._pending_prompt}-"):
                self._learned_prompt = self._pending_prompt
            self._pending_prompt = None
        return True

    def disconnect(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        with _ACTIVE_CONSOLES_LOCK:
            _ACTIVE_CONSOLES.discard(self)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.disconnect()

    def send_command(self, command: str, timeout: int = 0) -> str:
        t = timeout or self.timeout
        self._send_line(command)
        output = self._read_until_prompt(timeout=t)
        lines = output.splitlines()
        if lines and lines[0].strip() == command.strip():
            lines = lines[1:]
        while lines and self._is_prompt_line(lines[-1].strip()):
            lines.pop()
        return "\n".join(lines)

    def send_config_set(self, commands: list[str]) -> str:
        commands = validate_commands(commands)
        all_output: list[str] = []
        completed: list[str] = []
        failed = "system-view"
        failed_index = None
        primary_error = None
        cleanup_error = None
        may_be_in_system_view = False
        try:
            self._send_line("system-view")
            may_be_in_system_view = True
            output = self._read_until_prompt(timeout=min(5, self.timeout))
            all_output.append(output)
            if has_cli_error(output):
                may_be_in_system_view = False
                raise RuntimeError("设备拒绝进入 system-view")
            for index, command in enumerate(commands):
                failed_index = index
                failed = command
                self._send_line(command)
                output = self._read_until_prompt(timeout=min(5, self.timeout))
                all_output.append(output)
                if has_cli_error(output):
                    raise RuntimeError(f"设备拒绝命令 {command!r}")
                completed.append(command)
        except Exception as exc:
            primary_error = exc
        finally:
            if may_be_in_system_view:
                try:
                    self._send_line("return")
                    output = self._read_until_prompt(timeout=min(5, self.timeout))
                    all_output.append(output)
                    if has_cli_error(output):
                        raise RuntimeError("设备拒绝 return")
                except Exception as exc:
                    cleanup_error = exc
        if primary_error is not None or cleanup_error is not None:
            if primary_error is None:
                failed = "return"
            skipped = (
                commands[failed_index + 1:]
                if primary_error is not None and failed_index is not None
                else commands[len(completed):]
            )
            raise ConfigBatchError(
                completed, failed, skipped, str(primary_error or cleanup_error),
                "\n".join(all_output), cleanup_error,
            ) from (primary_error or cleanup_error)
        return "\n".join(all_output)

    def save_config(self, timeout: float = 30.0) -> str:
        """Confirm interactive saving by its success message and final prompt."""
        if not self._sock:
            raise RuntimeError("Socket not connected")
        deadline = time.monotonic() + timeout
        transcript = bytearray()
        segment = bytearray()
        interactions = 0
        complete = False
        self._send_line("save")
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if complete:
                        return _clean_text(bytes(transcript))
                    raise TimeoutError("保存结果未确认：等待成功回显和最终提示符超时")
                self._sock.settimeout(min(_PROMPT_SETTLE_SECONDS if complete else 1.0, remaining))
                try:
                    chunk = self._sock.recv(4096)
                except socket.timeout:
                    if complete:
                        return _clean_text(bytes(transcript))
                    continue
                if not chunk:
                    if complete:
                        return _clean_text(bytes(transcript))
                    raise ConnectionError("保存结果未确认：设备提前关闭连接")
                chunk = _strip_iac(chunk)
                transcript.extend(chunk)
                segment.extend(chunk)
                text = _clean_text(bytes(segment))
                if has_cli_error(text):
                    raise RuntimeError("设备拒绝保存配置")
                response = None
                if _SAVE_CONFIRM_RE.search(text):
                    response = "Y"
                elif _SAVE_FILENAME_RE.search(text):
                    response = ""
                if response is not None:
                    if complete:
                        raise RuntimeError("保存结果未确认：完成回显后仍请求输入")
                    interactions += 1
                    if interactions > 8:
                        raise RuntimeError("保存结果未确认：交互提示重复过多")
                    self._send_line(response)
                    segment.clear()
                else:
                    success = _SAVE_SUCCESS_RE.search(text)
                    # Only a prompt following completion can confirm this save.
                    if success and self._has_prompt(text[success.end():].encode("utf-8")):
                        complete = True
                    elif not success and self._has_prompt(bytes(segment)):
                        raise RuntimeError("保存结果未确认：返回提示符但没有保存成功回显")
        except Exception as exc:
            raise type(exc)(f"{exc}\n设备回显:\n{_clean_text(bytes(transcript))}") from exc
        finally:
            self._sock.settimeout(self.timeout)
            self._publish_output_block(bytes(transcript))

    def send_command_timing(self, command: str, delay: float = 2.0) -> str:
        self._send_line(command)
        time.sleep(delay)
        return self._read_available()

    def _send_line(self, text: str) -> None:
        if not self._sock:
            raise RuntimeError("Socket not connected")
        publish("command_send", device=self.device_label, text=text, payload={"command": text})
        # VRP treats CR as the command terminator. Sending LF as well can be
        # consumed by an interactive save prompt before we can answer it.
        self._sock.sendall((text + "\r").encode("utf-8"))
        sysname = _SYSNAME_RE.fullmatch(text.strip())
        if sysname is not None:
            self._pending_prompt = sysname.group(1)

    def _publish_output_block(self, data: bytes) -> None:
        text = _clean_text(data)
        if not text.strip():
            return

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or self._is_prompt_line(stripped):
                continue
            publish("output", device=self.device_label, text=line)

    def _read_until_prompt(self, timeout: int | float = 15, *, discover_prompt: bool = False) -> str:
        if not self._sock:
            raise RuntimeError("Socket not connected")

        buf = b""
        pending = bytearray()
        deadline = time.monotonic() + timeout

        prompt_seen = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # eNSP may send the same prompt twice. Keep reading briefly
                # after the first one so it cannot become the next command's
                # apparent response.
                self._sock.settimeout(min(_PROMPT_SETTLE_SECONDS if prompt_seen else 1.0, remaining))
                try:
                    chunk = self._sock.recv(4096)
                except socket.timeout:
                    if prompt_seen:
                        break
                    # A recv timeout can precede a delayed response. Keep the
                    # command's original deadline and preserve the buffer.
                    continue

                if not chunk:
                    raise ConnectionError(f"Console connection closed by {self.device_label}")
                chunk = _strip_iac(chunk)
                prompt_start = buf.rfind(b"\n") + 1
                buf += chunk
                pending.extend(chunk)

                if _MORE_TAIL_RE.search(_tail_text(buf)):
                    self._sock.sendall(b" ")
                    continue

                # A prompt is terminal for this read. Some devices (e.g. S5700
                # after a config change) emit an asynchronous syslog line just
                # after the prompt; once a prompt has been seen, a later chunk
                # must not clear that, or the read blocks until it times out
                # even though the command already succeeded.
                if self._has_prompt(buf[prompt_start:], discover_prompt=discover_prompt):
                    prompt_seen = True
                if b"\n" in chunk or prompt_seen:
                    self._publish_output_block(bytes(pending))
                    pending.clear()

            if not prompt_seen:
                raise TimeoutError(
                    f"Timed out waiting for a prompt from {self.device_label}; last output: {_tail_text(buf)!r}"
                )
        finally:
            self._sock.settimeout(self.timeout)
            if pending:
                self._publish_output_block(bytes(pending))
        text = _clean_text(buf)
        return text

    def _read_available(self) -> str:
        if not self._sock:
            raise RuntimeError("Socket not connected")

        buf = b""
        pending = bytearray()
        self._sock.settimeout(0.5)
        try:
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                chunk = _strip_iac(chunk)
                buf += chunk
                pending.extend(chunk)
        except (socket.timeout, OSError):
            pass
        self._sock.settimeout(self.timeout)
        if pending:
            self._publish_output_block(bytes(pending))
        text = _clean_text(buf)
        return text


def disconnect_active_devices(device_names: list[str] | None = None) -> dict[str, object]:
    target_names = {str(name).strip() for name in (device_names or []) if str(name).strip()}
    disconnected: list[str] = []
    skipped: list[str] = []

    with _ACTIVE_CONSOLES_LOCK:
        consoles = list(_ACTIVE_CONSOLES)

    for console in consoles:
        label = console.device_label
        if target_names and label not in target_names:
            continue
        try:
            console.disconnect()
            disconnected.append(label)
        except Exception:
            skipped.append(label)

    return {
        "disconnected": disconnected,
        "skipped": skipped,
        "count": len(disconnected),
    }
