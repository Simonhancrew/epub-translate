#!/usr/bin/env python3
"""通用的 epub 全书翻译流水线（OpenAI 兼容 API）。

用法：在本项目目录用 uv 运行，--dir 指向书籍工作目录（含 book/ 与 glossary.md）：
  uv run translate.py --dir <书籍目录> stats    干跑统计，不调用 API
  uv run translate.py --dir <书籍目录> run      翻译（默认双语对照，--mode replace 纯中文）
  uv run translate.py --dir <书籍目录> pack     打包 <书籍目录>/output/*.epub
  uv run translate.py --dir <书籍目录> unpack <原书.epub>   解包
  uv run translate.py --dir <书籍目录> dump     导出待译块供 agent 手翻
  uv run translate.py --dir <书籍目录> apply    导入 agent 译文并回填

工作目录文件约定：
  book/       解包后的 epub（unpack 生成）
  glossary.md 术语表（必需，含版本号；改版触发重译）
  book.md     可选：书名/领域/语气说明，注入 system prompt（同样参与缓存 key）

配置（环境变量）：
  TRANSLATE_API_KEY    必填（run 时）
  TRANSLATE_BASE_URL   默认 https://api.deepseek.com/v1
  TRANSLATE_MODEL      默认 deepseek-chat
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from bs4 import BeautifulSoup

WORKDIR = Path.cwd()
BOOK = WORKDIR / "book"
OUT = WORKDIR / "output"
CACHE_FILE = WORKDIR / "cache.json"
GLOSSARY_FILE = WORKDIR / "glossary.md"
BOOK_CTX_FILE = WORKDIR / "book.md"


def set_workdir(d: str):
    global WORKDIR, BOOK, OUT, CACHE_FILE, GLOSSARY_FILE, BOOK_CTX_FILE
    WORKDIR = Path(d).resolve()
    BOOK = WORKDIR / "book"
    OUT = WORKDIR / "output"
    CACHE_FILE = WORKDIR / "cache.json"
    GLOSSARY_FILE = WORKDIR / "glossary.md"
    BOOK_CTX_FILE = WORKDIR / "book.md"

BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6",
              "li", "td", "th", "dt", "dd", "figcaption", "caption", "blockquote"}
BATCH_MAX_CHARS = 6000
BATCH_MAX_ITEMS = 25

SYSTEM_PROMPT = """你是专业的书籍译者，正在把一本英文书翻译为中文。
{book_context}规则：
1. 翻译为专业、通顺的中文书评文风，避免翻译腔；保留作者的举例与语气
2. 原样保留所有 HTML 标签及其属性、相对位置，只翻译文本内容
3. 专业术语严格遵循文末术语表，全书统一
4. 图表引用 Figure 3.11 → 图 3.11；文献引用（作者 年份）保留原文
5. 数字、百分数、单位保留原格式，不做换算
6. 输入是一个 JSON 字符串数组，逐项翻译，输出必须也是 JSON 字符串数组，长度与顺序完全一致
7. 只输出 JSON 数组本身，不要代码块包裹，不要任何解释

术语表：
{glossary}
"""


def load_prompt_material() -> tuple[str, str]:
    """返回 (格式化好的 system_prompt, 缓存命名空间串)。book.md 存在时并入两者。"""
    glossary = GLOSSARY_FILE.read_text(encoding="utf-8")
    book_ctx = BOOK_CTX_FILE.read_text(encoding="utf-8").strip() if BOOK_CTX_FILE.exists() else ""
    sp = SYSTEM_PROMPT.format(
        glossary=glossary,
        book_context=f"书籍信息：\n{book_ctx}\n" if book_ctx else "",
    )
    ns = glossary + ("\n" + book_ctx if book_ctx else "")
    return sp, ns

cache_lock = threading.Lock()
cache: dict = {}


def load_cache():
    global cache
    if CACHE_FILE.exists():
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))


def save_cache():
    with cache_lock:
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=0),
                              encoding="utf-8")


def cache_key(model: str, glossary: str, src: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode())
    h.update(b"\x00")
    h.update(hashlib.sha256(glossary.encode()).digest())
    h.update(src.encode())
    return h.hexdigest()


def read_opf_path() -> Path:
    container = (BOOK / "META-INF" / "container.xml").read_text(encoding="utf-8")
    m = re.search(r'full-path="([^"]+)"', container)
    if not m:
        sys.exit("container.xml 中找不到 OPF 路径")
    return BOOK / m.group(1)


def spine_files() -> list[Path]:
    opf = read_opf_path()
    soup = BeautifulSoup(opf.read_text(encoding="utf-8"), "xml")
    manifest = {item["id"]: item["href"]
                for item in soup.find_all("item") if item.get("href", "").endswith("xhtml")}
    files = []
    for ref in soup.find_all("itemref"):
        href = manifest.get(ref["idref"])
        if href:
            files.append(opf.parent / href)
    return files


def split_prolog(src: str) -> tuple[str, str]:
    m = re.search(r"<html[\s>]", src)
    if not m:
        return "", src
    return src[: m.start()], src[m.start():]


def inner_html(el) -> str:
    return "".join(str(c) for c in el.contents)


def collect_units(path: Path):
    """返回 (prolog, soup, [element])，element 为叶子块级元素。"""
    src = path.read_text(encoding="utf-8")
    prolog, body = split_prolog(src)
    body = body.replace("&nbsp;", "&#160;")
    soup = BeautifulSoup(body, "xml")
    units = []
    for el in soup.find_all(BLOCK_TAGS):
        if el.find(BLOCK_TAGS):          # 只取叶子块，避免重复翻译嵌套块
            continue
        if el.find_parent("head"):
            continue
        if "zh" in (el.get("class") or []):  # 双语模式下已插入的中文块，跳过
            continue
        text = el.get_text(strip=True)
        if len(text) < 2 or not re.search(r"[A-Za-z]{2,}", text):
            continue
        units.append(el)
    return prolog, soup, units


def write_file(path: Path, prolog: str, soup):
    body = str(soup)
    body = re.sub(r"^\s*<\?xml[^?]*\?>\s*", "", body)  # 序列化会自带声明，去掉避免与 prolog 重复
    path.write_text(prolog + body, encoding="utf-8")


def make_batches(items: list[str]) -> list[list[int]]:
    """把索引按字符量分组成批。"""
    batches, cur, cur_chars = [], [], 0
    for i, s in enumerate(items):
        cur.append(i)
        cur_chars += len(s)
        if cur_chars >= BATCH_MAX_CHARS or len(cur) >= BATCH_MAX_ITEMS:
            batches.append(cur)
            cur, cur_chars = [], 0
    if cur:
        batches.append(cur)
    return batches


def chat(session: requests.Session, base: str, key: str, model: str,
         sys_prompt: str, srcs: list[str]) -> list[str]:
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 12000,
        "thinking": {"type": "disabled"},  # 关闭推理（DeepSeek 系参数；换别家 API 如遇报错可删除此行）
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": json.dumps(srcs, ensure_ascii=False)},
        ],
    }
    resp = session.post(
        f"{base.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json=payload,
        timeout=300,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.MULTILINE).strip()
    out = json.loads(content)
    if not isinstance(out, list) or len(out) != len(srcs):
        raise ValueError(f"返回数组长度不符: {len(out) if isinstance(out, list) else '?'} != {len(srcs)}")
    return [str(x) for x in out]


def translate_batch(session, base, key, model, sys_prompt, srcs, retries=4) -> list[str | None]:
    """返回译文列表；失败项为 None。批次解析失败时降级为逐条重试。"""
    for attempt in range(retries):
        try:
            return chat(session, base, key, model, sys_prompt, srcs)
        except (ValueError, json.JSONDecodeError) as e:
            if len(srcs) > 1:
                print(f"  批次解析失败，降级逐条翻译 ({e})", flush=True)
                results = []
                for s in srcs:
                    results.extend(translate_batch(session, base, key, model, sys_prompt, [s], retries))
                return results
            print(f"  单条解析失败(第{attempt + 1}次): {e}", flush=True)
        except requests.RequestException as e:
            print(f"  请求失败(第{attempt + 1}次): {e}", flush=True)
        time.sleep(2 ** attempt)
    return [None] * len(srcs)


def cmd_stats(args):
    total, total_chars, files = 0, 0, 0
    for f in spine_files():
        _, _, units = collect_units(f)
        chars = sum(len(inner_html(u)) for u in units)
        if units:
            files += 1
            print(f"{f.name:>18}: {len(units):>4} 块  {chars:>8,} 字符")
        total += len(units)
        total_chars += chars
    n_batches = len(make_batches(["x" * (total_chars // max(total, 1))] * total))
    est_in = total_chars / 3.2 + n_batches * 900
    print(f"\n共 {total} 块待译，{total_chars:,} 字符，约 {n_batches} 批请求")
    print(f"估算输入 {est_in / 1000:,.0f}K token，输出约 {total_chars / 2.5 / 1000:,.0f}K token")
    if args.show:
        f = spine_files()[args.file]
        _, _, units = collect_units(f)
        for u in units[: args.show]:
            print(f"\n--- {f.name} ---\n{inner_html(u)[:300]}")


def backfill(mode: str, model: str, glossary: str) -> int:
    """把缓存里的译文回填进 book/，返回写入的文件数。双语模式可重复执行（幂等）。"""
    written = 0
    for f in spine_files():
        prolog, soup, units = collect_units(f)
        changed = False
        for el in units:
            src = inner_html(el)
            tr = cache.get(cache_key(model, glossary, src))
            if tr is None:
                continue
            frag = BeautifulSoup(f"<w>{tr.replace('&nbsp;', '&#160;')}</w>", "xml")
            children = list(frag.find("w").contents)
            if mode == "bilingual":
                sib = el.find_next_sibling()
                if sib and "zh" in (sib.get("class") or []):
                    continue  # 该块已插入过译文
                new_el = soup.new_tag(el.name, **el.attrs)
                cls = new_el.get("class") or []
                if isinstance(cls, str):  # xml 模式下 class 是字符串
                    cls = cls.split()
                cls.append("zh")
                new_el["class"] = cls
                new_el.extend(children)
                new_el.attrs.pop("id", None)  # 克隆块去掉 id，避免与原块重复
                for t in new_el.find_all(True):
                    t.attrs.pop("id", None)
                el.insert_after(new_el)
            else:
                el.clear()
                el.extend(children)
            changed = True
        if changed:
            write_file(f, prolog, soup)
            print(f"已写入 {f.name}")
            written += 1
    return written


def cmd_run(args):
    key = os.environ.get("TRANSLATE_API_KEY")
    if not key:
        sys.exit("请先设置环境变量 TRANSLATE_API_KEY")
    base = os.environ.get("TRANSLATE_BASE_URL", "https://api.deepseek.com/v1")
    model = os.environ.get("TRANSLATE_MODEL", "deepseek-chat")
    sys_prompt, ns = load_prompt_material()
    load_cache()

    files = {}
    work = []  # (file, index_in_units, src)
    for i, f in enumerate(spine_files()):
        if args.file is not None and i != args.file:
            continue
        prolog, soup, units = collect_units(f)
        files[f] = (prolog, soup, units)
        for j, el in enumerate(units):
            src = inner_html(el)
            if cache_key(model, ns, src) not in cache:
                work.append((f, j, src))

    print(f"待翻译 {len(work)} 块（缓存命中跳过其余），模型 {model} @ {base}", flush=True)
    srcs = [w[2] for w in work]
    batches = make_batches(srcs)
    failures = 0
    done = 0

    def run_batch(idx_list):
        return idx_list, translate_batch(
            requests.Session(), base, key, model, sys_prompt,
            [srcs[i] for i in idx_list])

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_batch, b) for b in batches]
        for fut in as_completed(futures):
            idx_list, results = fut.result()
            with cache_lock:
                for i, tr in zip(idx_list, results):
                    if tr is None:
                        failures += 1
                    else:
                        cache[cache_key(model, ns, srcs[i])] = tr
                done += len(idx_list)
            save_cache()
            print(f"\r进度 {done}/{len(work)}", end="", flush=True)
    save_cache()
    print()

    backfill(args.mode, model, ns)

    print(f"\n完成。失败 {failures} 块" + ("（原文已保留，可重跑 run 续译）" if failures else ""))
    if failures:
        sys.exit(1)


AGENT_MODEL = "agent"  # 由当前 agent 人工翻译时使用的伪模型名，与真实 API 缓存隔离


def cmd_dump(args):
    """导出未缓存的待译块到 pending/<file>.json，供当前 agent 翻译。"""
    _, ns = load_prompt_material()
    load_cache()
    pend = WORKDIR / "pending"
    pend.mkdir(exist_ok=True)
    total = 0
    for i, f in enumerate(spine_files()):
        if args.file is not None and i != args.file:
            continue
        _, _, units = collect_units(f)
        items = {}
        for el in units:
            src = inner_html(el)
            k = cache_key(AGENT_MODEL, ns, src)
            if k not in cache:
                items[k] = src
        if items:
            out = pend / f"{f.stem}.json"
            out.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"{out.name}: {len(items)} 块")
            total += len(items)
    print(f"共导出 {total} 块")


def cmd_apply(args):
    """导入 done/*.json 译文到缓存并回填 book/。导入后删除 done 文件。"""
    _, ns = load_prompt_material()
    load_cache()
    done_dir = WORKDIR / "done"
    n = 0
    for p in sorted(done_dir.glob("*.json")):
        for k, v in json.loads(p.read_text(encoding="utf-8")).items():
            cache[k] = v
            n += 1
        p.unlink()
    save_cache()
    print(f"导入 {n} 条译文")
    backfill(args.mode, AGENT_MODEL, ns)


def cmd_pack(args):
    OUT.mkdir(exist_ok=True)
    out = OUT / args.name
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w") as z:
        z.write(BOOK / "mimetype", "mimetype", compress_type=zipfile.ZIP_STORED)
        for p in sorted(BOOK.rglob("*")):
            if p.is_file() and p.name not in ("mimetype", ".DS_Store"):
                z.write(p, p.relative_to(BOOK), compress_type=zipfile.ZIP_DEFLATED)
    print(f"已生成 {out}")


def cmd_unpack(args):
    if BOOK.exists() and not args.force:
        sys.exit("book/ 已存在，--force 覆盖")
    shutil.rmtree(BOOK, ignore_errors=True)
    BOOK.mkdir()
    with zipfile.ZipFile(args.epub) as z:
        z.extractall(BOOK)
    print(f"已解包到 {BOOK}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=".", help="书籍工作目录（含 book/、glossary.md），默认当前目录")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("stats", help="干跑统计")
    p.add_argument("--show", type=int, default=0, metavar="N", help="顺带打印样例块数")
    p.add_argument("--file", type=int, default=0, help="样例取自 spine 中第几个文件")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("run", help="执行翻译")
    p.add_argument("--mode", choices=["bilingual", "replace"], default="bilingual")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--file", type=int, default=None, help="只翻译 spine 中第 N 个文件")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("pack", help="打包 epub")
    p.add_argument("--name", default="triphasic-zh.epub")
    p.set_defaults(fn=cmd_pack)

    p = sub.add_parser("dump", help="导出待译块给当前 agent 翻译")
    p.add_argument("--file", type=int, default=None, help="只导出 spine 中第 N 个文件")
    p.set_defaults(fn=cmd_dump)

    p = sub.add_parser("apply", help="导入 agent 译文并回填")
    p.add_argument("--mode", choices=["bilingual", "replace"], default="bilingual")
    p.set_defaults(fn=cmd_apply)

    p = sub.add_parser("unpack", help="解包 epub")
    p.add_argument("epub")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_unpack)

    args = ap.parse_args()
    set_workdir(args.dir)
    args.fn(args)


if __name__ == "__main__":
    main()
