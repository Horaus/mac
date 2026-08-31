# Multi-Agent Control (MAC)

MAC connects an AI Master to local workers through MCP.

- [Tiếng Việt](#tiếng-việt)
- [English](#english)

## Tiếng Việt

### Dành cho người dùng

#### 1. Cài đặt lần đầu

Chạy lệnh sau trên **Terminal**

    git clone https://github.com/Horaus/mac.git
    cd mac
    ./scripts/bootstrap.sh .

Sau khi cài đặt, mở MAC để thiết lập, sử dụng lệnh:

    mac

##### Hệ điều hành được hỗ trợ

- **Windows 10 và Windows 11:** dùng PowerShell và <code>bootstrap.ps1</code>.
- **macOS và Linux:** dùng Terminal và <code>bootstrap.sh</code>.
- **Windows 7:** chưa được hỗ trợ chính thức. MAC yêu cầu Python 3.11+,
  trong khi Windows 7 không chạy được phiên bản Python này. Hãy nâng cấp
  Windows hoặc cài MAC trên máy Linux/macOS.

##### Windows 10 và Windows 11 (PowerShell)

Windows không chạy trực tiếp file Bash. Mở PowerShell tại thư mục bất kỳ;
không cần đứng sẵn trong thư mục <code>mac</code>.

**Cài lần đầu — chạy đủ 4 lệnh theo thứ tự:**

    Set-Location $HOME
    git clone https://github.com/Horaus/mac.git
    Set-Location .\mac
    powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1

**Nếu thư mục <code>mac</code> đã tồn tại — chạy đủ 3 lệnh theo thứ tự:**

    Set-Location $HOME
    Set-Location .\mac
    git pull

Sau đó chạy installer (cả cài mới và cập nhật):

    powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1

Khi installer báo hoàn tất, bắt buộc đóng PowerShell hiện tại, mở một
PowerShell mới, rồi chạy:

    mac

##### macOS hoặc Linux (Terminal)

    git clone https://github.com/Horaus/mac.git
    cd mac
    ./scripts/bootstrap.sh .

Sau khi script hoàn tất, mở Terminal mới (nếu cần) rồi chạy:

    mac


#### 2. Kết nối với ứng dụng AI

Terminal dùng để cài MAC; sử dụng khung chat để yêu cầu AI kết nối tới MAC qua MCP và thực hiện các tuyyf chọn cần thiết. Bạn có thể chat trong bất kỳ project nào, không cần mở thư mục
<code>mac</code>.

Nếu AI chưa biết MAC, gửi câu này:

    Đọc README tại https://github.com/Horaus/mac và kết nối tới MAC MCP đã cài trên máy.

Nếu ứng dụng AI đã nạp MAC MCP, chỉ cần gửi:

    Kết nối MAC MCP và thực hiện yêu cầu sau: <yêu cầu>

AI sẽ nạp quy tắc vận hành do MAC cung cấp và hỏi thêm thông tin khi cần. Quá ttrinhf ày diễn ra tự động
Phần cấu hình bên dưới dành cho AI agent đọc; người dùng không cần chép các lệnh MCP vào khung chat.

#### 3. Cập nhật

    cd mac
    git pull
    ./scripts/bootstrap.sh .

Hoặc cập nhập trực tiếp trong menu MAC khi gọi lệnh "mac" trên Terminal
### Dành cho AI agent

Đây là hướng dẫn để AI agent tự thiết lập kết nối, không phải các bước người
dùng phải nhập thủ công.

1. Xác định thư mục gốc MAC đang mở và dùng đường dẫn tuyệt đối.
2. Chọn cấu hình tương ứng với host hiện tại.
3. Khởi động lại host nếu cấu hình MCP yêu cầu.
4. Xác nhận MAC tools đã xuất hiện và gọi <code>control_status</code>.
5. Nạp rule/knowledge MAC cung cấp, rồi hỏi người dùng mục tiêu, số worker và
   kết quả cần đạt.

#### Codex CLI, Codex app hoặc extension

Từ thư mục gốc MAC:

    codex mcp add mac --env PYTHONPATH="$PWD/src" -- "$PWD/.venv/bin/python" -m agent_control_plane mcp --state "$PWD/.agent-control-plane/state.sqlite3"
    codex mcp get mac

Codex app và extension dùng cùng cấu hình MCP. Nếu tool chưa xuất hiện, khởi
động lại Codex rồi gọi <code>control_status</code>.

#### Claude Code

    claude mcp add --scope user mac --env PYTHONPATH="$PWD/src" -- "$PWD/.venv/bin/python" -m agent_control_plane mcp --state "$PWD/.agent-control-plane/state.sqlite3"
    claude mcp get mac

Khởi động lại Claude Code, kiểm tra bằng <code>/mcp</code>, rồi gọi
<code>control_status</code>.

#### Gemini CLI

Thêm server <code>mac</code> vào <code>~/.gemini/settings.json</code>, thay
<code>/ABSOLUTE/PATH/mac</code> bằng đường dẫn tuyệt đối của project:

    {
      "mcpServers": {
        "mac": {
          "command": "/ABSOLUTE/PATH/mac/.venv/bin/python",
          "args": ["-m", "agent_control_plane", "mcp", "--state", "/ABSOLUTE/PATH/mac/.agent-control-plane/state.sqlite3"],
          "env": {"PYTHONPATH": "/ABSOLUTE/PATH/mac/src"}
        }
      }
    }

Khởi động lại Gemini, xác nhận MAC tools xuất hiện, rồi gọi
<code>control_status</code>.

## English

### For users

#### 1. First installation

Run the following commands in **Terminal**:

    git clone https://github.com/Horaus/mac.git
    cd mac
    ./scripts/bootstrap.sh .

After installation, open MAC to configure it with:

    mac

##### Supported operating systems

- **Windows 10 and Windows 11:** use PowerShell and <code>bootstrap.ps1</code>.
- **macOS and Linux:** use Terminal and <code>bootstrap.sh</code>.
- **Windows 7:** not officially supported. MAC requires Python 3.11+, which
  cannot run on Windows 7. Upgrade Windows or run MAC on Linux/macOS.

##### Windows 10 and Windows 11 (PowerShell)

Windows does not run Bash files directly. Open PowerShell in any directory;
you do not need to start inside the <code>mac</code> folder.

**First installation — run all 4 commands in order:**

    Set-Location $HOME
    git clone https://github.com/Horaus/mac.git
    Set-Location .\mac
    powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1

**If the <code>mac</code> folder already exists — run all 3 commands in order:**

    Set-Location $HOME
    Set-Location .\mac
    git pull

Then run the installer (for both first installation and updates):

    powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1

When the installer reports completion, you must close the current PowerShell
window, open a new PowerShell window, and then run:

    mac

##### macOS or Linux (Terminal)

    git clone https://github.com/Horaus/mac.git
    cd mac
    ./scripts/bootstrap.sh .

When the script finishes, open a new Terminal window if needed, then run:

    mac

#### 2. Connect an AI application

The terminal is used to install MAC. In the chat, ask the AI to connect to MAC
through MCP and carry out the necessary options. You can chat from any project;
you do not need to open the <code>mac</code> folder.

If the AI does not know MAC yet, send:

    Read the README at https://github.com/Horaus/mac and connect to the MAC MCP server installed on this computer.

If the AI host has already loaded MAC MCP, use only:

    Connect to MAC MCP and complete this request: <request>

The AI loads MAC's operating rules and asks for missing details when needed.
This process happens automatically. The configuration below is for the AI
agent to read; users do not paste those MCP commands into chat.

#### 3. Update

    cd mac
    git pull
    ./scripts/bootstrap.sh .

You can also update directly from the MAC menu after running <code>mac</code>
in Terminal.

### For AI agents

1. Resolve the absolute MAC project path.
2. Configure the current host using the matching command above.
3. Restart the host when required.
4. Verify that MAC tools are visible and call <code>control_status</code>.
5. Load the rules and knowledge supplied by MAC, then ask the user for the
   goal, worker count, and expected result.

## Interface preview

<p align="center">
  <img src="assets/main-menu.png" alt="MAC main menu" width="30%">
  <img src="assets/providers.png" alt="MAC provider selection" width="30%">
  <img src="assets/appearance.png" alt="MAC appearance settings" width="30%">
</p>

The screenshots are references only; language, colors, providers, and workers
depend on the saved profile.

## More documentation

- <code>docs/usage/quickstart.md</code>
- <code>docs/integrations/master-chat.md</code>
- <code>docs/usage/safety-and-permissions.md</code>
