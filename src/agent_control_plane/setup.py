"""Dependency-free first-run terminal setup for the V1 control plane."""
from __future__ import annotations

import json
import shutil
import sys
import os
import urllib.request

_TTY = __import__("sys").stdout.isatty() and __import__("os").environ.get("ACP_NO_COLOR") != "1"
_CYAN = "\033[36m" if _TTY else ""; _GREEN = "\033[32m" if _TTY else ""; _YELLOW = "\033[33m" if _TTY else ""; _DIM = "\033[2m" if _TTY else ""; _RESET = "\033[0m" if _TTY else ""
from pathlib import Path
from .store import Store


def _ask(prompt: str, default: str = "") -> str:
    try:
        value = input(f"  {prompt}{' [' + default + ']' if default else ''}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print(f"  {prompt}: {default} (non-interactive default)")
        value = default
    return value or default


def _scan_providers():
    home = Path.home()
    markers = {
        "codex": (home / ".codex" / "auth.json",),
        # Personal Gemini OAuth is no longer a supported Gemini CLI tier;
        # only API-key or enterprise/project authentication is eligible.
        "gemini": (),
    }
    # An executable alone is not a usable provider: require a local login
    # marker (or an API key) before offering it for worker dispatch.
    ready = []
    for name in ("codex", "gemini"):
        path = shutil.which(name)
        enterprise = name == "gemini" and os.environ.get("GOOGLE_CLOUD_PROJECT")
        if path and (any(marker.is_file() for marker in markers[name]) or os.environ.get(f"{name.upper()}_API_KEY") or os.environ.get("GOOGLE_API_KEY") or enterprise):
            ready.append((name, path))
    return ready


def _fullscreen_setup(project: Path, found):
    """A small stdlib-only setup editor. It deliberately models workers as rows,
    rather than assigning one fixed role to each worker."""
    try:
        import curses
    except (ImportError, ModuleNotFoundError):
        # Windows Python does not ship the curses module; use the line-based
        # setup below instead of failing after the installer has created venv.
        return None

    def ask(stdscr, label, default=""):
        curses.echo()
        stdscr.addstr(curses.LINES - 2, 2, (label + (f" [{default}]" if default else ""))[:curses.COLS - 3])
        stdscr.clrtoeol(); stdscr.refresh()
        value = stdscr.getstr(curses.LINES - 1, 2, max(1, curses.COLS - 4)).decode(errors="replace").strip()
        curses.noecho()
        return value or default

    def editor(stdscr):
        stdscr.keypad(True)
        def read_key():
            key = stdscr.getch()
            if key != 27:
                return key
            stdscr.timeout(80)
            seq = []
            for _ in range(2):
                value = stdscr.getch()
                if value == -1:
                    break
                seq.append(value)
            stdscr.timeout(-1)
            return {"[A": curses.KEY_UP, "[B": curses.KEY_DOWN, "[C": curses.KEY_RIGHT, "[D": curses.KEY_LEFT}.get("".join(map(chr, seq)), 27)
        def put(y, x, value, attr=0):
            """Draw clipped text without letting a narrow terminal abort setup."""
            h, w = stdscr.getmaxyx()
            if y < 0 or y >= h or x >= w:
                return
            try:
                stdscr.addnstr(y, max(0, x), str(value), max(1, w - max(0, x) - 1), attr)
            except curses.error:
                pass
        if curses.has_colors():
            curses.start_color(); curses.use_default_colors()
        translations = {
            "en": {"title":" AGENT CONTROL PLANE / WORKSPACE SETUP ","tabs":["Providers","Workers","Language","Theme","Finish"],"provider":"Select one or more CLIs (Space toggles)","provider_note":"Codex and Gemini can run together; each worker has its own provider.","workers_title":"Workers · n count  a add  e edit  p provider  d delete","workers_note":"Master controls name, provider, role, and scope.","language":"Interface language","language_prompt":"Select the language used throughout MAC:","choose":"↑↓ move · Enter select","theme":"Color theme","theme_prompt":"Select the terminal appearance:","dark":"Dark","light":"Light","finish":"Review and save","providers":"Providers","workers":"Workers","footer":"↑↓ select   ←→/Tab change tab   q review & exit","exit":"Exit — save changes? [y] Save  [n] Don't save  [c] Cancel"},
            "vi": {"title":" AGENT CONTROL PLANE / THIẾT LẬP WORKSPACE ","tabs":["Provider","Worker","Ngôn ngữ","Giao diện","Hoàn tất"],"provider":"Chọn một hoặc nhiều CLI (Space bật/tắt)","provider_note":"Có thể dùng Codex và Gemini cùng lúc; mỗi worker chọn provider riêng.","workers_title":"Worker · n số lượng  a thêm  e sửa  p provider  d xóa","workers_note":"Master quản lý tên, provider, vai trò và phạm vi.","language":"Ngôn ngữ giao diện","language_prompt":"Chọn ngôn ngữ dùng trong toàn bộ MAC:","choose":"↑↓ di chuyển · Enter chọn","theme":"Màu giao diện","theme_prompt":"Chọn giao diện terminal:","dark":"Tối","light":"Sáng","finish":"Kiểm tra và lưu","providers":"Provider","workers":"Worker","footer":"↑↓ chọn   ←→/Tab đổi tab   q xem lại & thoát","exit":"Thoát, bạn có muốn lưu không? [y] Lưu  [n] Không lưu  [c] Hủy"},
            "zh": {"title":" AGENT CONTROL PLANE / 工作区设置 ","tabs":["提供商","工作者","语言","主题","完成"],"provider":"选择一个或多个 CLI（Space 切换）","provider_note":"Codex 与 Gemini 可同时使用；每个工作者可选择提供商。","workers_title":"工作者 · n 数量  a 添加  e 编辑  p 提供商  d 删除","workers_note":"Master 管理名称、提供商、角色和范围。","language":"界面语言","language_prompt":"选择整个 MAC 使用的语言：","choose":"↑↓ 移动 · Enter 选择","theme":"颜色主题","theme_prompt":"选择终端外观：","dark":"深色","light":"浅色","finish":"检查并保存","providers":"提供商","workers":"工作者","footer":"↑↓ 选择   ←→/Tab 切换   q 检查并退出","exit":"退出并保存更改？[y] 保存 [n] 不保存 [c] 取消"},
            "ja": {"title":" AGENT CONTROL PLANE / ワークスペース設定 ","tabs":["プロバイダー","ワーカー","言語","テーマ","完了"],"provider":"CLI を選択（Space で切替）","provider_note":"Codex と Gemini を同時に使用でき、ワーカーごとに選択できます。","workers_title":"ワーカー · n 数  a 追加  e 編集  p 提供元  d 削除","workers_note":"Master が名前、提供元、役割、範囲を管理します。","language":"表示言語","language_prompt":"MAC 全体で使用する言語を選択：","choose":"↑↓ 移動 · Enter 選択","theme":"カラーテーマ","theme_prompt":"ターミナルの外観を選択：","dark":"ダーク","light":"ライト","finish":"確認して保存","providers":"プロバイダー","workers":"ワーカー","footer":"↑↓ 選択   ←→/Tab タブ移動   q 確認して終了","exit":"終了して保存しますか？[y] 保存 [n] 保存しない [c] キャンセル"},
            "ko": {"title":" AGENT CONTROL PLANE / 작업 공간 설정 ","tabs":["제공자","워커","언어","테마","완료"],"provider":"CLI 선택 (Space 전환)","provider_note":"Codex와 Gemini를 함께 사용하고 워커별로 지정할 수 있습니다.","workers_title":"워커 · n 수  a 추가  e 편집  p 제공자  d 삭제","workers_note":"Master가 이름, 제공자, 역할, 범위를 관리합니다.","language":"인터페이스 언어","language_prompt":"MAC 전체에서 사용할 언어 선택:","choose":"↑↓ 이동 · Enter 선택","theme":"색상 테마","theme_prompt":"터미널 모양 선택:","dark":"다크","light":"라이트","finish":"검토 및 저장","providers":"제공자","workers":"워커","footer":"↑↓ 선택   ←→/Tab 탭 이동   q 검토 후 종료","exit":"종료하고 저장할까요? [y] 저장 [n] 저장 안 함 [c] 취소"},
            "fr": {"title":" AGENT CONTROL PLANE / CONFIGURATION ","tabs":["Fournisseurs","Workers","Langue","Thème","Terminer"],"provider":"Sélectionnez les CLI (Space active/désactive)","provider_note":"Codex et Gemini peuvent fonctionner ensemble, par worker.","workers_title":"Workers · n nombre  a ajouter  e modifier  p fournisseur  d supprimer","workers_note":"Le Master gère le nom, le fournisseur, le rôle et la portée.","language":"Langue de l’interface","language_prompt":"Choisissez la langue utilisée dans MAC :","choose":"↑↓ déplacer · Enter sélectionner","theme":"Thème de couleur","theme_prompt":"Choisissez l’apparence du terminal :","dark":"Sombre","light":"Clair","finish":"Vérifier et enregistrer","providers":"Fournisseurs","workers":"Workers","footer":"↑↓ choisir   ←→/Tab changer   q vérifier et quitter","exit":"Quitter et enregistrer ? [y] Oui [n] Non [c] Annuler"},
            "es": {"title":" AGENT CONTROL PLANE / CONFIGURACIÓN ","tabs":["Proveedores","Workers","Idioma","Tema","Finalizar"],"provider":"Seleccione uno o más CLI (Space alterna)","provider_note":"Codex y Gemini pueden usarse juntos, uno por worker.","workers_title":"Workers · n cantidad  a añadir  e editar  p proveedor  d borrar","workers_note":"El Master gestiona nombre, proveedor, rol y alcance.","language":"Idioma de la interfaz","language_prompt":"Seleccione el idioma utilizado en MAC:","choose":"↑↓ mover · Enter seleccionar","theme":"Tema de color","theme_prompt":"Seleccione la apariencia del terminal:","dark":"Oscuro","light":"Claro","finish":"Revisar y guardar","providers":"Proveedores","workers":"Workers","footer":"↑↓ elegir   ←→/Tab cambiar   q revisar y salir","exit":"¿Salir y guardar? [y] Sí [n] No [c] Cancelar"},
            "de": {"title":" AGENT CONTROL PLANE / EINRICHTUNG ","tabs":["Provider","Worker","Sprache","Design","Fertig"],"provider":"CLI auswählen (Space umschalten)","provider_note":"Codex und Gemini können gemeinsam pro Worker verwendet werden.","workers_title":"Worker · n Anzahl  a hinzufügen  e ändern  p Provider  d löschen","workers_note":"Der Master verwaltet Name, Provider, Rolle und Bereich.","language":"Oberflächensprache","language_prompt":"Sprache für MAC auswählen:","choose":"↑↓ bewegen · Enter wählen","theme":"Farbschema","theme_prompt":"Terminal-Darstellung auswählen:","dark":"Dunkel","light":"Hell","finish":"Prüfen und speichern","providers":"Provider","workers":"Worker","footer":"↑↓ wählen   ←→/Tab wechseln   q prüfen und beenden","exit":"Beenden und speichern? [y] Ja [n] Nein [c] Abbrechen"},
        }
        control_text = {
            "en": {"control":"Control","control_prompt":"Choose how workers are distributed among Masters:","lock":"Lock — fixed worker count; shortages remain PENDING and no extra workers are allocated","flexible":"Flexible — Masters adjust worker counts dynamically through MCP","control_note":"Flexible does not save chat history; Lock saves it by master_id + conversation_id.","policy":"Policy","role_label":"role","scope_label":"scope"},
            "vi": {"control":"Điều phối","control_prompt":"Chọn cách phân phối worker giữa các Master:","lock":"Lock — số worker cố định; thiếu thì báo PENDING và từ chối cấp thêm","flexible":"Flexible — Master điều chỉnh số lượng worker tức thời qua MCP","control_note":"Flexible không lưu lịch sử chat; Lock lưu theo master_id + conversation_id.","policy":"Chính sách","role_label":"vai trò","scope_label":"phạm vi"},
            "zh": {"control":"控制","control_prompt":"选择在各 Master 之间分配工作者的方式：","lock":"Lock — 工作者数量固定；不足时保持 PENDING，不再额外分配","flexible":"Flexible — Master 可通过 MCP 动态调整工作者数量","control_note":"Flexible 不保存聊天历史；Lock 按 master_id + conversation_id 保存。","policy":"策略","role_label":"角色","scope_label":"范围"},
            "ja": {"control":"制御","control_prompt":"Master 間でのワーカー配分方法を選択：","lock":"Lock — ワーカー数を固定し、不足時は PENDING のまま追加配分しない","flexible":"Flexible — Master が MCP 経由でワーカー数を動的に調整","control_note":"Flexible はチャット履歴を保存しません。Lock は master_id + conversation_id 単位で保存します。","policy":"ポリシー","role_label":"役割","scope_label":"範囲"},
            "ko": {"control":"제어","control_prompt":"Master 간 워커 분배 방식을 선택하세요:","lock":"Lock — 워커 수 고정; 부족하면 PENDING으로 두고 추가 할당하지 않음","flexible":"Flexible — Master가 MCP를 통해 워커 수를 동적으로 조정","control_note":"Flexible은 채팅 기록을 저장하지 않으며, Lock은 master_id + conversation_id별로 저장합니다.","policy":"정책","role_label":"역할","scope_label":"범위"},
            "fr": {"control":"Contrôle","control_prompt":"Choisissez la répartition des workers entre les Masters :","lock":"Lock — nombre de workers fixe ; en cas de manque, état PENDING sans allocation supplémentaire","flexible":"Flexible — les Masters ajustent dynamiquement le nombre de workers via MCP","control_note":"Flexible n’enregistre pas l’historique du chat ; Lock l’enregistre par master_id + conversation_id.","policy":"Politique","role_label":"rôle","scope_label":"portée"},
            "es": {"control":"Control","control_prompt":"Elija cómo distribuir los workers entre los Masters:","lock":"Lock — cantidad fija de workers; si faltan, queda PENDING y no asigna más","flexible":"Flexible — los Masters ajustan dinámicamente la cantidad de workers mediante MCP","control_note":"Flexible no guarda el historial del chat; Lock lo guarda por master_id + conversation_id.","policy":"Política","role_label":"rol","scope_label":"alcance"},
            "de": {"control":"Steuerung","control_prompt":"Verteilung der Worker auf die Master auswählen:","lock":"Lock — feste Worker-Anzahl; bei Mangel PENDING und keine zusätzliche Zuteilung","flexible":"Flexible — Master passen die Worker-Anzahl dynamisch über MCP an","control_note":"Flexible speichert keinen Chatverlauf; Lock speichert nach master_id + conversation_id.","policy":"Richtlinie","role_label":"Rolle","scope_label":"Bereich"},
        }
        for code, values in control_text.items():
            translations[code].update(values)
        fields = {
            "en": ("Worker name", "CLI provider", "Role", "Scope / location", "Worker count"),
            "vi": ("Tên worker", "CLI provider", "Vai trò", "Phạm vi / vị trí", "Số lượng worker"),
            "zh": ("工作者名称", "CLI 提供商", "角色", "范围 / 位置", "工作者数量"),
            "ja": ("ワーカー名", "CLI プロバイダー", "役割", "範囲 / 場所", "ワーカー数"),
            "ko": ("워커 이름", "CLI 제공자", "역할", "범위 / 위치", "워커 수"),
            "fr": ("Nom du worker", "Fournisseur CLI", "Rôle", "Portée / emplacement", "Nombre de workers"),
            "es": ("Nombre del worker", "Proveedor CLI", "Rol", "Alcance / ubicación", "Cantidad de workers"),
            "de": ("Worker-Name", "CLI-Provider", "Rolle", "Bereich / Ort", "Worker-Anzahl"),
        }
        appearance_names = {"en":"Appearance","vi":"Hiển thị","zh":"外观","ja":"外観","ko":"화면","fr":"Apparence","es":"Apariencia","de":"Anzeige"}
        providers = [name for name, _ in found]
        default_provider = providers[0] if providers else ""
        old_config = project / ".agent-control-plane" / "config.json"
        try: saved = json.loads(old_config.read_text()) if old_config.exists() else {}
        except (OSError, json.JSONDecodeError): saved = {}
        enabled = set(saved.get("providers", providers[:1])) & set(providers)
        if not enabled and providers:
            enabled = {providers[0]}
        workers = saved.get("workers") or [{"id": "worker-1", "provider": default_provider, "role": "general", "scope": "project"}]
        # Configurations created by older releases only stored id/provider.
        # Normalize them before rendering so changing tabs cannot crash TUI.
        normalized = []
        for index, worker in enumerate(workers, 1):
            item = dict(worker) if isinstance(worker, dict) else {}
            item.setdefault("id", f"worker-{index}")
            if item.get("provider") not in providers:
                item["provider"] = default_provider
            item.setdefault("role", "general")
            item.setdefault("scope", "project")
            normalized.append(item)
        workers = normalized
        languages = [("vi", "Tiếng Việt"), ("en", "English"), ("zh", "中文"), ("ja", "日本語"), ("ko", "한국어"), ("fr", "Français"), ("es", "Español"), ("de", "Deutsch")]
        language = saved.get("language", "en")
        theme = saved.get("theme", "dark")
        control = saved.get("control", {"policy": "lock"})
        if not isinstance(control, dict):
            control = {"policy": "lock"}
        if control.get("policy") not in {"lock", "flexible"}: control["policy"] = "lock"
        confirm = False
        page, cursor, message = 0, 0, ""
        curses.curs_set(0)
        while True:
            # Merge with English so partially translated/legacy profiles can
            # never crash when a newly added label is rendered.
            text = {**translations["en"], **translations.get(language, {})}
            if curses.has_colors():
                base_fg, base_bg = (curses.COLOR_WHITE, curses.COLOR_BLACK) if theme == "dark" else (curses.COLOR_BLACK, curses.COLOR_WHITE)
                curses.init_pair(1, base_fg, base_bg); curses.init_pair(2, base_bg, base_fg)
                base_attr, active_attr = curses.color_pair(1), curses.color_pair(2) | curses.A_BOLD
                stdscr.bkgd(" ", base_attr)
            else:
                base_attr, active_attr = 0, curses.A_REVERSE | curses.A_BOLD
            stdscr.erase(); h, w = stdscr.getmaxyx()
            title = text["title"]
            put(1, 2, title, curses.A_BOLD)
            tabs = [text["tabs"][0], text["tabs"][1], appearance_names.get(language, "Appearance"), text["control"], text["tabs"][-1]]
            x = 2
            for i, tab in enumerate(tabs):
                attr = active_attr if i == page else curses.A_BOLD
                put(2, x, f" {tab} ", attr); x += len(tab) + 4
            put(3, 2, "─" * max(1, w - 4))
            if page == 0:
                put(5, 4, text["provider"], curses.A_BOLD)
                for i, name in enumerate(providers):
                    mark = "●" if name in enabled else "○"
                    put(7+i, 6, f"{mark}  {name:<10} {dict(found).get(name, 'not found')}", active_attr if i == cursor else 0)
                if not providers:
                    put(7, 6, "No Codex or Gemini CLI is installed. Open Master Help to install one.", curses.A_DIM)
                put(11+len(providers), 4, text["provider_note"], curses.A_DIM)
            elif page == 1:
                put(5, 4, text["workers_title"], curses.A_BOLD)
                for i, worker in enumerate(workers):
                    line = f"{worker['id']:<18} {worker['provider']:<8} {text['role_label']}={worker['role']:<18} {text['scope_label']}={worker['scope']}"
                    put(7+i, 4, line, active_attr if i == cursor else 0)
                put(9+len(workers), 4, text["workers_note"], curses.A_DIM)
                if message:
                    put(11+len(workers), 4, message, curses.A_BOLD)
            elif page == 2:
                put(5, 4, text["language"] + " / " + text["theme"], curses.A_BOLD)
                put(7, 6, text["language_prompt"])
                for i, (code, name) in enumerate(languages):
                    attr = active_attr if i == cursor else 0
                    put(9+i, 8, ("●" if language == code else "○") + "  " + name, attr)
                put(18, 6, text["theme_prompt"])
                for i, (code, label) in enumerate((("dark", text["dark"]), ("light", text["light"]))):
                    put(19+i, 8, ("●" if theme == code else "○") + "  " + label, active_attr if cursor == len(languages)+i else 0)
                put(21, 6, text["choose"], curses.A_DIM)
            elif page == 3:
                put(5, 4, text["control"], curses.A_BOLD)
                put(7, 6, text["control_prompt"])
                options = [("lock", text["lock"]), ("flexible", text["flexible"])]
                for i, (code, label) in enumerate(options):
                    value = ("●" if control.get("policy") == code else "○") + "  " + label
                    put(9+i, 8, value, active_attr if i == cursor else 0)
                put(13, 6, text["control_note"], curses.A_DIM)
            else:
                put(5, 4, text["finish"], curses.A_BOLD)
                for row, value in enumerate((
                    text["providers"] + ": " + ", ".join(sorted(enabled)),
                    text["workers"] + ":   " + str(len(workers)),
                    text["language"] + ": " + dict(languages)[language],
                    text["theme"] + ": " + text[theme],
                    text["policy"] + ": " + control.get("policy", "lock"),
                ), 7):
                    put(row, 6, value)
                for i, x in enumerate(workers): put(13+i, 6, f"{x['id']} → {x['provider']} / {x['role']} / {x['scope']}")
                if confirm: put(h-4, 2, text["exit"], curses.A_BOLD | curses.A_STANDOUT)
                put(h-3, 2, message, curses.A_DIM)
            put(h-1, 2, text["footer"], curses.A_DIM)
            key = read_key()
            if key in (ord('q'), 27) and not confirm:
                page = 4; confirm = True; continue
            if confirm:
                if key in (ord('y'), ord('Y')): return enabled, workers, language, theme, control
                if key in (ord('n'), ord('N')): return False
                if key in (ord('c'), ord('C')): confirm = False; continue
                continue
            if key in (ord('q'), 27):
                confirm = True
            if key in (9, curses.KEY_RIGHT):
                page = (page + 1) % 5
                cursor = [code for code, _ in languages].index(language) if page == 2 else (0 if control.get("policy") == "lock" else 1) if page == 3 else 0
            elif key == curses.KEY_LEFT:
                page = (page - 1) % 5
                cursor = [code for code, _ in languages].index(language) if page == 2 else (0 if control.get("policy") == "lock" else 1) if page == 3 else 0
            elif key in (curses.KEY_DOWN, ord('j')): cursor += 1; cursor %= max(1, len(providers) if page == 0 else len(languages)+2 if page == 2 else 2 if page == 3 else len(workers))
            elif key in (curses.KEY_UP, ord('k')): cursor -= 1; cursor %= max(1, len(providers) if page == 0 else len(languages)+2 if page == 2 else 2 if page == 3 else len(workers))
            elif page == 0 and key == ord(' '): enabled.symmetric_difference_update({providers[cursor]})
            elif page == 1 and key == ord('a'):
                if not providers or not enabled:
                    message = "Cài và bật ít nhất một CLI trước khi thêm worker." if language == "vi" else "Install and enable at least one CLI before adding workers."
                    continue
                labels = fields.get(language, fields["en"])
                name = ask(stdscr, labels[0], f"worker-{len(workers)+1}")
                provider = ask(stdscr, labels[1], next(iter(enabled), default_provider))
                role = ask(stdscr, labels[2], "general"); scope = ask(stdscr, labels[3], "project")
                if any(worker.get("id") == name for worker in workers):
                    message = "Tên worker đã tồn tại; hãy chọn tên khác." if language == "vi" else "Worker name already exists; choose another name."
                    continue
                workers.append({"id": name, "provider": provider, "role": role, "scope": scope}); cursor = len(workers)-1
            elif page == 1 and key == ord('n'):
                if not providers or not enabled:
                    message = "Cài và bật ít nhất một CLI trước khi đổi số worker." if language == "vi" else "Install and enable at least one CLI before changing worker count."
                    continue
                count = ask(stdscr, fields.get(language, fields["en"])[4], str(len(workers)))
                try: wanted = max(1, min(24, int(count)))
                except ValueError: wanted = len(workers)
                while len(workers) < wanted:
                    i = len(workers) + 1; workers.append({"id": f"worker-{i}", "provider": next(iter(enabled), default_provider), "role": "general", "scope": "project"})
                while len(workers) > wanted and len(workers) > 1: workers.pop()
                cursor = min(cursor, len(workers)-1)
            elif page == 1 and workers and key == ord('e'):
                if not providers or not enabled:
                    message = "Bật ít nhất một CLI trước khi sửa worker." if language == "vi" else "Enable at least one CLI before editing workers."
                    continue
                labels = fields.get(language, fields["en"]); x = workers[cursor]
                old_id = x["id"]
                new_id = ask(stdscr, labels[0], old_id)
                if new_id != old_id and any(worker.get("id") == new_id for worker in workers):
                    message = "Tên worker đã tồn tại; thay đổi đã hủy." if language == "vi" else "Worker name already exists; edit canceled."
                    continue
                x["id"] = new_id; x["provider"] = ask(stdscr, labels[1], x["provider"]); x["role"] = ask(stdscr, labels[2], x["role"]); x["scope"] = ask(stdscr, labels[3], x["scope"])
            elif page == 1 and workers and key == ord('p'):
                if not providers or not enabled:
                    message = "Bật ít nhất một CLI trước khi đổi provider." if language == "vi" else "Enable at least one CLI before changing a provider."
                    continue
                choices = sorted(enabled) or providers; x = workers[cursor]; x["provider"] = choices[(choices.index(x["provider"]) + 1) % len(choices)] if x["provider"] in choices else choices[0]
            elif page == 1 and len(workers) > 1 and key == ord('d'): workers.pop(cursor); cursor %= len(workers)
            elif page == 2 and key in (curses.KEY_ENTER, 10, 13):
                if cursor < len(languages): language = languages[cursor][0]; message = translations[language]["choose"]
                else: theme = ("dark", "light")[cursor-len(languages)]
            elif page == 3 and key in (curses.KEY_ENTER, 10, 13): control["policy"] = ("lock", "flexible")[cursor]
            elif page == 4 and key in (10, 13):
                stdscr.addnstr(h-4, 2, text["exit"], max(1, w-4)); stdscr.refresh()
                answer = stdscr.getch()
                if answer in (ord('y'), ord('Y')): return enabled, workers, language, theme, control
                if answer in (ord('n'), ord('N')): return False
    try:
        return curses.wrapper(editor)
    except Exception:
        # Never strand the user outside setup because of an old or malformed
        # profile; run_setup will continue with the line-based editor.
        return None


def run_setup(project: str | Path) -> Path:
    project = Path(project).resolve()
    state_dir = project / ".agent-control-plane"
    state_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n{_CYAN}╭─ Agent Control Plane · thiết lập lần đầu ─╮{_RESET}")
    print(f"{_CYAN}│{_RESET} Master giữ quyền kiểm duyệt · worker dùng worktree riêng { _CYAN}│{_RESET}")
    print(f"{_CYAN}╰────────────────────────────────────────────╯{_RESET}\n")
    found = _scan_providers()
    if sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("ACP_NO_TUI") != "1":
        result = _fullscreen_setup(project, found)
        if result is False:
            print("\nĐã hủy setup; không thay đổi cấu hình.")
            return state_dir / "config.json"
        if result:
            enabled, workers, language, theme, control = result
            config = {"version": 1, "project": str(project), "providers": sorted(enabled), "language": language, "theme": theme, "control": control, "boss": {"knowledge_required": True, "history_linked": True}, "workers": workers}
            target = state_dir / "config.json"; target.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
            print(f"\n{_GREEN}✓ Đã lưu{_RESET} {target}")
            return target
    print(f"{_DIM}① Quét provider CLI{_RESET}")
    if not found:
        print("  ! Chưa thấy codex hoặc gemini trong PATH.")
        print("    Cài một CLI rồi chạy lại `acp setup`.")
    else:
        for i, (name, path) in enumerate(found, 1): print(f"  {_GREEN}{i}.{_RESET} {name:<8} {_DIM}{path}{_RESET}")
    provider = found[0][0] if found else ""
    if found:
        choice = _ask(f"Chọn CLI [1-{len(found)}]", "1")
        try: provider = found[max(0, min(len(found)-1, int(choice)-1))][0]
        except ValueError: provider = found[0][0]
    count = _ask("Số worker", "3")
    try: count = max(1, min(12, int(count)))
    except ValueError: count = 3
    workers = []
    for i in range(count):
        default = f"worker-{i+1}"
        workers.append({"id": _ask(f"Tên worker {i+1}", default), "provider": provider})
    config = {"version": 1, "project": str(project), "providers": [provider], "language": "en", "theme": "dark", "control": {"policy": "lock"}, "boss": {"knowledge_required": True, "history_linked": True}, "workers": workers}
    target = state_dir / "config.json"
    target.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    print(f"\n{_GREEN}✓ Đã lưu{_RESET} {target}")
    knowledge_files = [project / name for name in ("AGENTS.md", "CONTRIBUTING.md", "docs/worker-knowledge.md") if (project / name).is_file()]
    if knowledge_files and (state_dir / "state.sqlite3").exists():
        store = Store(state_dir / "state.sqlite3")
        digest = store.load_knowledge(knowledge_files[0]); store.acknowledge_knowledge("boss"); store.close()
        print(f"✓ Master đã nạp knowledge: {knowledge_files[0].relative_to(project)} ({digest[:12]}…)")
        print("  Worker acknowledgement còn lại:")
        for worker in workers: print(f"    acp knowledge ack worker --worker-id {worker['id']}")
    elif not knowledge_files:
        print("! Chưa có AGENTS.md/CONTRIBUTING.md/worker-knowledge.md; tạo knowledge trước khi dispatch.")
    print("✓ Bước tiếp theo: tạo task rồi Master sẽ dispatch theo dependency.")
    print("  Xem cấu hình: acp --project . config show")
    print("  Theo dõi:      acp --project . status")
    return target


def show_config(project: str | Path) -> None:
    path = Path(project) / ".agent-control-plane" / "config.json"
    if not path.exists(): print("Chưa setup. Chạy: acp setup"); return
    print(path.read_text())

def show_mcp_config(project: str | Path, output: str = "codex") -> None:
    root = Path(project).resolve()
    venv = root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
    python = str(venv if venv.exists() else sys.executable)
    state = str(root / ".agent-control-plane/state.sqlite3")
    if output == "json":
        print(json.dumps({"mcpServers": {"agent-control-plane": {"command": python, "args": ["-m", "agent_control_plane", "mcp", "--state", state]}}}, indent=2))
    else:
        print("[mcp_servers.agent_control_plane]")
        print(f'command = "{python}"')
        print(f'cwd = "{root}"')
        print(f'env = {{ PYTHONPATH = "{root / "src"}" }}')
        print(f'args = ["-m", "agent_control_plane", "mcp", "--state", "{state}"]')

def connect_codex(project: str | Path, force: bool = False) -> Path:
    root = Path(project).resolve(); target = root / ".codex" / "config.toml"
    if target.exists() and not force: raise FileExistsError(f"{target} already exists; use --force after reviewing it")
    target.parent.mkdir(parents=True, exist_ok=True)
    candidate = root / ".venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
    python = candidate if candidate.exists() else Path(sys.executable)
    target.write_text(f'''# Generated by acp connect codex; review before committing.\n[mcp_servers.agent_control_plane]\ncommand = "{python}"\ncwd = "{root}"\nenv = {{ PYTHONPATH = "{root / 'src'}" }}\nargs = ["-m", "agent_control_plane", "mcp", "--state", "{root / '.agent-control-plane/state.sqlite3'}"]\n''')
    print(f"✓ Codex project config written: {target}\n  Restart Codex in this project to discover ACP tools.")
    return target

def doctor(project: str | Path, port: int = 8765) -> int:
    root = Path(project).resolve(); state = root / ".agent-control-plane"
    ready_providers = {name for name, _ in _scan_providers()}
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.4) as response:
            background = response.status == 200
    except Exception:
        background = False
    checks = [("project", root.exists()), ("state", (state / "state.sqlite3").exists()),
              ("config", (state / "config.json").exists()), ("codex", "codex" in ready_providers),
              ("gemini", "gemini" in ready_providers), ("background-http", background)]
    print("\nAgent Control Plane · doctor\n")
    for name, ok in checks: print(f"  [{'✓' if ok else '!'}] {name}")
    if not checks[1][1]: print("\n  Next: acp --project . init")
    if not checks[2][1]: print("  Next: acp --project . setup")
    if not checks[3][1] and not checks[4][1]: print("  Install Codex or Gemini, then run acp doctor again")
    if background: print("  Background service is alive; terminal may close")
    else: print("  Background service is not detected; stdio host must remain open")
    return 0 if all(ok for _, ok in checks[:3]) and any(ok for _, ok in checks[3:]) else 1
