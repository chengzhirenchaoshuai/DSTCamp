"""通过 SSH/SFTP 把 deploy.py 生成的部署脚本推送到用户云服务器并执行。

安全：密码不落盘；主机密钥 TOFU（首次连接回调确认，指纹变化直接拒绝）；只上传/执行
部署脚本和匹配架构的 frps 二进制。

坑：
- 国内服务器访问 GitHub 很慢，amd64/arm64 直接 SFTP 推送打包的 frps，其他架构才让脚本自己下载；
- frps 以 .xz 打包、现场解压流式上传，本机不落地 ELF 文件（裸文件每次启动解压到
  _MEI 临时目录、或落盘到临时文件，都会被杀软按 HackTool 特征秒隔离）。
"""

from __future__ import annotations

import lzma
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable

# paramiko 顶层导入要初始化 OpenSSL 绑定（300ms+），而本模块在启动时就被导入，
# 所以只在函数内按需导入；类型注解靠 `from __future__ import annotations` 延迟求值。
if TYPE_CHECKING:
    import paramiko

from dstools.features.frp_selfhost import deploy
from dstools.shared.resource_paths import security_dir, tool_binary_dir

_SECURITY_DIR = security_dir("frp_selfhost", legacy_cache_name="frp_selfhost")
KNOWN_HOSTS_PATH = _SECURITY_DIR / "known_hosts"
# 初次鉴权生成的密钥对（只支持管理一台自建服务器，固定路径）；私钥只在本机，公钥写入服务器 authorized_keys
SSH_KEY_PATH = _SECURITY_DIR / "ssh_key"
SSH_PUBKEY_PATH = _SECURITY_DIR / "ssh_key.pub"

# 已打包的 Linux 二进制架构（amd64 覆盖多数云服务器，arm64 覆盖 ARM 实例），其他架构由脚本自行下载
_BUNDLED_ARCHS = ("amd64", "arm64")

ConfirmHostKeyFn = Callable[[str, str], bool]  # (host, fingerprint) -> 是否信任
LogFn = Callable[[str], None]

# 执行阶段轮询 cancel_event 的间隔：兼顾取消响应速度与系统调用开销
_CANCEL_POLL_INTERVAL = 0.5


class RemoteDeployError(Exception):
    """SSH 连接/上传/执行过程中的任何失败，统一包成这一种异常，调用方
    只需要展示 str(e) 给用户，不需要分辨具体是 paramiko 的哪个子异常。"""


class RemoteDeployCancelled(RemoteDeployError):
    """用户取消；继承 RemoteDeployError，原有兜底逻辑仍能捕获，需要区分时用 isinstance。"""


def _tofu_policy(confirm_host_key: ConfirmHostKeyFn) -> "paramiko.MissingHostKeyPolicy":
    """仅在 known_hosts 中完全没有该主机时调用（密钥不匹配会在此前以 BadHostKeyException 报错）。
    基类依赖 paramiko，所以类定义放在函数内。"""
    import paramiko

    class _TOFUPolicy(paramiko.MissingHostKeyPolicy):
        def __init__(self, confirm_host_key: ConfirmHostKeyFn):
            self._confirm = confirm_host_key

        def missing_host_key(self, client, hostname, key):
            fingerprint = key.get_fingerprint().hex(":")
            if not self._confirm(hostname, f"{key.get_name()} {fingerprint}"):
                raise paramiko.SSHException("用户未确认信任该服务器的主机密钥，已取消连接")
            client.get_host_keys().add(hostname, key.get_name(), key)
            KNOWN_HOSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
            client.get_host_keys().save(str(KNOWN_HOSTS_PATH))

    return _TOFUPolicy(confirm_host_key)


def classify_permission(uid: str, sudo_ok: bool) -> str:
    """按 ``id -u`` 与 ``sudo -n true`` 的结果归类为 root / sudo_nopasswd / no_permission（probe.py 复用）。"""
    if uid.strip() == "0":
        return "root"
    return "sudo_nopasswd" if sudo_ok else "no_permission"


def check_remote_permission(client: paramiko.SSHClient) -> str:
    """在已经连接好的 client 上跑 `id -u`（必要时再跑 `sudo -n true`），
    返回 classify_permission() 的三种状态之一。"""
    _stdin, stdout, _stderr = client.exec_command("id -u", timeout=10)
    uid = stdout.read().decode("utf-8", errors="replace").strip()
    stdout.channel.recv_exit_status()
    if uid == "0":
        return "root"
    _stdin, stdout, _stderr = client.exec_command("sudo -n true", timeout=10)
    sudo_ok = stdout.channel.recv_exit_status() == 0
    return classify_permission(uid, sudo_ok)


def has_local_key() -> bool:
    return SSH_KEY_PATH.exists() and SSH_PUBKEY_PATH.exists()


def ensure_local_keypair() -> str:
    """本地已有密钥就复用，否则生成 Ed25519（paramiko 不能生成，改用 cryptography），返回公钥行。"""
    if has_local_key():
        return SSH_PUBKEY_PATH.read_text(encoding="utf-8").strip()

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    priv_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_line = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("ascii") + " dstcamp-selfhost"

    SSH_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SSH_KEY_PATH.write_bytes(priv_pem)
    try:
        import os
        os.chmod(SSH_KEY_PATH, 0o600)  # Windows 上是no-op，Linux/Mac 上有意义
    except OSError:
        pass
    SSH_PUBKEY_PATH.write_text(pub_line + "\n", encoding="utf-8")
    return pub_line


def authorize_key_on_server(
    host: str, port: int, username: str, password: str, pubkey_line: str,
    on_log: LogFn, confirm_host_key: ConfirmHostKeyFn, connect_timeout: float = 15.0,
) -> None:
    """用密码登录一次，把公钥幂等地追加进服务器 ~/.ssh/authorized_keys。"""
    import paramiko

    client = paramiko.SSHClient()
    if KNOWN_HOSTS_PATH.exists():
        client.load_host_keys(str(KNOWN_HOSTS_PATH))
    client.set_missing_host_key_policy(_tofu_policy(confirm_host_key))
    try:
        on_log(f"正在用密码连接 {host}:{port} ...")
        client.connect(hostname=host, port=port, username=username, password=password,
                       timeout=connect_timeout, banner_timeout=connect_timeout,
                       auth_timeout=connect_timeout)
    except paramiko.BadHostKeyException as e:
        raise RemoteDeployError(
            f"服务器 {host} 的主机密钥和上次记录的不一致，为安全起见已拒绝连接。详情: {e}") from e
    except paramiko.AuthenticationException as e:
        raise RemoteDeployError(f"密码认证失败: {e}") from e
    except (paramiko.SSHException, OSError) as e:
        raise RemoteDeployError(f"连接失败: {e}") from e

    try:
        on_log("正在推送公钥到服务器...")
        # 用一条 shell 命令完成"不存在才追加"（grep -qxF 精确匹配整行），省一次往返
        escaped = pubkey_line.replace("'", "'\\''")
        cmd = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"touch ~/.ssh/authorized_keys && "
            f"grep -qxF '{escaped}' ~/.ssh/authorized_keys || echo '{escaped}' >> ~/.ssh/authorized_keys && "
            "chmod 600 ~/.ssh/authorized_keys"
        )
        _stdin, stdout, stderr = client.exec_command(cmd, timeout=15)
        exit_code = stdout.channel.recv_exit_status()
        if exit_code != 0:
            err_text = stderr.read().decode("utf-8", errors="replace").strip()
            raise RemoteDeployError(f"推送公钥失败（退出码 {exit_code}）: {err_text}")
        on_log("公钥已推送。")
    except (paramiko.SSHException, OSError) as e:
        raise RemoteDeployError(f"推送公钥失败: {e}") from e
    finally:
        client.close()


def verify_key_login(
    host: str, port: int, username: str, on_log: LogFn, connect_timeout: float = 15.0,
) -> bool:
    """只用本地私钥连接一次，确认公钥确实生效；失败抛 RemoteDeployError。"""
    import paramiko

    pkey = paramiko.Ed25519Key.from_private_key_file(str(SSH_KEY_PATH))
    client = paramiko.SSHClient()
    if KNOWN_HOSTS_PATH.exists():
        client.load_host_keys(str(KNOWN_HOSTS_PATH))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        on_log("正在用密钥验证登录...")
        client.connect(hostname=host, port=port, username=username, pkey=pkey,
                       timeout=connect_timeout, banner_timeout=connect_timeout,
                       auth_timeout=connect_timeout)
        on_log("验证成功，以后连接这台服务器不再需要密码。")
        return True
    except paramiko.AuthenticationException as e:
        raise RemoteDeployError(f"密钥验证失败（公钥可能没有正确推送）: {e}") from e
    except (paramiko.SSHException, OSError) as e:
        raise RemoteDeployError(f"密钥验证连接失败: {e}") from e
    finally:
        client.close()


def _local_frps_binary_xz(arch: str, on_log: LogFn) -> Path | None:
    """本机的 frps（.xz）：源码模式在 tools/ 里；发布版不打包，用已下载的副本或现场下载。拿不到返回 None。"""
    from dstools.shared import tool_store

    relative = f"frp_selfhost/frps_linux_{arch}.xz"
    bundled = tool_binary_dir() / relative
    if bundled.is_file():
        return bundled
    stored = tool_store.find_stored(relative)
    if stored is not None:
        return stored
    tool = tool_store.store_tool(relative)
    on_log(f"正在下载 frps 程序本体（{arch}，约 {tool.size / 1e6:.1f} MB，只需下载一次）...")
    shown = [0]

    def progress(done: int, total: int) -> None:
        percent = done * 100 // max(1, total)
        if percent >= shown[0] + 25:
            shown[0] = percent - percent % 25
            on_log(f"  已下载 {shown[0]}%")

    try:
        return tool_store.download_tool(relative, progress)
    except OSError as exc:
        on_log(f"下载 frps 失败（{exc}），改为让服务器自己下载。")
        return None


def _detect_remote_arch(client: paramiko.SSHClient) -> str:
    """``uname -m`` 映射成 frp 发行包的架构名（与 deploy.py 一致），识别不了返回空字符串。"""
    import paramiko

    try:
        _stdin, stdout, _stderr = client.exec_command("uname -m", timeout=10)
        machine = stdout.read().decode("utf-8", errors="replace").strip()
    except (paramiko.SSHException, OSError):
        return ""
    return {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm"}.get(machine, "")


# 远程固定缓存路径（按架构 + frp 版本区分）：同版本只上传一次，升级打包版本后路径随之变化
def _cached_frps_binary_path(arch: str) -> str:
    return f"/opt/dstcamp-frp/.frps_bin_cache_v{deploy.FRP_VERSION}_{arch}"


def _run_remote(client: paramiko.SSHClient, command: str, timeout: float = 30) -> tuple[int, str]:
    """执行一条远程命令，返回 (退出码, stderr 文本)。"""
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    return exit_code, stderr.read().decode("utf-8", errors="replace").strip()


def _maybe_upload_frps_binary(client: paramiko.SSHClient, on_log: LogFn, sudo: str) -> str | None:
    """架构匹配本机 frps 时上传到远程固定缓存路径并返回该路径，否则返回 None。

    ``sudo`` 是命令前缀（root 为空，免密 sudo 账号为 ``"sudo -n "``）。SFTP 只能以登录账号身份写文件，
    ubuntu 等普通账号写不进 /opt，所以先传到 /tmp，再用 sudo 复制进缓存目录；先复制成临时名再改名，
    避免断线留下半成品被误判为缓存。已存在缓存（``test -f``）则跳过上传。"""
    arch = _detect_remote_arch(client)
    if arch not in _BUNDLED_ARCHS:
        on_log(f"服务器架构（{arch or '未知'}）没有对应的本地打包二进制，改为让服务器自己下载。")
        return None
    remote_path = _cached_frps_binary_path(arch)
    exit_code, _err = _run_remote(client, f"test -f '{remote_path}'", timeout=10)
    if exit_code == 0:
        on_log(f"检测到服务器上已经缓存过 frps 程序本体（{arch}），跳过上传。")
        return remote_path
    xz_path = _local_frps_binary_xz(arch, on_log)
    if xz_path is None:
        return None

    on_log(f"正在上传 frps 程序本体（{arch}，避免服务器自己访问 GitHub）...")
    import secrets

    import paramiko
    token = secrets.token_hex(4)
    upload_path = f"/tmp/.dstcamp_frps_upload_{token}"
    partial_path = f"{remote_path}.part_{token}"
    try:
        # 用 putfo 直接上传解压出的字节流：落盘到本地临时文件同样会被杀软按 frp 内容特征查杀
        with lzma.open(xz_path, "rb") as fsrc:
            sftp = client.open_sftp()
            try:
                sftp.putfo(fsrc, upload_path)
            finally:
                sftp.close()
        # /tmp 可能是独立的 tmpfs，跨文件系统 mv 不是原子操作，所以先 cp 成临时名再同目录改名
        exit_code, err = _run_remote(
            client,
            f"{sudo}mkdir -p /opt/dstcamp-frp && {sudo}cp '{upload_path}' '{partial_path}'"
            f" && {sudo}chmod 755 '{partial_path}' && {sudo}mv -f '{partial_path}' '{remote_path}'",
            timeout=60)
        if exit_code != 0:
            _run_remote(client, f"{sudo}rm -f '{partial_path}'", timeout=10)
            raise OSError(f"放入缓存目录失败（退出码 {exit_code}）：{err or '无错误输出'}")
    finally:
        try:
            _run_remote(client, f"rm -f '{upload_path}'", timeout=10)
        except (paramiko.SSHException, OSError):
            pass  # 清理失败不影响结果，/tmp 会被系统定期清理
    return remote_path


def deploy_via_ssh(
    host: str, port: int, username: str, bind_port: int, token: str,
    on_log: LogFn, confirm_host_key: ConfirmHostKeyFn,
    password: str | None = None, key_path: str | None = None, key_passphrase: str | None = None,
    connect_timeout: float = 15.0, exec_timeout: float = 120.0,
    cancel_event: threading.Event | None = None,
) -> bool:
    """连接、上传、执行、清理，成功返回 True；失败抛 RemoteDeployError，取消抛 RemoteDeployCancelled。

    同步阻塞，需在后台线程调用。cancel_event 在连接/上传前检查一次，执行阶段按
    _CANCEL_POLL_INTERVAL 轮询，取消时关闭 channel（相当于 SIGHUP）。"""
    import secrets

    import paramiko

    def _check_cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise RemoteDeployCancelled("用户已取消部署")

    client = paramiko.SSHClient()
    if KNOWN_HOSTS_PATH.exists():
        client.load_host_keys(str(KNOWN_HOSTS_PATH))
    client.set_missing_host_key_policy(_tofu_policy(confirm_host_key))

    connect_kwargs = dict(hostname=host, port=port, username=username,
                          timeout=connect_timeout, banner_timeout=connect_timeout,
                          auth_timeout=connect_timeout)
    if key_path:
        try:
            pkey = paramiko.Ed25519Key.from_private_key_file(key_path, password=key_passphrase)
        except paramiko.SSHException:
            try:
                pkey = paramiko.RSAKey.from_private_key_file(key_path, password=key_passphrase)
            except paramiko.SSHException as e:
                raise RemoteDeployError(f"读取私钥文件失败: {e}") from e
        connect_kwargs["pkey"] = pkey
    else:
        connect_kwargs["password"] = password

    _check_cancelled()
    on_log(f"正在连接 {host}:{port} ...")
    try:
        client.connect(**connect_kwargs)
    except paramiko.BadHostKeyException as e:
        # 已记录的主机密钥不匹配（重装系统或中间人攻击），直接拒绝并给出明确提示
        raise RemoteDeployError(
            f"服务器 {host} 的主机密钥和上次记录的不一致，为安全起见已拒绝连接。"
            f"如果确认是服务器本身重装/更换了（不是遭到了中间人攻击），"
            f"需要手动删除本地记录的旧密钥后重试。详情: {e}") from e
    except paramiko.AuthenticationException as e:
        raise RemoteDeployError(f"认证失败，请检查用户名/密码或密钥: {e}") from e
    except (paramiko.SSHException, OSError) as e:
        raise RemoteDeployError(f"连接失败: {e}") from e

    # 先置 None，取消/出错时清理逻辑才能判断是否已上传，避免在 /tmp 留下孤儿文件
    remote_frps_path = None
    remote_script_path = None
    try:
        _check_cancelled()
        on_log("连接成功。")

        on_log("正在检查账号权限...")
        permission = check_remote_permission(client)
        if permission == "no_permission":
            raise RemoteDeployError(
                "当前账号既不是 root，也没有配置免密 sudo，无法执行安装脚本。"
                "请改用 root 账号登录，或者用 visudo 给该账号加一行"
                "「<用户名> ALL=(ALL) NOPASSWD:ALL」后重试。")

        try:
            remote_frps_path = _maybe_upload_frps_binary(
                client, on_log, "" if permission == "root" else "sudo -n ")
        except (paramiko.SSHException, OSError) as e:
            # 上传二进制失败不算致命——退回脚本自己下载那条路径，只是
            # 记一句日志说明为什么退回，不中断整个部署。
            on_log(f"上传 frps 二进制失败（{e}），改为让服务器自己下载。")
            remote_frps_path = None
        script_text = deploy.build_install_script(bind_port, token, local_frps_path=remote_frps_path)

        _check_cancelled()
        on_log("正在上传部署脚本...")
        try:
            sftp = client.open_sftp()
            remote_script_path = f"/tmp/dstcamp_install_frps_{secrets.token_hex(4)}.sh"
            with sftp.file(remote_script_path, "w") as f:
                f.write(script_text)
            sftp.chmod(remote_script_path, 0o700)
            sftp.close()
        except (paramiko.SSHException, OSError) as e:
            raise RemoteDeployError(f"上传脚本失败: {e}") from e

        _check_cancelled()
        if remote_frps_path:
            on_log("脚本已上传，开始执行...")
        else:
            on_log("脚本已上传，开始执行（可能需要一点时间从 GitHub 下载 frp）...")
        # sudo -n 非交互：需要密码时直接失败而不是卡住；root 登录同样适用
        _stdin, stdout, stderr = client.exec_command(
            f"sudo -n bash {remote_script_path}", timeout=exec_timeout)
        channel = stdout.channel
        channel.settimeout(_CANCEL_POLL_INTERVAL)
        buf = b""
        while True:
            if cancel_event is not None and cancel_event.is_set():
                channel.close()
                raise RemoteDeployCancelled("用户已取消部署")
            try:
                chunk = channel.recv(4096)
            except TimeoutError:
                continue
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                on_log(line.decode("utf-8", errors="replace"))
        if buf:
            on_log(buf.decode("utf-8", errors="replace"))
        exit_code = channel.recv_exit_status()
        err_text = stderr.read().decode("utf-8", errors="replace")
        if err_text.strip():
            on_log(err_text.strip())

        # 退出码 3 是部署脚本约定的"目标端口被占用，跳过安装"，与成功(0)、失败(其他)区分
        if exit_code == 3:
            raise RemoteDeployError(
                f"部署脚本检测到目标端口 {bind_port} 已经被服务器上其它服务占用（不是 "
                f"dstcamp-frps 自己），已跳过安装/更新。请换一个端口，或者先在服务器上"
                f"停掉占用这个端口的服务，再重新部署。")
        elif exit_code != 0:
            raise RemoteDeployError(f"部署脚本执行失败（退出码 {exit_code}），请查看上面的日志定位原因")
        on_log("部署完成。")
        return True
    except (paramiko.SSHException, OSError) as e:
        raise RemoteDeployError(f"执行脚本失败: {e}") from e
    finally:
        # frps 缓存路径故意保留供下次复用，只清理一次性的部署脚本
        if remote_script_path:
            try:
                client.exec_command(f"rm -f {remote_script_path}")
            except (paramiko.SSHException, OSError):
                pass
        client.close()
