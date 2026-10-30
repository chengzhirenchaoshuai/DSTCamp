"""生成自建 frps 的配置文本和一键部署脚本（纯字符串拼接，SSH 执行在 remote_deploy.py）。

FRP_VERSION 须与 tools/ 里按需下载的 frpc、frps 版本一致：服务器自行下载时按它取发行包，
远程 frps 缓存路径也带上它，换版本后重新部署才会上传新版。
"""

import re
import secrets

FRP_VERSION = "0.71.1"

DEFAULT_BIND_PORT = 7000


def generate_token() -> str:
    """生成一个给 frps/frpc 双方鉴权用的随机 token——32 个十六进制字符，
    足够长、不需要用户自己想一个（也不该自己想，容易图省事用弱口令）。"""
    return secrets.token_hex(16)


# 手填 Token 只收可见 ASCII 且不含双引号、反斜杠：frps.toml 把它原样拼进双引号字符串
_TOKEN_RE = re.compile(r'[!#-\[\]-~]{1,256}')


def is_valid_token(token: str) -> bool:
    return bool(_TOKEN_RE.fullmatch(token))


FRPS_TOML_PATH = "/opt/dstcamp-frp/frps.toml"  # 与部署脚本的 INSTALL_DIR 一致
_FRPS_PORT_RE = re.compile(r'^bindPort\s*=\s*(\d+)\s*$', re.MULTILINE)
_FRPS_TOKEN_RE = re.compile(r'^token\s*=\s*"([^"\\]*)"\s*$', re.MULTILINE)


def parse_frps_toml(text: str) -> tuple[int | None, str | None]:
    """从 build_frps_toml 写出的配置里读回 (端口, Token)，读不到的项为 None；共用别人部署的节点时据此填连接设置。"""
    port = _FRPS_PORT_RE.search(text)
    token = _FRPS_TOKEN_RE.search(text)
    return (int(port.group(1)) if port else None), (token.group(1) if token else None)


def build_frps_toml(bind_port: int, token: str) -> str:
    """服务端 frps.toml：只开鉴权与监听，不开 dashboard（避免多暴露一个公网管理页）。"""
    return (
        f'bindAddr = "0.0.0.0"\n'
        f'bindPort = {bind_port}\n'
        f'\n'
        f'[auth]\n'
        f'method = "token"\n'
        f'token = "{token}"\n'
    )


def build_install_script(bind_port: int, token: str, local_frps_path: str | None = None) -> str:
    """生成幂等的一键部署 bash 脚本：安装 frps、写 frps.toml、注册 systemd 服务并启动。

    ``local_frps_path`` 不为空时直接复制已上传的二进制，跳过从 GitHub 下载（国内访问很慢）。
    服务已在运行时只重写配置并重启；目标端口被其他服务占用时跳过安装。云服务商安全组无法自动放行，
    脚本最后提醒用户手动处理。"""
    frps_toml = build_frps_toml(bind_port, token)
    if local_frps_path:
        fetch_frps_block = f'''echo "==> 使用已经上传好的 frps 二进制（{local_frps_path}）..."
mkdir -p "$INSTALL_DIR"
cp "{local_frps_path}" "$INSTALL_DIR/frps"
chmod +x "$INSTALL_DIR/frps"
'''
    else:
        fetch_frps_block = '''case "$(uname -m)" in
    x86_64) ARCH="amd64" ;;
    aarch64) ARCH="arm64" ;;
    armv7l) ARCH="arm" ;;
    *)
        echo "不认识的 CPU 架构：$(uname -m)，需要手动安装 frp" >&2
        exit 1
        ;;
esac

echo "==> 下载 frp ${FRP_VERSION} (linux_${ARCH})..."
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
PKG_NAME="frp_${FRP_VERSION}_linux_${ARCH}"
DOWNLOAD_URL="https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/${PKG_NAME}.tar.gz"
# 国内云服务器访问 GitHub 常年长时间卡住而非直接失败，加超时上限让
# 它在几分钟内明确失败，而不是无限期卡着。
if ! curl -fL --connect-timeout 15 --max-time 180 --retry 2 -o "$TMP_DIR/frp.tar.gz" "$DOWNLOAD_URL"; then
    echo "下载失败或超时：$DOWNLOAD_URL" >&2
    echo "国内云服务器访问 GitHub 经常不稳定，可以稍后重试，或者自行配置好代理/镜像站再重新运行本脚本。" >&2
    exit 1
fi

echo "==> 解压并安装到 ${INSTALL_DIR}..."
mkdir -p "$INSTALL_DIR"
tar -xzf "$TMP_DIR/frp.tar.gz" -C "$TMP_DIR"
cp "$TMP_DIR/$PKG_NAME/frps" "$INSTALL_DIR/frps"
chmod +x "$INSTALL_DIR/frps"
'''
    return f'''#!/usr/bin/env bash
# DSTCamp 自建 frps 一键部署脚本 —— frp {FRP_VERSION}
# 用法：把这份脚本存成 install_frps.sh，上传到服务器后执行：
#   sudo bash install_frps.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 root 权限运行（例如 sudo bash install_frps.sh）" >&2
    exit 1
fi

INSTALL_DIR="/opt/dstcamp-frp"
SERVICE_NAME="dstcamp-frps"
BIND_PORT="{bind_port}"
FRP_VERSION="{FRP_VERSION}"

write_config_and_restart() {{
    mkdir -p "$INSTALL_DIR"
    cat > "$INSTALL_DIR/frps.toml" <<'FRPS_TOML_EOF'
{frps_toml}FRPS_TOML_EOF
    systemctl restart "$SERVICE_NAME"
    sleep 1
}}

print_success() {{
    echo ""
    echo "=========================================="
    echo "frps 正在运行，监听端口 ${{BIND_PORT}}"
    echo "重要：还需要去你云服务商的控制台，在安全组/防火墙里放行"
    echo "  - TCP/UDP ${{BIND_PORT}}（frps 本身）"
    echo "  - 以及之后在 DSTCamp 里给每个世界分配到的映射端口"
    echo "这一步 DSTCamp 帮不了忙，各家云服务商的安全组设置完全不同，"
    echo "需要自己去控制台操作。"
    echo "=========================================="
}}

# 端口冲突检测必须在"服务是否在跑"之前对所有情况执行（服务已在跑但换了被占用的新端口时也要拦下）。
# 用 dstcamp-frps 的 MainPID 与端口监听者 PID 比对，区分"监听者就是自己"与"被其他服务占用"。
if command -v ss >/dev/null 2>&1; then
    frps_pid="$(systemctl show -p MainPID --value "$SERVICE_NAME" 2>/dev/null || echo 0)"
    # 坑：端口未占用时 grep 无匹配返回 1，会被 set -euo pipefail 静默终止脚本，所以加 || true
    port_pid="$(ss -Htlnp "sport = :${{BIND_PORT}}" 2>/dev/null | grep -oP 'pid=\\K[0-9]+' | head -1 || true)"
    if [ -n "$port_pid" ] && [ "$port_pid" != "$frps_pid" ]; then
        port_holder="$(ss -Htlnp "sport = :${{BIND_PORT}}" 2>/dev/null | grep -oP '\\(\\("\\K[^"]+' | head -1 || true)"
        echo "==> 检测到端口 ${{BIND_PORT}} 已经被其它服务（${{port_holder:-未知进程}}）占用，不是 ${{SERVICE_NAME}}。"
        echo "为避免打断现有服务，跳过安装/更新。如果这就是你自己之前配置的 frps，"
        echo "请确认它的鉴权 token 和 DSTCamp 里填写的一致；如果不是，需要先手动"
        echo "停掉占用该端口的服务，再重新运行本脚本。"
        # 退出码 3 表示主动放弃（什么都没做），remote_deploy.py 据此与部署成功(0)区分
        exit 3
    fi
fi

# 已部署且服务在运行：只更新配置并重启
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    echo "==> 检测到 ${{SERVICE_NAME}} 服务已经在运行，复用现有安装，只更新配置。"
    write_config_and_restart
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        print_success
        exit 0
    fi
    echo "用新配置重启失败，运行下面的命令查看具体原因：" >&2
    echo "  journalctl -u ${{SERVICE_NAME}} -n 50 --no-pager" >&2
    exit 1
fi

{fetch_frps_block}
cat > "$INSTALL_DIR/frps.toml" <<'FRPS_TOML_EOF'
{frps_toml}FRPS_TOML_EOF

cat > "/etc/systemd/system/${{SERVICE_NAME}}.service" <<SERVICE_EOF
[Unit]
Description=DSTCamp self-hosted frps
After=network.target

[Service]
Type=simple
ExecStart=${{INSTALL_DIR}}/frps -c ${{INSTALL_DIR}}/frps.toml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE_EOF

echo "==> 启动 ${{SERVICE_NAME}} 服务..."
systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
sleep 1

if systemctl is-active --quiet "$SERVICE_NAME"; then
    print_success
else
    echo "frps 启动失败，运行下面的命令查看具体原因：" >&2
    echo "  journalctl -u ${{SERVICE_NAME}} -n 50 --no-pager" >&2
    exit 1
fi
'''
