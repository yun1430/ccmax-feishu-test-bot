"""Feishu bot adapter for the local CCMax test workbench.

The bot intentionally does not call an upstream model itself. It accepts a
URL/key/model command, asks for confirmation, then calls the local-only web
workbench serially. Credentials are held in memory and never written to disk.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import queue
import re
import secrets
import socket
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

import requests


LOGGER = logging.getLogger("ccmax.feishu")
ROOT = Path(__file__).resolve().parent
REPORT_DIR = ROOT / "reports" / "feishu"

SCREENSHOT_PATH = "/api/screenshot-suite/run"
PASSTHROUGH_PATH = "/api/claude-passthrough/run"
SCREENSHOT_BUDGET = 56
PASSTHROUGH_BUDGET = 10
TOTAL_BUDGET = SCREENSHOT_BUDGET + PASSTHROUGH_BUDGET
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}$")
CLASSIFIER_BETA_RE = re.compile(r"^auto-mode-classifier-[A-Za-z0-9._:-]{1,200}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{12,64}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
OPEN_ID_RE = re.compile(r"^ou_[A-Za-z0-9_-]{3,120}$")
CHAT_ID_RE = re.compile(r"^oc_[A-Za-z0-9_-]{3,160}$")
MAX_COMMAND_LENGTH = 12_000
MAX_KEY_LENGTH = 8_192
PENDING_TTL_SECONDS = 10 * 60
DEDUP_TTL_SECONDS = 15 * 60
REPORT_MAX_BYTES = 30 * 1024 * 1024


class BotError(Exception):
    """A safe, user-displayable error whose text contains no credentials."""


class CommandError(BotError):
    pass


@dataclass(frozen=True)
class BotConfig:
    app_id: str
    app_secret: str
    allowed_open_ids: frozenset[str]
    allowed_chat_ids: frozenset[str] = frozenset()
    bot_open_id: str = ""
    web_url: str = "http://127.0.0.1:8765"
    anthropic_auth: str = "x-api-key"
    classifier_beta: str | None = None
    timeout: int = 120
    allow_private_targets: bool = False
    start_workers: bool = True

    @classmethod
    def from_env(cls) -> "BotConfig":
        app_id = os.environ.get("FEISHU_APP_ID", "").strip()
        app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
        allowed = frozenset(
            item.strip()
            for item in os.environ.get("FEISHU_ALLOWED_OPEN_IDS", "").split(",")
            if item.strip()
        )
        if not app_id or not app_secret:
            raise BotError("缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET")
        if not allowed:
            raise BotError("FEISHU_ALLOWED_OPEN_IDS 不能为空；机器人拒绝所有未授权用户")
        invalid = [item for item in allowed if not OPEN_ID_RE.fullmatch(item)]
        if invalid:
            raise BotError("FEISHU_ALLOWED_OPEN_IDS 含格式无效的 open_id")
        allowed_chats = frozenset(
            item.strip()
            for item in os.environ.get("FEISHU_ALLOWED_CHAT_IDS", "").split(",")
            if item.strip()
        )
        if any(not CHAT_ID_RE.fullmatch(item) for item in allowed_chats):
            raise BotError("FEISHU_ALLOWED_CHAT_IDS 含格式无效的 chat_id")
        bot_open_id = os.environ.get("FEISHU_BOT_OPEN_ID", "").strip()
        if allowed_chats and not OPEN_ID_RE.fullmatch(bot_open_id):
            raise BotError("启用群聊时必须配置格式有效的 FEISHU_BOT_OPEN_ID")
        web_url = os.environ.get("CCMAX_WEB_URL", "http://127.0.0.1:8765").strip()
        auth = os.environ.get("CCMAX_ANTHROPIC_AUTH", "x-api-key").strip().lower()
        if auth not in ("x-api-key", "bearer"):
            raise BotError("CCMAX_ANTHROPIC_AUTH 只能是 x-api-key 或 bearer")
        beta = os.environ.get("CCMAX_CLASSIFIER_BETA", "").strip() or None
        if beta is not None and not CLASSIFIER_BETA_RE.fullmatch(beta):
            raise BotError("CCMAX_CLASSIFIER_BETA 必须是具体的 auto-mode-classifier-* 值，不允许通配符")
        try:
            timeout = int(os.environ.get("CCMAX_FEISHU_HTTP_TIMEOUT", "120"))
        except ValueError as exc:
            raise BotError("CCMAX_FEISHU_HTTP_TIMEOUT 必须是整数") from exc
        if not 10 <= timeout <= 120:
            raise BotError("CCMAX_FEISHU_HTTP_TIMEOUT 必须在 10 到 120 秒之间")
        config = cls(app_id, app_secret, allowed, allowed_chats, bot_open_id,
                     web_url, auth, beta, timeout,
                     _truthy(os.environ.get("CCMAX_ALLOW_PRIVATE_TARGETS", "")))
        validate_workbench_url(config.web_url)
        return config


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PendingJob:
    token: str
    open_id: str
    base_url: str
    api_key: str = field(repr=False)
    model: str
    created_at: float
    chat_id: str = ""


@dataclass(frozen=True)
class TestJob:
    open_id: str
    base_url: str
    api_key: str = field(repr=False)
    model: str
    run_id: str
    requested_at: str
    chat_id: str = ""


def parse_command(text: str) -> tuple[str, str, str] | None:
    """Parse the deliberately small command language.

    Accepted forms:
      /test URL|KEY|MODEL
      测试 URL|KEY|MODEL
    """
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or len(text) > MAX_COMMAND_LENGTH:
        return None
    match = re.match(r"^(?:/test|测试)\s+(.+)$", text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    parts = [part.strip() for part in match.group(1).split("|")]
    if len(parts) != 3 or any(not part for part in parts):
        raise CommandError("格式应为：测试 URL|KEY|MODEL")
    return parts[0], parts[1], parts[2]


def validate_base_url(value: str, *, allow_private: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 300:
        raise CommandError("URL 为空或过长")
    if any(ch.isspace() for ch in value):
        raise CommandError("URL 不能包含空白字符")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise CommandError("URL 格式无效") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise CommandError("URL 必须是绝对 http(s) 地址")
    if parsed.username or parsed.password:
        raise CommandError("URL 不能内嵌用户名或密码")
    if parsed.query or parsed.fragment:
        raise CommandError("URL 不能包含查询参数或片段，请填写网关基础地址")
    if port is not None and not 1 <= port <= 65535:
        raise CommandError("URL 端口无效")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} and not allow_private:
        raise CommandError("默认禁止 localhost 目标；本地调试需设置 CCMAX_ALLOW_PRIVATE_TARGETS=1")
    try:
        infos = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise CommandError("URL 主机名无法解析") from exc
    if not allow_private:
        for info in infos:
            try:
                address = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if (address.is_private or address.is_loopback or address.is_link_local
                    or address.is_reserved or address.is_unspecified or address.is_multicast):
                raise CommandError("默认禁止私网、回环或保留地址目标")
    return value.rstrip("/")


def validate_key(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_KEY_LENGTH:
        raise CommandError("KEY 为空或过长")
    if any(not (32 <= ord(ch) <= 126) for ch in value):
        raise CommandError("KEY 必须是可打印 ASCII，且不能含空白")
    return value


def validate_model(value: str) -> str:
    if not isinstance(value, str) or not MODEL_RE.fullmatch(value):
        raise CommandError("MODEL 只能包含字母、数字及 . _ : / @ + -")
    return value


def validate_workbench_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise BotError("CCMAX_WEB_URL 格式无效") from exc
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost")
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        raise BotError("CCMAX_WEB_URL 必须是本机 http://127.0.0.1:端口 地址")
    if port is None:
        raise BotError("CCMAX_WEB_URL 必须包含端口")
    return value.rstrip("/")


class WorkbenchClient(Protocol):
    def run_screenshot(self, job: TestJob) -> dict[str, Any]: ...
    def run_passthrough(self, job: TestJob) -> dict[str, Any]: ...


class LocalWorkbenchClient:
    """HTTP adapter for the loopback-only CCMax workbench."""

    def __init__(self, config: BotConfig, session: requests.Session | None = None):
        self.base_url = validate_workbench_url(config.web_url)
        self.auth = config.anthropic_auth
        self.classifier_beta = config.classifier_beta
        self.timeout = config.timeout
        self.session = session or requests.Session()
        self.session.trust_env = False

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        host = urlsplit(self.base_url).netloc
        headers = {
            "Content-Type": "application/json",
            "Host": host,
            "Origin": self.base_url,
            "Sec-Fetch-Site": "same-origin",
        }
        try:
            response = self.session.post(
                self.base_url + path, json=payload, headers=headers,
                timeout=(10, self.timeout), allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise BotError(f"本地工作台请求失败（{type(exc).__name__}）") from exc
        if response.status_code != 200:
            try:
                message = response.json().get("error", "")
            except (ValueError, AttributeError):
                message = ""
            message = redact_text(str(message), [str(payload.get("api_key", ""))], limit=200)
            message = message or "无错误详情"
            raise BotError(f"本地工作台返回 HTTP {response.status_code}: {message}")
        try:
            data = response.json()
        except ValueError as exc:
            raise BotError("本地工作台返回了无效 JSON") from exc
        if not isinstance(data, dict):
            raise BotError("本地工作台返回结构无效")
        return data

    def run_screenshot(self, job: TestJob) -> dict[str, Any]:
        payload = {
            "base_url": job.base_url,
            "api_key": job.api_key,
            "model": job.model,
            "protocol": "anthropic-messages",
            "anthropic_auth": self.auth,
            "timeout": self.timeout,
            "allow_paid_requests": True,
            "authorized_max_requests": SCREENSHOT_BUDGET,
            "run_id": job.run_id + "-screen",
        }
        return self._post(SCREENSHOT_PATH, payload)

    def run_passthrough(self, job: TestJob) -> dict[str, Any]:
        payload = {
            "base_url": job.base_url,
            "api_key": job.api_key,
            "model": job.model,
            "anthropic_auth": self.auth,
            "timeout": self.timeout,
            "allow_paid_requests": True,
            "authorized_max_requests": PASSTHROUGH_BUDGET,
            "run_id": job.run_id + "-pass",
        }
        if self.classifier_beta:
            payload["classifier_beta"] = self.classifier_beta
        return self._post(PASSTHROUGH_PATH, payload)


class FeishuApi(Protocol):
    def send_text(self, open_id: str, text: str, receive_id_type: str = "open_id") -> None: ...
    def send_markdown_file(self, open_id: str, path: Path, receive_id_type: str = "open_id") -> None: ...


class FeishuApiClient:
    """Typed lark-oapi adapter. Imports the SDK lazily for offline testability."""

    def __init__(self, app_id: str, app_secret: str):
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        self._lark = lark
        self._create_file_request = CreateFileRequest
        self._create_file_body = CreateFileRequestBody
        self._create_message_request = CreateMessageRequest
        self._create_message_body = CreateMessageRequestBody
        self.client = (lark.Client.builder().app_id(app_id).app_secret(app_secret).build())

    def send_text(self, open_id: str, text: str, receive_id_type: str = "open_id") -> None:
        body = (self._create_message_body.builder()
                .receive_id(open_id).msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False)).build())
        request = (self._create_message_request.builder()
                   .receive_id_type(receive_id_type).request_body(body).build())
        response = self.client.im.v1.message.create(request)
        if not response.success():
            raise BotError(f"飞书发送消息失败（code={getattr(response, 'code', 'unknown')}）")

    def send_markdown_file(self, open_id: str, path: Path, receive_id_type: str = "open_id") -> None:
        size = path.stat().st_size
        if size <= 0 or size > REPORT_MAX_BYTES:
            raise BotError("报告文件大小超出飞书文件上限")
        with path.open("rb") as handle:
            file_body = (self._create_file_body.builder()
                         .file_type("stream").file_name(path.name).file(handle).build())
            file_request = self._create_file_request.builder().request_body(file_body).build()
            response = self.client.im.v1.file.create(file_request)
        if not response.success() or not getattr(response, "data", None):
            raise BotError(f"飞书上传报告失败（code={getattr(response, 'code', 'unknown')}）")
        file_key = response.data.file_key
        body = (self._create_message_body.builder()
                .receive_id(open_id).msg_type("file")
                .content(json.dumps({"file_key": file_key})).build())
        request = (self._create_message_request.builder()
                   .receive_id_type(receive_id_type).request_body(body).build())
        response = self.client.im.v1.message.create(request)
        if not response.success():
            raise BotError(f"飞书发送报告失败（code={getattr(response, 'code', 'unknown')}）")


def redact_text(text: str, secrets_: list[str], *, limit: int = 1200) -> str:
    value = str(text)
    for secret in sorted({s for s in secrets_ if s}, key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)(authorization|api[-_]?key|x-api-key)\s*[:=]\s*\S+",
                   r"\1=[REDACTED]", value)
    return value[:limit]


def safe_target(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "[invalid-target]"
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}{parsed.path or ''}"


def _status_counts(items: list[Mapping[str, Any]]) -> dict[str, int]:
    aliases = {"n/a": "na", "not observable": "not_observable",
               "not_applicable": "na"}
    counts: Counter[str] = Counter()
    for item in items:
        status = str(item.get("status", "UNKNOWN")).strip().upper()
        key = aliases.get(status.lower(), status.lower())
        counts[key] += 1
    return dict(sorted(counts.items()))


def _passthrough_items(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items = result.get("items")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, Mapping)]
    groups = result.get("groups")
    if isinstance(groups, list):
        return [item for group in groups if isinstance(group, Mapping)
                for item in (group.get("items") or []) if isinstance(item, Mapping)]
    return []


def aggregate_report(screen: Mapping[str, Any], passthrough: Mapping[str, Any], job: TestJob,
                     duration_seconds: float) -> tuple[dict[str, Any], str]:
    screen_items = _passthrough_items(screen)
    pass_items = _passthrough_items(passthrough)
    screen_counts = _status_counts(screen_items)
    pass_counts = _status_counts(pass_items)
    all_items = screen_items + pass_items
    all_counts = _status_counts(all_items)
    nonpass = []
    for suite, items in (("截图对齐", screen_items), ("Claude 透传", pass_items)):
        for item in items:
            if str(item.get("status", "")).upper() != "PASS":
                nonpass.append({
                    "suite": suite, "id": item.get("id", ""), "title": item.get("title", ""),
                    "status": item.get("status", "UNKNOWN"),
                    "actual": redact_text(str(item.get("actual", "")), [job.api_key], limit=300),
                })
    anomalies = all_counts.get("fail", 0) + all_counts.get("error", 0)
    # Any status outside PASS is not evidence of a clean run. This includes
    # OBSERVED, which describes an observation rather than a passed assertion.
    inconclusive = sum(count for status, count in all_counts.items() if status != "pass")
    if anomalies:
        conclusion = "OBSERVED_ANOMALIES"
    elif inconclusive or not all_items:
        conclusion = "INCONCLUSIVE"
    else:
        conclusion = "NO_ANOMALY_IN_EXECUTED_CASES"
    screen_budget = screen.get("budget", {}) if isinstance(screen.get("budget"), Mapping) else {}
    pass_budget = passthrough.get("budget", {}) if isinstance(passthrough.get("budget"), Mapping) else {}
    result = {
        "schema_version": "1.0",
        "test_plan_id": "FEISHU-LOCAL-COMBINED-V1",
        "run_id": job.run_id,
        "requested_at": job.requested_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(duration_seconds, 3),
        "target": {"base_url": safe_target(job.base_url), "model": job.model,
                   "protocol": "anthropic-messages", "anthropic_auth": "configured"},
        "verification_type": "CLIENT_OBSERVABLE_ONLY",
        "conclusion": conclusion,
        "suites": {
            "screenshot_alignment": {
                "status": str(screen.get("status", "UNKNOWN")).upper(),
                "result_count": len(screen_items), "counts": screen_counts,
                "budget": {"authorized_max_requests": SCREENSHOT_BUDGET,
                           "requests_attempted": screen_budget.get("requests", 0)},
            },
            "claude_passthrough": {
                "status": str(passthrough.get("status", passthrough.get("summary", {}).get("conclusion", "UNKNOWN"))).upper(),
                "result_count": len(pass_items), "counts": pass_counts,
                "budget": {"authorized_max_requests": PASSTHROUGH_BUDGET,
                           "requests_attempted": pass_budget.get("requests_attempted", 0)},
            },
        },
        "budget": {"authorized_max_requests": TOTAL_BUDGET,
                   "note": "请求数上限不是金额上限；输入、输出、thinking、缓存和网关计费依目标而定。"},
        "nonpass_items": nonpass,
        "evidence_boundaries": [
            "仅包含客户端可观测结果；PASS 不代表模型官方身份或上游未改写。",
            "隐藏系统提示、中转出站请求和上游账号状态均不可观测。",
            "请求数上限不是金额上限；本工具无法硬性限制平台扣费。",
            "报告不包含 API Key、原始提示词、原始响应或 SSE 数据。",
        ],
    }
    return result, render_markdown(result)


def render_markdown(report: Mapping[str, Any]) -> str:
    suites = report.get("suites", {})
    target = report.get("target", {})
    lines = [
        "# 飞书机器人联合测试报告", "",
        f"- 结论：`{report.get('conclusion', 'INCONCLUSIVE')}`",
        f"- 运行编号：`{report.get('run_id', '')}`",
        f"- 目标：`{target.get('base_url', '')}`",
        f"- 模型：`{target.get('model', '')}`",
        f"- 协议：`{target.get('protocol', '')}`",
        f"- 总请求上限：`{report.get('budget', {}).get('authorized_max_requests', TOTAL_BUDGET)}`",
        f"- 耗时：`{report.get('duration_seconds', 0)} 秒`", "",
        "## 套件摘要", "", "| 套件 | 状态 | 结果数 | 状态计数 | 请求数/上限 |",
        "|---|---|---:|---|---:|",
    ]
    labels = {"screenshot_alignment": "截图对齐", "claude_passthrough": "Claude 透传"}
    for key in ("screenshot_alignment", "claude_passthrough"):
        suite = suites.get(key, {}) if isinstance(suites, Mapping) else {}
        budget = suite.get("budget", {}) if isinstance(suite, Mapping) else {}
        counts = json.dumps(suite.get("counts", {}), ensure_ascii=False)
        lines.append("| {} | `{}` | {} | `{}` | {}/{} |".format(
            labels[key], suite.get("status", "UNKNOWN"), suite.get("result_count", 0),
            counts.replace("|", "\\|"), budget.get("requests_attempted", 0),
            budget.get("authorized_max_requests", 0)))
    lines.extend(["", "## 非 PASS 项目", ""])
    nonpass = report.get("nonpass_items", [])
    if nonpass:
        lines.extend(["| 套件 | 项目 | 状态 | 观测 |", "|---|---|---|---|"])
        for item in nonpass:
            lines.append("| {} | {} ({}) | `{}` | {} |".format(
                item.get("suite", ""), item.get("title", ""), item.get("id", ""),
                item.get("status", ""), str(item.get("actual", "")).replace("|", "\\|")))
    else:
        lines.append("本轮未记录非 PASS 项目。")
    lines.extend(["", "## 证据边界", ""])
    lines.extend(f"- {line}" for line in report.get("evidence_boundaries", []))
    lines.extend(["", "> 本报告不包含 Key、原始提示词、原始响应或 SSE；结论仅描述本轮客户端可观测行为。", ""])
    return "\n".join(lines)


def write_reports(report: Mapping[str, Any], markdown: str, output_dir: Path = REPORT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = report.get("run_id", "")
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        run_id = uuid.uuid4().hex
    stem = f"feishu-combined-{run_id}"
    json_path = output_dir / f"{stem}.json"
    md_path = output_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    return json_path, md_path


class BotService:
    """State machine plus a single serial paid-test worker."""

    def __init__(self, config: BotConfig, feishu: FeishuApi,
                 workbench: WorkbenchClient | None = None,
                 *, report_dir: Path = REPORT_DIR):
        self.config = config
        self.feishu = feishu
        self.workbench = workbench or LocalWorkbenchClient(config)
        self.report_dir = report_dir
        self.pending: dict[str, PendingJob] = {}
        self.seen_messages: dict[str, float] = {}
        self.lock = threading.Lock()
        self.events: queue.Queue[tuple[str, str, str, str, str, bool] | None] = queue.Queue()
        self.jobs: queue.Queue[TestJob | None] = queue.Queue()
        self.dispatcher = threading.Thread(target=self._dispatch_loop, name="feishu-command-dispatcher", daemon=True)
        self.worker = threading.Thread(target=self._worker_loop, name="ccmax-paid-worker", daemon=True)
        if config.start_workers:
            self.dispatcher.start()
            self.worker.start()

    def close(self) -> None:
        if self.config.start_workers:
            self.events.put(None)
            self.jobs.put(None)
            self.dispatcher.join(timeout=5)
            self.worker.join(timeout=5)

    def enqueue_message(self, open_id: str, message_id: str, text: str,
                        chat_type: str = "p2p", chat_id: str = "",
                        mentioned_bot: bool = False) -> None:
        """Called by the SDK callback; this method performs no network I/O."""
        self.events.put((open_id, message_id, text, chat_type, chat_id, mentioned_bot))

    def _dispatch_loop(self) -> None:
        while True:
            event = self.events.get()
            try:
                if event is None:
                    return
                self.handle_message(*event)
            except Exception as exc:
                LOGGER.warning("message dispatch failed: %s", type(exc).__name__)
            finally:
                self.events.task_done()

    def handle_message(self, open_id: str, message_id: str, text: str,
                       chat_type: str = "p2p", chat_id: str = "",
                       mentioned_bot: bool = False) -> bool:
        """Handle one event; only allowlisted people and (for groups) chats can act."""
        if open_id not in self.config.allowed_open_ids:
            LOGGER.warning("rejected unauthorized Feishu sender")
            return False
        if chat_type == "group":
            if chat_id not in self.config.allowed_chat_ids:
                LOGGER.warning("rejected unauthorized Feishu group")
                return False
            if not mentioned_bot:
                return False
            reply_id, reply_type = chat_id, "chat_id"
        elif chat_type == "p2p":
            reply_id, reply_type = open_id, "open_id"
            chat_id = ""
        else:
            return False
        if not message_id or len(message_id) > 200:
            return False
        now = time.monotonic()
        dedup_key = f"{chat_type}:{chat_id or open_id}:{message_id}"
        with self.lock:
            self.seen_messages = {k: v for k, v in self.seen_messages.items() if now - v < DEDUP_TTL_SECONDS}
            if dedup_key in self.seen_messages:
                return False
            self.seen_messages[dedup_key] = now
            self.pending = {k: v for k, v in self.pending.items() if now - v.created_at < PENDING_TTL_SECONDS}
        stripped = text.strip()
        if stripped.startswith("/help") or stripped == "帮助":
            self._reply(reply_id, self._help_text(), reply_type)
            return True
        if stripped.startswith("确认 ") or stripped.startswith("/confirm "):
            token = stripped.split(None, 1)[1].strip()
            self._confirm(open_id, chat_id, token, reply_id, reply_type)
            return True
        try:
            parsed = parse_command(stripped)
        except CommandError as exc:
            self._reply(reply_id, str(exc), reply_type)
            return True
        if parsed is None:
            self._reply(reply_id, self._help_text(), reply_type)
            return True
        base_url, api_key, model = parsed
        try:
            base_url = validate_base_url(base_url, allow_private=self.config.allow_private_targets)
            api_key = validate_key(api_key)
            model = validate_model(model)
        except CommandError as exc:
            self._reply(reply_id, f"参数校验失败：{exc}", reply_type)
            return True
        token = secrets.token_urlsafe(12)
        with self.lock:
            self.pending[token] = PendingJob(token, open_id, base_url, api_key, model, now, chat_id)
        self._reply(reply_id, (
            "已接收测试请求，尚未调用付费接口。\n"
            f"目标：{safe_target(base_url)}\n模型：{model}\n"
            f"预算：截图对齐 {SCREENSHOT_BUDGET} 次 + Claude 透传 {PASSTHROUGH_BUDGET} 次 = {TOTAL_BUDGET} 次请求上限。\n"
            "请求数不是金额上限。\n"
            f"如确认执行，请在 10 分钟内回复：@机器人 确认 {token}\n"
            "注意：群消息中的 Key 和确认令牌对群成员可见，并会保留在飞书消息历史中。\n"
            "凭据只保存在本进程内存，运行后删除。"), reply_type)
        return True

    def _confirm(self, open_id: str, chat_id: str, token: str,
                 reply_id: str, reply_type: str) -> None:
        with self.lock:
            pending = self.pending.get(token)
            if pending and (pending.open_id != open_id or pending.chat_id != chat_id):
                pending = None
            elif pending:
                self.pending.pop(token, None)
        if not pending:
            self._reply(reply_id, "确认令牌无效、已过期，或不属于当前用户/会话。", reply_type)
            return
        job = TestJob(open_id, pending.base_url, pending.api_key, pending.model,
                      uuid.uuid4().hex, datetime.now(timezone.utc).isoformat(), chat_id)
        self.jobs.put(job)
        self._reply(reply_id, f"已进入串行队列，运行编号：{job.run_id}。开始后将依次执行两个套件。", reply_type)

    def _worker_loop(self) -> None:
        while True:
            job = self.jobs.get()
            try:
                if job is None:
                    return
                self._run_job(job)
            except Exception as exc:  # never log exception text or credentials
                LOGGER.error("worker job failed: %s", type(exc).__name__)
                self._reply(job.chat_id or job.open_id, f"测试执行失败（{type(exc).__name__}），请查看本地日志。", "chat_id" if job.chat_id else "open_id")
            finally:
                self.jobs.task_done()

    def _run_job(self, job: TestJob) -> None:
        started = time.monotonic()
        self._reply(job.chat_id or job.open_id, "开始执行截图对齐测试，预计最多 56 次请求。", "chat_id" if job.chat_id else "open_id")
        try:
            screen = self.workbench.run_screenshot(job)
            self._reply(job.chat_id or job.open_id, "截图对齐完成，开始 Claude 透传验证，预计最多 10 次请求。", "chat_id" if job.chat_id else "open_id")
            passthrough = self.workbench.run_passthrough(job)
            report, markdown = aggregate_report(screen, passthrough, job, time.monotonic() - started)
            _json_path, md_path = write_reports(report, markdown, self.report_dir)
            summary = self._summary(report)
            self._reply(job.chat_id or job.open_id, summary, "chat_id" if job.chat_id else "open_id")
            try:
                self.feishu.send_markdown_file(job.chat_id or job.open_id, md_path, "chat_id" if job.chat_id else "open_id")
            except Exception as exc:
                LOGGER.warning("report upload failed: %s", type(exc).__name__)
                self._reply(job.chat_id or job.open_id, "报告文件上传失败；本地报告已生成，请联系管理员。", "chat_id" if job.chat_id else "open_id")
        finally:
            # TestJob only lives for this worker call; do not retain credentials.
            del job

    def _summary(self, report: Mapping[str, Any]) -> str:
        suites = report.get("suites", {})
        screen = suites.get("screenshot_alignment", {}) if isinstance(suites, Mapping) else {}
        passthrough = suites.get("claude_passthrough", {}) if isinstance(suites, Mapping) else {}
        return (
            f"测试完成。结论：{report.get('conclusion')}\n"
            f"截图对齐：{screen.get('status')}，{json.dumps(screen.get('counts', {}), ensure_ascii=False)}\n"
            f"Claude 透传：{passthrough.get('status')}，{json.dumps(passthrough.get('counts', {}), ensure_ascii=False)}\n"
            f"运行编号：{report.get('run_id')}\n"
            "PASS 仅表示本轮可执行断言通过，不代表模型身份或上游无改写。")

    def _help_text(self) -> str:
        return (
            "CCMax 测试机器人\n"
            "1. 在已授权群聊中 @机器人：测试 URL|KEY|MODEL\n"
            "2. 核对目标、模型和 66 次请求上限后，@机器人 回复：确认 <令牌>\n"
            "3. 机器人依次执行截图对齐和 Claude 透传，并在原群发送 Markdown 报告。\n"
            "群消息中的 Key、确认令牌和结果对群成员可见；仅限受控测试群。\n"
            "仅支持 anthropic-messages 单端点；不支持查询参数或 URL 内嵌凭据。")

    def _reply(self, receive_id: str, text: str, receive_id_type: str) -> None:
        if receive_id_type == "open_id":
            self._queue_reply(receive_id, text)
        else:
            self._queue_reply(receive_id, text, receive_id_type)

    def _queue_reply(self, open_id: str, text: str, receive_id_type: str = "open_id") -> None:
        """Never block the event callback on a Feishu network call."""
        def send() -> None:
            try:
                self.feishu.send_text(open_id, text, receive_id_type)
            except Exception as exc:
                LOGGER.warning("Feishu reply failed: %s", type(exc).__name__)
        threading.Thread(target=send, name="feishu-reply", daemon=True).start()


def _event_fields(data: Any, bot_open_id: str = "") -> tuple[str, str, str, str, str, bool] | None:
    event = getattr(data, "event", None)
    message = getattr(event, "message", None)
    sender = getattr(event, "sender", None)
    sender_id = getattr(sender, "sender_id", None)
    open_id = getattr(sender_id, "open_id", None)
    if not open_id or not message:
        return None
    if getattr(message, "message_type", "text") not in (None, "", "text"):
        return None
    content = getattr(message, "content", "") or ""
    try:
        payload = json.loads(content)
        text = payload.get("text", "") if isinstance(payload, dict) else ""
    except (TypeError, ValueError):
        text = ""
    mentions = getattr(message, "mentions", None) or []
    mentioned_bot = False
    for mention in mentions:
        mention_id = getattr(mention, "id", None)
        mentioned_open_id = getattr(mention_id, "open_id", None) or mention_id
        if bot_open_id and str(mentioned_open_id or "") == bot_open_id:
            mentioned_bot = True
        key = getattr(mention, "key", None)
        if key:
            text = text.replace(str(key), " ")
    return (str(open_id), str(getattr(message, "message_id", "") or ""),
            str(text or "").strip(), str(getattr(message, "chat_type", "") or "p2p"),
            str(getattr(message, "chat_id", "") or ""), mentioned_bot)


def build_event_handler(service: BotService):
    import lark_oapi as lark

    def on_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        fields = _event_fields(data, service.config.bot_open_id)
        if fields:
            open_id, message_id, text, chat_type, chat_id, mentioned_bot = fields
            # SDK callback performs one in-memory queue put and returns immediately.
            service.enqueue_message(open_id, message_id, text, chat_type, chat_id, mentioned_bot)

    return (lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_message).build())


def run_bot(config: BotConfig | None = None) -> None:
    import lark_oapi as lark
    config = config or BotConfig.from_env()
    feishu = FeishuApiClient(config.app_id, config.app_secret)
    service = BotService(config, feishu)
    handler = build_event_handler(service)
    client = lark.ws.Client(config.app_id, config.app_secret, event_handler=handler,
                            log_level=lark.LogLevel.INFO)
    LOGGER.info("Feishu long connection starting")
    try:
        client.start()
    finally:
        service.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CCMax Feishu bot (long connection)")
    parser.add_argument("--check", action="store_true", help="validate local configuration and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = BotConfig.from_env()
        if args.check:
            print("配置有效；未建立飞书连接，未执行真实测试。")
            return 0
        run_bot(config)
        return 0
    except BotError as exc:
        LOGGER.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
