# 按你自己的语料生成分类参考列表

归档时 AI 会为每个条目生成一个 `category`。**分类带有很强的个人 / 语料色彩**——
什么算一个合适的分类，取决于你归档的是图形学论文、八卦杂谈还是美食菜谱。因此本项目
**不内置固定分类**，而是把它做成一个可选项：

- `[ai] category_reference` **留空**（默认）：AI 每次**自由生成**分类。
- `[ai] category_reference` **填写**：AI **优先**从你的列表里挑最贴切的一个，没有合适
  的才自拟。这能避免分类碎片化。

> 自由生成很容易碎片化。例如某个库里 669 条有分类的内容，竟分散在 **471 个**不同分类名
> 中（`游戏开发` / `游戏开发技术` / `游戏技术` / `游戏设计` 各自独立）。参考列表就是用来
> 把它们收敛成一组稳定、可复用的分类。

## 推荐工作流（先自由生成，再归并）

### 1. 首轮：自由生成

保持 `config.toml` 里 `[ai] category_reference = ""`，正常归档一批内容，让 AI 先自由
打标。库越大，第 2 步统计出的分布越能代表你的关注领域。

### 2. 统计分类分布

```bash
uv run python scripts/category_stats.py -o category_stats.md
```

- 默认从 `items`（系统记录）读取；`--source ai_cache` 可改读 AI 缓存。
- `--format tsv|json` 可换输出格式；不带 `-o` 时直接打印到终端。
- 输出顶部是统计摘要（条目数 / 有分类数 / 不同分类数），随后是按出现次数降序的
  `次数 — 分类名` 列表。`category_stats.md` 已在 `.gitignore` 中（属个人数据）。

### 3. 让 AI 归并成参考列表

把上一步 `category_stats.md` 的**全部内容**贴给任意 AI（DeepSeek / Claude / ChatGPT…），
配合下面这段提示词👇。它会把碎片化的分类合并成一组广义、互斥、可复用的分类。

### 4. 写入配置

把 AI 产出的列表粘贴进 `config.toml` 的 `[ai] category_reference`，用 TOML 多行字符串：

```toml
[ai]
category_reference = """
- 计算机图形学与渲染（图形学/渲染/着色器/光线追踪）
- 游戏开发（引擎/游戏编程/玩法与系统设计）
- 数学（高数/线代/分析/优化）
"""
```

括号内的范围示例是给模型判断归属用的，**模型被告知不会把括号写进结果**——所以你既可以
带着提示写，也可以只留分类名。

### 5. 之后归档

新归档（以及任何重新总结）的内容会**优先复用**列表里的分类。想进一步收敛或扩充，隔段
时间重复第 2–4 步即可。

## 用于生成参考列表的提示词（直接复制）

```text
下面是我的内容归档库里「AI 自由生成的分类 + 出现次数」统计。请把它归并成一份**参考分类列表**，
供后续归档时复用。要求：

1. 合并近义 / 同主题的碎片分类（如「游戏开发」「游戏开发技术」「游戏技术」「游戏设计」应合并）。
2. 产出 **15–25 个**广义、彼此互斥、可长期复用的分类，覆盖到长尾条目，不要遗漏明显的主题域。
3. 分类名简洁、概括；用中文（若我的语料是英文则用英文）。
4. 每个分类后用一对括号给出 3–6 个范围示例（用「/」分隔），仅作判断归属用。
5. 只输出一个 Markdown 无序列表，每行形如：`- 分类名（示例1/示例2/示例3）`，不要其它说明文字。

统计数据如下：
<在此粘贴 category_stats.md 的内容>
```

## 注意事项

- **缓存**：AI 结果按内容哈希缓存在 `ai_cache` 表里。设置或修改 `category_reference`
  只影响**尚未总结过**的内容；已归档条目不会自动重新分类（除非其正文变化触发重新总结）。
- **语言**：列表语言应与 `[ai] language` 一致；中英皆可。
- **可选**：随时把 `category_reference` 清空即可退回自由生成模式。
- **配置位置**：`config.toml` 是你的个人配置（已 gitignore）；`config.example.toml`
  给出空值示例与完整说明。

## 为 Obsidian 生成分类目录

归档导出到 Obsidian vault 后，可以按 Markdown frontmatter 中的 `category` 自动生成分类目录：

```bash
uv run python scripts/generate_category_pages.py /path/to/vault
```

脚本递归扫描 vault 中的 Markdown 文件，默认在 `<vault>/目录/` 下为每个分类生成一个
`<category>.md` 文件。目录文件包含静态 Markdown 表格：

```markdown
| File | Tags | Summary |
| --- | --- | --- |
| [[example]] | #Tag1 #Tag2 | 摘要 |
```

`File` 使用源 Markdown 文件名，不使用 frontmatter 中的 `title`。例如 `example.md`
会生成 `[[example]]`。可以使用 `-o` 指定 vault 内的其他目录：

```bash
uv run python scripts/generate_category_pages.py /path/to/vault -o 分类目录
```

如果目标目录已经存在，脚本会询问是删除、合并还是退出。非交互环境中应显式选择策略：

```bash
uv run python scripts/generate_category_pages.py /path/to/vault --if-exists merge
uv run python scripts/generate_category_pages.py /path/to/vault --if-exists delete
```

### 使用 Obsidian Dataview Serializer 插件

如果使用 [Dataview](https://blacksmithgu.github.io/obsidian-dataview/) 及 [Obsidian Dataview Serializer](https://developassion.gitbook.io/obsidian-dataview-serializer) 插件，可以让目录文件包含待序列化查询，而不是静态表格：

```bash
uv run python scripts/generate_category_pages.py /path/to/vault --dataview-serializer
```

生成内容形如：

```markdown
<!-- QueryToSerialize: TABLE tags AS "Tags", summary AS "Summary" SORT archived_at ASC WHERE category="技术" -->
```

### 按归档时间倒序

添加 `--sort-by-time` 可按 `archived_at` 倒序排列，最新归档的条目在最上方：

```bash
uv run python scripts/generate_category_pages.py /path/to/vault --sort-by-time
```

静态表格会直接按时间排序；Dataview Serializer 模式下会生成
`SORT archived_at DESC`。缺失或无法解析 `archived_at` 的条目排在最后。

### 生成图谱颜色设置

添加 `--generate-graph-settings` 可额外覆盖生成 `<vault>/.obsidian/graph.json`：

```bash
uv run python scripts/generate_category_pages.py /path/to/vault --generate-graph-settings
```

脚本为每个分类添加一个 Obsidian 图谱颜色组，并启用标签显示。颜色会在适合阅读的饱和度
和亮度范围内随机生成，同时分散色相，减少相邻分类颜色过于接近的情况。由于会直接覆盖
现有 `graph.json`，请先确认其中没有需要保留的手工配置。
