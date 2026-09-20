# epub-translate

通用的 epub 全书翻译流水线：解包 → 抽取正文块 → OpenAI 兼容 API 批次翻译（术语表注入）→ 缓存回填 → 重新打包。支持双语对照与纯中文两种模式，也支持由 AI agent 代替 API 手翻。

## 用法

每本书一个工作目录，里面放 `book/`（解包的 epub）和 `glossary.md`（术语表）：

```bash
# 建议设个别名
alias trs='uv run --project ~/Project/epub-translate ~/Project/epub-translate/translate.py --dir .'

mkdir ~/some-book && cd ~/some-book
trs unpack "/path/to/原书.epub"     # 解包到 book/
# 编写 glossary.md（参考 glossary.example.md；含版本号，改版触发重译）
# 可选：写 book.md（书名/领域/语气说明，注入 system prompt）

export TRANSLATE_API_KEY="你的key"   # 默认 DeepSeek；TRANSLATE_BASE_URL / TRANSLATE_MODEL 可换别家

trs stats                            # 干跑：块数/字符/token 估算，不花钱
trs run                              # 翻译，默认双语对照；--mode replace 出纯中文
trs pack                             # 打包 output/<书名>.epub
```

agent 手翻模式（不耗 API）：`trs dump` 导出待译块到 `pending/*.json` → agent 翻译后写 `done/*.json` → `trs apply` 导入回填。两个命名空间（agent / 各 API 模型）的缓存互不干扰，可混用。

## 工作目录约定

| 文件 | 说明 |
|---|---|
| `book/` | 解包的 epub 工作副本，译文直接回填 |
| `glossary.md` | 术语表 + 体例规则（必需），含版本号 |
| `book.md` | 可选，书籍信息注入 system prompt，参与缓存 key |
| `cache.json` | 译文缓存，key = sha256(模型 + 术语表 + book.md + 原文) |
| `pending/` `done/` | agent 手翻模式的导入导出 |
| `output/` | 打包产物 |

## 要点（踩过的坑）

- 翻译任务关闭推理：DeepSeek 系加 `"thinking": {"type": "disabled"}`（脚本已内置）；推理模型慢 5 倍以上
- bs4 xml 模式：`class` 是字符串不是 list；`str(soup)` 会自带 `<?xml?>` 声明，写回前必须剥掉（否则阅读器报错）
- lxml 解析 XHTML 前把 `&nbsp;` 换成 `&#160;`
- 双语回填幂等：克隆块加 `class="zh"` 并剥掉 id，已有 zh 兄弟节点的块跳过
- 批次解析失败自动降级逐条重试；缓存每批落盘，中断重跑零浪费
- 实测参考：9.3 万词的书（6277 块），deepseek-flash 并发 4 约 25 分钟、0 失败，费用几块钱
- 数字密集表格（训练计划、配方剂量等）是机翻最易出错处，人工校对优先看
- 版权：译文仅自用，勿传播

## 示例

`glossary.example.md` 是《Triphasic Training》（运动训练专著）的完整术语表，可作为新书的编写参考。该书成品见 `~/Downloads/triphasic-zh.epub`（双语对照）。
