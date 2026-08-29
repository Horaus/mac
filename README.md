# Multi-Agent Control (MAC)

MAC is a local MCP control plane that lets an AI Master coordinate configurable
workers. It manages worker capacity, task state, validation and approval while
keeping the Master in control.

## English

### Install

```bash
git clone https://github.com/Horaus/mac.git
cd mac
./scripts/bootstrap.sh .
```

After installation, run `mac` from any terminal. The first run opens setup;
later runs use the same command and preserve configuration and runtime data.

### Connect an AI host

MAC is called through MCP. After connecting it to Codex, Claude, Gemini or
another MCP-compatible host, write:

```text
Call MAC (Multi-Agent Control) through MCP and follow all instructions provided by MAC.
Goal: <what you want to achieve>
Workers requested: <number>
Expected result: <what must be delivered>
```

The AI host must show the MAC tools before it can call MAC. A prompt alone
cannot connect a local process that has not been configured as an MCP server.

### Update

```bash
cd mac
git pull
./scripts/bootstrap.sh .
```

The setup and state directory is kept outside the checkout, so updates do not
overwrite the saved profile.

## Tiếng Việt

### Cài đặt

```bash
git clone https://github.com/Horaus/mac.git
cd mac
./scripts/bootstrap.sh .
```

Sau khi cài đặt, chạy `mac` từ bất kỳ Terminal nào. Lần đầu MAC mở màn hình
thiết lập. Những lần sau vẫn chỉ cần dùng lệnh `mac`; cấu hình và dữ liệu chạy
được giữ nguyên khi cập nhật.

### Kết nối ứng dụng AI

MAC được gọi thông qua MCP. Sau khi kết nối MAC với Codex, Claude, Gemini hoặc
host hỗ trợ MCP khác, nhập:

```text
Gọi MAC (Multi-Agent Control) qua MCP và làm việc theo toàn bộ hướng dẫn MAC cung cấp.
Mục tiêu: <mô tả việc cần làm>
Số worker cần dùng: <số lượng>
Kết quả cần đạt: <mô tả kết quả>
```

Ứng dụng AI phải hiển thị các MAC tool trước khi gọi được MAC. Chỉ nhập prompt
không thể tự kết nối một process cục bộ nếu MCP chưa được cấu hình.

### Cập nhật

```bash
cd mac
git pull
./scripts/bootstrap.sh .
```

Thư mục cấu hình và dữ liệu được lưu ngoài checkout, vì vậy cập nhật source
không ghi đè profile đã lưu.

## Documentation / Tài liệu

- `docs/usage/quickstart.md`: first setup / thiết lập lần đầu.
- `docs/integrations/master-chat.md`: MCP connection / kết nối MCP.
- `docs/usage/safety-and-permissions.md`: permissions / quyền truy cập.
