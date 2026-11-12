"""专服控制台的暂停与玩家操作：拼装发给专服的 Lua 命令，解析它们打印的结果。

均已对照本机游戏脚本（databundles/scripts.zip）核对：
- 暂停：TheNet:SetServerPaused(bool)（mainfunctions.lua 的 SetServerPaused 也是调它）。状态变化时引擎回调
  OnServerPauseDirty，打印 "Server Paused" / "Server Unpaused"；玩家开地图等自动暂停打印 "Server Autopaused"，
  不算手动暂停。真机日志里主世界暂停时洞穴同一秒也打印 "Server Paused"，暂停会同步到所有世界，只发给主世界即可。
  空服暂停（pause_when_empty）打印的是 "Sim paused"，与这里无关。
  坑：只有管理员在线时游戏才接受暂停，否则命令被静默忽略、不报错（真机实测：用别人的令牌开服、自己不在
  adminlist 时无效，加入管理员后有效）。令牌主人进服时自动是管理员（日志 admin=1，未在 adminlist 里）。
  复活、杀死不受此限制。管理员是否在线用 TheNet:GetClientTable() 查：每项带 admin 字段；专服自身也在表里且
  admin=1，要按 performance 字段排除（与本体 c_listplayers 的写法一致）。
- 复活：游戏没有 c_revive；c_godmode 对鬼魂是复活、对活人却是切换无敌，不能直接用。改为按 userid 找到玩家，
  鬼魂发 "respawnfromghost"、尸体发 "respawnfromcorpse"（与 c_godmode 相同，事件不带参数也能处理）。
- 杀死：游戏没有 c_kill；Health:Kill() 对无敌玩家无效，优先用能绕过无敌的 ForceKill()。
- 每个世界的 AllPlayers 只有在本世界的玩家，所以按玩家所在世界发命令。
"""

import re
from dataclasses import dataclass

# 只把格式明确的 userid 拼进 Lua，玩家名字只读不拼，杜绝注入
_USERID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{4,32}$")
_PAUSE_LINE_RE = re.compile(r"\bServer (Paused|Unpaused|Autopaused)\b")

# 输出行的标记。Lua 里拼接生成（"DSTCamp".."P:"），命令文本本身不含完整标记，
# 专服回显命令时也不会被误当成结果行
_MARK_PREFIX = "DSTCampP:"
_ADMIN_MARK_PREFIX = "DSTCampA:"
# 专服把执行的命令记成 RemoteCommandInput: "..."，DSTCamp 自动发的查询都含这段拼接
_QUERY_ECHO_MARK = '"DSTCamp".."'
# 主世界日志里玩家进服、离开的行：出现时重新查询管理员是否在线
_PLAYER_PRESENCE_RE = re.compile(r"\[ClientObject\] Initialized \(authenticated\)|\[Shard\] \(\S+\) disconnected from")

PLAYER_ALIVE = "alive"
PLAYER_GHOST = "ghost"
PLAYER_CORPSE = "corpse"
PLAYER_DEAD = "dead"
REVIVABLE_STATES = (PLAYER_GHOST, PLAYER_CORPSE)


@dataclass(frozen=True)
class OnlinePlayer:
    userid: str
    name: str
    prefab: str
    state: str  # PLAYER_* 之一；未知取值原样保留


def pause_command(paused: bool) -> str:
    return f"TheNet:SetServerPaused({'true' if paused else 'false'})"


def parse_pause_line(line: str) -> bool | None:
    """一行日志对应的手动暂停状态：Paused→True，Unpaused/Autopaused→False，无关的行→None。"""
    match = _PAUSE_LINE_RE.search(line)
    if match is None:
        return None
    return match.group(1) == "Paused"


def _check_userid(userid: str) -> str:
    if not _USERID_RE.fullmatch(userid):
        raise ValueError(f"非法的玩家 ID：{userid!r}")
    return userid


def _find_player(userid: str) -> str:
    """Lua 片段：在本世界按 userid 找玩家，结果放在局部变量 p（找不到为 nil）。"""
    return (f'local p for _,v in ipairs(AllPlayers) do if v.userid=="{_check_userid(userid)}" '
            "then p=v end end")


def list_players_command(nonce: str) -> str:
    """打印本世界全部玩家，每人一行（标记、userid、名字、角色、状态以 \\t 分隔），最后一行是 END。"""
    if not _NONCE_RE.fullmatch(nonce):
        raise ValueError(f"非法的标记：{nonce!r}")
    return (
        f'local m="DSTCamp".."P:{nonce}" '
        "for _,p in ipairs(AllPlayers) do "
        'local h=p.components.health '
        'local s=(p:HasTag("playerghost") and "ghost") or (p:HasTag("corpse") and "corpse") '
        'or ((h~=nil and h:IsDead()) and "dead") or "alive" '
        'local n=string.gsub(tostring(p.name),"[\\t\\r\\n]"," ") '
        'print(m.."\\t"..tostring(p.userid).."\\t"..n.."\\t"..tostring(p.prefab).."\\t"..s) '
        "end "
        'print(m.."\\tEND")'
    )


def parse_player_lines(lines, nonce: str) -> tuple[list[OnlinePlayer], bool]:
    """从日志行里取出 list_players_command(nonce) 的结果，返回（玩家列表, 是否已读到 END）。"""
    marker = f"{_MARK_PREFIX}{nonce}\t"
    players: list[OnlinePlayer] = []
    done = False
    for line in lines:
        index = line.find(marker)
        if index < 0:
            continue
        fields = line[index + len(marker):].rstrip("\r\n").split("\t")
        if fields[0] == "END":
            done = True
            break
        if len(fields) >= 4:
            players.append(OnlinePlayer(fields[0], fields[1], fields[2], fields[3].strip()))
    return players, done


def admin_query_command(nonce: str) -> str:
    """打印一行：标记、在线管理员数、在线玩家数（不含专服自身），以 \\t 分隔。"""
    if not _NONCE_RE.fullmatch(nonce):
        raise ValueError(f"非法的标记：{nonce!r}")
    return (
        f'local m="DSTCamp".."A:{nonce}" local a,n=0,0 '
        "for _,c in ipairs(TheNet:GetClientTable() or {}) do "
        "if c.performance==nil then n=n+1 if c.admin then a=a+1 end end end "
        'print(m.."\\t"..a.."\\t"..n)'
    )


def parse_admin_line(lines, nonce: str) -> tuple[int, int] | None:
    """取出 admin_query_command(nonce) 的结果（在线管理员数, 在线玩家数）；还没读到时返回 None。"""
    marker = f"{_ADMIN_MARK_PREFIX}{nonce}\t"
    for line in lines:
        index = line.find(marker)
        if index < 0:
            continue
        fields = line[index + len(marker):].split("\t")
        try:
            return int(fields[0]), int(fields[1])
        except (IndexError, ValueError):
            return None
    return None


def is_player_presence_line(line: str) -> bool:
    """主世界日志里是否是玩家进服/离开的行（据此重新查询管理员是否在线）。"""
    return _PLAYER_PRESENCE_RE.search(line) is not None


def is_query_output(line: str) -> bool:
    """是不是 DSTCamp 自动查询的结果行或命令回显（控制台不显示它们）。"""
    return _MARK_PREFIX in line or _ADMIN_MARK_PREFIX in line or _QUERY_ECHO_MARK in line


def revive_command(userid: str) -> str:
    """复活一名鬼魂或尸体状态的玩家；活着的玩家不受影响。"""
    return (f"{_find_player(userid)} if p then "
            'if p:HasTag("playerghost") then p:PushEvent("respawnfromghost") '
            'elseif p:HasTag("corpse") then p:PushEvent("respawnfromcorpse") end end')


def revive_all_command() -> str:
    """复活本世界所有鬼魂和尸体状态的玩家。"""
    return ("for _,p in ipairs(AllPlayers) do "
            'if p:HasTag("playerghost") then p:PushEvent("respawnfromghost") '
            'elseif p:HasTag("corpse") then p:PushEvent("respawnfromcorpse") end end')


def kill_command(userid: str) -> str:
    """杀死一名活着的玩家；开了无敌也有效（ForceKill），已是鬼魂的不受影响。"""
    return (f"{_find_player(userid)} "
            'if p and not p:HasTag("playerghost") and p.components.health~=nil '
            "and not p.components.health:IsDead() then local h=p.components.health "
            "if h.ForceKill then h:ForceKill() else h:Kill() end end")
