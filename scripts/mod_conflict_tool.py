"""维护同类 Mod 名单（dstools/features/mod/similar_mods.py）的开发工具，不随 EXE 打包。

直接运行（或双击 scripts/mod_conflict_tool.bat）会在本机起一个只监听 127.0.0.1 的小服务并打开浏览器：
勾选类别 →「搜索工坊」→ 逐个选「收录 / 排除」→「保存」，写回名单并按需同步常用mod集合的 modmain.lua。
上次的搜索结果存在 build/mod_conflict_review/，下次打开可直接「加载上次结果」接着审，不必重新搜索。

搜索走工坊浏览网页（不需要 API 密钥；官方 QueryFiles 接口无密钥返回 403），解析页面内嵌的
valve-ssr-data JSON。这不是正式接口，Steam 改版后可能要跟着改 _parse_browse_page。

  python scripts/mod_conflict_tool.py                         打开审核页面
  python scripts/mod_conflict_tool.py sync --modmain 路径 [--dry-run]   只同步常用mod集合
  python scripts/mod_conflict_tool.py check                   检查名单里的 Mod 是否还在工坊上
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dstools.features.mod import similar_mods  # noqa: E402
from dstools.features.mod.workshop_webapi import fetch_remote_items  # noqa: E402
from dstools.shared.ssl_context import default_ssl_context  # noqa: E402

DATA_PATH = ROOT / "dstools" / "features" / "mod" / "similar_mods.py"
WORK_DIR = ROOT / "build" / "mod_conflict_review"  # 本机状态：上次搜索结果、modmain 路径（不进 Git）
LAST_SEARCH = WORK_DIR / "last_search.json"
SETTINGS = WORK_DIR / "settings.json"
_BROWSE_URL = "https://steamcommunity.com/workshop/browse/"
_REQUEST_GAP = 1.0  # 两次网页请求之间的间隔（秒），别把工坊刷太快
_TIMEOUT = 20


# ── 名单读写 ─────────────────────────────────────────────────────────

def load_groups() -> list[dict]:
    importlib.reload(similar_mods)
    return [dict(group) for group in similar_mods.GROUPS]


def _render_ids(label: str, mapping: dict[str, str]) -> list[str]:
    lines = [f"        {label!r}: {{"]
    lines += [f"            {wid!r}: {title!r}," for wid, title in mapping.items()]
    lines.append("        },")
    return lines


def render_module(groups: list[dict]) -> str:
    """整份生成 similar_mods.py；格式固定，便于看 diff。"""
    lines = [
        '"""同类 Mod 名单：功能相同、不该同时启用的 Mod，供 duplicates.py 检测。',
        "",
        "由 scripts/mod_conflict_tool.py 生成和维护（会整份重写，手改的注释会丢）。字段：",
        "  key            类别键（也是工具命令行里的名字）",
        "  zh / en        类别显示名",
        "  search         在创意工坊搜索候选时用的关键词",
        "  note           备注（只给人看）",
        "  mods           收录的 Mod：{工坊 ID: 标题}，同时启用其中两个以上就提示",
        "  excluded       审核时排除的 Mod，以后搜索不再列出",
        "  cms_key        常用mod集合 modmain.lua 里对应的 safeImport 配置键；None 表示集合里没有这个功能",
        "  cms_extra      只写进常用mod集合冲突列表、DSTCamp 不提示的 Mod（如多功能合集 Lag Remover）",
        '"""',
        "",
        "GROUPS = [",
    ]
    for group in groups:
        lines.append("    {")
        for field in ("key", "zh", "en"):
            lines.append(f"        {field!r}: {group[field]!r},")
        lines.append(f"        'search': {list(group.get('search') or [])!r},")
        lines.append(f"        'note': {group.get('note', '')!r},")
        lines += _render_ids("mods", group.get("mods") or {})
        lines += _render_ids("excluded", group.get("excluded") or {})
        lines.append(f"        'cms_key': {group.get('cms_key')!r},")
        lines += _render_ids("cms_extra", group.get("cms_extra") or {})
        lines.append("    },")
    lines.append("]")
    return "\n".join(lines) + "\n"


def save_groups(groups: list[dict]) -> None:
    owner: dict[str, str] = {}
    for group in groups:
        for wid in group["mods"]:
            if wid in owner:
                raise ValueError(f"工坊 ID {wid} 同时在 {owner[wid]} 和 {group['key']} 里，一个 ID 只能放进一组")
            owner[wid] = group["key"]
    with open(DATA_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(render_module(groups))


def find_group(groups: list[dict], key: str) -> dict:
    for group in groups:
        if group["key"] == key:
            return group
    raise ValueError(f"没有类别 {key}")


def _known_ids(groups: list[dict]) -> set[str]:
    return {wid for group in groups for wid in (*group["mods"], *group["excluded"], *group["cms_extra"])}


# ── 工坊搜索 ─────────────────────────────────────────────────────────

def _parse_browse_page(page: str) -> tuple[list[dict], int]:
    """返回 (本页结果, 总页数)。结果在 valve-ssr-data 里（2026-11 实测在 renderContext.queryData，
    值是又一层 JSON 字符串）；整体递归查找带 results 和 total_pages 的对象，不依赖具体位置。"""
    match = re.search(r'<script type="application/json" id="valve-ssr-data"[^>]*>(.*?)</script>', page, re.S)
    if not match:
        raise RuntimeError("页面里找不到 valve-ssr-data，工坊网页可能改版了")
    found = _find_results(json.loads(match.group(1)))
    if found is None:
        raise RuntimeError("valve-ssr-data 里找不到搜索结果，工坊网页可能改版了")
    return found


def _find_results(node):
    if isinstance(node, str):
        if "total_pages" not in node or node[:1] not in "{[":
            return None
        try:
            node = json.loads(node)
        except ValueError:
            return None
    if isinstance(node, dict):
        if isinstance(node.get("results"), list) and "total_pages" in node:
            return node["results"], int(node.get("total_pages") or 0)
        values = node.values()
    elif isinstance(node, list):
        values = node
    else:
        return None
    for value in values:
        found = _find_results(value)
        if found is not None:
            return found
    return None


def search_workshop(text: str, page: int) -> tuple[list[dict], int]:
    query = urllib.parse.urlencode({"appid": "322330", "searchtext": text, "browsesort": "textsearch",
                                    "section": "readytouseitems", "p": str(page)})
    request = urllib.request.Request(f"{_BROWSE_URL}?{query}", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT, context=default_ssl_context()) as response:
        return _parse_browse_page(response.read().decode("utf-8", "replace"))


def _title_hits(title: str, terms: list[str]) -> bool:
    """标题是否含某个关键词（不分大小写、忽略空白和撇号；多词关键词要求每个词都出现）。"""
    def norm(text: str) -> str:
        return re.sub(r"[\s'’]+", "", text).casefold()
    plain = norm(title)
    return any(all(norm(word) in plain for word in term.split()) for term in terms)


def _candidate(item: dict) -> dict:
    tags = [str(tag.get("tag", "")) for tag in item.get("tags") or [] if isinstance(tag, dict)]
    return {
        "id": str(item.get("publishedfileid")),
        "title": str(item.get("title") or ""),
        "desc": str(item.get("short_description") or "")[:300],
        "subs": int(item.get("subscriptions") or 0),
        "updated": time.strftime("%Y-%m-%d", time.localtime(int(item.get("time_updated") or 0))),
        "preview": str(item.get("preview_url") or ""),
        "tags": [tag for tag in tags if not tag.startswith("version:")],
    }


def collect_candidates(keys: list[str], pages: int, min_subs: int, log) -> dict[str, list[dict]]:
    """按类别搜索工坊，返回 {类别键: 候选}；已收录、已排除的不列出。标题含关键词的排前面，其余按相关度。"""
    groups = load_groups()
    known = _known_ids(groups)
    result: dict[str, list[dict]] = {}
    for key in keys:
        group = find_group(groups, key)
        found: dict[str, dict] = {}
        for text in group.get("search") or []:
            for page in range(1, pages + 1):
                log(f"[{group['zh']}] 搜索「{text}」第 {page} 页")
                try:
                    items, total_pages = search_workshop(text, page)
                except Exception as exc:  # 网络或解析失败：报出来，继续下一个词
                    log(f"[{group['zh']}] 搜索「{text}」第 {page} 页失败：{exc}")
                    break
                for rank, item in enumerate(items):
                    cand = _candidate(item)
                    if cand["id"] not in known and cand["subs"] >= min_subs and cand["id"] not in found:
                        cand["rank"] = (page - 1) * len(items) + rank  # 工坊返回的相关度名次
                        cand["hit"] = _title_hits(cand["title"], group["search"])
                        found[cand["id"]] = cand
                time.sleep(_REQUEST_GAP)
                if page >= total_pages:
                    break
        cands = sorted(found.values(), key=lambda c: (not c["hit"], c["rank"]))
        log(f"[{group['zh']}] 新候选 {len(cands)} 个，标题含关键词 {sum(1 for c in cands if c['hit'])} 个")
        result[key] = cands
    return result


# ── 写回与同步 ───────────────────────────────────────────────────────

def apply_decisions(decisions: dict, log) -> None:
    """decisions 为 {类别键: {"include": {ID: 标题}, "exclude": {ID: 标题}}}。"""
    groups = load_groups()
    for key, choice in decisions.items():
        group = find_group(groups, key)
        for wid, title in (choice.get("include") or {}).items():
            group["excluded"].pop(wid, None)
            group["mods"][wid] = title
        for wid, title in (choice.get("exclude") or {}).items():
            if wid not in group["mods"]:
                group["excluded"][wid] = title
        log(f"[{group['zh']}] 收录 {len(choice.get('include') or {})}，排除 {len(choice.get('exclude') or {})}")
    save_groups(groups)
    log(f"已写回 {DATA_PATH.relative_to(ROOT)}")


def add_group(key: str, zh: str, en: str, search: list[str], cms_key: str | None) -> None:
    groups = load_groups()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
        raise ValueError("类别键只能用小写字母、数字和下划线，并以字母开头")
    if any(group["key"] == key for group in groups):
        raise ValueError(f"类别 {key} 已存在")
    if not (zh and en and search):
        raise ValueError("中文名、英文名和搜索关键词都要填")
    groups.append({"key": key, "zh": zh, "en": en, "search": search, "note": "",
                   "mods": {}, "excluded": {}, "cms_key": cms_key or None, "cms_extra": {}})
    save_groups(groups)


_SAFE_IMPORT_RE = r'(safeImport\(\s*"{key}"\s*,\s*"[^"]*"\s*,\s*"[^"]*")((?:\s*,\s*"\d+")*)(\s*\))'


def sync_modmain(groups: list[dict], modmain: Path, dry_run: bool, log=print) -> None:
    """按名单改写常用mod集合里对应功能的冲突 ID：保留原有顺序，补上新增的，去掉名单里没有的。
    只动 cms_key 不为空的类别；按字节读写，保留原文件的 CRLF。"""
    text = modmain.read_bytes().decode("utf-8")
    changed = False
    for group in groups:
        key = group.get("cms_key")
        if not key:
            continue
        matches = list(re.finditer(_SAFE_IMPORT_RE.format(key=re.escape(key)), text))
        if len(matches) != 1:
            log(f"[{group['zh']}] modmain 里 safeImport(\"{key}\", ...) 出现 {len(matches)} 次，跳过")
            continue
        match = matches[0]
        current = re.findall(r'"(\d+)"', match.group(2))
        target = [*group["mods"], *[wid for wid in group["cms_extra"] if wid not in group["mods"]]]
        merged = [wid for wid in current if wid in target] + [wid for wid in target if wid not in current]
        if merged == current:
            continue
        added = [wid for wid in merged if wid not in current]
        removed = [wid for wid in current if wid not in merged]
        log(f"[{group['zh']}] -> {key}：新增 {added or '无'}，删除 {removed or '无'}")
        text = text[:match.start(2)] + "".join(f',"{wid}"' for wid in merged) + text[match.end(2):]
        changed = True
    if not changed:
        log("常用mod集合无需改动")
    elif dry_run:
        log("（预演，未写入）")
    else:
        modmain.write_bytes(text.encode("utf-8"))
        log(f"已写入 {modmain}，发布前记得用 Lua 检查语法")


# ── 本机状态 ─────────────────────────────────────────────────────────

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _pending_review() -> dict[str, list[dict]]:
    """上次搜索结果里还没审核的候选（已收录、已排除的去掉）。"""
    last = _read_json(LAST_SEARCH, {})
    known = _known_ids(load_groups())
    return {key: [c for c in cands if c["id"] not in known] for key, cands in last.items()}


# ── 本地网页服务 ─────────────────────────────────────────────────────

class _Job:
    """后台搜索任务；同一时间只跑一个。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.log: list[str] = []

    def add(self, line: str) -> None:
        with self.lock:
            self.log.append(line)
        print(line)

    def start(self, keys: list[str], pages: int, min_subs: int) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.log = []

        def work():
            try:
                found = collect_candidates(keys, pages, min_subs, self.add)
                last = _read_json(LAST_SEARCH, {})
                last.update(found)  # 只覆盖这次搜过的类别，其它类别上次的结果保留
                _write_json(LAST_SEARCH, last)
                self.add("搜索完成")
            except Exception as exc:
                self.add(f"搜索出错：{exc}")
            finally:
                with self.lock:
                    self.running = False

        threading.Thread(target=work, daemon=True).start()
        return True


_JOB = _Job()


def _groups_summary() -> list[dict]:
    return [{"key": g["key"], "zh": g["zh"], "en": g["en"], "search": g["search"], "cms_key": g["cms_key"],
             "mods": g["mods"]} for g in load_groups()]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # 不刷访问日志
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            with _JOB.lock:
                running, log = _JOB.running, list(_JOB.log)
            self._json({"groups": _groups_summary(), "running": running, "log": log,
                        "has_last": LAST_SEARCH.is_file(), "settings": _read_json(SETTINGS, {})})
        elif path == "/api/review":
            self._json(_pending_review())
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        # 只接受本页面脚本发来的请求（自定义请求头跨站表单带不了），防止别的网页借浏览器改名单
        if self.headers.get("X-Tool") != "1":
            self._send(403, b"forbidden", "text/plain")
            return
        path = urllib.parse.urlparse(self.path).path
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if path == "/api/search":
                keys = [k for k in body.get("keys") or [] if isinstance(k, str)]
                if not keys:
                    raise ValueError("先勾选要搜索的类别")
                if not _JOB.start(keys, int(body.get("pages") or 2), int(body.get("min_subs") or 0)):
                    raise ValueError("上一次搜索还没结束")
                self._json({"ok": True})
            elif path == "/api/save":
                lines: list[str] = []
                apply_decisions(body.get("decisions") or {}, lines.append)
                modmain = str(body.get("modmain") or "").strip()
                _write_json(SETTINGS, {"modmain": modmain})
                if modmain:
                    if not Path(modmain).is_file():
                        raise ValueError(f"找不到 {modmain}，名单已保存，常用mod集合没有同步")
                    sync_modmain(load_groups(), Path(modmain), dry_run=False, log=lines.append)
                self._json({"ok": True, "log": lines})
            elif path == "/api/add-group":
                search = [s.strip() for s in str(body.get("search") or "").split(",") if s.strip()]
                add_group(str(body.get("key") or "").strip(), str(body.get("zh") or "").strip(),
                          str(body.get("en") or "").strip(), search, str(body.get("cms_key") or "").strip())
                self._json({"ok": True})
            elif path == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._send(404, b"not found", "text/plain")
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, 400)


_PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>同类 Mod 审核</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2129;--muted:#6b7280;--line:#e3e6ea;--accent:#2f6fde;--ok:#1f9d55;--no:#c0392b}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--card:#20242a;--text:#e6e8eb;--muted:#9aa1ab;--line:#30353d;--accent:#5b8ff0;--ok:#3cc47c;--no:#e0675a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Microsoft YaHei",system-ui,sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
header h1{font-size:18px;margin:0 auto 0 0}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:6px;padding:5px 12px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button:disabled{opacity:.5;cursor:default}
input[type=text],input[type=number]{font:inherit;color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:4px 8px}
main{max-width:1100px;margin:0 auto;padding:8px 16px 48px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin-top:12px}
.panel h3{margin:0 0 8px;font-size:15px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:8px}
.chips{display:flex;flex-wrap:wrap;gap:6px 14px}.chips label{white-space:nowrap;cursor:pointer}
#log{white-space:pre-wrap;font-size:12px;color:var(--muted);max-height:140px;overflow:auto;margin-top:8px}
section{margin-top:20px}h2{font-size:16px;margin:0 0 4px}.known{color:var(--muted);font-size:12px;margin-bottom:8px}
.item{display:flex;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;margin:8px 0}
.item.include{border-color:var(--ok);box-shadow:inset 4px 0 var(--ok)}.item.exclude{opacity:.55;box-shadow:inset 4px 0 var(--no)}
.item img{width:72px;height:72px;object-fit:cover;border-radius:6px;flex:none;background:var(--line)}
.body{flex:1;min-width:0}.title a{color:var(--text);font-weight:600;text-decoration:none}.title a:hover{color:var(--accent)}
.meta{color:var(--muted);font-size:12px}.desc{font-size:12px;color:var(--muted);margin-top:4px;overflow-wrap:anywhere}
.choice{display:flex;flex-direction:column;gap:4px;flex:none}.choice label{white-space:nowrap;cursor:pointer}
.badge{font-size:11px;color:var(--ok);border:1px solid var(--ok);border-radius:4px;padding:0 4px;margin-left:6px}
body.hitonly .item.nohit{display:none}.empty{color:var(--muted)}.err{color:var(--no)}
details summary{cursor:pointer;color:var(--muted)}
</style></head><body class="hitonly">
<header><h1>同类 Mod 审核</h1>
<label><input type="checkbox" id="hitonly" checked> 只看标题含关键词</label>
<span id="count" class="meta"></span>
<button id="save" class="primary">保存选择</button><button id="quit">关闭工具</button></header>
<main>
<div class="panel"><h3>搜索创意工坊</h3>
  <div class="row"><button id="all">全选</button><button id="none">全不选</button></div>
  <div class="chips" id="chips" style="margin-top:8px"></div>
  <div class="row">每个关键词翻 <input type="number" id="pages" value="2" min="1" max="10" style="width:60px"> 页（每页 30 个）
    · 订阅数至少 <input type="number" id="minsubs" value="500" min="0" style="width:90px">
    <button id="search" class="primary">搜索工坊</button><button id="loadlast">加载上次结果</button></div>
  <div id="log"></div>
</div>
<div class="panel"><h3>保存设置</h3>
  <div class="row">常用mod集合 modmain.lua：<input type="text" id="modmain" style="flex:1;min-width:280px" placeholder="留空则只保存名单、不同步集合 Mod"></div>
  <details style="margin-top:8px"><summary>新建类别</summary>
    <div class="row">类别键 <input type="text" id="g_key" placeholder="如 auto_fish" style="width:130px">
      中文名 <input type="text" id="g_zh" style="width:120px"> 英文名 <input type="text" id="g_en" style="width:140px"></div>
    <div class="row">搜索关键词（逗号分隔）<input type="text" id="g_search" style="flex:1;min-width:220px" placeholder="auto fish, 自动钓鱼">
      集合 Mod 配置键 <input type="text" id="g_cms" style="width:130px" placeholder="没有就留空">
      <button id="g_add">新建</button></div>
  </details>
</div>
<div id="results"></div>
</main>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let GROUPS = [], wasRunning = false;
async function api(path, body) {
  const opt = body === undefined ? {} : {method: "POST", headers: {"X-Tool": "1", "Content-Type": "application/json"}, body: JSON.stringify(body)};
  const r = await fetch(path, opt);
  const data = await r.json();
  if (!r.ok || data.ok === false) throw new Error(data.error || r.status);
  return data;
}
function renderChips() {
  const checked = new Set([...document.querySelectorAll("#chips input:checked")].map(i => i.value));
  $("chips").innerHTML = GROUPS.map(g => `<label title="${esc(g.search.join(", "))}"><input type="checkbox" value="${esc(g.key)}" ${checked.has(g.key) ? "checked" : ""}> ${esc(g.zh)} <span class="meta">(${Object.keys(g.mods).length})</span></label>`).join("");
}
async function refresh(first) {
  const s = await api("/api/state");
  GROUPS = s.groups; renderChips();
  if (first && s.settings.modmain) $("modmain").value = s.settings.modmain;
  $("log").textContent = s.log.join("\n"); $("log").scrollTop = 1e9;
  $("search").disabled = s.running; $("loadlast").disabled = s.running || !s.has_last;
  if (wasRunning && !s.running) await loadReview();
  wasRunning = s.running;
  if (s.running) setTimeout(refresh, 1000);
}
async function loadReview() {
  const review = await api("/api/review");
  const box = $("results"); box.innerHTML = "";
  for (const g of GROUPS) {
    const cands = review[g.key];
    if (!cands) continue;
    const hits = cands.filter(c => c.hit).length;
    const sec = document.createElement("section");
    sec.innerHTML = `<h2>${esc(g.zh)} <span class="meta">候选 ${cands.length}，标题含关键词 ${hits}</span></h2>
      <div class="known">已收录：${esc(Object.values(g.mods).join("、") || "（暂无）")}</div>` + (cands.length ? "" : `<div class="empty">没有待审核的候选</div>`);
    for (const c of cands) {
      const div = document.createElement("div");
      div.className = "item " + (c.hit ? "hit" : "nohit");
      div.dataset.group = g.key; div.dataset.id = c.id; div.dataset.title = c.title;
      const name = `${g.key}-${c.id}`;
      div.innerHTML = `<img loading="lazy" src="${esc(c.preview)}" alt="">
        <div class="body"><div class="title"><a href="https://steamcommunity.com/sharedfiles/filedetails/?id=${c.id}" target="_blank">${esc(c.title)}</a>${c.hit ? '<span class="badge">标题含关键词</span>' : ""}</div>
        <div class="meta">ID ${c.id} · 订阅 ${c.subs.toLocaleString()} · 更新 ${c.updated} · ${esc(c.tags.join(", "))}</div>
        <div class="desc">${esc(c.desc)}</div></div>
        <div class="choice"><label><input type="radio" name="${name}" value="include"> 收录</label>
        <label><input type="radio" name="${name}" value="exclude"> 排除</label>
        <label><input type="radio" name="${name}" value="" checked> 待定</label></div>`;
      div.querySelectorAll("input").forEach(r => r.onchange = () => {
        div.classList.toggle("include", r.value === "include");
        div.classList.toggle("exclude", r.value === "exclude");
        updateCount();
      });
      sec.appendChild(div);
    }
    box.appendChild(sec);
  }
  if (!box.innerHTML) box.innerHTML = `<p class="empty">还没有搜索结果</p>`;
  updateCount();
}
function decisions() {
  const out = {};
  document.querySelectorAll(".item").forEach(div => {
    const v = div.querySelector("input:checked").value;
    if (!v) return;
    const g = out[div.dataset.group] ??= {include: {}, exclude: {}};
    g[v][div.dataset.id] = div.dataset.title;
  });
  return out;
}
function updateCount() {
  let inc = 0, exc = 0;
  for (const g of Object.values(decisions())) { inc += Object.keys(g.include).length; exc += Object.keys(g.exclude).length; }
  $("count").textContent = `已选 收录 ${inc} · 排除 ${exc}`;
}
const say = (text, err) => { $("log").innerHTML = `<span class="${err ? "err" : ""}">${esc(text)}</span>`; };
$("hitonly").onchange = e => document.body.classList.toggle("hitonly", e.target.checked);
$("all").onclick = () => document.querySelectorAll("#chips input").forEach(i => i.checked = true);
$("none").onclick = () => document.querySelectorAll("#chips input").forEach(i => i.checked = false);
$("search").onclick = async () => {
  const keys = [...document.querySelectorAll("#chips input:checked")].map(i => i.value);
  try { await api("/api/search", {keys, pages: +$("pages").value, min_subs: +$("minsubs").value}); wasRunning = true; refresh(); }
  catch (e) { say(e.message, true); }
};
$("loadlast").onclick = loadReview;
$("save").onclick = async () => {
  const d = decisions();
  if (!Object.keys(d).length) { say("还没有选择任何候选", true); return; }
  try {
    const r = await api("/api/save", {decisions: d, modmain: $("modmain").value});
    await refresh(); await loadReview(); say(r.log.join("\n"));
  } catch (e) { say(e.message, true); await refresh(); await loadReview(); }
};
$("g_add").onclick = async () => {
  try {
    await api("/api/add-group", {key: $("g_key").value, zh: $("g_zh").value, en: $("g_en").value, search: $("g_search").value, cms_key: $("g_cms").value});
    say(`已新建类别 ${$("g_zh").value}，勾选它后点「搜索工坊」`); await refresh();
  } catch (e) { say(e.message, true); }
};
$("quit").onclick = async () => { await api("/api/quit", {}); document.body.innerHTML = "<main><p>工具已关闭，可以关掉这个标签页。</p></main>"; };
refresh(true).then(() => { if (!$("loadlast").disabled) loadReview(); });
</script></body></html>
"""


def serve(open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"同类 Mod 审核：{url}（在页面上点「关闭工具」或按 Ctrl+C 退出）")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def cmd_check(_args) -> None:
    groups = load_groups()
    ids = [wid for group in groups for wid in (*group["mods"], *group["cms_extra"])]
    remote = fetch_remote_items(ids)
    missing = [(group["key"], wid, title) for group in groups
               for wid, title in (*group["mods"].items(), *group["cms_extra"].items()) if wid not in remote]
    print(f"共 {len(set(ids))} 个，工坊上查不到 {len(missing)} 个")
    for key, wid, title in missing:
        print(f"  [{key}] {wid} {title}")


def main() -> None:
    parser = argparse.ArgumentParser(description="维护同类 Mod 名单；不带参数时打开审核页面")
    parser.add_argument("--no-browser", action="store_true", help="只起服务，不自动打开浏览器")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("sync", help="只同步常用mod集合")
    p.add_argument("--modmain", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=lambda a: sync_modmain(load_groups(), Path(a.modmain), dry_run=a.dry_run))
    p = sub.add_parser("check", help="检查名单里的 Mod 是否还在工坊上")
    p.set_defaults(func=cmd_check)
    args = parser.parse_args()
    if args.cmd:
        args.func(args)
    else:
        serve(open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
