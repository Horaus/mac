# Kết nối Master chat với MAC

MAC là MCP server chạy cục bộ. Một AI chat chỉ điều phối được MAC khi host của
chat hỗ trợ MCP stdio, hoặc có bridge tương thích. Việc model mang tên Codex,
Claude hay Gemini không tự tạo quyền truy cập; chính ứng dụng host phải nạp MCP
server và cho phép gọi tool.

## Prompt để AI gọi MAC

Sau khi MCP đã được cài vào host, dán prompt sau vào khung chat:

```text
Gọi MAC (Multi-Agent Control) qua MCP và làm việc theo toàn bộ hướng dẫn MAC
cung cấp. Khi giao việc, nói rõ mục tiêu, số worker mong muốn và kết quả cần đạt.
```

## Luồng chuẩn sau khi đã kết nối

1. Gọi `control_register_master` với `master_id` ổn định.
2. Gọi `control_request_master_workers` trước khi tạo task.
3. Nếu nhận `PENDING`, báo người dùng pool đang bận và không tự tạo worker.
4. Nếu nhận `ALLOCATED`, tạo task, dispatch worker và giữ nguyên
   `conversation_id` trong chế độ Lock.
5. Validate kết quả; worker không tự merge.
6. Gọi `control_release_master` sau khi hoàn tất hoặc hủy công việc.

Prompt thử nhanh:

```text
Use the MAC MCP tools. Register master_id "app-main" with 3 workers, request
the group, report PENDING instead of creating extra workers, then show
control_status. Do not dispatch until the full group is allocated.
```

## Codex CLI, IDE extension và Codex app

### Codex CLI

Cài Node.js, rồi cài và đăng nhập Codex:

```bash
npm install --global @openai/codex
codex login
```

Hoàn tất đăng nhập ChatGPT theo cửa sổ trình duyệt. Sau đó thêm MAC:

Codex lưu MCP trong `~/.codex/config.toml` hoặc `.codex/config.toml` của
project. CLI, IDE extension và Codex app dùng chung cấu hình này. Từ thư mục
MAC, chạy:

```bash
codex mcp add mac --env PYTHONPATH="$PWD/src" -- "$PWD/.venv/bin/python" -m agent_control_plane mcp --state "$PWD/.agent-control-plane/state.sqlite3"
codex mcp get mac
```

Khởi động lại Codex sau khi thêm. Dán prompt ở mục trên và yêu cầu gọi
`control_status` để xác nhận kết nối.

### Codex app hoặc IDE extension

Cài ứng dụng/extension Codex chính thức, đăng nhập bằng ChatGPT, mở
**Settings → MCP servers → Add server**. Nhập cùng `command`, `args` và
`PYTHONPATH` như lệnh CLI ở trên. Sau khi lưu, mở cuộc trò chuyện mới và dán
prompt gọi MAC.

## Claude Code

Cài Node.js, cài Claude Code và đăng nhập theo hướng dẫn trên màn hình:

```bash
npm install --global @anthropic-ai/claude-code
claude
```

Sau đó thêm MAC:

```bash
claude mcp add --scope user mac --env PYTHONPATH="$PWD/src" -- "$PWD/.venv/bin/python" -m agent_control_plane mcp --state "$PWD/.agent-control-plane/state.sqlite3"
claude mcp get mac
```

Trong Claude Code, `/mcp` hiển thị và quản lý các MCP server. Dán prompt gọi
MAC sau khi server xuất hiện.

## Gemini CLI

Cài Node.js, cài Gemini CLI và đăng nhập Google khi được yêu cầu:

```bash
npm install --global @google/gemini-cli
gemini
```

Sau đó thêm MAC vào `~/.gemini/settings.json`:

Thêm vào `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "mac": {
      "command": "/ABSOLUTE/PATH/multi-agent-control/.venv/bin/python",
      "args": ["-m", "agent_control_plane", "mcp", "--state", "/ABSOLUTE/PATH/multi-agent-control/.agent-control-plane/state.sqlite3"],
      "env": {"PYTHONPATH": "/ABSOLUTE/PATH/multi-agent-control/src"}
    }
  }
}
```

Mở lại Gemini CLI, kiểm tra `/mcp` tùy phiên bản, rồi dán prompt gọi MAC.

## Claude Desktop và host desktop khác

Nếu host cho thêm local MCP server, dùng cùng ba giá trị:

- `command`: đường dẫn tuyệt đối đến `.venv/bin/python`
- `args`: `-m agent_control_plane mcp --state <absolute-state-path>`
- `env.PYTHONPATH`: đường dẫn tuyệt đối đến `src`

Tên file và giao diện cấu hình phụ thuộc host. Nếu ứng dụng không công bố MCP,
plugin API hoặc tool API thì MAC không thể tự chèn tool vào khung chat đó.

## Lock và Flexible

- `Lock`: pool cố định; hết worker trả `PENDING`; lưu history theo
  `master_id + conversation_id`.
- `Flexible`: cho đổi số lượng yêu cầu tức thời; không inject hoặc lưu shared
  chat history.

Đổi chat nhưng muốn tiếp tục công việc: dùng lại đúng `master_id` và
`conversation_id`. Nếu một trong hai khác, MAC coi là context mới để tránh nối
nhầm dữ liệu.

## FAQ

### Chat nói không thấy tool MAC

Kiểm tra MCP server có xuất hiện trong host, đường dẫn Python/state là tuyệt
đối, `PYTHONPATH` trỏ tới `src`, rồi khởi động lại host.

### Có thể chỉ gõ “gọi MAC” mà không cấu hình MCP không?

Không. Model không thể gọi một process cục bộ nếu host chưa cấp MCP/tool.

### Hai Master dùng cùng worker được không?

Lock từ chối khi pool hết. Flexible dùng pool động nhưng vẫn không cho hai
Master sở hữu cùng một worker tại cùng thời điểm.

### Worker có tự merge không?

Không. Master phải validate và chấp nhận integration.
