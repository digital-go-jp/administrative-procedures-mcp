"""admin_procedures.cli — ダイレクト実行 CLI (apcli)。

MCP サーバー不要でレジストリをインプロセス構築し、
response.py のパイプライン関数を直接呼び出す。

--html フラグで自己完結型 HTML レポートを出力可能。
ブラウザで直接開く・ファイル保存・パイプに流す等の2次利用ができる。

Exit codes:
    0 — 成功
    1 — 入力エラー (不正なパラメータ、JSON パース失敗等)
    2 — データセット未発見
"""

from __future__ import annotations

import functools
import json
import logging
import sys
from pathlib import Path
from typing import Any

import click

from admin_procedures.response import DatasetResolveError, ToolInputError

builtins_list = list  # click コマンド "list" との名前衝突を回避

# Exit codes
EXIT_OK = 0
EXIT_INPUT_ERROR = 1
EXIT_DATASET_NOT_FOUND = 2


@functools.lru_cache(maxsize=1)
def _get_registry():
    """レジストリをインプロセスで構築する (遅延 import、同一プロセス内キャッシュ)。"""
    from admin_procedures.loader import init_registry, resolve_data_dir
    return init_registry(resolve_data_dir())


def _error_exit(exc: Exception) -> None:
    """例外を JSON エラーとして stderr に出力し、適切な exit code で終了する。"""
    if isinstance(exc, DatasetResolveError):
        msg = json.dumps(exc.to_dict(), ensure_ascii=False, indent=2)
        code = EXIT_DATASET_NOT_FOUND
    elif isinstance(exc, ToolInputError):
        msg = json.dumps(exc.to_dict(), ensure_ascii=False, indent=2)
        code = EXIT_INPUT_ERROR
    else:
        arg = exc.args[0] if exc.args else str(exc)
        if isinstance(arg, dict):
            msg = json.dumps(arg, ensure_ascii=False, indent=2)
        else:
            msg = json.dumps({"error": str(arg)}, ensure_ascii=False, indent=2)
        code = EXIT_INPUT_ERROR
    click.echo(msg, err=True)
    sys.exit(code)


def _emit_result(
    data: dict[str, Any],
    *,
    template: str,
    as_html: bool,
    output_file: str | None,
) -> None:
    """結果を JSON / HTML / ファイルに出力する共通関数。"""
    data.pop("dataset_id", None)
    out = Path(output_file) if output_file else None
    html = as_html or out is not None

    if out:
        from admin_procedures.ui import render_standalone
        out.write_text(render_standalone(template, data), encoding="utf-8")
        click.echo(f"Saved: {out}", err=True)
    elif html:
        from admin_procedures.ui import render_standalone
        click.echo(render_standalone(template, data))
    else:
        click.echo(json.dumps(data, ensure_ascii=False, indent=2))


def _html_option(f):
    """--html / -o オプションを追加するデコレータ。"""
    f = click.option("--html", "as_html", is_flag=True, help="自己完結型 HTML で出力する。")(f)
    f = click.option("-o", "--output", "output_file", type=click.Path(), default=None,
                     help="HTML 出力先ファイルパス (--html 暗黙有効)。")(f)
    return f


def _data_dir_option(f):
    """--data-dir オプションを追加するデコレータ (fetch/add で共有)。"""
    return click.option(
        "--data-dir", type=click.Path(path_type=Path), default=None,
        help="datasets/ を置くベースディレクトリ (既定: ADMIN_PROCEDURES_DATA_DIR → 自動検出)。",
    )(f)


def _resolve_multi(values: tuple[str, ...]) -> list[str] | None:
    """複数指定オプションの値を解決する。

    --flag a --flag b の複数指定と、--flag '["a","b"]' の JSON 配列（既存互換）の
    両方を受け付ける。単一のプレーンな値（--flag a）はそのまま1要素のリストにする。
    JSON 配列の短縮記法は単独指定のときのみ有効（繰り返しとの併用は非対応）。
    """
    if not values:
        return None
    if len(values) == 1 and values[0].lstrip().startswith("["):
        from admin_procedures.query import coerce_list
        return coerce_list(values[0])
    return builtins_list(values)


def _multi_option(*param_decls, help_text: str):
    """複数指定可能なリスト系オプション用デコレータ。"""
    return click.option(*param_decls, multiple=True,
                         help=f"{help_text} (複数指定可、または JSON 配列)。")


_HELP_EPILOG = """\b
ワークフロー:
  1. apcli list              — dataset_id を確認
  2. apcli inspect <id>      — フィールド名・型・品質を確認
  3. apcli query <id>        — データ取得
  4. apcli summarize <id>    — 集計
\b
手元の CSV を分析する:
  apcli add <id> --csv data.csv   — YAML + Parquet を生成して取り込む
  apcli inspect <id>              — 生成直後からそのまま分析できる
  （YAML の desc / notes 補完は任意。埋めると AI の回答精度が上がる）
\b
inspect フィールド:
  role          id=識別子, dim=分析軸, measure=数値項目, attr=属性
  groupable     1 のフィールドのみ --group-by に使用可能
  aggregatable  1 のフィールドのみ sum/avg/min/max に使用可能
  multi_value   1 のフィールドはセミコロン区切り (group_by で自動展開)
\b
複数指定オプション (--select / --search-fields / --group-by / --metrics):
  -g 所管府省庁 -g 手続類型        フラグを繰り返す（推奨、-g value の形式で）
  --group-by '["所管府省庁","手続類型"]'  JSON 配列でも可（単独指定のみ）
\b
主な短縮形:
  -q  キーワード検索 (list / query に対して)
  -w  --where          -s  --select (query に対して)
  -g  --group-by       -m  --metrics (summarize に対して)
  -o  --output（--html 出力先）
\b
where 構文:
  文字列          部分一致
  配列            IN (完全一致のいずれか)
  $gte/$lte      範囲
  $ne            不等
  $not_contains  部分不一致
  $not_empty     非空 (値は null: {"$not_empty": null})
  複数キー        複合条件 (AND)
\b
出力形式:
  JSON       デフォルト (columnar: columns + rows)
  --html     自己完結型 HTML (ブラウザで閲覧可能)
  -o FILE    HTML ファイルに保存
\b
MCP Apps UI の確認:
  apcli preview              プレビューホストを起動 (Chrome 内蔵 AI で対話確認)
\b
Exit codes:
  0  成功
  1  入力エラー
  2  データセット未発見
\b
LLM エージェント向け:
  apcli describe             全ツール定義を JSON Schema で出力
  apcli describe <tool_name> 個別ツール定義を出力
"""


@click.group(epilog=_HELP_EPILOG, context_settings={"max_content_width": 120})
@click.option("--quiet", is_flag=True, help="stderr への診断メッセージを抑制する。")
def main(quiet: bool) -> None:
    """apcli — 行政手続データ分析 CLI"""
    if quiet:
        logging.disable(logging.CRITICAL)


# apcli の短い名前 → fastmcp install のサブコマンド
_INSTALL_TARGETS = {
    "desktop": "claude-desktop",
    "claude-code": "claude-code",
    "cursor": "cursor",
    "gemini-cli": "gemini-cli",
    "goose": "goose",
    "json": "mcp-json",
}


@main.command()
@click.argument("target", type=click.Choice(sorted(_INSTALL_TARGETS)))
@click.option("--name", default="admin-procedures", help="クライアント設定上のサーバー名。")
def install(target: str, name: str) -> None:
    """MCP クライアントにこのサーバーを登録する。

    claude_desktop_config.json 等を手で編集する必要をなくす。
    `json` を指定した場合は設定内容を表示するだけで、ファイルは変更しない。

    Claude Code はリポジトリ同梱の .mcp.json を読むため、登録は不要。
    """
    import os
    import subprocess

    import admin_procedures
    from admin_procedures.loader import DATA_DIR_ENV_VAR

    # server.py は import しない。モジュールレベルで create_mcp() が走るため、
    # データ未取得やパス誤りがあるとインストール前に失敗してしまう。
    package_dir = Path(admin_procedures.__file__).resolve().parent
    server_file = package_dir / "server.py"
    project_root = package_dir.parent.parent

    argv = [
        sys.executable, "-m", "fastmcp.cli", "install", _INSTALL_TARGETS[target],
        f"{server_file}:mcp", "--name", name,
        "--with", "fastexcel",
    ]
    # リポジトリを clone して使う想定なので editable を優先する
    if (project_root / "pyproject.toml").exists():
        argv += ["--with-editable", str(project_root)]
    else:
        argv += ["--with", "administrative-procedures-mcp"]

    # データを別ディレクトリに置いている場合、その場所を引き継ぐ
    data_dir = os.environ.get(DATA_DIR_ENV_VAR)
    if data_dir:
        argv += ["--env", f"{DATA_DIR_ENV_VAR}={data_dir}"]

    sys.exit(subprocess.run(argv).returncode)


@main.command()
@click.argument("tool_name", required=False, default=None)
def describe(tool_name: str | None) -> None:
    """ツール定義を JSON で出力する。LLM エージェント向け。"""
    from admin_procedures.response import TOOL_DEFINITIONS, get_full_description, get_tool_def

    if tool_name:
        data = get_tool_def(tool_name)
        if data is None:
            names = [t["name"] for t in TOOL_DEFINITIONS]
            click.echo(
                json.dumps({"error": f"Unknown tool: {tool_name}", "available": names},
                           ensure_ascii=False, indent=2),
                err=True,
            )
            sys.exit(EXIT_INPUT_ERROR)
    else:
        data = get_full_description()
    click.echo(json.dumps(data, ensure_ascii=False, indent=2))


@main.command("list")
@click.option("-q", "--query", "q", default=None, help="キーワード検索 (dataset_id / タイトル部分一致)。")
@click.option("--publisher", default=None, help="発行者名でフィルタ (部分一致)。")
@_html_option
def list_datasets(q: str | None, publisher: str | None, as_html: bool, output_file: str | None) -> None:
    """利用可能なデータセット一覧を表示する。"""
    from admin_procedures.response import build_list_response

    data = build_list_response(_get_registry(), q=q, publisher=publisher)
    _emit_result(data, template="list_datasets", as_html=as_html, output_file=output_file)


@main.command()
@click.argument("dataset_id")
@_data_dir_option
@click.option("--allowed-host", "allowed_hosts", multiple=True,
              help="接続を許可するホスト名（複数指定可）。指定時はこのホスト以外への接続を拒否する。")
def fetch(dataset_id: str, data_dir: Path | None, allowed_hosts: tuple[str, ...]) -> None:
    """配布元から最新データを取得して取り込む。

    dataset.yaml の source.url（配布ページ）を開き、source.asset_pattern に
    一致する配布ファイルを探して取得する。取得日は fetched_at に記録される。
    """
    from admin_procedures.prepare_dataset import FetchError
    from admin_procedures.prepare_dataset import fetch as _fetch

    try:
        _fetch(dataset_id, base_dir=data_dir, allowed_hosts=list(allowed_hosts) or None)
    except FetchError as e:
        click.echo(
            f"{e}\n\n"
            f"自動取得に失敗しました。配布ページからファイルを手元に保存し、\n"
            f"次のコマンドで取り込んでください:\n"
            f"  apcli add {dataset_id} --csv <保存したファイル>",
            err=True,
        )
        sys.exit(EXIT_INPUT_ERROR)
    except (ValueError, FileNotFoundError) as e:
        _error_exit(e)
    _get_registry.cache_clear()


@main.command()
@click.argument("dataset_id")
@click.option("--csv", "csv_path", type=click.Path(path_type=Path), default=None,
              help="CSV ファイルパス (dataset.yaml が無い場合は必須)。")
@click.option("--header-rows", type=int, default=1, help="CSV ヘッダー行数 (scaffold 時)。")
@click.option("--encoding", default="utf-8-sig", help="CSV エンコーディング (scaffold 時)。")
@_data_dir_option
@click.option("--force-scaffold", is_flag=True, help="dataset.yaml が存在しても再生成する。")
@click.option("-y", "--yes", "assume_yes", is_flag=True,
              help="補完済み YAML を上書きする際の確認を省略する。")
def add(
    dataset_id: str,
    csv_path: Path | None,
    header_rows: int,
    encoding: str,
    data_dir: Path | None,
    force_scaffold: bool,
    assume_yes: bool,
) -> None:
    """CSV からデータセットを追加する。

    dataset.yaml が無ければ CSV を分析して YAML + Parquet を生成し (scaffold)、
    あれば YAML 定義に基づいて Parquet を再変換する (convert)。
    生成直後の YAML は desc が空だが、そのまま query / summarize できる。
    """
    from admin_procedures.prepare_dataset import run

    try:
        run(
            dataset_id,
            csv=csv_path,
            header_rows=header_rows,
            encoding=encoding,
            base_dir=data_dir,
            force_scaffold=force_scaffold,
            assume_yes=assume_yes,
        )
    except (ValueError, FileNotFoundError) as e:
        _error_exit(e)
    # 同一プロセスで直後に inspect 等を呼ぶ場合に備えてキャッシュを捨てる
    _get_registry.cache_clear()


@main.command()
@click.argument("dataset_id")
@_html_option
def inspect(dataset_id: str, as_html: bool, output_file: str | None) -> None:
    """データセットの構造と品質を検査する。"""
    from admin_procedures.response import build_inspect_response, resolve_dataset  # noqa: F811

    try:
        entry, ver, dsd = resolve_dataset(_get_registry(), dataset_id)
    except DatasetResolveError as e:
        _error_exit(e)
    data = build_inspect_response(entry, ver, dsd, dataset_id=dataset_id)
    _emit_result(data, template="inspect_dataset", as_html=as_html, output_file=output_file)


@main.command()
@click.argument("dataset_id")
@click.option("-q", "--query", "q", default=None, help="全文検索キーワード。")
@_multi_option("--search-fields", help_text="全文検索対象フィールド")
@_multi_option("-s", "--select", help_text="出力フィールド")
@click.option("-w", "--where", default=None, help="フィルタ条件 (JSON オブジェクト)。")
@click.option("--order-by", default=None, help="ソートフィールド ('-' で降順)。")
@click.option("--limit", type=int, default=50, help="最大レコード数。")
@click.option("--cursor", default=None, help="ページネーションカーソル。")
@_html_option
def query(
    dataset_id: str,
    q: str | None,
    search_fields: tuple[str, ...],
    select: tuple[str, ...],
    where: str | None,
    order_by: str | None,
    limit: int,
    cursor: str | None,
    as_html: bool,
    output_file: str | None,
) -> None:
    """データセットのレコードを検索する。"""
    from admin_procedures.query import coerce_dict
    from admin_procedures.response import execute_query

    try:
        result = execute_query(
            _get_registry(), dataset_id,
            q=q,
            search_fields=_resolve_multi(search_fields),
            select=_resolve_multi(select),
            where=coerce_dict(where),
            order_by=order_by,
            limit=limit,
            cursor=cursor,
        )
    except (DatasetResolveError, ToolInputError, ValueError) as e:
        _error_exit(e)
    _emit_result(result, template="query_records", as_html=as_html, output_file=output_file)


@main.command()
@click.argument("dataset_id")
@_multi_option("-g", "--group-by", help_text="グループ化フィールド")
@_multi_option("-m", "--metrics", help_text="集計メトリクス")
@click.option("-w", "--where", default=None, help="フィルタ条件 (JSON オブジェクト)。")
@click.option("--having", default=None, help="集計後フィルタ (JSON オブジェクト)。")
@click.option("--explode", default=None, help="展開する multi_value フィールド。")
@click.option("--limit", type=int, default=200, help="最大グループ数。")
@_html_option
def summarize(
    dataset_id: str,
    group_by: tuple[str, ...],
    metrics: tuple[str, ...],
    where: str | None,
    having: str | None,
    explode: str | None,
    limit: int,
    as_html: bool,
    output_file: str | None,
) -> None:
    """データセットレコードを集計する。"""
    from admin_procedures.query import coerce_dict
    from admin_procedures.response import execute_summarize

    try:
        result = execute_summarize(
            _get_registry(), dataset_id,
            metrics=_resolve_multi(metrics),
            group_by=_resolve_multi(group_by),
            where=coerce_dict(where),
            having=coerce_dict(having),
            explode=explode,
            limit=limit,
        )
    except (DatasetResolveError, ToolInputError, ValueError) as e:
        _error_exit(e)
    _emit_result(result, template="summarize_records", as_html=as_html, output_file=output_file)


@main.command()
@click.option("--port", type=int, default=8765, help="待ち受けポート (既定: 8765)。")
@click.option("--host", "bind_host", default="127.0.0.1", help="バインドアドレス (既定: 127.0.0.1)。")
@click.option("--no-open", is_flag=True, help="ブラウザを自動で開かない。")
@click.option("--unsafe-expose", is_flag=True, help="非 loopback (0.0.0.0 など) へのバインドを許可。")
def preview(port: int, bind_host: str, no_open: bool, unsafe_expose: bool) -> None:
    """MCP Apps UI のプレビューホストをブラウザで起動する。

    SEP-1865 ホスト役を模したページを localhost に立てる。
    Chrome の内蔵 AI (Prompt API / Gemini Nano) をモデルとして、
    質問 → ツール選択 → UI 描画の一連の流れを実ホスト無しで確認できる。
    内蔵 AI が使えない環境でも、起動時のデータセット探索までは UI の描画を確認できる。
    """
    from admin_procedures.preview import run_preview

    is_loopback = bind_host in ("127.0.0.1", "localhost", "::1", "[::1]")
    if not is_loopback and not unsafe_expose:
        click.echo(
            f"エラー: {bind_host} での外部公開はデフォルトで拒否されています。\n"
            "  --unsafe-expose を指定して明示的に有効化してください。",
            err=True,
        )
        sys.exit(EXIT_INPUT_ERROR)

    try:
        run_preview(_get_registry(), host=bind_host, port=port, open_browser=not no_open)
    except OSError as e:
        click.echo(f"サーバーを起動できません ({bind_host}:{port}): {e}", err=True)
        click.echo("--port で別のポートを指定してください。", err=True)
        sys.exit(EXIT_INPUT_ERROR)


if __name__ == "__main__":
    main()
