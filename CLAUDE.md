# CLAUDE.md

DSTCamp：Windows 上的《饥荒：联机版》存档、Mod、世界、专服、内网穿透与大厅加速管理工具。包名 `dstools`，界面为 Qt（PySide6），入口 `dstools.qt.app.main`，发布入口 `scripts/run_gui.py`。

默认中文交流、中文文档与注释；标识符、协议字段和第三方 API 保留原文。`CLAUDE.md` 与 `AGENTS.md` 内容保持一致（标题行除外），改一个就同步另一个。

## 工作方式

- 先确认范围与验收标准，做最小必要改动，保留已有用户改动，不顺带重构无关代码。
- 开始和提交前看 `git status --short`、`git diff --check`；精确暂存，禁止 `git add .`。
- 代码改动验证通过后默认创建一次中文本地提交；不推送、不打标签、不发布，除非用户明确要求。无法与已有未提交内容分开、验证未通过或用户要求不提交时，保留改动并说明原因。
- 网络、Steam、SSH、真实 GUI 和游戏行为以实际文件、日志或真机结果为准，不用静态推测冒充验证。
- 没有可靠的无障碍自动化工具时，不用坐标点击模拟 GUI 交互；启动、截图确认渲染、日志和单元测试照做，交互结果由用户手工确认。
- 经 Bash 工具传递的命令（含 heredoc）会把 `\\` 折叠成 `\`：含反斜杠的脚本、正则或替换文本一律用文件编辑工具写成文件再运行，不内联进命令；改完用 `python -W error::SyntaxWarning` 编译检查。
- 删除 dataclass 字段或函数参数时，除按名字搜索外，还要检查按位置传参的调用点。

## 项目结构

```text
dstools/qt/           界面：app.py 装配与入口、window.py 主窗口、pages/ 各页签、dialogs.py 通用弹窗、
                      theme.py 主题与字体、threads.py 后台任务
dstools/features/     单功能业务逻辑（不依赖界面，可直接测试）
dstools/shared/       两个以上功能共用的基础设施；palettes.py 调色板、font_styles.py 字体样式、
                      resource_paths.py 资源与运行时目录（顶部有完整目录清单）
dstools/i18n/strings.py  中英文文案唯一来源
icons/、tools/        随发布打包的固定资源
reference/            开发核对资料（含 tk_legacy/ 旧 Tk 代码、dev_scripts/），不进 Git、不参与运行
build/、release/      可重建的构建中间产物与发布产物，不进 Git
```

运行时目录 `%APPDATA%/DSTCamp/`：`settings.json`；`cache/` 可重建（可改位置，须纯 ASCII）；`data/` 需保留的数据；`security/` SSH 密钥、WireGuard/Mihomo 配置与 OAuth 令牌。新增目录时按此分类并更新 `resource_paths.py` 顶部清单。

## 常用命令

```powershell
pip install -e .
python -m dstools.qt.app
python tests/run_all.py
pip install -e ".[build]"
python scripts/build_exe.py
```

- 测试不用 pytest/unittest 框架：`run_all.py` 自动发现 `tests/test_*.py` 并在隔离子进程执行，每个文件以 `run(globals())`（`tests/_harness.py`）按定义顺序运行全部 `test_*`。只测 Qt 版与纯逻辑模块；测试越少越好，不测静态数据、简单读写往返和实现细节字符串。
- 用真实存档核对世界预设：`python -X utf8 -m dstools.features.world.preset_cli check --cluster <存档名>`（只读；另有 `list`/`save`/`apply`，`apply` 先备份、可加 `--dry-run`，均支持 `--json`）。本机已可编辑安装，任意目录可用，`D:\mymod\scripts\world_preset.ps1` 是它的包装。
- 版本号以 `pyproject.toml` 与 `dstools/__init__.py` 为准，发布时同步两处，文档不写死当前版本。
- 构建只收固定资源白名单、在 `build/` 暂存，禁止包含缓存、持久数据、安全材料和 `reference/`。打包后实际启动 EXE；冒烟测试不能替代 GUI、Steam、frpc 或游戏内验证。验证 EXE 与 `sha256.json` 后再提交、推送、打 `vX.Y.Z` 标签并创建 Release。
- 发布构建用 `python scripts/build_exe.py --patch-from <近几个正式版的 DSTCamp-X.Y.Z.exe>` 生成增量补丁（旧版 EXE 不能放在 `release/` 里，构建会清空它），`.hdiff` 与 EXE、`sha256.json` 一起上传为 Release 附件；没有对应补丁的旧版客户端自动下载整包。
- 正式构建开始前自动运行 `reference/dev_scripts/audit_world_settings.py`，世界设置登记表没跟上游戏或已适配 Mod 的更新时中止：按报告更新登记表与 `MOD_VERIFIED_VERSIONS`，再用 `--snapshot` 存快照；确认先不处理才加 `--skip-world-audit`。
- 密钥放项目外的 `F:\AI\mywork\ProjectKey\DSTCamp\`（可用环境变量 `DSTCAMP_KEY_DIR` 改），不放进项目目录。`sha256.json` 必须用其中的 `update_signing_key.pem`（Ed25519，不进 Git，务必另行备份）签名，客户端只认带有效签名的清单；`--unsigned` 只用于自测。私钥丢失后已装客户端无法再自动更新。
- `release/offline/` 下的完整包、补丁包（以及工具包）只传蓝奏云等网盘，不传 GitHub/Gitee；用户在「关于 → 手动更新 → 从文件导入」安装。
- 按需下载的工具登记在 `shared/tool_store.py`（大小与 SHA-256 写死，有测试核对 `tools/`）。换工具版本时升 `TOOLS_RELEASE_TAG`，运行 `python scripts/make_tools_release.py`（在构建 EXE 之后），把 `release/tools-N/` 上传为 GitHub 标签 `tools-N` 的 Release 并标记为非 latest（`gh release create --latest=false`），且要在依赖它的版本发行版之前创建：1.7.1 及更早的客户端用 `releases/latest` 检查更新。网盘工具包 `DSTCamp-tools-N.zip` 放进蓝奏云「手动更新」分享文件夹的根目录（蓝奏云不能单独分享子文件夹），另外单独分享一份，把链接和密码填进 `tool_store.TOOLS_LANZOU_URL`/`TOOLS_LANZOU_PASSWORD`（下载失败时直接打开）。

## 更新日志规则

编写 `README.md`/`README.en.md`/Release 说明时不得凭记忆总结：

1. 以 `git log <上个tag>..HEAD --oneline` 的完整提交列表为唯一输入。
2. 普通用户能感知的（bug 症状、新能力、可感知的性能/内存变化、可见的视觉/文案调整）才收录；纯测试、文档、无外部行为变化的重构、构建脚本内部调整和版本号提交默认不收录。
3. 只有同一修复的多次迭代才合并成一条，不能因主题相近就合并。
4. 写完反查：每条提交要么对应一条日志，要么已明确判定不收录。

## Gitee 发行版

- 说明不能含表情符号（同步脚本已自动去掉）和"代理"等字样，否则整段被替换为"内容可能含有违规信息"；改用"TUN 虚拟网卡"等说法。
- 同步后用 `GET /api/v5/repos/orange-blade/DSTCamp/releases/tags/vX.Y.Z` 回读说明与附件，被替换时改写后 `PATCH .../releases/{id}`。
- 社区版附件单文件不超过 100 MB、单仓库附件总量 1 GB（同步脚本保留 3 个版本和最新的 `tools-N`），EXE 不能涨过 100 MB。
- 本机没有 git 凭据，`git push gitee` 会卡住：用临时 `GIT_ASKPASS` 读密钥目录里的 `gitee_token.txt`（用户名 `orange-blade`），令牌不写进 URL、命令行或配置。

## 关键约束

界面：

- 颜色只用 `theme.hex()`/`theme.color()`；新增颜色键须加到 `shared/palettes.py` 全部主题（测试校验键一致）。成功状态用 `SUCCESS`，失败用 `ERROR`。
- 字体只用 `theme.font(size_key)`；字体样式只在 `shared/font_styles.py` 注册，字体及许可证放 `tools/fonts/`。
- 弹窗用 `qt/dialogs.py`（`Dialog`、`show_*`、`ask_yes_no`、`ask_choice`、`LogDialog`），宽度只在 `DIALOG_WIDTHS` 三档里选，不用 `QMessageBox`；大窗口用 `dialogs.fit_to_screen()`，不写死超出高缩放屏工作区的尺寸。
- 耗时工作用 `run_async`/`post_to_ui` 放后台，工作线程不碰控件。
- 页面构造不做重活，按 `Page.load()`/`on_cluster_changed()` 懒加载；持续监督的状态（专服、frpc）用定时器，只在状态变化时刷新界面。
- 下拉列表等弹出窗口不开 `WA_TranslucentBackground`（Windows 上整块发黑），半透明用截取背景的"假透明"。
- 图片按屏幕缩放比缩到物理像素并设置 `devicePixelRatio`，不要先缩到逻辑尺寸再被二次放大。
- 无人值守流程（如崩溃自动重启）不弹模态框，失败原因写横幅、托盘通知和日志。

业务：

- `ktech.exe` 输出先落纯 ASCII 临时目录，再移动到目标路径。
- Mod 目录联接用 `os.path.isjunction()` 判断、`os.rmdir()` 删除，禁止 `shutil.rmtree()`。
- `CLUSTER_INI_DEFAULTS` 只补缺失字段；`NO_TYPE_COERCE_FIELDS` 中的密码等字段保持字符串。
- 森林与洞穴配置独立；新增世界 key、取值或 Mod 支持前必须核对实际 Lua 源码。
- ktech 随包；frpc、frps、樱花 frpc、vcredist 不打包，首次使用时经 `tool_store` 从官网（vcredist）或 `tools-N` 发行版下载，按写死的 SHA-256 校验，存 `data/tools/` 并保持 `.xz`（裸 frp 落盘会被杀软隔离），也可从网盘工具包导入。自建/Lolia 的 frpc 由开服等无人值守流程拉起，经 `qt/frpc_tool.py` 后台静默下载、失败显示在状态行，不弹窗。Linux 二进制用 `sftp.putfo()` 流式上传，本机不落地解压。
- LuaJIT 补丁依赖的 VC++ 2015-2022 x64 运行库版本会随微软更新而变，不进 `tool_store`：安装补丁前按配套 DLL 的链接器版本检测，缺失时从微软官网下载，校验内嵌的微软 Authenticode 签名后安装（`features/local_service/luajit_runtime.py`）。
- 内置 SteamCMD（`tools/steamcmd/`，原样附 Valve 许可协议，不得修改程序）首次使用时释放到缓存目录；它不能在非 ASCII 路径下运行，联网自更新由它自己完成。
- WeGame 不支持一键启动专服，不实现绕过方案。
- 动态 Mod 名称、版本、图标和配置以受限 Lua 5.1 沙箱结果为准；缓存校验包含内容哈希、来源路径、`folder_name` 和协议版本。
- V1 Legacy 包必须校验 ZIP/CRC/路径与链接，临时解压后原子替换并支持回滚；保留客户端 `mods` junction，V2 流程独立。
- 新令牌在 Klei 端释放前不能重复注册：崩溃/冲突后的令牌写入等待记录（`retry_at`/`failures`），选令牌只排除仍在等待期内的。
- 解析专服日志判断连接方式时，`[P2P]` 行里的地址是饥荒生成的伪地址（端口恒为 1），不能当成对端真实 IP。
- 开服程序目录只能经 `features/local_service/server_runtime.py` 获取（有测试守护）。

若代码知识图谱可用，优先用符号搜索与调用追踪；字面量、配置和图谱不足时再用 `rg`。
