"""admin_procedures.preview — MCP Apps プレビューホスト (ローカル HTTP サーバー)。

Claude Code など MCP Apps UI を描画しないホストでの開発向けに、
SEP-1865 ホスト役を模したページを localhost に立てる。
ページ側は Chrome の内蔵 AI (Prompt API / Gemini Nano) をモデルとして
ツール呼び出し → UI 描画の一連の流れをブラウザだけで確認できる。

エンドポイント:
    GET  /                -- ホストページ (チャット + iframe ホスト)
    GET  /ui/<name>       -- MCP Apps UI テンプレート (iframe 用)
    GET  /api/describe    -- ツール定義 + 使い方ガイド (JSON)
    GET  /api/version     -- ハーネス内容ハッシュ (?watch の自動再実行用)
    GET  /api/eval-cases  -- eval ケース取得 (?set=full 等でセット選択)
    POST /api/call        -- ツール実行 {"name": ..., "arguments": {...}}
    POST /api/turn-log    -- ターン単位のライブログ追記 (tests/evals/results/turns.jsonl)
    POST /api/eval-result -- eval 結果保存 (tests/evals/results/latest.json ほか)

/api/call のレスポンスは MCP の tools/call result と同形
(content + structuredContent) で、ホストページがそのまま
ui/notifications/tool-result として iframe に転送する。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from admin_procedures.query import (
    apply_columnar,
    coerce_dict,
    coerce_list,
    json_compact,
)
from admin_procedures.response import (
    DatasetResolveError,
    ToolInputError,
    build_inspect_response,
    build_list_response,
    execute_query,
    execute_summarize,
    get_full_description,
    resolve_dataset,
)

logger = logging.getLogger(__name__)

# HTTP リクエスト制限
MAX_POST_BODY_SIZE = 1_000_000  # 1 MB

_UI_URI_PREFIX = "ui://administrative-procedures-mcp/"

# ツール名 → UI テンプレート名 (server.py の resource_uri と同じ対応)
TOOL_TEMPLATES = {
    "list_datasets": "list_datasets",
    "inspect_dataset": "inspect_dataset",
    "query_records": "query_records",
    "summarize_records": "summarize_records",
}


def _require(arguments: dict[str, Any], key: str) -> Any:
    value = arguments.get(key)
    if value is None or value == "":
        raise ValueError(f"{key} は必須です")
    return value


def call_tool(registry, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """ツールを実行して columnar 形式の結果 dict を返す。

    server.py のツール実装と同じく JSON 文字列パラメータを coercion し、
    MCP サーバーと同一のレスポンス (dataset_id を含む) を返す。

    Raises:
        ValueError            -- 未知のツール名・必須パラメータ欠落・coercion 失敗
        DatasetResolveError   -- データセット未発見
        ToolInputError        -- クエリパラメータ不正
    """
    args = arguments or {}

    if name == "list_datasets":
        result = build_list_response(
            registry, q=args.get("q"), publisher=args.get("publisher"),
        )
    elif name == "inspect_dataset":
        dataset_id = _require(args, "dataset_id")
        entry, ver, dsd = resolve_dataset(registry, dataset_id)
        result = build_inspect_response(entry, ver, dsd, dataset_id=dataset_id)
    elif name == "query_records":
        result = execute_query(
            registry, _require(args, "dataset_id"),
            q=args.get("q"),
            search_fields=coerce_list(args.get("search_fields")),
            select=coerce_list(args.get("select")),
            where=coerce_dict(args.get("where")),
            order_by=args.get("order_by"),
            limit=args.get("limit") or 50,
            cursor=args.get("cursor"),
        )
    elif name == "summarize_records":
        result = execute_summarize(
            registry, _require(args, "dataset_id"),
            metrics=coerce_list(args.get("metrics")),
            group_by=coerce_list(args.get("group_by")),
            where=coerce_dict(args.get("where")),
            having=coerce_dict(args.get("having")),
            explode=args.get("explode") or None,
            limit=args.get("limit") or 200,
        )
    else:
        raise ValueError(
            f"未知のツール: {name}。利用可能: {', '.join(sorted(TOOL_TEMPLATES))}"
        )

    return apply_columnar(result)


def build_call_result(registry, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """MCP tools/call result 形式 (content + structuredContent) を組み立てる。

    ツール実行エラーは isError: true の正常レスポンスとして返す
    (MCP のツール実行エラーと同じ扱い。プロトコルエラーは呼び出し元で処理)。
    """
    try:
        data = call_tool(registry, name, arguments)
    except (DatasetResolveError, ToolInputError) as e:
        data = e.to_dict()
    except ValueError as e:
        data = {"error": str(e)}
    else:
        return {
            "content": [{"type": "text", "text": json_compact(data)}],
            "structuredContent": data,
            "isError": False,
            "_meta": {"ui": {"resourceUri": _UI_URI_PREFIX + TOOL_TEMPLATES[name]}},
        }
    return {
        "content": [{"type": "text", "text": json_compact(data)}],
        "structuredContent": data,
        "isError": True,
    }


# DADS ライトのセマンティックトークン (base.css と同値) を後勝ちで再宣言し、
# prefers-color-scheme: dark のメディアクエリより優先させる。
_LIGHT_THEME_OVERRIDE = """<style>
:root {
  --bg: #ffffff; --bg-alt: #f2f2f2;
  --text: #1a1a1a; --text-muted: #767676;
  --border: #cccccc; --border-strong: #666666;
  --accent: #0017c1; --accent-light: #e8f1fe;
  --green: #259d63; --green-bg: #e6f5ec;
  --yellow: #b78f00; --yellow-bg: #fbf5e0;
  --red: #ec0000; --red-bg: #fdeeee;
  --chart-1: #0017C1; --chart-2: #D2A400; --chart-3: #3460FB;
  --chart-4: #A58000; --chart-5: #7096F8; --chart-other: #999999;
  color-scheme: light;
}
</style>"""


def _load_host_page() -> str:
    """ホストページに base.css (DADS トークン + コンポーネント) をインライン注入して返す。

    UI テンプレートと同じ /* INLINE_BASE_CSS */ マーカー方式。
    base.css の変更も /api/version のハッシュに反映され、watch の再実行対象になる。
    """
    ui_dir = Path(__file__).parent / "ui"
    html = (ui_dir / "preview_host.html").read_text(encoding="utf-8")
    css = (ui_dir / "base.css").read_text(encoding="utf-8")
    return html.replace("/* INLINE_BASE_CSS */", css)


def _eval_dir() -> Path:
    """eval ケース・結果の置き場所。

    既定はリポジトリの tests/evals/。ADMIN_PROCEDURES_EVAL_DIR で上書き可
    (テストや pip インストール環境向け)。
    """
    env = os.environ.get("ADMIN_PROCEDURES_EVAL_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "tests" / "evals"


def _save_eval_result(payload: dict[str, Any]) -> Path:
    """eval 結果をタイムスタンプ付き + latest.json で保存する。"""
    results_dir = _eval_dir() / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["saved_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    stamped = results_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    stamped.write_text(body, encoding="utf-8")
    (results_dir / "latest.json").write_text(body, encoding="utf-8")
    return stamped


# ── リクエスト検証 (DNS リバインディング / CSRF 対策) ──
#
# 127.0.0.1 バインドでも、利用者のブラウザを経由して外部サイトからリクエストが届き得る:
#   - CSRF: 悪意あるページからの form POST (応答は読めないが書き込み・ツール実行は成立する)
#   - DNS リバインディング: 攻撃者ドメインを 127.0.0.1 に解決させ、同一オリジン扱いで
#     応答まで読み取る (apcli add で取り込んだ私有データの窃取につながる)
# 前者は POST の Content-Type: application/json 必須化で遮断する
# (HTML form は urlencoded / multipart / text-plain しか送れない)。
# 後者は Host ヘッダーのホスト名照合で遮断する (攻撃者ドメイン経由のアクセスは
# Host: attacker.example になるため、ホスト名を見るだけで判別できる)。

_LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})
# ワイルドカードバインド時は正当なアクセス元ホスト名を列挙できないため照合しない
# (cli 側で外部公開の警告を出す)
_WILDCARD_BIND_HOSTS = frozenset({"0.0.0.0", "::", "[::]"})


def _hostname_of(host_header: str) -> str:
    """Host ヘッダーからポート部を除いたホスト名を返す ("[::1]:8765" → "[::1]")。"""
    h = host_header.strip().lower()
    if h.startswith("["):
        return h.split("]", 1)[0] + "]"
    return h.split(":", 1)[0]


def host_header_allowed(host_header: str | None, bind_host: str) -> bool:
    """Host ヘッダーが待ち受けホストとして正当か判定する。ポートは照合しない。"""
    if bind_host in _WILDCARD_BIND_HOSTS:
        return True
    if not host_header:
        return False
    return _hostname_of(host_header) in _LOOPBACK_HOSTNAMES | {bind_host.lower()}


# 全アセット同梱・外部通信なしの前提を CSP でも強制する (多層防御)。
# インラインスクリプト主体のため script-src は 'unsafe-inline' が必要だが、
# connect-src 'self' により万一スクリプトが混入しても外部送信は遮断される。
_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self' data:; connect-src 'self'; frame-src 'self'; "
    "base-uri 'none'; form-action 'self'"
)


def make_server(registry, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """プレビューホストの HTTP サーバーを構築する (起動はしない)。"""

    class PreviewHandler(BaseHTTPRequestHandler):
        server_version = "apcli-preview"

        def log_message(self, fmt: str, *log_args: Any) -> None:
            logger.debug("%s " + fmt, self.address_string(), *log_args)

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # 開発サーバーのためキャッシュさせない (テンプレート編集を即反映)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str, status: int = 200) -> None:
            self.send_response(status)
            body = html.encode("utf-8")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", _CSP)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, data: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")

        def _reject_untrusted(self, *, check_content_type: bool = False) -> bool:
            """Host / Content-Type を検証し、不正なら 403 / 415 を返して True を返す。"""
            if not host_header_allowed(self.headers.get("Host"), host):
                self._send_json(
                    {"error": "不正な Host ヘッダーです (DNS リバインディング対策)"},
                    status=403,
                )
                return True
            if check_content_type:
                ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0]
                if ctype.strip().lower() != "application/json":
                    self._send_json(
                        {"error": "Content-Type: application/json で送信してください"},
                        status=415,
                    )
                    return True
            return False

        def do_GET(self) -> None:  # noqa: N802 (http.server 規約)
            if self._reject_untrusted():
                return
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send_html(_load_host_page())
            elif path.startswith("/ui/"):
                name = path[len("/ui/"):]
                if name not in TOOL_TEMPLATES.values():
                    self._send_json({"error": f"未知のテンプレート: {name}"}, status=404)
                    return
                from urllib.parse import parse_qs, urlparse
                from admin_procedures.ui import load_template
                html = load_template(name)
                qs = parse_qs(urlparse(self.path).query)
                if (qs.get("theme") or [""])[0] == "light":
                    # プレビューホストの白基調に合わせ、OS がダークでもライトで描画する。
                    # base.css のライトのセマンティックトークンを後勝ちで再宣言する
                    # (テンプレートのスクリプトはこの値を computed style から読む)。
                    html = html.replace("</head>", _LIGHT_THEME_OVERRIDE + "\n</head>")
                self._send_html(html)
            elif path == "/api/describe":
                self._send_json(get_full_description())
            elif path == "/api/version":
                # ハーネス (ホストページ + eval ケース) の内容ハッシュ。
                # watch モードのページがこれを監視し、変化したら自動リロードする。
                import hashlib
                h = hashlib.sha1()
                h.update(_load_host_page().encode("utf-8"))
                for cases_file in sorted(_eval_dir().glob("cases*.json")):
                    h.update(cases_file.read_bytes())
                self._send_json({"etag": h.hexdigest()})
            elif path == "/api/eval-cases":
                # ?set=full で cases-full.json 等の別セットを選択できる
                import re as _re
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                set_name = (qs.get("set") or [""])[0]
                if set_name and not _re.fullmatch(r"[a-z0-9-]{1,32}", set_name):
                    self._send_json({"error": f"未対応の set 名: {set_name}"}, status=400)
                    return
                filename = f"cases-{set_name}.json" if set_name else "cases.json"
                cases_file = _eval_dir() / filename
                if not cases_file.exists():
                    self._send_json(
                        {"error": f"eval ケースがありません: {cases_file}"}, status=404)
                    return
                try:
                    cases = json.loads(cases_file.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as e:
                    self._send_json({"error": f"cases.json を読めません: {e}"}, status=500)
                    return
                self._send_json({"cases": cases})
            else:
                self._send_json({"error": "not found"}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            if self._reject_untrusted(check_content_type=True):
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send_json({"error": "Content-Length が不正です"}, status=400)
                return
            if length < 0 or length > MAX_POST_BODY_SIZE:
                self._send_json(
                    {"error": f"リクエストサイズが上限を超えています（上限: {MAX_POST_BODY_SIZE} バイト）"},
                    status=413
                )
                return
            path = self.path.split("?", 1)[0]
            if path == "/api/turn-log":
                # ターン単位のライブログ (Q → ツール呼び出し → A)。
                # 外部ツール (Claude Code 等) が tail してチャット内容を追跡できる。
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(payload, dict):
                        raise TypeError("JSON オブジェクトで送信してください")
                    # 良性のブラウザ警告は記録しない (古いタブからの洪水対策)
                    if "ResizeObserver loop" in str(payload.get("answer", "")):
                        self._send_json({"ok": True, "skipped": True})
                        return
                    payload["at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    results_dir = _eval_dir() / "results"
                    results_dir.mkdir(parents=True, exist_ok=True)
                    with open(results_dir / "turns.jsonl", "a", encoding="utf-8") as f:
                        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
                except (json.JSONDecodeError, TypeError, OSError) as e:
                    self._send_json({"error": f"記録に失敗しました: {e}"}, status=400)
                    return
                self._send_json({"ok": True})
                return
            if path == "/api/eval-result":
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(payload, dict):
                        raise TypeError("結果は JSON オブジェクトで送信してください")
                    saved = _save_eval_result(payload)
                except (json.JSONDecodeError, TypeError, OSError) as e:
                    self._send_json({"error": f"保存に失敗しました: {e}"}, status=400)
                    return
                self._send_json({"saved": str(saved)})
                return
            if path != "/api/call":
                self._send_json({"error": "not found"}, status=404)
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                name = payload["name"]
                arguments = payload.get("arguments") or {}
                if not isinstance(arguments, dict):
                    raise TypeError("arguments はオブジェクトで指定してください")
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                self._send_json({"error": f"リクエストを解釈できません: {e}"}, status=400)
                return
            if name not in TOOL_TEMPLATES:
                self._send_json(
                    {"error": f"未知のツール: {name}",
                     "available": sorted(TOOL_TEMPLATES)},
                    status=400,
                )
                return
            self._send_json(build_call_result(registry, name, arguments))

    return ThreadingHTTPServer((host, port), PreviewHandler)


def run_preview(
    registry,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    """プレビューホストを起動し、Ctrl-C まで待ち受ける。"""
    import webbrowser

    server = make_server(registry, host=host, port=port)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"MCP Apps プレビューホスト: {url}", flush=True)
    print("Chrome で開くと内蔵 AI (Prompt API / Gemini Nano) との対話を確認できます。", flush=True)
    print("停止: Ctrl-C", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
