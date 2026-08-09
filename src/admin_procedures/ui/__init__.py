"""admin_procedures.ui — MCP Apps UI テンプレートローダー。

HTML テンプレートに base.css / base.js / ECharts をインライン注入して返す。
Claude Desktop の CSP 制約により外部 CDN は使用不可のため、
全アセットを HTML 内にインラインで埋め込む。

Functions:
    load_template      -- テンプレート名から完成 HTML を生成 (キャッシュ付き)
    render_standalone   -- テンプレート + JSON データ → 自己完結型 HTML
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_UI_DIR = Path(__file__).parent

# 共通アセットは初回アクセス時に遅延ロード（--no-ui 時のメモリ節約）
_BASE_CSS: str | None = None
_BASE_JS: str | None = None
_ECHARTS_JS: str | None = None
_template_cache: dict[str, str] = {}


def _get_base_assets() -> tuple[str, str, str]:
    """共通 CSS/JS/ECharts を遅延ロードして返す。"""
    global _BASE_CSS, _BASE_JS, _ECHARTS_JS
    if _BASE_CSS is None or _BASE_JS is None or _ECHARTS_JS is None:
        css = (_UI_DIR / "base.css").read_text(encoding="utf-8")
        js = (_UI_DIR / "base.js").read_text(encoding="utf-8")
        echarts = (_UI_DIR / "echarts.min.js").read_text(encoding="utf-8")
        _BASE_CSS, _BASE_JS, _ECHARTS_JS = css, js, echarts
    return _BASE_CSS, _BASE_JS, _ECHARTS_JS


def _build_template(name: str) -> str:
    """HTML テンプレートを読み込み、共通 CSS/JS をインライン注入する。"""
    css, js, echarts_js = _get_base_assets()
    template = (_UI_DIR / f"{name}.html").read_text(encoding="utf-8")
    return (
        template
        .replace("/* INLINE_BASE_CSS */", css)
        .replace("/* INLINE_BASE_JS */", js)
        .replace("/* INLINE_ECHARTS_JS */", echarts_js)
    )


def load_template(name: str) -> str:
    """キャッシュ済み HTML テンプレートを返す。初回アクセス時にビルドしてキャッシュする。"""
    cached = _template_cache.get(name)
    if cached is not None:
        return cached
    result = _build_template(name)
    _template_cache[name] = result
    return result


def _json_for_html(data: dict[str, Any]) -> str:
    """JSON を <script> 内に安全に埋め込める文字列にシリアライズする。

    ``</script>`` や ``<!--`` がデータ中に含まれると HTML パーサーが
    スクリプトブロックを途中で閉じてしまうため、危険な文字を
    Unicode エスケープに置換する。
    """
    return (
        json.dumps(data, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render_standalone(name: str, data: dict[str, Any]) -> str:
    """テンプレート + JSON データ → 自己完結型 HTML を生成する。

    base.js の onData() が window.__STANDALONE_DATA__ を検出し、
    MCP Apps 接続をスキップして埋め込みデータを直接描画する。
    """
    html = load_template(name)
    script = (
        '<script>window.__STANDALONE_DATA__ = '
        + _json_for_html(data)
        + ';</script>'
    )
    return html.replace("</head>", script + "\n</head>")
