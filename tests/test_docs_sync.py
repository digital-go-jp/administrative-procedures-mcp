"""日本語ドキュメントと英語版のコード例が一致していることを確認する。

日本語版が正本で、英語版は翻訳。説明文は言語ごとに異なってよいが、
bash / json のコードブロックはコマンドや設定例そのものなので、両言語で
同一でなければならない。翻訳されうる部分（bash のコメント、<...> の
プレースホルダ）は比較から除く。
"""

import re
from collections import Counter
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent

# (日本語版, 英語版)
DOC_PAIRS = [
    ("README.md", "README.en.md"),
    ("docs/development.md", "docs/development.en.md"),
    ("docs/dataset-yaml-guide.md", "docs/dataset-yaml-guide.en.md"),
]

# 比較対象の言語。text（プロンプト例・ツリー図）や mermaid は翻訳されるので除外
COMPARED_LANGS = {"bash", "json"}

_FENCE_RE = re.compile(r"^```(\w*)[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"<[^<>]+>")


def _normalize_line(lang: str, line: str) -> str | None:
    if lang == "bash":
        if line.lstrip().startswith("#"):
            return None
        line = re.sub(r"\s+#.*$", "", line)
    line = _PLACEHOLDER_RE.sub("<...>", line).rstrip()
    return line or None


def _code_blocks(rel_path: str) -> list[tuple[str, str]]:
    text = (ROOT_DIR / rel_path).read_text(encoding="utf-8")
    blocks: list[tuple[str, str]] = []
    for lang, body in _FENCE_RE.findall(text):
        if lang not in COMPARED_LANGS:
            continue
        lines = [n for n in (_normalize_line(lang, ln) for ln in body.splitlines()) if n]
        blocks.append((lang, "\n".join(lines)))
    return blocks


class TestDocsSync:
    @pytest.mark.parametrize("ja,en", DOC_PAIRS, ids=[p[0] for p in DOC_PAIRS])
    def test_english_doc_exists_and_links_back(self, ja, en):
        """英語版が存在し、冒頭で日本語版を正本として参照していること。"""
        en_text = (ROOT_DIR / en).read_text(encoding="utf-8")
        assert Path(ja).name in en_text
        ja_text = (ROOT_DIR / ja).read_text(encoding="utf-8")
        assert Path(en).name in ja_text, "日本語版に英語版への切り替えリンクが無い"

    @pytest.mark.parametrize("ja,en", DOC_PAIRS, ids=[p[0] for p in DOC_PAIRS])
    def test_code_blocks_match(self, ja, en):
        """bash / json のコードブロックが両言語で同一であること。"""
        ja_blocks = Counter(_code_blocks(ja))
        en_blocks = Counter(_code_blocks(en))
        assert ja_blocks, f"{ja} に比較対象のコードブロックが無い"
        only_ja = list((ja_blocks - en_blocks).elements())
        only_en = list((en_blocks - ja_blocks).elements())
        assert not only_ja and not only_en, (
            f"{ja} と {en} のコード例がずれています。\n"
            f"日本語版のみ: {only_ja}\n英語版のみ: {only_en}"
        )
