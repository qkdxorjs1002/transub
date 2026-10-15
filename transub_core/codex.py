"""A fresh Codex CLI process per request, with a private authentication-only home."""
from __future__ import annotations

import collections
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path


class CodexError(RuntimeError):
    pass


DISABLED_FEATURES = (
    "plugins", "remote_plugin", "workspace_dependencies", "apps", "hooks", "memories",
    "multi_agent", "multi_agent_v2", "shell_tool", "unified_exec", "shell_snapshot",
    "skill_search", "skill_mcp_dependency_install", "browser_use", "computer_use", "image_generation",
)


def clean_environment(home: Path, temporary: Path) -> dict[str, str]:
    # Do not inherit parent Codex session/MCP/provider/API-key overrides.
    allowed = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
               "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT")
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(CODEX_HOME=str(home), TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary),
               XDG_CONFIG_HOME=str(home / "xdg-config"), XDG_DATA_HOME=str(home / "xdg-data"),
               XDG_CACHE_HOME=str(home / "xdg-cache"), NO_COLOR="1", TERM="dumb")
    return env


DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_REASONING = "medium"


class CodexRunner:
    def __init__(self, executable: str = "codex", model: str | None = DEFAULT_MODEL,
                 reasoning: str | None = DEFAULT_REASONING, auth_home: Path | None = None,
                 timeout: float = 900):
        binary = shutil.which(executable)
        if not binary:
            raise CodexError("Codex CLI를 찾지 못했습니다. codex를 설치하고 로그인하세요.")
        self.executable = str(Path(binary).resolve())
        self.model, self.reasoning, self.timeout = model, reasoning, timeout
        self.source_home = auth_home or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        self.temp: tempfile.TemporaryDirectory | None = None
        self.audit: list[dict] = []

    def __enter__(self):
        auth = self.source_home / "auth.json"
        if not auth.is_file():
            raise CodexError("auth.json이 없습니다. 파일 저장 방식으로 codex login 후 --auth-home을 지정하세요. "
                             "키체인 전용 로그인은 자동 복사하지 않습니다.")
        self.temp = tempfile.TemporaryDirectory(prefix="transub-codex-")
        try:
            self.root = Path(self.temp.name).resolve()
            self.home, self.cwd = self.root / "codex-home", self.root / "work"
            self.home.mkdir(mode=0o700)
            self.cwd.mkdir(mode=0o700)
            self.env = clean_environment(self.home, self.root)
            # Opaque credential copy only. No config, skills, rules, plugins or session state.
            fd = os.open(self.home / "auth.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as dest, auth.open("rb") as source:
                shutil.copyfileobj(source, dest)
            help_result = subprocess.run([self.executable, "exec", "--help"], env=self.env,
                                         cwd=self.cwd, capture_output=True, text=True, timeout=30)
            required = ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--output-schema")
            if help_result.returncode or not all(flag in help_result.stdout for flag in required):
                raise CodexError("현재 Codex CLI에 필수 격리 옵션이 없습니다. Codex CLI를 업데이트하세요.")
            features = subprocess.run([self.executable, "features", "list"], env=self.env,
                                      cwd=self.cwd, capture_output=True, text=True, timeout=30)
            names = {line.split()[0] for line in features.stdout.splitlines() if line.split()}
            if features.returncode or "skip_host_skill_discovery" not in names:
                raise CodexError("이 Codex CLI는 호스트 스킬 검색 차단을 지원하지 않습니다.")
            self.disabled = [name for name in DISABLED_FEATURES if name in names]
            self.version = subprocess.run([self.executable, "--version"], env=self.env,
                                          cwd=self.cwd, capture_output=True, text=True, timeout=30).stdout.strip()
            # Managed/system settings remain in force. Fail if any MCP server survives isolation.
            mcp = subprocess.run([self.executable, "mcp", "list", "--json"], env=self.env,
                                 cwd=self.cwd, capture_output=True, text=True, timeout=30)
            if mcp.returncode or json.loads(mcp.stdout) != []:
                raise CodexError("격리된 홈에도 MCP 설정이 있습니다. 시스템/관리 설정을 확인하세요.")
            return self
        except BaseException:
            self.temp.cleanup()
            raise

    def __exit__(self, *args):
        if self.temp:
            self.temp.cleanup()

    def command(self, schema_path: Path, output_path: Path, research: bool) -> list[str]:
        args = [self.executable, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                "--skip-git-repo-check", "--sandbox", "read-only", "--cd", str(self.cwd),
                "--color", "never", "--json", "--output-schema", str(schema_path),
                "--output-last-message", str(output_path)]
        settings = ["project_doc_max_bytes=0", 'cli_auth_credentials_store="file"',
                    'forced_login_method="chatgpt"', 'history.persistence="none"',
                    'approval_policy="never"', f'web_search="{"live" if research else "disabled"}"']
        if self.reasoning:
            settings.append(f"model_reasoning_effort={json.dumps(self.reasoning)}")
        for setting in settings:
            args += ["-c", setting]
        for feature in self.disabled:
            args += ["--disable", feature]
        args += ["--enable", "skip_host_skill_discovery"]
        if self.model:
            args += ["--model", self.model]
        return args + ["-"]

    def call(self, prompt: str, schema: dict, label: str, research: bool = False) -> dict:
        with tempfile.TemporaryDirectory(prefix="request-", dir=self.root) as directory:
            folder = Path(directory)
            schema_path, output_path = folder / "schema.json", folder / "result.json"
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
            command = self.command(schema_path, output_path, research)
            started = time.monotonic()
            # Files prevent pipe deadlocks and avoid retaining unlimited stdout in memory.
            with (folder / "events.jsonl").open("w+") as events, (folder / "stderr.log").open("w+") as errors:
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=events, stderr=errors,
                                           text=True, encoding="utf-8", env=self.env, cwd=self.cwd,
                                           start_new_session=os.name != "nt")
                try:
                    process.communicate(prompt, timeout=self.timeout)
                except BaseException:
                    if process.poll() is None:
                        if os.name != "nt":
                            os.killpg(process.pid, signal.SIGTERM)
                        else:
                            process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            if os.name != "nt":
                                os.killpg(process.pid, signal.SIGKILL)
                            else:
                                process.kill()
                            process.wait()
                    raise
                events.seek(0)
                types = collections.Counter()
                usage = {}
                safe_errors = []
                warnings = []
                for line in events:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    item = event.get("item", {})
                    if event.get("type") == "item.completed":
                        types[item.get("type", "unknown")] += 1
                        if item.get("type") == "error":
                            warnings.append(str(item.get("message", ""))[:500])
                    if event.get("type") == "turn.completed":
                        usage = event.get("usage", {})
                    if event.get("type") in ("error", "turn.failed"):
                        message = event.get("message") or event.get("error", {}).get("message", "Codex turn failed")
                        # Do not surface arbitrary CLI stderr, which may contain secrets/headers.
                        safe_errors.append(str(message)[:800])
                audit = {"label": label, "seconds": round(time.monotonic() - started, 1),
                         "exit_code": process.returncode, "item_types": dict(types), "usage": usage,
                         "web_search_enabled": research, "codex_version": self.version, "model": self.model,
                         "isolated_home": True, "mcp_servers": 0, "user_config_loaded": False,
                         "host_skill_discovery": False, "disabled_features": self.disabled}
                # Keep non-secret diagnostics only, never stderr or complete raw events.
                import re
                audit["warnings"] = [re.sub(r"sk-[\w-]+|(?i:bearer)\s+\S+", "[REDACTED]", w) for w in warnings]
                self.audit.append(audit)
            if process.returncode:
                detail = "; ".join(safe_errors) or "네트워크·로그인·사용량·Codex 버전을 확인하세요."
                # Provider messages normally contain no credentials; redact common key/bearer formats.
                import re
                detail = re.sub(r"sk-[\w-]+|(?i:bearer)\s+\S+", "[REDACTED]", detail)
                raise CodexError(f"{label}: Codex 실패 ({process.returncode}): {detail}")
            if types["mcp_tool_call"] or types["command_execution"]:
                raise CodexError("격리 검증 실패: 예상하지 않은 MCP/명령 실행 이벤트")
            if research and not types["web_search"]:
                raise CodexError("작품 조사에서 실제 Codex 웹 검색 실행이 확인되지 않았습니다.")
            try:
                result = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CodexError(f"{label}: 구조화된 JSON 결과가 없습니다.") from exc
            return result
