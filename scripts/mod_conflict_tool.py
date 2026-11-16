"""维护同类 Mod 名单（dstools/features/mod/similar_mods.py）的开发工具，不随 EXE 打包。

流程：
  1. search   按各类别的关键词搜索创意工坊，生成本地审核网页（已收录、已排除的不再列出）
  2. 在网页里逐个选「收录 / 排除」，点「导出结果」得到 JSON 文件
  3. apply    把导出的结果写回名单；给了 --modmain 时顺带同步常用mod集合
  其他：add-group 新建类别；sync 只同步常用mod集合；check 检查名单里的 Mod 是否还在工坊上

搜索走工坊浏览网页（不需要 API 密钥；官方 QueryFiles 接口无密钥返回 403），解析页面内嵌的
valve-ssr-data JSON。这不是正式接口，Steam 改版后可能要跟着改 _parse_browse_page。

  python scripts/mod_conflict_tool.py search [--group KEY ...] [--pages 2] [--min-subs 500]
  python scripts/mod_conflict_tool.py apply 结果.json [--modmain 路径\\modmain.lua]
  python scripts/mod_conflict_tool.py add-group KEY --zh 中文名 --en "English" --search 词 [--search 词] [--cms-key 键]
  python scripts/mod_conflict_tool.py sync --modmain 路径\\modmain.lua [--dry-run]
  python scripts/mod_conflict_tool.py check
"""

from __future__ import annotations

import argparse
import html
import importlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dstools.features.mod import similar_mods  # noqa: E402
from dstools.features.mod.workshop_webapi import fetch_remote_items  # noqa: E402
from dstools.shared.ssl_context import default_ssl_context  # noqa: E402

DATA_PATH = ROOT / "dstools" / "features" / "mod" / "similar_mods.py"
REVIEW_DIR = ROOT / "build" / "mod_conflict_review"
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
                raise SystemExit(f"工坊 ID {wid} 同时在 {owner[wid]} 和 {group['key']} 里，一个 ID 只能放进一组")
            owner[wid] = group["key"]
    with open(DATA_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(render_module(groups))


def find_group(groups: list[dict], key: str) -> dict:
    for group in groups:
        if group["key"] == key:
            return group
    raise SystemExit(f"没有类别 {key}；现有：{', '.join(g['key'] for g in groups)}")


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


def cmd_search(args) -> None:
    groups = load_groups()
    chosen = [find_group(groups, key) for key in args.group] if args.group else groups
    known = {}
    for group in groups:
        for wid in (*group["mods"], *group["excluded"], *group["cms_extra"]):
            known[wid] = group["key"]
    review = []
    for group in chosen:
        if not group.get("search"):
            print(f"[{group['key']}] 没有搜索关键词，跳过（用 add-group 重建或手改 search）")
            continue
        found: dict[str, dict] = {}
        for text in group["search"]:
            for page in range(1, args.pages + 1):
                try:
                    items, total_pages = search_workshop(text, page)
                except Exception as exc:  # 网络或解析失败：报出来，继续下一个词
                    print(f"[{group['key']}] 搜索「{text}」第 {page} 页失败：{exc}")
                    break
                for rank, item in enumerate(items):
                    cand = _candidate(item)
                    if cand["id"] not in known and cand["subs"] >= args.min_subs and cand["id"] not in found:
                        cand["rank"] = (page - 1) * len(items) + rank  # 工坊返回的相关度名次
                        cand["hit"] = _title_hits(cand["title"], group["search"])
                        found[cand["id"]] = cand
                time.sleep(_REQUEST_GAP)
                if page >= total_pages:
                    break
        # 工坊文本搜索匹配很松：标题含关键词的排前面，其余按相关度
        cands = sorted(found.values(), key=lambda c: (not c["hit"], c["rank"]))
        hits = sum(1 for c in cands if c["hit"])
        print(f"[{group['key']}] {group['zh']}：新候选 {len(cands)} 个，其中标题含关键词 {hits} 个")
        review.append({"key": group["key"], "zh": group["zh"], "mods": group["mods"], "candidates": cands})
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    out = REVIEW_DIR / "review.html"
    out.write_text(render_review_page(review), encoding="utf-8")
    print(f"审核网页：{out}")
    if not args.no_open:
        webbrowser.open(out.as_uri())


# ── 审核网页 ─────────────────────────────────────────────────────────

_REVIEW_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>同类 Mod 审核</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2129;--muted:#6b7280;--line:#e3e6ea;--accent:#2f6fde;--ok:#1f9d55;--no:#c0392b}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--card:#20242a;--text:#e6e8eb;--muted:#9aa1ab;--line:#30353d;--accent:#5b8ff0;--ok:#3cc47c;--no:#e0675a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Microsoft YaHei",system-ui,sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:18px;margin:0 auto 0 0}button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:6px;padding:5px 12px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
main{max-width:1100px;margin:0 auto;padding:8px 16px 48px}section{margin-top:20px}
h2{font-size:16px;margin:0 0 4px}.known{color:var(--muted);font-size:12px;margin-bottom:8px}
.item{display:flex;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;margin:8px 0}
.item.include{border-color:var(--ok);box-shadow:inset 4px 0 var(--ok)}.item.exclude{opacity:.55;box-shadow:inset 4px 0 var(--no)}
.item img{width:72px;height:72px;object-fit:cover;border-radius:6px;flex:none;background:var(--line)}
.body{flex:1;min-width:0}.title a{color:var(--text);font-weight:600;text-decoration:none}.title a:hover{color:var(--accent)}
.meta{color:var(--muted);font-size:12px}.desc{font-size:12px;color:var(--muted);margin-top:4px;overflow-wrap:anywhere}
.choice{display:flex;flex-direction:column;gap:4px;flex:none}.choice label{white-space:nowrap;cursor:pointer}
.empty{color:var(--muted)}.badge{font-size:11px;color:var(--ok);border:1px solid var(--ok);border-radius:4px;padding:0 4px;margin-left:6px}
body.hitonly .item.nohit{display:none}
</style></head><body class="hitonly">
<header><h1>同类 Mod 审核</h1><label><input type="checkbox" id="hitonly" checked> 只看标题含关键词</label>
<span id="count" class="meta"></span>
<button id="export" class="primary">导出结果</button></header>
<main id="main"></main>
<script>
const DATA = __DATA__;
const main = document.getElementById("main");
const esc = s => s.replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
for (const g of DATA) {
  const sec = document.createElement("section");
  const known = Object.values(g.mods).join("、") || "（暂无）";
  const hits = g.candidates.filter(c => c.hit).length;
  sec.innerHTML = `<h2>${esc(g.zh)} <span class="meta">${esc(g.key)} · 候选 ${g.candidates.length}，标题含关键词 ${hits}</span></h2>
    <div class="known">已收录：${esc(known)}</div>`;
  if (!g.candidates.length) sec.innerHTML += `<div class="empty">没有新候选</div>`;
  for (const c of g.candidates) {
    const name = `${g.key}-${c.id}`;
    const div = document.createElement("div");
    div.className = "item " + (c.hit ? "hit" : "nohit");
    div.innerHTML = `<img loading="lazy" src="${esc(c.preview)}" alt="">
      <div class="body"><div class="title"><a href="https://steamcommunity.com/sharedfiles/filedetails/?id=${c.id}" target="_blank">${esc(c.title)}</a>${c.hit ? '<span class="badge">标题含关键词</span>' : ""}</div>
      <div class="meta">ID ${c.id} · 订阅 ${c.subs.toLocaleString()} · 更新 ${c.updated} · ${esc(c.tags.join(", "))}</div>
      <div class="desc">${esc(c.desc)}</div></div>
      <div class="choice"><label><input type="radio" name="${name}" value="include"> 收录</label>
      <label><input type="radio" name="${name}" value="exclude"> 排除</label>
      <label><input type="radio" name="${name}" value="" checked> 待定</label></div>`;
    div.querySelectorAll("input").forEach(r => r.addEventListener("change", () => {
      div.classList.toggle("include", r.value === "include");
      div.classList.toggle("exclude", r.value === "exclude");
      updateCount();
    }));
    div.dataset.group = g.key; div.dataset.id = c.id; div.dataset.title = c.title;
    sec.appendChild(div);
  }
  main.appendChild(sec);
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
  document.getElementById("count").textContent = `收录 ${inc} · 排除 ${exc}`;
}
document.getElementById("hitonly").onchange = e => document.body.classList.toggle("hitonly", e.target.checked);
document.getElementById("export").onclick = () => {
  const blob = new Blob([JSON.stringify(decisions(), null, 1)], {type: "application/json"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = "mod_conflict_review_result.json"; a.click();
};
updateCount();
</script></body></html>
"""


def render_review_page(review: list[dict]) -> str:
    data = json.dumps(review, ensure_ascii=False).replace("</", "<\\/")
    return _REVIEW_TEMPLATE.replace("__DATA__", data)


# ── 写回与同步 ───────────────────────────────────────────────────────

def cmd_apply(args) -> None:
    decisions = json.loads(Path(args.result).read_text(encoding="utf-8"))
    groups = load_groups()
    for key, choice in decisions.items():
        group = find_group(groups, key)
        for wid, title in (choice.get("include") or {}).items():
            group["excluded"].pop(wid, None)
            group["mods"][wid] = title
        for wid, title in (choice.get("exclude") or {}).items():
            if wid not in group["mods"]:
                group["excluded"][wid] = title
        print(f"[{key}] 收录 {len(choice.get('include') or {})}，排除 {len(choice.get('exclude') or {})}")
    save_groups(groups)
    print(f"已写回 {DATA_PATH}")
    if args.modmain:
        sync_modmain(groups, Path(args.modmain), dry_run=False)


def cmd_add_group(args) -> None:
    groups = load_groups()
    if any(group["key"] == args.key for group in groups):
        raise SystemExit(f"类别 {args.key} 已存在")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", args.key):
        raise SystemExit("类别键只能用小写字母、数字和下划线，并以字母开头")
    groups.append({"key": args.key, "zh": args.zh, "en": args.en, "search": args.search, "note": args.note,
                   "mods": {}, "excluded": {}, "cms_key": args.cms_key, "cms_extra": {}})
    save_groups(groups)
    print(f"已新建类别 {args.key}；接着运行 search --group {args.key}")


_SAFE_IMPORT_RE = r'(safeImport\(\s*"{key}"\s*,\s*"[^"]*"\s*,\s*"[^"]*")((?:\s*,\s*"\d+")*)(\s*\))'


def sync_modmain(groups: list[dict], modmain: Path, dry_run: bool) -> None:
    """按名单改写常用mod集合里对应功能的冲突 ID：保留原有顺序，补上新增的，去掉名单里没有的。
    只动 cms_key 不为空的类别；按字节读写，保留原文件的 CRLF。"""
    text = modmain.read_bytes().decode("utf-8")
    changed = False
    for group in groups:
        key = group.get("cms_key")
        if not key:
            continue
        pattern = re.compile(_SAFE_IMPORT_RE.format(key=re.escape(key)))
        matches = list(pattern.finditer(text))
        if len(matches) != 1:
            print(f"[{group['key']}] modmain 里 safeImport(\"{key}\", ...) 出现 {len(matches)} 次，跳过")
            continue
        match = matches[0]
        current = re.findall(r'"(\d+)"', match.group(2))
        target = [*group["mods"], *[wid for wid in group["cms_extra"] if wid not in group["mods"]]]
        merged = [wid for wid in current if wid in target] + [wid for wid in target if wid not in current]
        if merged == current:
            continue
        added = [wid for wid in merged if wid not in current]
        removed = [wid for wid in current if wid not in merged]
        print(f"[{group['key']}] -> {key}：新增 {added or '无'}，删除 {removed or '无'}")
        ids = "".join(f',"{wid}"' for wid in merged)
        text = text[:match.start(2)] + ids + text[match.end(2):]
        changed = True
    if not changed:
        print("常用mod集合无需改动")
    elif dry_run:
        print("（--dry-run，未写入）")
    else:
        modmain.write_bytes(text.encode("utf-8"))
        print(f"已写入 {modmain}，记得用 Lua 检查语法后再发布")


def cmd_sync(args) -> None:
    sync_modmain(load_groups(), Path(args.modmain), dry_run=args.dry_run)


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
    parser = argparse.ArgumentParser(description="维护同类 Mod 名单")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("search", help="搜索创意工坊并生成审核网页")
    p.add_argument("--group", action="append", default=[], help="只搜这些类别（可重复）")
    p.add_argument("--pages", type=int, default=2, help="每个关键词翻几页（每页 30 个）")
    p.add_argument("--min-subs", type=int, default=500, help="订阅数低于此值的不列出")
    p.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    p.set_defaults(func=cmd_search)
    p = sub.add_parser("apply", help="把审核网页导出的结果写回名单")
    p.add_argument("result")
    p.add_argument("--modmain", help="同时同步常用mod集合的 modmain.lua")
    p.set_defaults(func=cmd_apply)
    p = sub.add_parser("add-group", help="新建类别")
    p.add_argument("key")
    p.add_argument("--zh", required=True)
    p.add_argument("--en", required=True)
    p.add_argument("--search", action="append", required=True, help="搜索关键词（可重复）")
    p.add_argument("--note", default="")
    p.add_argument("--cms-key", default=None, help="常用mod集合里对应的 safeImport 配置键")
    p.set_defaults(func=cmd_add_group)
    p = sub.add_parser("sync", help="只同步常用mod集合")
    p.add_argument("--modmain", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_sync)
    p = sub.add_parser("check", help="检查名单里的 Mod 是否还在工坊上")
    p.set_defaults(func=cmd_check)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
