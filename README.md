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

Trong tab **Worker**, chọn từng worker và nhấn `x` để đặt model, Codex
profile, reasoning effort và sandbox riêng. Để trống một mục nếu muốn kế thừa
cấu hình mặc định của provider. Model chỉ chạy được khi tài khoản/provider hiện
tại có quyền sử dụng model đó.

##### Có gì mới trong phiên bản 0.5.5

MAC giờ phân biệt worker cơ bản và worker chuyên gia. Worker có trí nhớ làm việc
để tiếp tục một goal, còn Master quyết định nội dung nào đáng tin cậy để đưa vào
trí nhớ lâu dài. Goal, checkpoint, worker pack và các hành động đang dở được lưu
bền vững để MAC có thể phục hồi trung thực sau khi hết quota, mất kết nối hoặc
khởi động lại. Nếu phiên model cũ không nối lại được, MAC sẽ nói rõ thay vì giả
vờ worker vẫn nhớ.

Phiên bản này cũng thêm đường API miễn phí cho Google Gemini và Cloudflare
Workers AI. Master chọn provider và chính sách fallback; MAC theo dõi quota,
request ID và token usage. Secret chỉ được đọc qua tham chiếu tới biến môi
trường, không được nhận trực tiếp trong task hay lưu vào evidence.

Hotfix 0.5.5 sửa kết quả `result_only` để chỉ trả task được hỏi, giữ checkpoint
`WAITING_DECISION` hợp lệ và báo lỗi worker-pack theo schema thay vì lỗi Python.
Màn trạng thái không còn tải toàn bộ output lịch sử. MCP reconnect cũng không
còn đánh dấu `ORPHANED` giả khi process worker cũ vẫn sống và đúng identity.
Tạo lại cùng goal với metadata không đổi giờ trả xác nhận `already_exists` thay
vì exception; dùng lại ID với metadata khác vẫn bị từ chối.
Cấu hình goal đã lưu vẫn được giữ nguyên nếu client kết nối lại bỏ qua hoặc dùng
giá trị mặc định cho các trường tạo goal tùy chọn.

##### Thử API Google và Cloudflare

Cách dễ nhất là chạy `mac`, chọn **Thêm API Google / Cloudflare**, rồi nhập
key/token trong ô ẩn. Trên macOS, secret được lưu vào Keychain; MAC chỉ giữ
tham chiếu và fingerprint.

Đặt secret bằng trình quản lý bí mật hoặc cấu hình shell của máy, rồi export
tên biến cho tiến trình chạy MAC:

    export GOOGLE_API_KEY
    export CLOUDFLARE_API_TOKEN

Sau đó yêu cầu Master đăng ký tham chiếu `env://GOOGLE_API_KEY` hoặc
`env://CLOUDFLARE_API_TOKEN` bằng tool `org_register_credential_ref`, cấu hình
free routing, rồi gọi `org_free_route`. Không gửi API key vào prompt hoặc tham
số MCP. MAC từ chối secret thô, paid routing và fixture data trên đường
production.

HTTP adapter, cách đọc usage, lỗi quota/429, fallback và recovery đã được kiểm
thử trên máy phát hành bằng response có cấu trúc như production. Lệnh gọi bằng
credential thật được dành cho người cài đặt chủ động thử vì MAC không lưu secret
của người dùng.

##### Đã thử với Sol và Luna

MAC không chỉ được kiểm tra bằng fixture. Một phiên làm việc thật đã dùng
**gpt-5.6-sol làm Master trên ChatGPT** và **gpt-5.6-luna làm worker do MAC quản
lý**. Luna chạy đúng model, không fallback, hoàn thành task có phạm vi rõ ràng
và dừng ở trạng thái <code>REVIEW</code>. Sol sau đó đọc lại kết quả, đối chiếu
với file gốc rồi mới quyết định chấp nhận. MAC cũng lưu output, token usage,
thread ID và lịch sử run để Master có thể retry, cancel hoặc validate.

Đây là cách MAC được thiết kế để làm việc: worker tập trung thực hiện phần việc
được giao, còn Master giữ quyền kiểm tra và quyết định cuối cùng. Luồng
Sol–Luna ở trên là kiểm thử CLI thật. Google Gemini và Cloudflare là đường API
riêng; chưa dùng tài khoản hoặc quota thật của người phát hành.

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

Nếu trước đó đã gặp lỗi <code>The string is missing the terminator</code>, đó
là file installer cũ. Không chạy lại file cũ; cập nhật clone trước:

    Set-Location $HOME
    Set-Location .\mac
    git pull origin main
    powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1

Sau khi installer hoàn tất, đóng PowerShell này, mở PowerShell mới rồi chạy
<code>mac</code>.

##### macOS hoặc Linux (Terminal)

    git clone https://github.com/Horaus/mac.git
    cd mac
    ./scripts/bootstrap.sh .

Sau khi script hoàn tất, mở Terminal mới (nếu cần) rồi chạy:

    mac


#### 2. Kết nối với ứng dụng AI

Terminal dùng để cài MAC; sử dụng khung chat để yêu cầu AI kết nối tới MAC qua MCP và thực hiện các tùy chọn cần thiết. Bạn có thể chat trong bất kỳ project nào, không cần mở thư mục
<code>mac</code>.

Nếu AI chưa biết MAC, gửi câu này:

    Đọc README tại https://github.com/Horaus/mac và kết nối tới MAC MCP đã cài trên máy.

Nếu ứng dụng AI đã nạp MAC MCP, chỉ cần gửi:

    Kết nối MAC MCP và thực hiện yêu cầu sau: <yêu cầu>

AI sẽ nạp quy tắc vận hành do MAC cung cấp và hỏi thêm thông tin khi cần. Quá trình này diễn ra tự động.
Phần cấu hình bên dưới dành cho AI agent đọc; người dùng không cần chép các lệnh MCP vào khung chat.

#### 3. Cập nhật

    cd mac
    git pull
    ./scripts/bootstrap.sh .

Hoặc cập nhật trực tiếp trong menu MAC sau khi gọi lệnh <code>mac</code> trên Terminal.

#### 4. Gỡ cài đặt

Chọn **Gỡ cài đặt MAC** trong menu, hoặc chạy:

    powershell -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1 -Yes
    ./scripts/uninstall.sh --yes

Lệnh xóa môi trường Python, dữ liệu MAC và launcher nhưng giữ mã nguồn.
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

#### Gemini CLI và Antigravity

Gemini CLI dùng <code>~/.gemini/settings.json</code>. Antigravity dùng
<code>~/.gemini/config/mcp_config.json</code> (không dùng settings.json). Thêm
server <code>mac</code>, thay
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

Khởi động lại Gemini hoặc Antigravity, xác nhận MAC tools xuất hiện, rồi gọi
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

In the **Workers** tab, select a worker and press `x` to configure its model,
Codex profile, reasoning effort, and sandbox. Leave a field blank to inherit
the provider default. A configured model can run only when the current account
and provider are entitled to use it.

##### What is new in version 0.5.5

MAC now distinguishes basic workers from specialists. Workers retain durable
working memory for an active goal, while the Master decides what trusted
knowledge should become long-term memory. Goals, checkpoints, worker packs,
and interrupted actions can be recovered after quota exhaustion, disconnects,
or restarts. If the original provider session cannot be resumed, MAC discloses
that fact instead of pretending the worker still remembers it.

Version 0.5.5 also adds free-tier API routing for Google Gemini and Cloudflare
Workers AI. The Master owns provider selection and fallback policy; MAC records
quota classification, request IDs, and token usage. Secrets are resolved from
environment references and are never accepted as task or MCP payload values.

The 0.5.5 hotfix makes `result_only` truly task-scoped, preserves explicit
`WAITING_DECISION` checkpoints, returns schema errors for malformed worker
packs, and keeps historical output out of the status screen. Reconnecting MCP
clients no longer falsely orphan a live identity-matched worker process.
Replaying goal creation with identical immutable metadata now confirms the
existing goal; conflicting reuse of an ID still fails closed.
Stored goal configuration remains authoritative when a reconnecting client
omits or defaults optional creation fields.

##### Trying the Google and Cloudflare APIs

The easiest route is to run `mac`, choose **Add Google / Cloudflare API**, and
enter the key/token in the hidden prompt. On macOS the secret is stored in
Keychain; MAC retains only its reference and fingerprint.

Set the values through the machine's secret manager or shell configuration,
then export the variable names to the process that runs MAC:

    export GOOGLE_API_KEY
    export CLOUDFLARE_API_TOKEN

Ask the Master to register `env://GOOGLE_API_KEY` or
`env://CLOUDFLARE_API_TOKEN` with `org_register_credential_ref`, configure the
free routing policy, and call `org_free_route`. Never paste an API key into a
prompt or MCP argument. MAC rejects raw secrets, paid routing, and fixture data
on the production path.

The HTTP adapters, production-shaped usage parsing, quota/429 handling,
fallback, and recovery paths were tested locally. A call using the installer's
own live credentials remains an explicit opt-in test because MAC does not ship
or retain user credentials.

##### Tested with Sol and Luna

MAC has been exercised beyond fixtures. In a real session,
**gpt-5.6-sol acted as the ChatGPT Master** while **gpt-5.6-luna ran as the
MAC-managed worker**. Luna ran on the requested model without fallback,
completed a tightly scoped task, and stopped at <code>REVIEW</code>. Sol then
checked the result against the source file before accepting it. MAC retained
the output, token usage, thread ID, and run history so the Master could retry,
cancel, validate, or accept the work.

That is the intended working relationship: the worker focuses on its assigned
job while the Master keeps final review authority. The Sol–Luna result above is
a real CLI test. Google Gemini and Cloudflare use a separate API path; no
publisher account or live quota was used for the release tests.

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

If you previously saw <code>The string is missing the terminator</code>, you
are running an old installer. Do not run that old file again; update the clone
first:

    Set-Location $HOME
    Set-Location .\mac
    git pull origin main
    powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1

After the installer finishes, close that PowerShell window, open a new one,
and run <code>mac</code>.

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

#### 4. Uninstall

Choose **Uninstall MAC** in the menu, or run:

    powershell -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1 -Yes
    ./scripts/uninstall.sh --yes

This removes the Python environment, MAC state and launchers while keeping the source checkout.

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
