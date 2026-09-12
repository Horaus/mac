from __future__ import annotations

import argparse
import json
import subprocess
import sys
import os
import io
import contextlib
import textwrap
from pathlib import Path

from .providers import provider
from .service import run_worker, validate, HOST_INSTANCE_ID
from .store import Store
from .setup import connect_codex, doctor, run_setup, show_config, show_mcp_config
from .profiles import resolve_profile
from . import __version__
from .authority import AuthorityPolicy, ApprovalToken, ExecutionMode, RuntimeReport, digest
from .service import authorize_runtime_action
from .organization import (add_evidence, cancel_goal as org_cancel_goal, create_goal as org_create_goal,
                            follow_up as org_follow_up, record_feedback as org_feedback, get_evidence, poll_goal, subscribe_goal, acknowledge_goal, resume_goal as org_resume_goal,
                            set_inspection_mode as org_set_inspection, summary as org_summary, inspect_archetype,
                            set_archetype_enabled, assign_archetype)
from .organization import recommend_worker
from .organization_runtime import OrganizationDaemon, configure_free_routing, configure_provider_eligibility, register_credential_ref
from .organization import garbage_collect_conversation, physically_collect_payloads, record_budget_telemetry
from .service_fixtures import ProductionServiceManager, SandboxedServiceManager, subprocess_service_executor

def _release_info(root: Path) -> tuple[str, str]:
    try:
        commit = subprocess.run(["git", "log", "-1", "--format=%h"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
        when = subprocess.run(["git", "log", "-1", "--format=%ad", "--date=iso"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
        return f"{__version__} ({commit})", when
    except (OSError, subprocess.CalledProcessError):
        return __version__, "unknown"

def _source_version(root: Path) -> str:
    """Read the installed source metadata after update, not this process import."""
    metadata = root / "pyproject.toml"
    try:
        for line in metadata.read_text().splitlines():
            if line.strip().startswith("version") and "=" in line:
                return line.split("=", 1)[1].strip().strip('"\'')
    except OSError:
        pass
    return __version__

def _update_project(root: Path) -> list[str]:
    subprocess.run(["git", "fetch", "origin", "main"], cwd=root, capture_output=True, text=True, check=True)
    subprocess.run(["git", "reset", "--hard", "origin/main"], cwd=root, capture_output=True, text=True, check=True)
    subprocess.run([str(_venv_python(root)), "-m", "pip", "install", "-e", "."], cwd=root, capture_output=True, text=True, check=True)
    _, when = _release_info(root)
    return [f"✓ Updated to version {_source_version(root)}.", f"✓ Update time: {when}.", "✓ Configuration and data were preserved."]

def _venv_python(root: Path) -> Path:
    """Return the project's virtualenv interpreter on POSIX or Windows."""
    candidates = [root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python"), Path(sys.executable)]
    return next((path for path in candidates if path.exists()), candidates[-1])

def _menu(project="."):
    try:
        import curses
    except (ImportError, ModuleNotFoundError):
        # Windows Python does not ship curses. Use line-based setup instead.
        run_setup(project)
        return 0
    def keypress(win):
        key = win.getch()
        if key == ord("["):
            win.timeout(80)
            tail = win.getch()
            win.timeout(-1)
            return {ord("A"): curses.KEY_UP, ord("B"): curses.KEY_DOWN,
                    ord("C"): curses.KEY_RIGHT, ord("D"): curses.KEY_LEFT}.get(tail, key)
        if key == 27:
            win.timeout(80)
            tail = []
            for _ in range(2):
                value = win.getch()
                if value == -1:
                    break
                tail.append(value)
            win.timeout(-1)
            seq = "".join(chr(x) for x in tail)
            return {"[A": curses.KEY_UP, "[B": curses.KEY_DOWN, "[C": curses.KEY_RIGHT, "[D": curses.KEY_LEFT}.get(seq, 27)
        return key
    menu_text = {
        "en": ["Configure workers / providers", "View status", "Update MAC", "System check", "Exit", "Select an action and press Enter", "↑↓ select   Enter open   q exit"],
        "vi": ["Thiết lập worker / provider", "Xem trạng thái", "Cập nhật MAC", "Kiểm tra hệ thống", "Thoát", "Chọn thao tác rồi nhấn Enter", "↑↓ chọn   Enter mở   q thoát"],
        "zh": ["配置工作者 / 提供商", "查看状态", "更新 MAC", "系统检查", "退出", "选择操作并按 Enter", "↑↓ 选择   Enter 打开   q 退出"],
        "ja": ["ワーカー / プロバイダー設定", "状態を表示", "MAC を更新", "システム確認", "終了", "操作を選択して Enter", "↑↓ 選択   Enter 開く   q 終了"],
        "ko": ["워커 / 제공자 설정", "상태 보기", "MAC 업데이트", "시스템 검사", "종료", "작업을 선택하고 Enter", "↑↓ 선택   Enter 열기   q 종료"],
        "fr": ["Configurer workers / fournisseurs", "Voir l’état", "Mettre MAC à jour", "Vérifier le système", "Quitter", "Choisissez une action puis Enter", "↑↓ choisir   Enter ouvrir   q quitter"],
        "es": ["Configurar workers / proveedores", "Ver estado", "Actualizar MAC", "Comprobar sistema", "Salir", "Seleccione una acción y pulse Enter", "↑↓ elegir   Enter abrir   q salir"],
        "de": ["Worker / Provider einrichten", "Status anzeigen", "MAC aktualisieren", "System prüfen", "Beenden", "Aktion wählen und Enter drücken", "↑↓ wählen   Enter öffnen   q beenden"],
    }
    config_path = Path(project) / ".agent-control-plane" / "config.json"
    try: profile = json.loads(config_path.read_text()) if config_path.exists() else {}
    except (OSError, json.JSONDecodeError): profile = {}
    labels = menu_text.get(profile.get("language", "en"), menu_text["en"])
    theme = profile.get("theme", "dark")
    guide_names = {"en":"Install CLI / Master Help","vi":"Cài CLI / Hướng dẫn Master","zh":"安装 CLI / Master 帮助","ja":"CLI インストール / ヘルプ","ko":"CLI 설치 / Master 도움말","fr":"Installer un CLI / Aide","es":"Instalar CLI / Ayuda","de":"CLI installieren / Master-Hilfe"}
    guide_label = guide_names.get(profile.get("language", "en"), guide_names["en"])
    uninstall_names = {"en": "Uninstall MAC", "vi": "Gỡ cài đặt MAC", "zh": "卸载 MAC", "ja": "MAC をアンインストール", "ko": "MAC 제거", "fr": "Désinstaller MAC", "es": "Desinstalar MAC", "de": "MAC deinstallieren"}
    uninstall_label = uninstall_names.get(profile.get("language", "en"), uninstall_names["en"])
    items = [(labels[0], "setup"), (labels[1], "status"), (guide_label, "guide"), (labels[2], "update"), (labels[3], "doctor"), (uninstall_label, "uninstall"), (labels[4], "quit")]
    def apply_theme(win):
        if curses.has_colors():
            curses.start_color(); curses.use_default_colors()
            fg, bg = (curses.COLOR_WHITE, curses.COLOR_BLACK) if theme == "dark" else (curses.COLOR_BLACK, curses.COLOR_WHITE)
            curses.init_pair(1, fg, bg); curses.init_pair(2, bg, fg)
            win.bkgd(" ", curses.color_pair(1)); return curses.color_pair(2) | curses.A_BOLD
        return curses.A_REVERSE | curses.A_BOLD
    def screen(win):
        curses.curs_set(0); win.keypad(True); selected = 0; selected_attr = apply_theme(win)
        while True:
            win.erase(); h, w = win.getmaxyx(); win.addstr(2, 4, "MAC · MULTI-AGENT CONTROL", curses.A_BOLD); win.addstr(4, 4, labels[5], curses.A_DIM)
            for i, (label, _) in enumerate(items): win.addstr(7+i, 7, ("› " if i == selected else "  ") + label, selected_attr if i == selected else 0)
            win.addstr(h-2, 4, labels[6], curses.A_DIM); win.refresh(); key = keypress(win)
            if key in (ord("q"), 27): return "quit"
            if key in (curses.KEY_UP, ord("k")): selected = (selected - 1) % len(items)
            elif key in (curses.KEY_DOWN, ord("j")): selected = (selected + 1) % len(items)
            elif key in (10, 13): return items[selected][1]
    def panel(title, lines):
        def view(win): show_panel(win, title, lines)
        curses.wrapper(view)
    def show_panel(win, title, lines):
        """Render a panel without starting a nested curses session."""
        apply_theme(win); curses.curs_set(0); offset = 0
        while True:
            win.erase(); h, w = win.getmaxyx()
            win.addstr(2, 4, "MAC · " + title, curses.A_BOLD)
            win.addstr(4, 4, "─" * max(1, min(w-8, 72)))
            visible = max(1, h - 9)
            width = max(12, w - 12)
            wrapped = []
            for line in lines:
                value = str(line)
                # Keep blank lines and wrap long prompts/commands instead of
                # truncating them. Continuation lines remain copyable in full.
                chunks = textwrap.wrap(value, width=width, replace_whitespace=False,
                                       drop_whitespace=False) or [""]
                wrapped.extend(chunks)
            for i, line in enumerate(wrapped[offset:offset + visible]):
                win.addnstr(6+i, 6, line, width)
            footer = "↑↓ cuộn   Esc / q / Enter: quay lại" if profile.get("language", "en") == "vi" else "↑↓ scroll   Esc / q / Enter: back"
            win.addstr(h-2, 4, footer, curses.A_DIM)
            win.refresh(); key = keypress(win)
            if key in (27, ord("q"), 10, 13): return
            if key in (curses.KEY_UP, ord("k")): offset = max(0, offset - 1)
            elif key in (curses.KEY_DOWN, ord("j")): offset = min(max(0, len(wrapped)-visible), offset + 1)
    def guide():
        lang = profile.get("language", "en")
        root = Path(project).resolve(); python = _venv_python(root); state = root / ".agent-control-plane/state.sqlite3"
        commands = {
            "codex": f"codex mcp add mac --env PYTHONPATH={root / 'src'} -- {python} -m agent_control_plane mcp --state {state}",
            "claude": f"claude mcp add --scope user mac --env PYTHONPATH={root / 'src'} -- {python} -m agent_control_plane mcp --state {state}",
        }
        if lang == "vi":
            topics = [("Gọi MAC", ["Terminal chỉ dùng để cài MAC. Trong khung chat, nếu MCP đã được nạp, dùng:", "Kết nối MAC MCP và thực hiện yêu cầu sau: <yêu cầu>", "Nếu AI chưa biết MAC, dùng: Đọc README tại https://github.com/Horaus/mac và kết nối tới MAC MCP đã cài trên máy.", "MAC tự nạp quy tắc vận hành và hỏi thêm thông tin khi cần."]),
            ("Codex", ["Cách 1 — Codex CLI: cài Node.js trước, sau đó chạy:", "npm install --global @openai/codex", "codex login", "Đăng nhập bằng ChatGPT trên trang web khi Codex yêu cầu.", "Thêm MAC bằng lệnh sau:", commands["codex"], "Khởi động lại Codex. Dán prompt ở mục Gọi MAC.", "Cách 2 — Codex app/IDE: tải ứng dụng Codex chính thức, đăng nhập ChatGPT, mở Settings → MCP servers → Add server; nhập cùng command, args và PYTHONPATH như trên."]),
            ("Claude", ["Claude Code: cài Node.js rồi chạy:", "npm install --global @anthropic-ai/claude-code", "claude", "Đăng nhập theo hướng dẫn hiện trên màn hình.", "Kết nối MAC bằng lệnh:", commands["claude"], "Kiểm tra bằng: claude mcp get mac hoặc /mcp. Sau đó dán prompt gọi MAC.", "Claude Desktop hoặc host khác: mở phần MCP/local server và nhập command Python, args và PYTHONPATH tương ứng."]),
            ("Gemini / Antigravity", ["Gemini CLI: cài Node.js rồi chạy:", "npm install --global @google/gemini-cli", "gemini", "Đăng nhập tài khoản Google theo hướng dẫn của Gemini.", "Gemini CLI dùng ~/.gemini/settings.json; Antigravity dùng ~/.gemini/config/mcp_config.json (không dùng settings.json). Thêm server mac với command Python tuyệt đối, args -m agent_control_plane mcp --state <state>, env.PYTHONPATH trỏ tới src.", "Khởi động lại Gemini hoặc Antigravity, kiểm tra MCP rồi dán prompt gọi MAC."]),
            ("Chế độ điều phối", ["Lock: tổng worker là cố định. Nếu hết worker, yêu cầu mới trả về PENDING và không được tự cấp thêm. Lịch sử được lưu theo master_id + conversation_id.", "Flexible: số worker có thể thay đổi tức thời qua MCP. MAC không lưu hoặc nạp lịch sử chat dùng chung để tránh lẫn dữ liệu.", "Không đổi master_id giữa chừng nếu muốn tiếp tục cùng một luồng công việc."]),
            ("Câu hỏi thường gặp", ["Không thấy tool? Kiểm tra đường dẫn tuyệt đối, PYTHONPATH, quyền MCP và khởi động lại ứng dụng AI.", "Đổi master_id hoặc conversation_id sẽ tạo ngữ cảnh mới.", "Worker chỉ tạo kết quả để duyệt; Master là bên kiểm tra và quyết định tích hợp.", "MAC không tự đoán worker phù hợp: hãy yêu cầu Master đăng ký và request đúng số lượng."])]
        else:
            topics = [("Call MAC", ["The terminal only installs MAC. If MCP is loaded, use this in chat:", "Connect to MAC MCP and complete this request: <request>", "If the AI does not know MAC, use: Read the README at https://github.com/Horaus/mac and connect to the MAC MCP server installed on this computer.", "MAC loads its operating rules and asks for missing details when needed."]),
            ("Codex", ["Codex CLI: install Node.js, then run:", "npm install --global @openai/codex", "codex login", "Complete ChatGPT sign-in when Codex opens the login flow.", "Add MAC with:", commands["codex"], "Restart Codex, then paste the MAC prompt and ask it to call control_status.", "Codex app/IDE: install the official app or extension, sign in with ChatGPT, open Settings → MCP servers → Add server, and enter the same command, args and PYTHONPATH."]),
            ("Claude", ["Claude Code: install Node.js, then run:", "npm install --global @anthropic-ai/claude-code", "claude", "Complete the sign-in flow shown on screen.", "Connect MAC with:", commands["claude"], "Verify with claude mcp get mac or /mcp, then paste the MAC prompt.", "For Claude Desktop or another host, use its MCP/local-server settings with the same Python command, args and PYTHONPATH."]),
            ("Gemini / Antigravity", ["Gemini CLI: install Node.js, then run:", "npm install --global @google/gemini-cli", "gemini", "Complete Google sign-in when prompted.", "Gemini CLI uses ~/.gemini/settings.json; Antigravity uses ~/.gemini/config/mcp_config.json (not settings.json). Add server mac with an absolute Python command, args -m agent_control_plane mcp --state <state>, and env.PYTHONPATH pointing to src.", "Restart Gemini or Antigravity, check MCP, then paste the MAC prompt."]),
            ("Control modes", ["Lock: worker capacity is fixed. When capacity is full, MAC returns PENDING and refuses extra workers. History is stored by master_id + conversation_id.", "Flexible: worker counts can change at runtime. Shared chat history is not stored or injected.", "Do not change master_id mid-thread if you need to continue the same work."]),
            ("FAQ", ["No tool visible? Check absolute paths, PYTHONPATH, MCP permissions and restart the host.", "Changing master_id or conversation_id starts a new context.", "Workers produce reviewable results; Master validates and decides what is integrated.", "MAC does not guess worker assignments: ask Master to register and request the required count."])]
        guide_title = "MAC · HƯỚNG DẪN MASTER" if lang == "vi" else "MAC · MASTER HELP"
        choose = "Chọn chủ đề" if lang == "vi" else "Choose a topic"
        footer = "↑↓ chọn   Enter mở   q quay lại" if lang == "vi" else "↑↓ select   Enter open   q back"
        def topic_list(win):
            win.keypad(True); curses.curs_set(0); selected = 0
            while True:
                win.erase(); h, w = win.getmaxyx(); win.addstr(2, 4, guide_title, curses.A_BOLD); win.addstr(4, 4, choose, curses.A_DIM)
                for i, (name, _) in enumerate(topics): win.addstr(7+i, 7, ("› " if i == selected else "  ") + name, apply_theme(win) if i == selected else 0)
                win.addstr(h-2, 4, footer, curses.A_DIM); win.refresh(); key = keypress(win)
                if key in (ord('q'), 27): return
                if key in (curses.KEY_UP, ord('k')): selected = (selected - 1) % len(topics)
                elif key in (curses.KEY_DOWN, ord('j')): selected = (selected + 1) % len(topics)
                elif key in (10, 13):
                    title, lines = topics[selected]
                    show_panel(win, title, lines)
        curses.wrapper(topic_list)
    try: choice = curses.wrapper(screen)
    except (curses.error, OSError): print("MAC cần một Terminal tương tác."); return 1
    if choice == "quit": return 0
    if choice == "setup": run_setup(project)
    elif choice == "status":
        store = Store(state_path(project)); snapshot = store.snapshot(); store.close()
        lines = ["Chưa có hoạt động nào. MAC đã sẵn sàng nhận task." if not snapshot else f"{key}: {value}" for key, value in snapshot.items()]
        if not lines: lines = ["Chưa có hoạt động nào. MAC đã sẵn sàng nhận task."]
        panel("TRẠNG THÁI", lines or ["Chưa có dữ liệu task."])
    elif choice == "guide":
        root = Path(project).resolve(); python = _venv_python(root); state = root / ".agent-control-plane/state.sqlite3"
        guide()
    elif choice == "doctor":
        output = io.StringIO()
        with contextlib.redirect_stdout(output): doctor(project)
        panel("KIỂM TRA HỆ THỐNG", output.getvalue().splitlines())
    elif choice == "update":
        root = Path(project).resolve()
        try:
            panel("CẬP NHẬT MAC", _update_project(root))
            if os.name != "nt":
                os.execve(str(root / "mac"), [str(root / "mac")], os.environ.copy())
            # Windows cannot exec the POSIX `mac` shell wrapper. Continue in
            # the current interpreter after reinstalling the package.
            return _menu(project)
        except subprocess.CalledProcessError as error:
            panel("CẬP NHẬT MAC", ["! Cập nhật chưa hoàn tất.", error.stderr or str(error)])
    elif choice == "uninstall":
        root = Path(project).resolve()
        script = root / "scripts" / ("uninstall.ps1" if os.name == "nt" else "uninstall.sh")
        try:
            command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Yes"] if os.name == "nt" else ["bash", str(script), "--yes"]
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=True)
            panel("GỠ CÀI ĐẶT MAC", result.stdout.splitlines() or ["MAC đã được gỡ cài đặt."])
        except subprocess.CalledProcessError as error:
            panel("GỠ CÀI ĐẶT MAC", ["! Gỡ cài đặt chưa hoàn tất.", error.stderr or str(error)])
    return _menu(project)


def state_path(project: str) -> Path:
    return Path(project) / ".agent-control-plane" / "state.sqlite3"


def main(argv=None, _daemon_owner=False) -> int:
    if not (sys.argv[1:] if argv is None else argv): return _menu(os.environ.get("MAC_PROJECT_ROOT", "."))
    parser = argparse.ArgumentParser(prog="acp")
    parser.add_argument("--project", default=os.environ.get("MAC_PROJECT_ROOT", "."))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("setup", help="interactive first-run terminal setup")
    doctor_parser = sub.add_parser("doctor", help="check installation and provider readiness"); doctor_parser.add_argument("--port", type=int, default=8765)
    config = sub.add_parser("config").add_subparsers(dest="config_command", required=True)
    config.add_parser("show")
    config_mcp = config.add_parser("mcp"); config_mcp.add_argument("--format", choices=["codex", "json"], default="codex")
    connect = sub.add_parser("connect").add_subparsers(dest="connect_command", required=True)
    connect_codex_parser = connect.add_parser("codex"); connect_codex_parser.add_argument("--force", action="store_true")
    status = sub.add_parser("status")
    sub.add_parser("update", help="update MAC and show version/time")
    tasks = sub.add_parser("task").add_subparsers(dest="task_command", required=True)
    create = tasks.add_parser("create")
    create.add_argument("--id", required=True); create.add_argument("--title", required=True)
    create.add_argument("--model", default=None); create.add_argument("--reasoning-effort", choices=["low", "medium", "high"], default=None)
    create.add_argument("--provider", default=None); create.add_argument("--depends-on", action="append", default=[])
    retry = tasks.add_parser("retry")
    retry.add_argument("--id", required=True); retry.add_argument("--worker-id", default=None)
    cancel = tasks.add_parser("cancel")
    cancel.add_argument("--id", required=True); cancel.add_argument("--reason", default="supervisor cancellation")
    resource = sub.add_parser("resource").add_subparsers(dest="resource_command", required=True)
    add_resource = resource.add_parser("add")
    add_resource.add_argument("name"); add_resource.add_argument("kind"); add_resource.add_argument("--path", action="append", default=[])
    renew_resource = resource.add_parser("renew")
    renew_resource.add_argument("name"); renew_resource.add_argument("--task-id", required=True)
    renew_resource.add_argument("--worker-id", required=True); renew_resource.add_argument("--ttl", type=int, default=300)
    message = sub.add_parser("message")
    message.add_argument("type", choices=["INFORMATION","PROPOSAL","BLOCKER","RESOURCE_REQUEST","CONTRACT_CHANGE","CONFLICT_REPORT","REVIEW_FINDING","TASK_COMPLETE"])
    message.add_argument("--task-id"); message.add_argument("--worker-id"); message.add_argument("--payload", required=True)
    accept = sub.add_parser("accept")
    accept.add_argument("task_id")
    worker = sub.add_parser("worker").add_subparsers(dest="worker_command", required=True)
    run = worker.add_parser("run")
    run.add_argument("--task-id", required=True); run.add_argument("--worker-id", required=True)
    run.add_argument("--cwd", default=None); run.add_argument("--prompt", required=True)
    run.add_argument("--provider", choices=["codex", "gemini"], default=None); run.add_argument("--conversation-id", default=None)
    run.add_argument("--resume-session-id", default=None); run.add_argument("--context-checkpoint", default=None)
    run.add_argument("--model", default=None); run.add_argument("--reasoning-effort", choices=["low", "medium", "high"], default=None)
    run.add_argument("--mode", choices=[m.value for m in ExecutionMode], default=ExecutionMode.ISOLATED_SANDBOX.value)
    run.add_argument("--capability", action="append", default=[])
    check = sub.add_parser("validate")
    check.add_argument("--task-id", required=True); check.add_argument("--cwd", default=None); check.add_argument("--timeout", type=float, default=300); check.add_argument("--capability", action="append", default=[]); check.add_argument("validation_command", nargs=argparse.REMAINDER)
    inbox = sub.add_parser("inbox")
    history = sub.add_parser("history").add_subparsers(dest="history_command", required=True)
    history_add = history.add_parser("add")
    history_add.add_argument("conversation_id"); history_add.add_argument("role"); history_add.add_argument("actor"); history_add.add_argument("content")
    history_list = history.add_parser("list"); history_list.add_argument("conversation_id"); history_list.add_argument("--limit", type=int, default=50)
    knowledge = sub.add_parser("knowledge").add_subparsers(dest="knowledge_command", required=True)
    knowledge_load = knowledge.add_parser("load"); knowledge_load.add_argument("path")
    knowledge_ack = knowledge.add_parser("ack"); knowledge_ack.add_argument("actor", choices=["boss", "worker"]); knowledge_ack.add_argument("--worker-id")
    sub.add_parser("reconcile")
    activity = sub.add_parser("activity").add_subparsers(dest="activity_command", required=True)
    activity_show = activity.add_parser("show"); activity_show.add_argument("run_id")
    activity_heartbeat = activity.add_parser("heartbeat"); activity_heartbeat.add_argument("run_id"); activity_heartbeat.add_argument("--action")
    activity_steer = activity.add_parser("queue-steering"); activity_steer.add_argument("run_id"); activity_steer.add_argument("instruction"); activity_steer.add_argument("--scope", required=True)
    review = sub.add_parser("review").add_subparsers(dest="review_command", required=True)
    review_evidence = review.add_parser("evidence")
    review_evidence.add_argument("task_id")
    runtime = sub.add_parser("runtime").add_subparsers(dest="runtime_command", required=True)
    runtime_auth = runtime.add_parser("authorize")
    runtime_auth.add_argument("--capability", required=True); runtime_auth.add_argument("--report", required=True)
    runtime_auth.add_argument("--mode", choices=[m.value for m in ExecutionMode], default=ExecutionMode.OPEN_OPERATOR.value)
    runtime_auth.add_argument("--resource"); runtime_auth.add_argument("--task-id"); runtime_auth.add_argument("--worker-id")
    runtime_auth.add_argument("--active-job", action="store_true"); runtime_auth.add_argument("--target-id"); runtime_auth.add_argument("--project-id")
    runtime_auth.add_argument("--operation", choices=["observe", "navigate", "reload_tab", "reload_extension", "restart"], required=True)
    approval = sub.add_parser("approval").add_subparsers(dest="approval_command", required=True)
    create_approval = approval.add_parser("create")
    for name in ("token-id", "capability", "project-id", "provider-id", "job-id", "run-id", "task-id", "idempotency-key", "payload"):
        create_approval.add_argument("--" + name, required=True)
    create_approval.add_argument("--max-attempts", type=int, default=1); create_approval.add_argument("--expires-at", type=float, default=0)
    consume_approval = approval.add_parser("consume")
    for name in ("token-id", "capability", "project-id", "provider-id", "job-id", "run-id", "task-id", "idempotency-key", "payload"):
        consume_approval.add_argument("--" + name, required=True)
    organization = sub.add_parser("organization").add_subparsers(dest="organization_command", required=True)
    goal_create = organization.add_parser("goal-create"); goal_create.add_argument("--id", required=True); goal_create.add_argument("--title", required=True); goal_create.add_argument("--owner", default="master"); goal_create.add_argument("--worker-class", choices=("basic", "specialist"), default="basic"); goal_create.add_argument("--specialist-id"); goal_create.add_argument("--inspection-mode", default="result_only")
    goal_summary = organization.add_parser("goal-summary"); goal_summary.add_argument("goal_id"); goal_summary.add_argument("--cursor", type=int, default=0); goal_summary.add_argument("--limit", type=int, default=20)
    goal_inspection = organization.add_parser("inspection"); goal_inspection.add_argument("goal_id"); goal_inspection.add_argument("mode"); goal_inspection.add_argument("--actor", default="master")
    goal_follow = organization.add_parser("follow-up"); goal_follow.add_argument("goal_id"); goal_follow.add_argument("conversation_id"); goal_follow.add_argument("message"); goal_follow.add_argument("--actor", default="master")
    goal_feedback = organization.add_parser("feedback"); goal_feedback.add_argument("goal_id"); goal_feedback.add_argument("kind", choices=("constraint", "question", "lesson", "redirect")); goal_feedback.add_argument("content"); goal_feedback.add_argument("--actor", default="master")
    routing = organization.add_parser("free-routing"); routing.add_argument("providers", nargs="+"); routing.add_argument("--disable", action="store_true"); routing.add_argument("--actor", default="master")
    eligibility = organization.add_parser("provider-eligibility"); eligibility.add_argument("provider"); eligibility.add_argument("model"); eligibility.add_argument("status", choices=("eligible", "ineligible", "unknown")); eligibility.add_argument("--source", default="recorded"); eligibility.add_argument("--actor", default="master")
    worker_recommend = organization.add_parser("recommend-worker"); worker_recommend.add_argument("--goal", required=True); worker_recommend.add_argument("--risk", choices=("low", "standard", "high", "critical"), default="standard"); worker_recommend.add_argument("--constraints", default="{}")
    telemetry = organization.add_parser("budget-telemetry"); telemetry.add_argument("--metrics", required=True); telemetry.add_argument("--source", required=True); telemetry.add_argument("--run-id"); telemetry.add_argument("--task-id")
    payload_gc = organization.add_parser("gc-payloads"); payload_gc.add_argument("--grace-days", type=int, default=7)
    goal_resume = organization.add_parser("resume"); goal_resume.add_argument("goal_id"); goal_resume.add_argument("--actor", default="master")
    goal_cancel = organization.add_parser("cancel"); goal_cancel.add_argument("goal_id"); goal_cancel.add_argument("reason"); goal_cancel.add_argument("--actor", default="master")
    goal_subscribe = organization.add_parser("subscribe"); goal_subscribe.add_argument("goal_id"); goal_subscribe.add_argument("subscriber_id"); goal_subscribe.add_argument("--event-types", nargs="*")
    goal_poll = organization.add_parser("poll"); goal_poll.add_argument("goal_id"); goal_poll.add_argument("subscriber_id"); goal_poll.add_argument("--limit", type=int, default=20); goal_poll.add_argument("--byte-limit", type=int, default=16384); goal_poll.add_argument("--reconnect", action="store_true")
    goal_ack = organization.add_parser("ack"); goal_ack.add_argument("goal_id"); goal_ack.add_argument("subscriber_id"); goal_ack.add_argument("sequence", type=int)
    evidence_get = organization.add_parser("evidence"); evidence_get.add_argument("evidence_id"); evidence_get.add_argument("goal_id"); evidence_get.add_argument("--requester", default="master"); evidence_get.add_argument("--deep", action="store_true"); evidence_get.add_argument("--fields", nargs="*"); evidence_get.add_argument("--byte-limit", type=int, default=16384)
    archetype_inspect = organization.add_parser("archetype-inspect"); archetype_inspect.add_argument("archetype_id"); archetype_inspect.add_argument("--version", type=int)
    archetype_enable = organization.add_parser("archetype-enable"); archetype_enable.add_argument("archetype_id"); archetype_enable.add_argument("version", type=int); archetype_enable.add_argument("--actor", default="master")
    archetype_disable = organization.add_parser("archetype-disable"); archetype_disable.add_argument("archetype_id"); archetype_disable.add_argument("version", type=int); archetype_disable.add_argument("--actor", default="master")
    archetype_assign = organization.add_parser("archetype-assign"); archetype_assign.add_argument("goal_id"); archetype_assign.add_argument("instance_id"); archetype_assign.add_argument("archetype_id"); archetype_assign.add_argument("version", type=int); archetype_assign.add_argument("--actor", default="master")
    daemon = organization.add_parser("daemon").add_subparsers(dest="daemon_command", required=True)
    for command in ("start", "tick", "status", "stop", "service-plan"):
        daemon.add_parser(command)
    credential_ref = organization.add_parser("credential-ref")
    credential_ref.add_argument("--provider", required=True); credential_ref.add_argument("--account-ref", required=True)
    credential_ref.add_argument("--secret-ref", required=True); credential_ref.add_argument("--fingerprint", required=True)
    credential_ref.add_argument("--source", default="environment")
    gc_conversation = organization.add_parser("gc-conversation")
    gc_conversation.add_argument("--inactivity-days", type=int, default=30)
    service_fixture = organization.add_parser("service-fixture")
    service_fixture.add_argument("action", choices=("install", "uninstall", "install-unit", "uninstall-unit")); service_fixture.add_argument("--manager", choices=("launchd", "systemd"), required=True)
    service_fixture.add_argument("--fixture-root", required=True); service_fixture.add_argument("--state-path"); service_fixture.add_argument("--command", dest="service_command", nargs="*", default=[])
    service = organization.add_parser("service")
    service.add_argument("action", choices=("install", "uninstall", "status")); service.add_argument("--manager", choices=("launchd", "systemd"), required=True); service.add_argument("--root", required=True); service.add_argument("--state-path"); service.add_argument("--command", dest="service_command", nargs="*", default=[])
    args = parser.parse_args(argv)
    path = state_path(args.project)
    if not _daemon_owner:
        marker = path.parent / "daemon.ipc.json"
        if marker.exists():
            from .daemon_ipc import DaemonIPCClient
            try:
                socket_path = json.loads(marker.read_text())["socket"]
                response = DaemonIPCClient(socket_path).call({"method": "cli", "params": {"argv": list(argv or sys.argv[1:])}})
                if not response.get("ok"):
                    raise RuntimeError(response.get("error", "daemon CLI request failed"))
                output = response["result"].get("stdout", "")
                if output: print(output, end="")
                return int(response["result"].get("exit_code", 0))
            except (OSError, ValueError, KeyError, ConnectionError):
                raise RuntimeError("daemon IPC owner is unavailable")
    store = Store(path)
    # Startup reconciliation is safe and idempotent; explicit `reconcile`
    # remains available for operators who want the decision list printed.
    if args.command != "init":
        store.reconcile(HOST_INSTANCE_ID)
    try:
        if args.command == "init":
            print(f"initialized {path}")
        elif args.command == "setup":
            run_setup(args.project)
        elif args.command == "doctor":
            return doctor(args.project, args.port)
        elif args.command == "config" and args.config_command == "show":
            show_config(args.project)
        elif args.command == "config" and args.config_command == "mcp":
            show_mcp_config(args.project, args.format)
        elif args.command == "connect" and args.connect_command == "codex":
            connect_codex(args.project, args.force)
        elif args.command == "status":
            print(json.dumps(store.snapshot(), indent=2, default=str))
        elif args.command == "update":
            try:
                print("\n".join(_update_project(Path(args.project).resolve())))
            except subprocess.CalledProcessError as error:
                print(f"Update failed: {error.stderr or error}", file=sys.stderr)
                return 1
        elif args.command == "activity":
            if args.activity_command == "show":
                try: print(json.dumps(store.activity_snapshot(args.run_id), default=str))
                except ValueError: print("unknown run", file=sys.stderr); return 1
            elif args.activity_command == "heartbeat":
                store.heartbeat(args.run_id, args.action); print("heartbeat recorded")
            else:
                store.queue_steering(args.run_id, args.instruction, args.scope); print("queued steering recorded")
        elif args.command == "task" and args.task_command == "create":
            execution = {k: v for k, v in {"model": args.model, "reasoning_effort": args.reasoning_effort}.items() if v is not None}
            store.add_task(args.id, args.title, args.provider, args.depends_on, execution); print(args.id)
        elif args.command == "task" and args.task_command == "retry":
            store.retry_task(args.id, args.worker_id); print(f"retrying {args.id}")
        elif args.command == "task" and args.task_command == "cancel":
            store.cancel_task(args.id, args.reason); print(f"cancelled {args.id}")
        elif args.command == "resource" and args.resource_command == "add":
            store.add_resource(args.name, args.kind, args.path); print(args.name)
        elif args.command == "resource" and args.resource_command == "renew":
            renewed = store.renew(args.name, args.task_id, args.worker_id, args.ttl)
            print(json.dumps({"renewed": renewed}))
            return 0 if renewed else 1
        elif args.command == "message":
            store.add_message(args.type, json.loads(args.payload), args.task_id, args.worker_id); print("queued")
        elif args.command == "accept":
            store.accept_task(args.task_id); print(f"accepted {args.task_id}")
        elif args.command == "worker" and args.worker_command == "run":
            cwd = Path(args.cwd or args.project)
            task_provider = args.provider or store.task(args.task_id)["provider"] or "codex"
            prompt = args.prompt
            config_path = Path(args.project) / ".agent-control-plane" / "config.json"
            config = json.loads(config_path.read_text()) if config_path.exists() else {}
            worker_config = next((w for w in config.get("workers", []) if w.get("id") == args.worker_id), {})
            if args.conversation_id:
                history_limit = int(worker_config.get("execution", {}).get("context", {}).get("history_limit", 6))
                context = list(reversed(store.history(args.conversation_id, history_limit)))
                prompt += "\n\nSHARED BOSS/WORKER HISTORY:\n" + "\n".join(f"[{row['role']}/{row['actor']}] {row['content']}" for row in context)
            override = {k: v for k, v in {"model": args.model, "reasoning_effort": args.reasoning_effort}.items() if v is not None}
            task = store.task(args.task_id)
            task_override = json.loads(task["execution_json"]) if task and task["execution_json"] else {}
            profile = resolve_profile(config.get("execution", {}), worker_config, task_override, override)
            authority = AuthorityPolicy(ExecutionMode(args.mode), frozenset(args.capability or ["filesystem.read", "provider.preflight"]))
            checkpoint = json.loads(args.context_checkpoint) if args.context_checkpoint else None
            result = run_worker(store, args.task_id, args.worker_id, provider(task_provider), prompt, cwd, profile=profile, authority=authority,
                                conversation_id=args.conversation_id, resume_session_id=args.resume_session_id,
                                context_checkpoint=checkpoint)
            print(json.dumps({"exit_code": result.exit_code, "output": result.output, "status": store.task(args.task_id)["status"]}))
            return result.exit_code
        elif args.command == "validate":
            cwd = Path(args.cwd or args.project)
            validation_authority = AuthorityPolicy(capabilities=frozenset(args.capability or ["filesystem.read", "provider.preflight"]))
            exit_code = validate(store, args.task_id, " ".join(args.validation_command), cwd, args.timeout, validation_authority)
            print(json.dumps({"exit_code": exit_code}))
            return exit_code
        elif args.command == "inbox":
            print(json.dumps([dict(row) for row in store.inbox()], indent=2))
        elif args.command == "history" and args.history_command == "add":
            store.record_history(args.conversation_id, args.role, args.actor, args.content); print("recorded")
        elif args.command == "history" and args.history_command == "list":
            print(json.dumps([dict(row) for row in store.history(args.conversation_id, args.limit)], ensure_ascii=False, indent=2))
        elif args.command == "knowledge" and args.knowledge_command == "load":
            print(json.dumps({"digest": store.load_knowledge(args.path)}))
        elif args.command == "knowledge" and args.knowledge_command == "ack":
            store.acknowledge_knowledge(args.actor, args.worker_id); print("acknowledged")
        elif args.command == "reconcile":
            print(json.dumps({"recovered_tasks": store.reconcile(HOST_INSTANCE_ID)}))
        elif args.command == "review" and args.review_command == "evidence":
            print(json.dumps(store.review_evidence(args.task_id)))
        elif args.command == "runtime" and args.runtime_command == "authorize":
            policy = AuthorityPolicy(ExecutionMode(args.mode), frozenset({args.capability}))
            authorize_runtime_action(store, policy, args.capability, RuntimeReport(**json.loads(args.report)), args.resource, args.task_id, args.worker_id, args.active_job, args.target_id, args.project_id, args.operation)
            print(json.dumps({"authorized": True}))
        elif args.command == "approval" and args.approval_command == "create":
            payload = json.loads(args.payload)
            token = ApprovalToken(args.token_id, args.capability, args.project_id, args.provider_id, args.job_id,
                                  digest(payload), args.idempotency_key, args.max_attempts, expires_at=args.expires_at,
                                  run_id=args.run_id, task_id=args.task_id)
            store.create_approval(token); print(args.token_id)
        elif args.command == "approval" and args.approval_command == "consume":
            payload = json.loads(args.payload)
            policy = AuthorityPolicy(capabilities=frozenset({args.capability}))
            store.consume_approval(args.token_id, policy, payload, args.project_id, args.provider_id, args.job_id,
                                   args.idempotency_key, args.run_id, args.task_id); print(json.dumps({"consumed": True}))
        elif args.command == "organization" and args.organization_command == "goal-create":
            print(json.dumps(org_create_goal(store, args.id, args.title, args.owner, args.specialist_id, inspection_mode=args.inspection_mode, worker_class=args.worker_class), default=str))
        elif args.command == "organization" and args.organization_command == "goal-summary":
            print(json.dumps(org_summary(store, args.goal_id, args.cursor, args.limit), default=str))
        elif args.command == "organization" and args.organization_command == "inspection":
            org_set_inspection(store, args.goal_id, args.mode, args.actor); print(json.dumps({"updated": True}))
        elif args.command == "organization" and args.organization_command == "follow-up":
            print(json.dumps(org_follow_up(store, args.goal_id, args.conversation_id, args.message, actor=args.actor), default=str))
        elif args.command == "organization" and args.organization_command == "feedback":
            print(json.dumps(org_feedback(store, args.goal_id, args.kind, args.content, args.actor), default=str))
        elif args.command == "organization" and args.organization_command == "free-routing":
            print(json.dumps(configure_free_routing(store, args.providers, args.actor, not args.disable), default=str))
        elif args.command == "organization" and args.organization_command == "provider-eligibility":
            print(json.dumps(configure_provider_eligibility(store, args.provider, args.model, args.status, args.actor, args.source), default=str))
        elif args.command == "organization" and args.organization_command == "recommend-worker":
            print(json.dumps(recommend_worker(store, args.goal, args.risk, json.loads(args.constraints)), default=str))
        elif args.command == "organization" and args.organization_command == "budget-telemetry":
            print(json.dumps(record_budget_telemetry(store, json.loads(args.metrics), args.source, args.run_id, args.task_id), default=str))
        elif args.command == "organization" and args.organization_command == "gc-payloads":
            print(json.dumps(physically_collect_payloads(store, grace_days=args.grace_days), default=str))
        elif args.command == "organization" and args.organization_command == "resume":
            print(json.dumps(org_resume_goal(store, args.goal_id, args.actor), default=str))
        elif args.command == "organization" and args.organization_command == "cancel":
            print(json.dumps(org_cancel_goal(store, args.goal_id, args.reason, args.actor), default=str))
        elif args.command == "organization" and args.organization_command == "subscribe":
            print(json.dumps(subscribe_goal(store, args.goal_id, args.subscriber_id, args.event_types), default=str))
        elif args.command == "organization" and args.organization_command == "poll":
            print(json.dumps(poll_goal(store, args.goal_id, args.subscriber_id, args.limit, args.byte_limit, args.reconnect), default=str))
        elif args.command == "organization" and args.organization_command == "ack":
            print(json.dumps(acknowledge_goal(store, args.goal_id, args.subscriber_id, args.sequence), default=str))
        elif args.command == "organization" and args.organization_command == "evidence":
            print(json.dumps(get_evidence(store, args.evidence_id, args.goal_id, args.requester, args.deep, args.fields, args.byte_limit), default=str))
        elif args.command == "organization" and args.organization_command == "archetype-inspect":
            print(json.dumps(inspect_archetype(store, args.archetype_id, args.version), default=str))
        elif args.command == "organization" and args.organization_command in ("archetype-enable", "archetype-disable"):
            enabled = args.organization_command == "archetype-enable"
            print(json.dumps(set_archetype_enabled(store, args.archetype_id, args.version, enabled, args.actor), default=str))
        elif args.command == "organization" and args.organization_command == "archetype-assign":
            print(json.dumps(assign_archetype(store, args.goal_id, args.instance_id, args.archetype_id, args.version, args.actor), default=str))
        elif args.command == "organization" and args.organization_command == "daemon":
            runtime_daemon = OrganizationDaemon(store, mode="foreground")
            result = runtime_daemon.start() if args.daemon_command == "start" else runtime_daemon.tick() if args.daemon_command == "tick" else runtime_daemon.stop() if args.daemon_command == "stop" else runtime_daemon.service_plan() if args.daemon_command == "service-plan" else runtime_daemon.health()
            print(json.dumps(result, default=str))
        elif args.command == "organization" and args.organization_command == "credential-ref":
            print(json.dumps(register_credential_ref(store, args.provider, args.account_ref, args.secret_ref, args.fingerprint, args.source), default=str))
        elif args.command == "organization" and args.organization_command == "gc-conversation":
            print(json.dumps({"expired": garbage_collect_conversation(store, inactivity_days=args.inactivity_days)}, default=str))
        elif args.command == "organization" and args.organization_command == "service-fixture":
            manager = SandboxedServiceManager(args.fixture_root, args.manager)
            result = manager.install(args.service_command, args.state_path or str(store.path)) if args.action == "install" else manager.install_unit(args.service_command, args.state_path or str(store.path)) if args.action == "install-unit" else {"removed": manager.uninstall() if args.action == "uninstall" else manager.uninstall_unit()}
            print(json.dumps(result, default=str))
        elif args.command == "organization" and args.organization_command == "service":
            manager = ProductionServiceManager(args.root, args.manager, subprocess_service_executor(args.manager))
            result = manager.install(args.service_command or ["acp", "daemon-ipc"], args.state_path or str(store.path)) if args.action == "install" else manager.uninstall() if args.action == "uninstall" else manager.status()
            print(json.dumps(result, default=str))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
