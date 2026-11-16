"""维护同类 Mod 名单（dstools/features/mod/similar_mods.py）的开发工具，不随 EXE 打包。

直接运行（或双击 scripts/mod_conflict_tool.bat）会在本机起一个只监听 127.0.0.1 的小服务并打开浏览器：
勾选类别 →「搜索工坊」→ 逐个选「收录 / 排除 / 待定」（可按状态、类别筛选后批量设置；已收录的也能移出）
→「保存改动」，写回名单并按需同步常用mod集合的 modmain.lua。
页面列出各类别已收录、已排除的 Mod 和上次搜索里还没审的候选（存在 build/mod_conflict_review/），不必重新搜索。

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
        "  cms_key        常用mod集合 modmain.lua 里对应的 safeImport 配置键；只有它不为 None 的类别才会同步到集合 Mod。",
        "                 集合 Mod 的功能暂不增删，这些键按集合现有功能填好后不再改；新建类别一律为 None",
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
    """decisions 为 {类别键: {工坊 ID: {"to": "include" | "exclude" | "pending", "title": 标题}}}。
    include 收录；exclude 移出收录并记为排除（以后搜索不再列出）；pending 从收录和排除里都移出。"""
    groups = load_groups()
    for key, changes in decisions.items():
        group = find_group(groups, key)
        counts = {"include": 0, "exclude": 0, "pending": 0}
        for wid, change in changes.items():
            to, title = change.get("to"), str(change.get("title") or "")
            if to not in counts or not str(wid).isdigit():
                raise ValueError(f"无效的选择：{key} {wid} {to}")
            title = title or group["mods"].get(wid) or group["excluded"].get(wid) or ""
            group["mods"].pop(wid, None)
            group["excluded"].pop(wid, None)
            if to == "include":
                group["mods"][wid] = title
            elif to == "exclude":
                group["excluded"][wid] = title
            counts[to] += 1
        log(f"[{group['zh']}] 收录 {counts['include']}，排除 {counts['exclude']}，移出 {counts['pending']}")
    save_groups(groups)
    log(f"已写回 {DATA_PATH.relative_to(ROOT)}")


def add_group(key: str, zh: str, en: str, search: list[str]) -> None:
    """新建类别。cms_key 固定为 None：常用mod集合的功能暂不增删，新类别只给 DSTCamp 检测用、不会同步过去。"""
    groups = load_groups()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
        raise ValueError("类别键只能用小写字母、数字和下划线，并以字母开头")
    if any(group["key"] == key for group in groups):
        raise ValueError(f"类别 {key} 已存在")
    if not (zh and en and search):
        raise ValueError("中文名、英文名和搜索关键词都要填")
    groups.append({"key": key, "zh": zh, "en": en, "search": search, "note": "",
                   "mods": {}, "excluded": {}, "cms_key": None, "cms_extra": {}})
    save_groups(groups)


_SAFE_IMPORT_RE = r'(safeImport\(\s*"{key}"\s*,\s*"[^"]*"\s*,\s*"[^"]*")((?:\s*,\s*"\d+")*)(\s*\))'


def sync_modmain(groups: list[dict], modmain: Path, dry_run: bool, log=print) -> None:
    """按名单改写常用mod集合里对应功能的冲突 ID：保留原有顺序，补上新增的，去掉名单里没有的。

    只同步特定名单：cms_key 不为空的类别，即常用mod集合现有功能（safeImport 配置键）对应的类别。
    集合 Mod 的功能暂不增删，所以 cms_key 只在建名单时按集合现有功能填好，工具不提供修改；
    新建的类别和集合里没有的功能（如物品信息显示）只给 DSTCamp 检测用，不会写进集合 Mod。
    按字节读写，保留原文件的 CRLF。"""
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


_DETAILS: dict[str, dict] = {}  # 已收录/已排除 Mod 的工坊详情，本次运行内缓存
_DETAILS_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"


def _fetch_details(ids: list[str]) -> None:
    """用 Steam 公开接口批量补齐图标、订阅数等；失败时只显示名单里记的标题。"""
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        form = {"itemcount": str(len(batch))}
        form.update({f"publishedfileids[{n}]": wid for n, wid in enumerate(batch)})
        request = urllib.request.Request(_DETAILS_URL, data=urllib.parse.urlencode(form).encode("ascii"))
        with urllib.request.urlopen(request, timeout=_TIMEOUT, context=default_ssl_context()) as response:
            payload = json.loads(response.read().decode("utf-8"))
        for item in payload.get("response", {}).get("publishedfiledetails", []):
            if item.get("result") == 1:
                cand = _candidate(item)
                cand["desc"] = re.sub(r"\[[^\]]*\]", "", str(item.get("file_description") or ""))[:300]
                _DETAILS[cand["id"]] = cand


def review_items(log=print) -> dict[str, list[dict]]:
    """每个类别的全部条目：已收录、已排除，以及上次搜索结果里还没审核的候选（status 区分）。"""
    groups = load_groups()
    need = [wid for g in groups for wid in (*g["mods"], *g["excluded"]) if wid not in _DETAILS]
    if need:
        try:
            _fetch_details(need)
        except Exception as exc:
            log(f"查询已收录 Mod 的工坊详情失败（只显示标题）：{exc}")
    last = _read_json(LAST_SEARCH, {})
    known = _known_ids(groups)
    out: dict[str, list[dict]] = {}
    for g in groups:
        items = []
        for status, mapping in (("included", g["mods"]), ("excluded", g["excluded"])):
            for wid, title in mapping.items():
                item = dict(_DETAILS.get(wid) or {"id": wid, "desc": "", "subs": 0, "updated": "", "preview": "",
                                                   "tags": []})
                item.update(title=title or item.get("title", ""), status=status, hit=True)
                items.append(item)
        items += [{**c, "status": "candidate"} for c in last.get(g["key"], []) if c["id"] not in known]
        out[g["key"]] = items
    return out


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


def _sync_from_settings(settings: dict, dry_run: bool, log) -> None:
    modmain = str(settings.get("modmain") or "")
    if not modmain:
        raise ValueError("先在「设置」里填写常用mod集合 modmain.lua 的路径并保存")
    if not Path(modmain).is_file():
        raise ValueError(f"找不到 {modmain}")
    sync_modmain(load_groups(), Path(modmain), dry_run=dry_run, log=log)


def _groups_summary() -> list[dict]:
    return [{"key": g["key"], "zh": g["zh"], "en": g["en"], "search": g["search"], "cms_key": g["cms_key"],
             "mods": g["mods"], "cms_extra": g["cms_extra"]} for g in load_groups()]


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
                        "settings": _read_json(SETTINGS, {})})
        elif path == "/api/review":
            self._json(review_items(_JOB.add))
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
                settings = _read_json(SETTINGS, {})
                if settings.get("auto_sync") and settings.get("modmain"):
                    _sync_from_settings(settings, dry_run=False, log=lines.append)
                else:
                    lines.append("未开启自动同步，常用mod集合没有改动（可在「设置」页签同步）")
                self._json({"ok": True, "log": lines})
            elif path == "/api/settings":
                modmain = str(body.get("modmain") or "").strip()
                if modmain and not Path(modmain).is_file():
                    raise ValueError(f"找不到 {modmain}")
                _write_json(SETTINGS, {"modmain": modmain, "auto_sync": bool(body.get("auto_sync"))})
                self._json({"ok": True})
            elif path == "/api/sync":
                lines = []
                _sync_from_settings(_read_json(SETTINGS, {}), dry_run=bool(body.get("dry_run")), log=lines.append)
                self._json({"ok": True, "log": lines})
            elif path == "/api/add-group":
                search = [s.strip() for s in re.split(r"[,，]", str(body.get("search") or "")) if s.strip()]
                add_group(str(body.get("key") or "").strip(), str(body.get("zh") or "").strip(),
                          str(body.get("en") or "").strip(), search)
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
:root{
  --bg:#f3f5f8;--card:#fff;--card2:#f8f9fb;--text:#1c2330;--muted:#6b7482;--line:#e2e6ec;
  --accent:#3366e0;--accent-soft:#e8eefc;--ok:#18935a;--ok-soft:#e3f5ec;--no:#c8443a;--no-soft:#fbe9e7;
  --warn:#c58a00;--mark:#ffe58a;--shadow:0 1px 2px rgba(20,30,50,.06),0 2px 8px rgba(20,30,50,.04)}
@media (prefers-color-scheme:dark){:root{
  --bg:#14171c;--card:#1d2128;--card2:#232832;--text:#e5e8ec;--muted:#98a0ab;--line:#2f3540;
  --accent:#6a92f2;--accent-soft:#25304a;--ok:#41c483;--ok-soft:#1d3329;--no:#e6776c;--no-soft:#3a2321;
  --warn:#e3ad3a;--mark:#6b5a12;--shadow:none}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.55 "Microsoft YaHei UI","Microsoft YaHei",system-ui,sans-serif}
button{font:inherit;color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:5px 14px;cursor:pointer;transition:background .15s,border-color .15s}
button:hover{border-color:var(--accent)}button:disabled{opacity:.5;cursor:default}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button.primary:hover{filter:brightness(1.08)}
button.ghost{background:transparent;border-color:transparent;color:var(--muted)}button.ghost:hover{color:var(--text);border-color:var(--line)}
input[type=text],input[type=number],select{font:inherit;color:var(--text);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:5px 10px}
input:focus-visible,select:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}

/* 顶栏 */
.topbar{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line);box-shadow:var(--shadow)}
.topbar-in{max-width:1180px;margin:0 auto;padding:10px 20px;display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.brand{font-size:17px;font-weight:700;letter-spacing:.5px}
.tabs{display:flex;gap:4px;background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:3px}
.tabs button{border:none;background:transparent;padding:4px 16px;border-radius:7px;color:var(--muted)}
.tabs button.on{background:var(--card);color:var(--text);box-shadow:var(--shadow);font-weight:600}
.spacer{flex:1}.changes{font-size:13px;color:var(--muted)}.changes b{color:var(--warn)}

main{max-width:1180px;margin:0 auto;padding:16px 20px 60px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:14px;box-shadow:var(--shadow)}
.card h3{margin:0 0 12px;font-size:15px;display:flex;align-items:center;gap:8px}
.hint{color:var(--muted);font-size:12.5px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.row+.row{margin-top:10px}

/* 类别胶囊 */
.pills{display:flex;flex-wrap:wrap;gap:8px}
.pill{position:relative}.pill input{position:absolute;opacity:0;pointer-events:none}
.pill span{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border:1px solid var(--line);border-radius:999px;background:var(--card2);cursor:pointer;user-select:none;transition:all .15s}
.pill span:hover{border-color:var(--accent)}
.pill input:checked+span{background:var(--accent-soft);border-color:var(--accent);color:var(--accent);font-weight:600}
.pill input:focus-visible+span{outline:2px solid var(--accent);outline-offset:1px}
.num{font-size:11px;min-width:18px;text-align:center;padding:0 5px;border-radius:999px;background:var(--line);color:var(--muted);font-weight:400}
#log{white-space:pre-wrap;font:12px/1.6 Consolas,"Microsoft YaHei",monospace;color:var(--muted);max-height:150px;overflow:auto;margin-top:12px;background:var(--card2);border-radius:8px;padding:8px 10px}
#log:empty{display:none}.err{color:var(--no)}

/* 筛选栏 */
.filter{position:sticky;top:58px;z-index:4;display:flex;align-items:center;gap:8px 16px;flex-wrap:wrap;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 16px;margin-bottom:14px;box-shadow:var(--shadow)}
.filter .lbl{color:var(--muted);font-size:13px}.filter .sep{width:1px;height:20px;background:var(--line)}
.filter .pill span{padding:2px 10px;font-size:13px}

/* 类别 */
.group{margin-bottom:18px}
.ghead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 4px 8px}
.ghead h2{margin:0;font-size:16px}
.stat{font-size:12px;padding:1px 8px;border-radius:999px;background:var(--card2);border:1px solid var(--line);color:var(--muted)}
.kw{display:flex;gap:6px;flex-wrap:wrap}.kw span{font-size:12px;padding:0 8px;border-radius:6px;background:var(--accent-soft);color:var(--accent)}
.bulk{margin-left:auto;display:flex;gap:4px;align-items:center}.bulk .hint{margin-right:4px}
.bulk button{font-size:12px;padding:2px 10px}
.gempty{color:var(--muted);font-size:13px;padding:6px 4px}

/* 条目 */
.item{display:flex;gap:14px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:8px 0;box-shadow:var(--shadow);transition:border-color .15s}
.item.changed{border-color:var(--warn)}.item.to-exclude .body,.item.to-exclude img{opacity:.55}
.item img,.noimg{width:60px;height:60px;object-fit:cover;border-radius:10px;flex:none;background:var(--line)}
.allempty{text-align:center;color:var(--muted);padding:40px 0}
.body{flex:1;min-width:0}
.title{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.title a{color:var(--text);font-weight:600;font-size:15px;text-decoration:none}.title a:hover{color:var(--accent)}
mark{background:var(--mark);color:inherit;border-radius:3px;padding:0 1px}
.badge{font-size:11px;padding:0 7px;border-radius:999px;line-height:18px;font-weight:600}
.b-included{background:var(--ok-soft);color:var(--ok)}.b-excluded{background:var(--no-soft);color:var(--no)}.b-candidate{background:var(--accent-soft);color:var(--accent)}
.meta{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:3px}
.tags{display:flex;gap:5px;flex-wrap:wrap;margin-top:5px}
.tags span{font-size:11px;padding:0 7px;border-radius:5px;border:1px solid var(--line);color:var(--muted);background:var(--card2)}
.desc{font-size:12px;color:var(--muted);margin-top:5px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow-wrap:anywhere}

/* 分段选择 */
.seg{display:inline-flex;border:1px solid var(--line);border-radius:9px;overflow:hidden;flex:none;background:var(--card2)}
.seg input{position:absolute;opacity:0;pointer-events:none}
.seg label{padding:5px 13px;font-size:13px;cursor:pointer;color:var(--muted);user-select:none;transition:background .15s,color .15s}
.seg label+input+label{border-left:1px solid var(--line)}
.seg label:hover{color:var(--text)}
.seg input:focus-visible+label{outline:2px solid var(--accent);outline-offset:-2px}
.seg input[value=include]:checked+label{background:var(--ok);color:#fff}
.seg input[value=exclude]:checked+label{background:var(--no);color:#fff}
.seg input[value=pending]:checked+label{background:var(--muted);color:#fff}

/* 设置 */
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-weight:600;background:var(--card2)}
code{font:12px Consolas,monospace;background:var(--card2);border:1px solid var(--line);border-radius:4px;padding:0 4px}
.note{background:var(--accent-soft);border-radius:8px;padding:10px 12px;font-size:13px;line-height:1.7;margin-bottom:12px}
.switch{display:inline-flex;align-items:center;gap:6px;cursor:pointer}
.hidden{display:none!important}
@media (max-width:720px){.item{flex-wrap:wrap}.seg{width:100%}.seg label{flex:1;text-align:center}.filter{top:0;position:static}}
</style></head><body>
<div class="topbar"><div class="topbar-in">
  <span class="brand">同类 Mod 审核</span>
  <div class="tabs"><button data-tab="review" class="on">审核</button><button data-tab="settings">设置</button></div>
  <span class="spacer"></span>
  <span class="changes" id="count"></span>
  <button id="reset" class="ghost">撤销改动</button>
  <button id="save" class="primary">保存改动</button>
  <button id="quit" class="ghost">关闭工具</button>
</div></div>

<main>
<div id="tab-review">
  <div class="card">
    <h3>搜索创意工坊 <span class="hint">选好类别后点「搜索工坊」；已收录、已排除的不会再出现在候选里</span></h3>
    <div class="pills" id="chips"></div>
    <div class="row" style="margin-top:12px">
      <button class="ghost" id="all">全选</button><button class="ghost" id="none">全不选</button>
      <span class="spacer"></span>
      <span class="hint">每个关键词翻</span><input type="number" id="pages" value="2" min="1" max="10" style="width:64px"><span class="hint">页（每页 30 个），订阅数至少</span>
      <input type="number" id="minsubs" value="500" min="0" style="width:90px">
      <button id="search" class="primary">搜索工坊</button>
    </div>
    <div id="log"></div>
  </div>

  <div class="filter">
    <span class="lbl">显示</span>
    <label class="pill"><input type="checkbox" class="stf" value="candidate" checked><span>待审候选</span></label>
    <label class="pill"><input type="checkbox" class="stf" value="included"><span>已收录</span></label>
    <label class="pill"><input type="checkbox" class="stf" value="excluded"><span>已排除</span></label>
    <label class="pill"><input type="checkbox" id="hitonly" checked><span>候选只看标题含关键词</span></label>
    <span class="sep"></span>
    <span class="lbl">类别</span><select id="gfilter"><option value="">全部</option></select>
    <span class="sep"></span>
    <span class="lbl">当前显示的全部设为</span>
    <button data-bulk="include">收录</button><button data-bulk="exclude">排除</button><button data-bulk="pending">待定</button>
  </div>
  <div id="results"></div>
  <div id="allempty" class="allempty hidden">当前筛选下没有内容。搜索工坊找新候选，或在上面勾选「已收录」「已排除」查看名单。</div>
</div>

<div id="tab-settings" class="hidden">
  <div class="card">
    <h3>同步到常用mod集合</h3>
    <div class="note">只会同步<b>特定名单</b>：下表里的类别，也就是常用mod集合现在已有的功能。保存审核结果后，按名单改写集合 Mod
      <code>modmain.lua</code> 里对应 <code>safeImport</code> 的冲突 ID 列表（保留原有顺序，补上新收录的，去掉移出的）。<br>
      集合里没有的功能（如物品信息显示）和新建的类别只用于 DSTCamp 的重复检测，<b>不会</b>写进集合 Mod；集合 Mod 的功能暂不增删。</div>
    <div class="row"><span>modmain.lua 路径</span>
      <input type="text" id="modmain" style="flex:1;min-width:300px" placeholder="例如 D:\mymod\mods\CommonModSets\modmain.lua"></div>
    <div class="row"><label class="switch"><input type="checkbox" id="autosync"> 保存审核结果时自动同步</label>
      <span class="spacer"></span><button id="savesettings" class="primary">保存设置</button>
      <button id="dryrun">预演同步</button><button id="syncnow">立即同步</button></div>
    <div id="synclog" class="hint" style="white-space:pre-wrap;margin-top:10px"></div>
  </div>
  <div class="card">
    <h3>会同步的类别</h3>
    <table><thead><tr><th>类别</th><th>集合 Mod 配置键</th><th>已收录</th><th>集合专用 ID</th></tr></thead><tbody id="cmstable"></tbody></table>
    <p class="hint" style="margin:10px 0 0">集合专用 ID：只写进集合 Mod、DSTCamp 不提示的 Mod（如多功能合集 Lag Remover），不受审核影响。</p>
  </div>
  <div class="card">
    <h3>新建类别 <span class="hint">新类别只用于 DSTCamp 检测，不会同步到集合 Mod</span></h3>
    <div class="row">类别键 <input type="text" id="g_key" placeholder="如 auto_fish" style="width:140px">
      中文名 <input type="text" id="g_zh" style="width:130px"> 英文名 <input type="text" id="g_en" style="width:160px"></div>
    <div class="row">搜索关键词 <input type="text" id="g_search" style="flex:1;min-width:240px" placeholder="用逗号分隔，如 auto fish, 自动钓鱼">
      <button id="g_add" class="primary">新建</button></div>
  </div>
</div>
</main>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const ORIG = {included: "include", excluded: "exclude", candidate: "pending"};  // 现状对应的默认选项
const ST_TEXT = {included: "已收录", excluded: "已排除", candidate: "候选"};
const TAGS = {all_clients_require_mod: "所有人需安装", client_only_mod: "仅客户端", server_only_mod: "仅服务器",
  server_admin: "服务器管理", character: "角色", item: "物品", art: "美术", creature: "生物", environment: "环境",
  interface: "界面", language: "语言", pet: "宠物", scenario: "剧本", tutorial: "教程", tweak: "调整",
  utility: "工具", worldgen: "世界生成", other: "其他"};
let GROUPS = [], wasRunning = false;

async function api(path, body) {
  const opt = body === undefined ? {} : {method: "POST", headers: {"X-Tool": "1", "Content-Type": "application/json"}, body: JSON.stringify(body)};
  const r = await fetch(path, opt);
  const data = await r.json();
  if (!r.ok || data.ok === false) throw new Error(data.error || r.status);
  return data;
}
const groupOf = key => GROUPS.find(g => g.key === key);

// 标题里高亮该类别的搜索关键词
function highlight(title, terms) {
  const words = [...new Set(terms.flatMap(t => t.split(/\s+/)).filter(Boolean))].sort((a, b) => b.length - a.length);
  if (!words.length) return esc(title);
  const re = new RegExp(words.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "gi");
  let out = "", last = 0;
  for (const m of title.matchAll(re)) { out += esc(title.slice(last, m.index)) + `<mark>${esc(m[0])}</mark>`; last = m.index + m[0].length; }
  return out + esc(title.slice(last));
}

function renderGroups() {
  const checked = new Set([...document.querySelectorAll("#chips input:checked")].map(i => i.value));
  $("chips").innerHTML = GROUPS.map(g => `<label class="pill" title="关键词：${esc(g.search.join("、"))}"><input type="checkbox" value="${esc(g.key)}" ${checked.has(g.key) ? "checked" : ""}><span>${esc(g.zh)}<em class="num">${Object.keys(g.mods).length}</em></span></label>`).join("");
  const sel = $("gfilter"), cur = sel.value;
  sel.innerHTML = `<option value="">全部</option>` + GROUPS.map(g => `<option value="${esc(g.key)}">${esc(g.zh)}</option>`).join("");
  sel.value = cur;
  $("cmstable").innerHTML = GROUPS.filter(g => g.cms_key).map(g => `<tr><td>${esc(g.zh)}</td><td><code>${esc(g.cms_key)}</code></td>
    <td>${Object.keys(g.mods).length}</td><td>${Object.values(g.cms_extra).map(esc).join("、") || "—"}</td></tr>`).join("");
}

async function refresh(first) {
  const s = await api("/api/state");
  GROUPS = s.groups; renderGroups();
  if (first) { $("modmain").value = s.settings.modmain || ""; $("autosync").checked = !!s.settings.auto_sync; }
  $("log").textContent = s.log.join("\n"); $("log").scrollTop = 1e9;
  $("search").disabled = s.running;
  $("search").textContent = s.running ? "搜索中…" : "搜索工坊";
  if (wasRunning && !s.running) await loadReview();
  wasRunning = s.running;
  if (s.running) setTimeout(refresh, 1000);
}

function itemHtml(g, c) {
  const name = `${g.key}-${c.id}`, orig = ORIG[c.status];
  const seg = [["include", "收录"], ["exclude", "排除"], ["pending", c.status === "included" ? "移出" : "待定"]]
    .map(([v, text]) => `<input type="radio" id="${name}-${v}" name="${name}" value="${v}" ${v === orig ? "checked" : ""}><label for="${name}-${v}">${text}</label>`).join("");
  const meta = [`ID ${c.id}`];
  if (c.subs) meta.push(`订阅 ${c.subs.toLocaleString()}`);
  if (c.updated) meta.push(`更新 ${c.updated}`);
  const tags = (c.tags || []).map(t => `<span>${esc(TAGS[t] || t)}</span>`).join("");
  const img = c.preview ? `<img loading="lazy" src="${esc(c.preview)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'noimg'}))">` : `<div class="noimg"></div>`;
  return `${img}
    <div class="body">
      <div class="title"><a href="https://steamcommunity.com/sharedfiles/filedetails/?id=${c.id}" target="_blank">${highlight(c.title, g.search)}</a>
        <span class="badge b-${c.status}">${ST_TEXT[c.status]}</span></div>
      <div class="meta">${meta.map(m => `<span>${m}</span>`).join("")}</div>
      ${tags ? `<div class="tags">${tags}</div>` : ""}
      ${c.desc ? `<div class="desc">${esc(c.desc)}</div>` : ""}
    </div>
    <div class="seg">${seg}</div>`;
}

async function loadReview() {
  const review = await api("/api/review");
  const box = $("results"); box.innerHTML = "";
  for (const g of GROUPS) {
    const items = review[g.key] || [];
    const n = s => items.filter(c => c.status === s).length;
    const sec = document.createElement("section");
    sec.className = "group"; sec.dataset.group = g.key;
    sec.innerHTML = `<div class="ghead"><h2>${esc(g.zh)}</h2>
        <span class="stat">已收录 ${n("included")}</span><span class="stat">已排除 ${n("excluded")}</span><span class="stat">候选 ${n("candidate")}</span>
        <span class="kw">${g.search.map(t => `<span>${esc(t)}</span>`).join("")}</span>
        <span class="bulk"><span class="hint">本类显示的全部设为</span><button data-gbulk="include">收录</button><button data-gbulk="exclude">排除</button><button data-gbulk="pending">待定</button></span></div>
      <div class="gempty hidden">当前筛选下没有内容</div>`;
    for (const c of items) {
      const div = document.createElement("div");
      div.className = "item";
      Object.assign(div.dataset, {group: g.key, id: c.id, title: c.title, status: c.status, hit: c.hit ? "1" : ""});
      div.innerHTML = itemHtml(g, c);
      div.querySelectorAll("input").forEach(r => r.onchange = () => { mark(div); updateCount(); });
      mark(div);
      sec.appendChild(div);
    }
    sec.querySelectorAll("[data-gbulk]").forEach(b => b.onclick = () => bulk(b.dataset.gbulk, sec));
    box.appendChild(sec);
  }
  applyFilter(); updateCount();
}
function choiceOf(div) { return div.querySelector("input:checked").value; }
function mark(div) {
  const v = choiceOf(div);
  div.classList.toggle("to-exclude", v === "exclude");
  div.classList.toggle("changed", v !== ORIG[div.dataset.status]);
}
function applyFilter() {
  const shown = new Set([...document.querySelectorAll(".stf:checked")].map(i => i.value));
  const hitonly = $("hitonly").checked, gsel = $("gfilter").value;
  let total = 0;
  document.querySelectorAll("#results .group").forEach(sec => {
    const gok = !gsel || sec.dataset.group === gsel;
    let any = false;
    sec.querySelectorAll(".item").forEach(div => {
      const d = div.dataset;
      const ok = gok && shown.has(d.status) && !(d.status === "candidate" && hitonly && !d.hit);
      div.classList.toggle("hidden", !ok);
      any = any || ok;
      total += ok ? 1 : 0;
    });
    // 选“全部”类别时空类别整个隐藏；只看某个类别时保留标题并提示为空
    sec.classList.toggle("hidden", !gok || (!gsel && !any));
    sec.querySelector(".gempty").classList.toggle("hidden", any);
  });
  $("allempty").classList.toggle("hidden", total > 0 || !!gsel);
}
function bulk(value, scope) {
  (scope || $("results")).querySelectorAll(".item:not(.hidden)").forEach(div => {
    div.querySelector(`input[value="${value}"]`).checked = true; mark(div);
  });
  updateCount();
}
function changes() {
  const out = {};
  let n = 0;
  document.querySelectorAll(".item.changed").forEach(div => {
    (out[div.dataset.group] ??= {})[div.dataset.id] = {to: choiceOf(div), title: div.dataset.title};
    n++;
  });
  return [out, n];
}
function updateCount() {
  const c = {include: 0, exclude: 0, pending: 0};
  document.querySelectorAll(".item.changed").forEach(div => c[choiceOf(div)]++);
  const total = c.include + c.exclude + c.pending;
  $("count").innerHTML = total ? `未保存：收录 <b>${c.include}</b> · 排除 <b>${c.exclude}</b> · 待定/移出 <b>${c.pending}</b>` : "没有未保存的改动";
}
const say = (text, err, el) => { (el || $("log")).innerHTML = `<span class="${err ? "err" : ""}">${esc(text)}</span>`; };

document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => {
  document.querySelectorAll(".tabs button").forEach(x => x.classList.toggle("on", x === b));
  $("tab-review").classList.toggle("hidden", b.dataset.tab !== "review");
  $("tab-settings").classList.toggle("hidden", b.dataset.tab !== "settings");
});
document.querySelectorAll(".stf").forEach(i => i.onchange = applyFilter);
$("hitonly").onchange = applyFilter;
$("gfilter").onchange = applyFilter;
document.querySelectorAll("[data-bulk]").forEach(b => b.onclick = () => bulk(b.dataset.bulk));
$("all").onclick = () => document.querySelectorAll("#chips input").forEach(i => i.checked = true);
$("none").onclick = () => document.querySelectorAll("#chips input").forEach(i => i.checked = false);
$("search").onclick = async () => {
  const keys = [...document.querySelectorAll("#chips input:checked")].map(i => i.value);
  if (changes()[1] && !confirm("有未保存的改动，搜索完成后列表会刷新、改动会丢失。继续吗？")) return;
  try { await api("/api/search", {keys, pages: +$("pages").value, min_subs: +$("minsubs").value}); wasRunning = true; refresh(); }
  catch (e) { say(e.message, true); }
};
$("reset").onclick = () => {
  document.querySelectorAll(".item").forEach(div => { div.querySelector(`input[value="${ORIG[div.dataset.status]}"]`).checked = true; mark(div); });
  updateCount();
};
$("save").onclick = async () => {
  const [d, n] = changes();
  if (!n) { say("没有改动"); return; }
  try {
    const r = await api("/api/save", {decisions: d});
    await refresh(); await loadReview(); say(r.log.join("\n"));
  } catch (e) { say(e.message, true); await refresh(); await loadReview(); }
};
$("savesettings").onclick = async () => {
  try { await api("/api/settings", {modmain: $("modmain").value, auto_sync: $("autosync").checked}); say("设置已保存", false, $("synclog")); }
  catch (e) { say(e.message, true, $("synclog")); }
};
async function doSync(dry) {
  try {
    await api("/api/settings", {modmain: $("modmain").value, auto_sync: $("autosync").checked});
    const r = await api("/api/sync", {dry_run: dry}); say(r.log.join("\n"), false, $("synclog"));
  } catch (e) { say(e.message, true, $("synclog")); }
}
$("dryrun").onclick = () => doSync(true);
$("syncnow").onclick = () => { if (confirm("按当前名单改写集合 Mod 的 modmain.lua，确定吗？（可先点「预演同步」看会改什么）")) doSync(false); };
$("g_add").onclick = async () => {
  try {
    await api("/api/add-group", {key: $("g_key").value, zh: $("g_zh").value, en: $("g_en").value, search: $("g_search").value});
    alert(`已新建类别「${$("g_zh").value}」，回到「审核」页签勾选它后点「搜索工坊」`);
    ["g_key", "g_zh", "g_en", "g_search"].forEach(id => $(id).value = "");
    await refresh(); await loadReview();
  } catch (e) { alert(e.message); }
};
$("quit").onclick = async () => {
  if (changes()[1] && !confirm("有未保存的改动，确定关闭吗？")) return;
  await api("/api/quit", {}); document.body.innerHTML = "<main><div class='card'>工具已关闭，可以关掉这个标签页。</div></main>";
};
window.addEventListener("beforeunload", e => { if (changes()[1]) e.preventDefault(); });
if (location.hash === "#settings") document.querySelector('[data-tab="settings"]').click();
refresh(true).then(loadReview);
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
