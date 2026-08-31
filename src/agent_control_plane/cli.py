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
from .service import run_worker, validate
from .store import Store
from .setup import connect_codex, doctor, run_setup, show_config, show_mcp_config

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
    guide_names = {"en":"Connect a Master / Help","vi":"Kết nối Master / Hướng dẫn","zh":"连接 Master / 帮助","ja":"Master 接続 / ヘルプ","ko":"Master 연결 / 도움말","fr":"Connecter un Master / Aide","es":"Conectar Master / Ayuda","de":"Master verbinden / Hilfe"}
    guide_label = guide_names.get(profile.get("language", "en"), guide_names["en"])
    items = [(labels[0], "setup"), (labels[1], "status"), (guide_label, "guide"), (labels[2], "update"), (labels[3], "doctor"), (labels[4], "quit")]
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
        root = Path(project).resolve(); python = root / ".venv/bin/python"; state = root / ".agent-control-plane/state.sqlite3"
        commands = {
            "codex": f"codex mcp add mac --env PYTHONPATH={root / 'src'} -- {python} -m agent_control_plane mcp --state {state}",
            "claude": f"claude mcp add --scope user mac --env PYTHONPATH={root / 'src'} -- {python} -m agent_control_plane mcp --state {state}",
        }
        if lang == "vi":
            topics = [("Gọi MAC", ["Terminal chỉ dùng để cài MAC. Trong khung chat, nếu MCP đã được nạp, dùng:", "Kết nối MAC MCP và thực hiện yêu cầu sau: <yêu cầu>", "Nếu AI chưa biết MAC, dùng: Đọc README tại https://github.com/Horaus/mac và kết nối tới MAC MCP đã cài trên máy.", "MAC tự nạp quy tắc vận hành và hỏi thêm thông tin khi cần."]),
            ("Codex", ["Cách 1 — Codex CLI: cài Node.js trước, sau đó chạy:", "npm install --global @openai/codex", "codex login", "Đăng nhập bằng ChatGPT trên trang web khi Codex yêu cầu.", "Thêm MAC bằng lệnh sau:", commands["codex"], "Khởi động lại Codex. Dán prompt ở mục Gọi MAC.", "Cách 2 — Codex app/IDE: tải ứng dụng Codex chính thức, đăng nhập ChatGPT, mở Settings → MCP servers → Add server; nhập cùng command, args và PYTHONPATH như trên."]),
            ("Claude", ["Claude Code: cài Node.js rồi chạy:", "npm install --global @anthropic-ai/claude-code", "claude", "Đăng nhập theo hướng dẫn hiện trên màn hình.", "Kết nối MAC bằng lệnh:", commands["claude"], "Kiểm tra bằng: claude mcp get mac hoặc /mcp. Sau đó dán prompt gọi MAC.", "Claude Desktop hoặc host khác: mở phần MCP/local server và nhập command Python, args và PYTHONPATH tương ứng."]),
            ("Gemini", ["Gemini CLI: cài Node.js rồi chạy:", "npm install --global @google/gemini-cli", "gemini", "Đăng nhập tài khoản Google theo hướng dẫn của Gemini.", "Mở ~/.gemini/settings.json và thêm server mac với command là đường dẫn .venv/bin/python, args là -m agent_control_plane mcp --state <state>, env.PYTHONPATH trỏ tới src.", "Khởi động lại Gemini, kiểm tra /mcp rồi dán prompt gọi MAC."]),
            ("Chế độ điều phối", ["Lock: tổng worker là cố định. Nếu hết worker, yêu cầu mới trả về PENDING và không được tự cấp thêm. Lịch sử được lưu theo master_id + conversation_id.", "Flexible: số worker có thể thay đổi tức thời qua MCP. MAC không lưu hoặc nạp lịch sử chat dùng chung để tránh lẫn dữ liệu.", "Không đổi master_id giữa chừng nếu muốn tiếp tục cùng một luồng công việc."]),
            ("Câu hỏi thường gặp", ["Không thấy tool? Kiểm tra đường dẫn tuyệt đối, PYTHONPATH, quyền MCP và khởi động lại ứng dụng AI.", "Đổi master_id hoặc conversation_id sẽ tạo ngữ cảnh mới.", "Worker chỉ tạo kết quả để duyệt; Master là bên kiểm tra và quyết định tích hợp.", "MAC không tự đoán worker phù hợp: hãy yêu cầu Master đăng ký và request đúng số lượng."])]
        else:
            topics = [("Call MAC", ["The terminal only installs MAC. If MCP is loaded, use this in chat:", "Connect to MAC MCP and complete this request: <request>", "If the AI does not know MAC, use: Read the README at https://github.com/Horaus/mac and connect to the MAC MCP server installed on this computer.", "MAC loads its operating rules and asks for missing details when needed."]),
            ("Codex", ["Codex CLI: install Node.js, then run:", "npm install --global @openai/codex", "codex login", "Complete ChatGPT sign-in when Codex opens the login flow.", "Add MAC with:", commands["codex"], "Restart Codex, then paste the MAC prompt and ask it to call control_status.", "Codex app/IDE: install the official app or extension, sign in with ChatGPT, open Settings → MCP servers → Add server, and enter the same command, args and PYTHONPATH."]),
            ("Claude", ["Claude Code: install Node.js, then run:", "npm install --global @anthropic-ai/claude-code", "claude", "Complete the sign-in flow shown on screen.", "Connect MAC with:", commands["claude"], "Verify with claude mcp get mac or /mcp, then paste the MAC prompt.", "For Claude Desktop or another host, use its MCP/local-server settings with the same Python command, args and PYTHONPATH."]),
            ("Gemini", ["Gemini CLI: install Node.js, then run:", "npm install --global @google/gemini-cli", "gemini", "Complete Google sign-in when prompted.", "Edit ~/.gemini/settings.json. Add server mac with command .venv/bin/python, args -m agent_control_plane mcp --state <state>, and env.PYTHONPATH pointing to src.", "Restart Gemini, check /mcp, then paste the MAC prompt."]),
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
        root = Path(project).resolve(); python = root / ".venv/bin/python"; state = root / ".agent-control-plane/state.sqlite3"
        guide()
    elif choice == "doctor":
        output = io.StringIO()
        with contextlib.redirect_stdout(output): doctor(project)
        panel("KIỂM TRA HỆ THỐNG", output.getvalue().splitlines())
    elif choice == "update":
        root = Path(project).resolve()
        try:
            pull = subprocess.run(["git", "fetch", "origin", "main"], cwd=root, capture_output=True, text=True, check=True)
            subprocess.run(["git", "reset", "--hard", "origin/main"], cwd=root, capture_output=True, text=True, check=True)
            pip = subprocess.run([str(root / ".venv/bin/python"), "-m", "pip", "install", "-e", "."], cwd=root, capture_output=True, text=True, check=True)
            panel("CẬP NHẬT MAC", ["✓ Đã tải phiên bản mới.", "✓ Đã cài lại package.", "✓ Cấu hình và dữ liệu được giữ nguyên.", "", pull.stdout.strip()[-500:]])
            os.execve(str(root / "mac"), [str(root / "mac")], os.environ.copy())
        except subprocess.CalledProcessError as error:
            panel("CẬP NHẬT MAC", ["! Cập nhật chưa hoàn tất.", error.stderr or str(error)])
    return _menu(project)


def state_path(project: str) -> Path:
    return Path(project) / ".agent-control-plane" / "state.sqlite3"


def main(argv=None) -> int:
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
    tasks = sub.add_parser("task").add_subparsers(dest="task_command", required=True)
    create = tasks.add_parser("create")
    create.add_argument("--id", required=True); create.add_argument("--title", required=True)
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
    check = sub.add_parser("validate")
    check.add_argument("--task-id", required=True); check.add_argument("--cwd", default=None); check.add_argument("--timeout", type=float, default=300); check.add_argument("command", nargs="+")
    inbox = sub.add_parser("inbox")
    history = sub.add_parser("history").add_subparsers(dest="history_command", required=True)
    history_add = history.add_parser("add")
    history_add.add_argument("conversation_id"); history_add.add_argument("role"); history_add.add_argument("actor"); history_add.add_argument("content")
    history_list = history.add_parser("list"); history_list.add_argument("conversation_id"); history_list.add_argument("--limit", type=int, default=50)
    knowledge = sub.add_parser("knowledge").add_subparsers(dest="knowledge_command", required=True)
    knowledge_load = knowledge.add_parser("load"); knowledge_load.add_argument("path")
    knowledge_ack = knowledge.add_parser("ack"); knowledge_ack.add_argument("actor", choices=["boss", "worker"]); knowledge_ack.add_argument("--worker-id")
    sub.add_parser("reconcile")
    args = parser.parse_args(argv)
    path = state_path(args.project)
    store = Store(path)
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
        elif args.command == "task" and args.task_command == "create":
            store.add_task(args.id, args.title, args.provider, args.depends_on); print(args.id)
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
            if args.conversation_id:
                context = list(reversed(store.history(args.conversation_id, 20)))
                prompt += "\n\nSHARED BOSS/WORKER HISTORY:\n" + "\n".join(f"[{row['role']}/{row['actor']}] {row['content']}" for row in context)
            result = run_worker(store, args.task_id, args.worker_id, provider(task_provider), prompt, cwd)
            print(json.dumps({"exit_code": result.exit_code, "output": result.output, "status": store.task(args.task_id)["status"]}))
            return result.exit_code
        elif args.command == "validate":
            cwd = Path(args.cwd or args.project)
            exit_code = validate(store, args.task_id, " ".join(args.command), cwd, args.timeout)
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
            print(json.dumps({"recovered_tasks": store.reconcile()}))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
