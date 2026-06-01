from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_category_pages.py"
SPEC = importlib.util.spec_from_file_location("generate_category_pages", SCRIPT)
assert SPEC is not None
generate_category_pages = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(generate_category_pages)


def test_generates_markdown_table_pages_by_default(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text(
        "---\ntitle: 标题 A\ncategory: 技术\nsummary: 摘要 | A\ntags:\n"
        "  - Tag1\n  - '#Tag2'\n---\nbody\n",
        encoding="utf-8",
    )
    (vault / "b.md").write_text(
        "---\ntitle: 标题 B\ncategory: 生活\ntags: tag3 tag4\n---\nbody\n",
        encoding="utf-8",
    )
    (vault / "no-category.md").write_text("---\ntags:\n  - x\n---\n", encoding="utf-8")

    rc = generate_category_pages.main([str(vault), "--if-exists", "abort"])

    assert rc == 0
    assert (vault / "目录" / "技术.md").read_text(encoding="utf-8") == (
        "| File | Tags | Summary |\n"
        "| --- | --- | --- |\n"
        "| [[a]] | #Tag1 #Tag2 | 摘要 \\| A |\n"
    )
    assert (vault / "目录" / "生活.md").exists()


def test_dataview_serializer_generates_query_pages(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text(
        "---\ncategory: 技术\nsummary: 摘要\n---\nbody\n",
        encoding="utf-8",
    )

    rc = generate_category_pages.main(
        [str(vault), "--if-exists", "abort", "--dataview-serializer"]
    )

    assert rc == 0
    assert (vault / "目录" / "技术.md").read_text(encoding="utf-8") == (
        '<!-- QueryToSerialize: TABLE tags AS "Tags", summary AS "Summary" '
        'WHERE category="技术" -->\n'
    )


def test_merge_keeps_existing_files_and_overwrites_generated(tmp_path):
    vault = tmp_path / "vault"
    out = vault / "目录"
    out.mkdir(parents=True)
    (out / "keep.txt").write_text("keep", encoding="utf-8")
    (out / "技术.md").write_text("old", encoding="utf-8")
    (vault / "a.md").write_text("---\ncategory: 技术\n---\n", encoding="utf-8")

    rc = generate_category_pages.main([str(vault), "--if-exists", "merge"])

    assert rc == 0
    assert (out / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert "[[a]]" in (out / "技术.md").read_text(encoding="utf-8")


def test_table_links_to_file_name_not_title(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "example.md").write_text(
        "---\ntitle: Different Title\ncategory: 技术\n---\n",
        encoding="utf-8",
    )

    rc = generate_category_pages.main([str(vault), "--if-exists", "abort"])

    assert rc == 0
    content = (vault / "目录" / "技术.md").read_text(encoding="utf-8")
    assert "[[example]]" in content
    assert "[[Different Title]]" not in content


def test_sort_archived_desc_orders_table_newest_first(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "old.md").write_text(
        "---\ncategory: 技术\narchived_at: '2024-01-01 00:00:00'\n---\n",
        encoding="utf-8",
    )
    (vault / "new.md").write_text(
        "---\ncategory: 技术\narchived_at: '2024-02-01 00:00:00'\n---\n",
        encoding="utf-8",
    )
    (vault / "missing.md").write_text("---\ncategory: 技术\n---\n", encoding="utf-8")

    rc = generate_category_pages.main(
        [str(vault), "--if-exists", "abort", "--sort-by-time"]
    )

    assert rc == 0
    content = (vault / "目录" / "技术.md").read_text(encoding="utf-8")
    assert content.index("[[new]]") < content.index("[[old]]")
    assert content.index("[[old]]") < content.index("[[missing]]")


def test_sort_archived_desc_updates_dataview_serializer_sort(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("---\ncategory: 技术\n---\n", encoding="utf-8")

    rc = generate_category_pages.main(
        [
            str(vault),
            "--if-exists",
            "abort",
            "--dataview-serializer",
            "--sort-by-time",
        ]
    )

    assert rc == 0
    content = (vault / "目录" / "技术.md").read_text(encoding="utf-8")
    assert "SORT archived_at DESC" in content


def test_delete_recreates_output_directory(tmp_path):
    vault = tmp_path / "vault"
    out = vault / "目录"
    out.mkdir(parents=True)
    (out / "old.txt").write_text("old", encoding="utf-8")
    (vault / "a.md").write_text("---\ncategory: 技术\n---\n", encoding="utf-8")

    rc = generate_category_pages.main([str(vault), "--if-exists", "delete"])

    assert rc == 0
    assert not (out / "old.txt").exists()
    assert (out / "技术.md").exists()


def test_sanitizes_filenames_and_disambiguates_collisions(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text('---\ncategory: "a/b"\n---\n', encoding="utf-8")
    (vault / "b.md").write_text("---\ncategory: ab\n---\n", encoding="utf-8")

    rc = generate_category_pages.main(
        [str(vault), "--if-exists", "abort", "--dataview-serializer"]
    )

    assert rc == 0
    paths = sorted(path.name for path in (vault / "目录").glob("*.md"))
    assert len(paths) == 2
    assert "ab.md" in paths
    disambiguated = [path for path in paths if path != "ab.md"]
    assert disambiguated[0].startswith("ab-")
    assert disambiguated[0].endswith(".md")


def test_escapes_quotes_in_query_literal(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text(
        '---\ncategory: "a \\"quote\\""\n---\n', encoding="utf-8"
    )

    rc = generate_category_pages.main(
        [str(vault), "--if-exists", "abort", "--dataview-serializer"]
    )

    assert rc == 0
    output = next((vault / "目录").glob("*.md")).read_text(encoding="utf-8")
    assert 'WHERE category="a \\"quote\\""' in output


def test_skips_invalid_frontmatter(tmp_path, capsys):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "bad.md").write_text(
        "---\ncategory: [unterminated\n---\n", encoding="utf-8"
    )
    (vault / "good.md").write_text("---\ncategory: 技术\n---\n", encoding="utf-8")

    rc = generate_category_pages.main([str(vault), "--if-exists", "abort"])

    assert rc == 0
    assert (vault / "目录" / "技术.md").exists()
    assert "warning: skipped" in capsys.readouterr().err


def test_generate_graph_settings_writes_obsidian_graph_json(tmp_path):
    vault = tmp_path / "vault"
    graph_path = vault / ".obsidian" / "graph.json"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_text("old", encoding="utf-8")
    (vault / "a.md").write_text("---\ncategory: 技术\n---\n", encoding="utf-8")
    (vault / "b.md").write_text("---\ncategory: 生活\n---\n", encoding="utf-8")

    rc = generate_category_pages.main(
        [str(vault), "--if-exists", "abort", "--generate-graph-settings"]
    )

    assert rc == 0
    data = json.loads(graph_path.read_text(encoding="utf-8"))
    assert data["showTags"] is True
    assert [group["query"] for group in data["colorGroups"]] == [
        '["category":技术]',
        '["category":生活]',
    ]
    for group in data["colorGroups"]:
        assert group["color"]["a"] == 1
        assert 0 <= group["color"]["rgb"] <= 0xFFFFFF


def test_render_graph_settings_can_escape_json_strings():
    content = generate_category_pages.render_graph_settings(
        ['a "quote"'],
        rng=generate_category_pages.random.Random(0),
    )

    data = json.loads(content)
    assert data["colorGroups"][0]["query"] == '["category":a "quote"]'
