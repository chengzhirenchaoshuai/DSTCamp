"""专服控制台的暂停与玩家操作：拼装发给专服的 Lua 命令，解析它们打印的结果。

均已对照本机游戏脚本（databundles/scripts.zip）核对：
- 暂停：TheNet:SetServerPaused(bool)（mainfunctions.lua 的 SetServerPaused 也是调它）。状态变化时引擎回调
  OnServerPauseDirty，打印 "Server Paused" / "Server Unpaused"；玩家开地图等自动暂停打印 "Server Autopaused"，
  不算手动暂停。真机日志里主世界暂停时洞穴同一秒也打印 "Server Paused"，暂停会同步到所有世界，只发给主世界即可。
  空服暂停（pause_when_empty）打印的是 "Sim paused"，与这里无关。
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


def is_query_output(line: str) -> bool:
    """是不是 list_players_command 打印的结果行（控制台不显示它们）。"""
    return _MARK_PREFIX in line


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
