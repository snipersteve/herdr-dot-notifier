#!/usr/bin/python3
"""Push Herdr background-agent completion events to a MindReset Dot display."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

PLUGIN_ID = "show.herdr-dot-notifier"
VERSION = "1.10.0"
DEFAULT_API_BASE = "https://dot.mindreset.tech"
DEFAULT_KEYCHAIN_SERVICE = "show.herdr-dot-notifier.dot-api-key"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CHATGPT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DEFAULT_CLAUDE_CREDENTIALS = Path.home() / ".claude/.credentials.json"
DEFAULT_OPENAI_AUTH = Path.home() / ".pi/agent/auth.json"
USAGE_CACHE_SECONDS = 300
USAGE_FORMAT_VERSION = 2
# signature 在设备端按单行渲染，因此只放压缩后的用量摘要。
DEFAULT_SIGNATURE_STYLE = {"fontFamily": "FusionPixel12", "fontSize": 12}


def log(message: str) -> None:
    """Write a timestamped line to the plugin log and stderr."""
    line = f"{datetime.now().astimezone().isoformat(timespec='seconds')} {message}"
    print(line, file=sys.stderr, flush=True)
    state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if not state_dir:
        return
    try:
        path = Path(state_dir)
        path.mkdir(parents=True, exist_ok=True)
        with (path / "notifier.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def load_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_config() -> Dict[str, Any]:
    config_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if not config_dir:
        raise RuntimeError("HERDR_PLUGIN_CONFIG_DIR is not set")
    config_path = Path(config_dir) / "config.json"
    config = load_json(config_path)
    required = ("device_id", "task_key")
    missing = [key for key in required if not str(config.get(key, "")).strip()]
    if missing:
        raise RuntimeError(f"missing config fields: {', '.join(missing)}")
    return config


def parse_event() -> Dict[str, Any]:
    raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON", "")
    if not raw:
        raise RuntimeError("HERDR_PLUGIN_EVENT_JSON is empty")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid event JSON: {exc}") from exc
    if not isinstance(envelope, dict):
        raise RuntimeError("event JSON is not an object")
    data = envelope.get("data")
    return data if isinstance(data, dict) else envelope


def run_herdr(*args: str) -> Dict[str, Any]:
    binary = os.environ.get("HERDR_BIN_PATH", "herdr")
    completed = subprocess.run(
        [binary, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"herdr {' '.join(args)} failed: {detail[:300]}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"herdr {' '.join(args)} returned invalid JSON") from exc
    return payload if isinstance(payload, dict) else {}


def get_agent_info(pane_id: str) -> Dict[str, Any]:
    payload = run_herdr("agent", "get", pane_id)
    result = payload.get("result")
    if not isinstance(result, dict):
        return {}
    agent = result.get("agent")
    return agent if isinstance(agent, dict) else {}


def list_working_sessions(exclude_pane_id: str, max_items: int = 3) -> str:
    """Return one compact line naming other agents that are still working."""
    try:
        payload = run_herdr("agent", "list")
        result = payload.get("result")
        agents = result.get("agents") if isinstance(result, dict) else None
        if not isinstance(agents, list):
            return "进行中：无"

        counts: Dict[str, int] = {}
        for agent in agents:
            if not isinstance(agent, dict):
                continue
            if agent.get("agent_status") != "working":
                continue
            if str(agent.get("pane_id") or "") == exclude_pane_id:
                continue

            label = session_label(agent)
            if label:
                counts[label] = counts.get(label, 0) + 1

        if not counts:
            return "进行中：无"
        labels = [
            f"{title}×{count}" if count > 1 else title
            for title, count in counts.items()
        ]
        shown = labels[:max_items]
        total_sessions = sum(counts.values())
        suffix = f" 等{total_sessions}个会话" if len(labels) > max_items else ""
        return "进行中：" + "、".join(shown) + suffix
    except Exception as exc:
        log(f"working-session lookup skipped: {exc}")
        return "进行中：未知"


def clean_text(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip()


def display_agent_name(value: str) -> str:
    names = {
        "pi": "Pi",
        "claude": "Claude",
        "codex": "Codex",
        "opencode": "OpenCode",
        "copilot": "Copilot",
        "kimi": "Kimi",
        "grok": "Grok",
        "gemini": "Gemini",
        "droid": "Droid",
    }
    cleaned = clean_text(value, 30)
    return names.get(cleaned.casefold(), cleaned or "Agent")


def session_label(agent_info: Dict[str, Any], fallback_agent: Any = None) -> str:
    """Build the canonical folder-Agent label without using terminal titles."""
    cwd = clean_text(
        agent_info.get("foreground_cwd") or agent_info.get("cwd"), 300
    )
    folder = Path(cwd).name if cwd else "未知目录"
    raw_agent = agent_info.get("agent") or fallback_agent
    return f"{folder}-{display_agent_name(str(raw_agent or ''))}"


def keychain_password(service: str, account: str) -> str:
    completed = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            service,
            "-a",
            account,
            "-w",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Dot API key was not found in macOS Keychain")
    token = completed.stdout.strip()
    if not token:
        raise RuntimeError("Dot API key in macOS Keychain is empty")
    return token


def state_directory() -> Path:
    state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if not state_dir:
        state_dir = str(Path(tempfile.gettempdir()) / PLUGIN_ID)
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def state_paths() -> tuple[Path, Path]:
    directory = state_directory()
    return directory / "notified.json", directory / "notified.lock"


def save_json_atomic(path: Path, value: Dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def usage_label(limit: Dict[str, Any]) -> str:
    kind = limit.get("kind")
    if kind == "session":
        return "5h"
    if kind == "weekly_all":
        return "周"
    scope = limit.get("scope")
    model = scope.get("model") if isinstance(scope, dict) else None
    name = model.get("display_name") if isinstance(model, dict) else None
    return clean_text(name, 12) or clean_text(kind, 12) or "?"


def reset_timestamp(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value) / 1000 if value > 10_000_000_000 else float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def pace_arrow(
    used_percent: float,
    resets_at: Any,
    window_seconds: Any,
    now: Optional[float] = None,
) -> str:
    """Compare usage with elapsed window progress: ↑ ahead, ↓ behind, → level."""
    reset = reset_timestamp(resets_at)
    if reset is None or not isinstance(window_seconds, (int, float)) or window_seconds <= 0:
        return ""
    elapsed = max(0.0, min(1.0, 1.0 - (reset - (now or time.time())) / window_seconds))
    progress_percent = elapsed * 100
    delta = used_percent - progress_percent
    if abs(delta) < 0.5:
        return "→"
    return "↑" if delta > 0 else "↓"


def claude_window_seconds(limit: Dict[str, Any]) -> Optional[int]:
    if limit.get("kind") == "session":
        return 5 * 60 * 60
    if limit.get("group") == "weekly" or str(limit.get("kind", "")).startswith("weekly"):
        return 7 * 24 * 60 * 60
    return None


def format_usage(data: Dict[str, Any]) -> str:
    """Condense Claude usage and compare each limit with elapsed-window progress."""
    parts = []
    limits = data.get("limits")
    if isinstance(limits, list) and limits:
        for limit in limits:
            if not isinstance(limit, dict) or limit.get("percent") is None:
                continue
            used = float(limit["percent"])
            arrow = pace_arrow(
                used, limit.get("resets_at"), claude_window_seconds(limit)
            )
            mark = "!" if limit.get("severity") not in (None, "normal") else ""
            parts.append(f"{usage_label(limit)} {round(used)}%{arrow}{mark}")
    else:
        for key, label, seconds in (
            ("five_hour", "5h", 5 * 60 * 60),
            ("seven_day", "周", 7 * 24 * 60 * 60),
        ):
            window = data.get(key)
            if isinstance(window, dict) and window.get("utilization") is not None:
                used = float(window["utilization"])
                arrow = pace_arrow(used, window.get("resets_at"), seconds)
                parts.append(f"{label} {round(used)}%{arrow}")
    return " · ".join(parts)


def fetch_claude_usage(config: Dict[str, Any]) -> str:
    path = Path(
        str(config.get("claude_credentials_path") or DEFAULT_CLAUDE_CREDENTIALS)
    ).expanduser()
    oauth = load_json(path).get("claudeAiOauth")
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        raise RuntimeError(f"no Claude OAuth token in {path}")
    # 只读 token、从不刷新：刷新会轮换 refresh token，与 Claude Code 抢写凭据文件。
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at / 1000 <= time.time():
        raise RuntimeError("Claude OAuth token expired; waiting for Claude Code to refresh it")
    request = urllib.request.Request(
        CLAUDE_USAGE_URL,
        method="GET",
        headers={
            "Authorization": f"Bearer {oauth['accessToken']}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": f"{PLUGIN_ID}/{VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Claude usage API returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Claude usage request failed: {exc}") from exc
    line = format_usage(data) if isinstance(data, dict) else ""
    if not line:
        raise RuntimeError("Claude usage response has no recognizable limits")
    return line


def claude_usage_line(config: Dict[str, Any], state_dir: Path) -> str:
    """Return a cached one-line Claude Code usage summary; empty when disabled."""
    if config.get("claude_usage") is False:
        return ""
    cache_path = state_dir / "claude_usage.json"
    cache = load_json(cache_path)
    fetched_at = cache.get("fetched_at")
    if (
        cache.get("format_version") == USAGE_FORMAT_VERSION
        and isinstance(fetched_at, (int, float))
        and time.time() - fetched_at < USAGE_CACHE_SECONDS
    ):
        return str(cache.get("line") or "")
    try:
        line = fetch_claude_usage(config)
    except Exception as exc:
        log(f"Claude usage lookup failed: {exc}")
        line = "用量未知"
    # 失败也缓存：token 过期期间不必每个状态事件都去请求一次。
    save_json_atomic(
        cache_path,
        {"format_version": USAGE_FORMAT_VERSION, "fetched_at": time.time(), "line": line},
    )
    return line


def chatgpt_window_label(window: Dict[str, Any]) -> str:
    seconds = window.get("limit_window_seconds")
    if isinstance(seconds, (int, float)):
        if seconds <= 6 * 60 * 60:
            return "GPT5h"
        if seconds >= 6 * 24 * 60 * 60:
            return "GPT周"
    return "GPT"


def format_chatgpt_usage(data: Dict[str, Any]) -> str:
    """Condense Codex limits into used percentages, e.g. `GPT周 18%`."""
    rate_limit = data.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return ""
    parts = []
    for key in ("primary_window", "secondary_window"):
        window = rate_limit.get(key)
        if not isinstance(window, dict) or window.get("used_percent") is None:
            continue
        used_raw = max(0.0, min(100.0, float(window["used_percent"])))
        used = round(used_raw)
        arrow = pace_arrow(
            used_raw, window.get("reset_at"), window.get("limit_window_seconds")
        )
        parts.append(f"{chatgpt_window_label(window)} {used}%{arrow}")
    return " · ".join(parts)


def fetch_chatgpt_usage(config: Dict[str, Any]) -> str:
    path = Path(str(config.get("openai_auth_path") or DEFAULT_OPENAI_AUTH)).expanduser()
    credential = load_json(path).get("openai-codex")
    if not isinstance(credential, dict):
        raise RuntimeError(f"no openai-codex OAuth credential in {path}")
    access = credential.get("access")
    account_id = credential.get("accountId")
    if not access or not account_id:
        raise RuntimeError(f"incomplete openai-codex OAuth credential in {path}")
    # 与 Claude 一样只读现有 token，不刷新、不改写 Pi 的 auth.json。
    expires_at = credential.get("expires")
    if isinstance(expires_at, (int, float)) and expires_at / 1000 <= time.time():
        raise RuntimeError("OpenAI OAuth token expired; waiting for Pi to refresh it")
    request = urllib.request.Request(
        CHATGPT_USAGE_URL,
        method="GET",
        headers={
            "Authorization": f"Bearer {access}",
            "chatgpt-account-id": str(account_id),
            "originator": "pi",
            "Accept": "application/json",
            "User-Agent": f"{PLUGIN_ID}/{VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"ChatGPT usage API returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"ChatGPT usage request failed: {exc}") from exc
    line = format_chatgpt_usage(data) if isinstance(data, dict) else ""
    if not line:
        raise RuntimeError("ChatGPT usage response has no recognizable limits")
    return line


def chatgpt_usage_line(config: Dict[str, Any], state_dir: Path) -> str:
    """Return a cached ChatGPT Codex used-percentage summary; empty when disabled."""
    if config.get("chatgpt_usage") is False:
        return ""
    cache_path = state_dir / "chatgpt_usage.json"
    cache = load_json(cache_path)
    fetched_at = cache.get("fetched_at")
    if (
        cache.get("format_version") == USAGE_FORMAT_VERSION
        and isinstance(fetched_at, (int, float))
        and time.time() - fetched_at < USAGE_CACHE_SECONDS
    ):
        return str(cache.get("line") or "")
    try:
        line = fetch_chatgpt_usage(config)
    except Exception as exc:
        log(f"ChatGPT usage lookup failed: {exc}")
        line = "GPT?"
    save_json_atomic(
        cache_path,
        {"format_version": USAGE_FORMAT_VERSION, "fetched_at": time.time(), "line": line},
    )
    return line


def compact_claude_usage(line: str) -> str:
    """Turn `5h 3% · 周 30% · Fable 45%` into `Claude 30% Fable 45%`."""
    if not line:
        return ""
    if line == "用量未知":
        return "Claude ?"
    weekly = ""
    models = []
    for raw_part in line.split("·"):
        part = raw_part.strip()
        match = re.match(r"^(.+?)\s+(\d+%(?:[↑↓→])?!?$)", part)
        if not match:
            continue
        label, percent = match.groups()
        if label == "周":
            weekly = f"Claude {percent}"
        elif label != "5h":
            models.append(f"{label} {percent}")
    return " ".join(([weekly] if weekly else []) + models)


def compact_chatgpt_usage(line: str) -> str:
    if not line:
        return ""
    match = re.search(r"(\d+%(?:[↑↓→])?)", line)
    return f"GPT {match.group(1)}" if match else "GPT ?"


def usage_summary(config: Dict[str, Any], state_dir: Path) -> str:
    return " ".join(
        part
        for part in (
            compact_claude_usage(claude_usage_line(config, state_dir)),
            compact_chatgpt_usage(chatgpt_usage_line(config, state_dir)),
        )
        if part
    )


def build_signature(usage_line: str) -> str:
    return usage_line or datetime.now().astimezone().strftime("%H:%M")


def build_styles(config: Dict[str, Any]) -> Dict[str, Any]:
    signature_style = config.get("signature_style")
    return {
        "signature": (
            signature_style
            if isinstance(signature_style, dict)
            else dict(DEFAULT_SIGNATURE_STYLE)
        ),
    }


def push_dot(config: Dict[str, Any], api_key: str, payload: Dict[str, Any]) -> str:
    api_base = str(config.get("api_base") or DEFAULT_API_BASE).rstrip("/")
    device_id = str(config["device_id"]).strip()
    url = f"{api_base}/api/authV2/open/device/{device_id}/text"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"{PLUGIN_ID}/{VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", "replace")
            if response.status != 200:
                raise RuntimeError(f"Dot API returned HTTP {response.status}: {body[:300]}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Dot API returned HTTP {exc.code}: {body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Dot API request failed: {exc.reason}") from exc
    return body


def resolve_api_key(config: Dict[str, Any]) -> str:
    api_key = str(config.get("api_key") or "").strip()
    if api_key:
        return api_key
    service = str(config.get("keychain_service") or DEFAULT_KEYCHAIN_SERVICE)
    account = str(config.get("keychain_account") or os.environ.get("USER") or "steve")
    return keychain_password(service, account)


def build_payload(
    config: Dict[str, Any],
    event: Dict[str, Any],
    agent_info: Dict[str, Any],
    working_sessions: str,
    usage_line: str,
) -> Dict[str, Any]:
    raw_agent = event.get("display_agent") or event.get("agent")
    message = "\n".join(
        ["已完成：" + session_label(agent_info, raw_agent), working_sessions]
    )

    return {
        "refreshNow": True,
        "taskKey": str(config["task_key"]).strip(),
        "taskAlias": str(config.get("task_alias") or "Herdr Agent")[:100],
        "title": "Herdr通知",
        "message": message[:500],
        "signature": build_signature(usage_line),
        "styles": build_styles(config),
    }


def refresh_working_line(
    config: Dict[str, Any], stored_payload: Any, working_sessions: str, usage_line: str
) -> Dict[str, Any]:
    """Update the live working-session line and usage footer of the last card."""
    if isinstance(stored_payload, dict):
        payload = dict(stored_payload)
        lines = str(payload.get("message") or "").splitlines()
        completed_line = next(
            (line for line in lines if line.startswith("已完成：")), "已完成：无"
        )
        payload["title"] = "Herdr通知"
        payload["message"] = (completed_line + "\n" + working_sessions)[-500:]
        payload["signature"] = build_signature(usage_line)
        payload["styles"] = build_styles(config)
        payload["refreshNow"] = True
        payload["taskKey"] = str(config["task_key"]).strip()
        return payload

    return {
        "refreshNow": True,
        "taskKey": str(config["task_key"]).strip(),
        "taskAlias": str(config.get("task_alias") or "Herdr Agent")[:100],
        "title": "Herdr通知",
        "message": "已完成：无\n" + working_sessions,
        "signature": build_signature(usage_line),
        "styles": build_styles(config),
    }


def compact_suppressed(pane_id) -> bool:
    """show.herdr-idle-compact 刚对该窗格发了 /compact，压缩引起的状态变化不推送。"""
    path = (Path.home() / ".local/state/herdr/plugins/show.herdr-idle-compact"
            / "suppress.json")
    try:
        return json.loads(path.read_text(encoding="utf-8")).get(pane_id, 0) > time.time()
    except (OSError, ValueError, AttributeError):
        return False


def main() -> int:
    if os.environ.get("HERDR_PLUGIN_EVENT") not in (
        None,
        "",
        "pane.agent_status_changed",
    ):
        return 0

    try:
        event = parse_event()
    except Exception as exc:
        log(f"ignored malformed event: {exc}")
        return 0

    status = str(event.get("agent_status") or "")
    if status not in {"idle", "working", "blocked", "done", "unknown"}:
        return 0

    pane_id = clean_text(event.get("pane_id"), 100)
    if not pane_id:
        log("ignored status event without pane_id")
        return 0

    if compact_suppressed(pane_id):
        log(f"{pane_id} {status} ignored: idle auto-compact")
        return 0

    try:
        config = load_config()
        notified_path, lock_path = state_paths()

        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            state = load_json(notified_path)
            working_sessions = list_working_sessions(pane_id if status == "done" else "")

            if status != "done":
                # 用量只随既有推送顺带刷新，不单独触发墨水屏刷新。
                if state.get("last_working_sessions") == working_sessions:
                    return 0
                usage_line = usage_summary(config, notified_path.parent)
                payload = refresh_working_line(
                    config, state.get("last_payload"), working_sessions, usage_line
                )
                if os.environ.get("HERDR_DOT_DRY_RUN") == "1":
                    log("dry-run status payload: " + json.dumps(payload, ensure_ascii=False))
                    return 0
                response = push_dot(config, resolve_api_key(config), payload)
                state["last_payload"] = payload
                state["last_working_sessions"] = working_sessions
                state["last_success_at"] = datetime.now().astimezone().isoformat(
                    timespec="seconds"
                )
                save_json_atomic(notified_path, state)
                log(
                    f"working sessions refreshed after {pane_id} -> {status}; "
                    f"summary={working_sessions}; usage={usage_line}; "
                    f"Dot response={clean_text(response, 240)}"
                )
                return 0

            agent_info = get_agent_info(pane_id)
            sequence = agent_info.get("state_change_seq")
            event_key = (
                str(sequence)
                if sequence is not None
                else json.dumps(event, sort_keys=True)
            )
            last_by_pane = state.get("last_by_pane")
            if not isinstance(last_by_pane, dict):
                last_by_pane = {}
            if last_by_pane.get(pane_id) == event_key:
                log(f"duplicate completion ignored for {pane_id} seq={event_key}")
                return 0

            usage_line = usage_summary(config, notified_path.parent)
            payload = build_payload(config, event, agent_info, working_sessions, usage_line)

            if os.environ.get("HERDR_DOT_DRY_RUN") == "1":
                log("dry-run payload: " + json.dumps(payload, ensure_ascii=False))
                return 0

            response = push_dot(config, resolve_api_key(config), payload)
            last_by_pane[pane_id] = event_key
            state["last_by_pane"] = last_by_pane
            state["last_payload"] = payload
            state["last_working_sessions"] = working_sessions
            state["last_success_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            save_json_atomic(notified_path, state)
            log(
                f"completion pushed for {pane_id} seq={event_key}; "
                f"summary={working_sessions}; usage={usage_line}; "
                f"Dot response={clean_text(response, 240)}"
            )
        return 0
    except Exception as exc:
        log(f"status push failed for {pane_id} -> {status}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
