"""admin_procedures.server — MCP ツール登録 + サーバー起動。

FastMCP 固有の入力 coercion (JSON 文字列→型変換) と出力整形
(ToolResult, AppConfig) をここに閉じ込め、実処理は response モジュールの
execute_query / execute_summarize に委譲する。

Functions:
    register_all_tools      -- 4 MCP Tools を FastMCP に一括登録
    register_discovery_tool -- inspect_dataset ツール登録
    register_list_datasets  -- list_datasets ツール登録
    register_data_tools     -- query_records / summarize_records 登録
    create_mcp              -- FastMCP インスタンスの構築
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

_READ_ONLY_TOOL_ANNOTATIONS = {"readOnlyHint": True}

# MCP spec 2026-07-28 の cacheable list results (ttlMs / cacheScope)。
# キャッシュ対象の tools/list・resources/list・resources/read はいずれも
# 起動時に確定する静的内容 (ツール 4 件・ui:// リソース 4 件) で、
# 利用者ごとに変わらないため public スコープで共有キャッシュしてよい。
# ECharts をインライン同梱した UI テンプレートは大きいため効果が大きい。
CACHE_TTL_MS = 3_600_000  # 1 時間
CACHE_SCOPE = "public"

_INSPECT_URI = "ui://administrative-procedures-mcp/inspect_dataset"
_LIST_URI = "ui://administrative-procedures-mcp/list_datasets"
_QUERY_URI = "ui://administrative-procedures-mcp/query_records"
_SUMMARIZE_URI = "ui://administrative-procedures-mcp/summarize_records"

# fastmcp.apps / fastmcp.tools は FastMCP 3.2.0 以降と 4.x に共通の正規パス。
# 旧 fastmcp.server.apps は 3.4 で deprecated、4.x で削除されている。
from fastmcp.apps import AppConfig
from fastmcp.tools import ToolResult
from mcp.types import TextContent

from admin_procedures.response import (  # noqa: F401 — re-export
    DatasetResolveError,
    ToolInputError,
    build_inspect_response,
    build_list_response,
    execute_query,
    execute_summarize,
    resolve_dataset,
    strip_matching_quotes,
)
from admin_procedures.query import (
    apply_columnar,
    coerce_dict,
    coerce_list,
    error_response,
    json_compact,
)

from admin_procedures.loader import init_registry, resolve_data_dir
from admin_procedures.models import (
    DatasetRegistry,
)
from admin_procedures.ui import load_template

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)


def _build_tool_kwargs(
    *,
    enable_ui: bool,
    resource_uri: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """FastMCP ツール登録の共通 kwargs を組み立てる。"""
    tool_kwargs: dict[str, Any] = {
        "annotations": dict(_READ_ONLY_TOOL_ANNOTATIONS),
    }
    if enable_ui and resource_uri:
        # MCP Apps (SEP-1865, io.modelcontextprotocol/ui) の _meta.ui。
        # visibility: 既定は ["model","app"]。エージェントからも UI からも
        #   呼べる必要があるため、既定のまま明示する。
        # csp: 宣言を省くとホストは default-src 'none' 相当の最も厳しい既定を
        #   適用する。本サーバーは CSS/JS/ECharts を全てインライン同梱しており
        #   外部通信を一切行わないため、その既定が正しい。あえて緩めない。
        tool_kwargs["app"] = AppConfig(
            resource_uri=resource_uri,
            visibility=["model", "app"],
            prefers_border=True,
        )
    if description:
        tool_kwargs["description"] = description
    return tool_kwargs


def _tool_output(
    result: dict[str, Any],
    *,
    resource_uri: str,
    enable_ui: bool,
) -> str | ToolResult:
    """UI 有効時は ToolResult、無効時はプレーン JSON テキストを返す。

    LLM テキストと UI structured_content は同一の dict を使用する。
    """
    # records/groups を columnar 形式 (columns + rows) に変換 — トークン節約
    data = apply_columnar(result)

    json_text = json_compact(data)

    if enable_ui:
        return ToolResult(
            content=[TextContent(type="text", text=json_text)],
            structured_content=data,
            meta={"ui": {"resourceUri": resource_uri}},
        )
    return json_text



def _coerce_params(**kwargs: tuple) -> dict[str, Any] | str:
    """JSON 文字列パラメータを一括変換。失敗時はエラー JSON 文字列を返す。

    Usage::

        p = _coerce_params(where=(where, coerce_dict), select=(select, coerce_list))
        if isinstance(p, str):
            return p
        where, select = p["where"], p["select"]
    """
    try:
        return {k: fn(v) for k, (v, fn) in kwargs.items()}
    except ValueError as e:
        return json_compact({"error": str(e)})


def _coerce_single_string_param(value: Any, *, label: str) -> str | None:
    """単一文字列パラメータを正規化する。

    - `'"field"'` のような余分なクォートを除去
    - `["field"]` 形式の JSON 配列文字列や長さ1の list を許容
    - 複数要素の配列は明示エラー
    """
    if value is None:
        return None
    if isinstance(value, list):
        if len(value) == 1 and isinstance(value[0], str):
            return strip_matching_quotes(value[0])
        raise ValueError(
            f"{label} は単一の文字列で指定してください。"
            f"配列ではなく文字列を渡してください。"
        )
    if not isinstance(value, str):
        raise ValueError(f"{label} は文字列で指定してください。")

    stripped = strip_matching_quotes(value)
    if stripped is None:
        return None
    candidate = stripped.strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return stripped
        if isinstance(parsed, list):
            if len(parsed) == 1 and isinstance(parsed[0], str):
                return strip_matching_quotes(parsed[0])
            raise ValueError(
                f"{label} は単一の文字列で指定してください。"
                f"配列ではなく文字列を渡してください。"
            )
    return stripped


# =============================================================
# ディスカバリ: inspect_dataset
# =============================================================


def register_discovery_tool(
    mcp: FastMCP,
    registry: DatasetRegistry,
    *,
    enable_ui: bool = True,
) -> None:
    """inspect_dataset ツールを登録する。"""
    @mcp.tool(**_build_tool_kwargs(enable_ui=enable_ui, resource_uri=_INSPECT_URI))
    def inspect_dataset(
        dataset_id: str,
    ) -> str | ToolResult:
        """データセットの構造と品質を検査する。

        columnar 形式でフィールド一覧を返す（columns + rows、末尾 null 省略）。
        数値統計は numeric_stats に分離。codelist は静的定義のみ（auto は省略）。

        query_records / summarize_records の前に呼び、フィールド名・品質を確認する。

        Args:
            dataset_id: データセット識別子。
        """
        try:
            entry, ver, dsd = resolve_dataset(registry, dataset_id)
        except DatasetResolveError as e:
            detail = e.detail
            return error_response(
                dataset_id,
                message=detail.get("error"),
                available_datasets=detail.get("available_datasets"),
            )

        response = build_inspect_response(
            entry,
            ver,
            dsd,
            dataset_id=dataset_id,
        )
        return _tool_output(
            response,
            resource_uri=_INSPECT_URI,
            enable_ui=enable_ui,
        )


# =============================================================
# list_datasets — データセット一覧
# =============================================================


def register_list_datasets(
    mcp: FastMCP,
    registry: DatasetRegistry,
    *,
    enable_ui: bool = True,
) -> None:
    """list_datasets ツールを登録する。"""
    @mcp.tool(**_build_tool_kwargs(enable_ui=enable_ui, resource_uri=_LIST_URI))
    def list_datasets(
        q: str | None = None,
        publisher: str | None = None,
    ) -> str | ToolResult:
        """利用可能なデータセットの一覧を返す。

        dataset_id が不明な場合はまずこれを呼ぶ。
        各データセットの ID・タイトル・発行者・レコード数を返す
        （レコード数が取得できないデータセットでは record_count を省略する）。

        Args:
            q: キーワード検索。dataset_id またはタイトルに部分一致するデータセットを絞り込む。
            publisher: 発行者名でフィルタ（部分一致）。
        """
        result = build_list_response(registry, q=q, publisher=publisher)
        return _tool_output(result, resource_uri=_LIST_URI, enable_ui=enable_ui)


# =============================================================
# データアクセス: query_records, summarize_records
# =============================================================


def register_data_tools(
    mcp: FastMCP,
    registry: DatasetRegistry,
    *,
    enable_ui: bool = True,
) -> None:
    """データアクセスツール群を登録する。

    query_records、summarize_records の2ツールを登録する。

    Args:
        mcp: FastMCP サーバーインスタンス。
        registry: データセットレジストリ。
        enable_ui: MCP Apps UI を有効にするか。
    """
    _register_query_records(mcp, registry, enable_ui=enable_ui)
    _register_summarize_records(mcp, registry, enable_ui=enable_ui)


def _register_query_records(
    mcp: FastMCP,
    registry: DatasetRegistry,
    *,
    enable_ui: bool = True,
) -> None:
    @mcp.tool(**_build_tool_kwargs(
        enable_ui=enable_ui,
        resource_uri=_QUERY_URI,
    ))
    def query_records(
        dataset_id: str,
        q: str | None = None,
        search_fields: list[str] | str | None = None,
        select: list[str] | str | None = None,
        where: dict[str, Any] | str | None = None,
        order_by: str | None = None,
        limit: int | None = 50,
        cursor: str | None = None,
    ) -> str | ToolResult:
        """レコードをフィルタ・選択・ソートして取得する。カーソルページネーション対応。

        事前に inspect_dataset でフィールド名を確認すること。
        返却値のみ提示し、null や欠損を推測・補完しないこと。
        注意事項（※）がある場合は回答の脚注に含めること。
        resolved_fields がある場合、入力フィールド名がその正式名に自動補正されている。回答で補正に言及すること。

        Args:
            dataset_id: データセット識別子。
            q: 全文検索キーワード（部分一致 OR、where と AND 結合）。
            search_fields: JSON 配列で指定（例: ["field1","field2"]）。全文検索対象フィールド名（デフォルト: 全テキスト）。
            select: JSON 配列で指定（例: ["field1","field2"]）。出力フィールド名（None = 全フィールド）。
            where: フィルタ条件。文字列=部分一致、配列=IN、$gte/$lte=範囲、$ne=不等、$not_contains=部分不一致、$not_empty=非空。
            order_by: ソートフィールド（'-' プレフィックスで降順）。
            limit: 最大レコード数（1-5000、デフォルト 50）。
            cursor: ページネーションカーソル。
        """
        # 入力 coercion (MCP/LLM 由来の JSON 文字列を適切な型に変換)
        p = _coerce_params(
            where=(where, coerce_dict),
            select=(select, coerce_list),
            search_fields=(search_fields, coerce_list),
        )
        if isinstance(p, str):
            return p
        where, select, search_fields = p["where"], p["select"], p["search_fields"]

        # 実行
        try:
            result = execute_query(
                registry, dataset_id,
                q=q,
                search_fields=search_fields,
                select=select,
                where=where,
                order_by=order_by,
                limit=limit,
                cursor=cursor,
            )
        except DatasetResolveError as e:
            detail = e.detail
            return error_response(
                dataset_id,
                message=detail.get("error"),
                available_datasets=detail.get("available_datasets"),
            )
        except ToolInputError as e:
            return json_compact(e.to_dict())

        return _tool_output(
            result,
            resource_uri=_QUERY_URI,
            enable_ui=enable_ui,
        )


def _register_summarize_records(
    mcp: FastMCP,
    registry: DatasetRegistry,
    *,
    enable_ui: bool = True,
) -> None:
    @mcp.tool(**_build_tool_kwargs(
        enable_ui=enable_ui,
        resource_uri=_SUMMARIZE_URI,
    ))
    def summarize_records(
        dataset_id: str,
        metrics: list[str] | str | None = None,
        group_by: list[str] | str | None = None,
        where: dict[str, Any] | str | None = None,
        having: dict[str, Any] | str | None = None,
        explode: str | None = None,
        limit: int | None = 200,
    ) -> str | ToolResult:
        """集計統計を計算する（GROUP BY × metrics）。件数・合計・平均が必要なときに使う。

        groupable=true フィールドでグループ化し count/sum/avg/min/max を計算。
        computed_measure の加重平均にも対応。クロス集計は group_by に複数指定。
        個別レコードが必要なら query_records を使う。
        事前に inspect_dataset でフィールドを確認すること。
        返却値のみ提示し、丸め・推定・補完をしないこと。
        注意事項（※）がある場合は回答の脚注に含めること。
        resolved_fields がある場合、入力フィールド名がその正式名に自動補正されている。回答で補正に言及すること。

        Args:
            dataset_id: データセット識別子。
            metrics: JSON 配列で指定（例: ["count","sum:field"]）。'count','sum:field','avg:field','min:field','max:field'。computed_measure は 'avg:<name>'。デフォルト ["count"]。
            group_by: JSON 配列で指定（例: ["field1"]、クロス集計: ["field1","field2"]）。空=全体1グループ。
            where: フィルタ条件（query_records と同じ構文）。
            having: 集計後フィルタ。キーは結果列名、値は演算子構文（例: {"count": {"$gte": 10}}）。
            explode: multi_value フィールドを展開（自動的に group_by に追加）。
            limit: 最大グループ数（デフォルト 200、上限 10,000）。
        """
        # 入力 coercion
        p = _coerce_params(
            where=(where, coerce_dict),
            having=(having, coerce_dict),
            metrics=(metrics, coerce_list),
            group_by=(group_by, coerce_list),
        )
        if isinstance(p, str):
            return p
        where, having = p["where"], p["having"]
        metrics, group_by = p["metrics"], p["group_by"]
        try:
            explode = _coerce_single_string_param(explode, label="explode")
        except ValueError as e:
            return json_compact({"error": str(e)})

        # 実行
        try:
            result = execute_summarize(
                registry, dataset_id,
                metrics=metrics,
                group_by=group_by,
                where=where,
                having=having,
                explode=explode,
                limit=limit,
            )
        except DatasetResolveError as e:
            detail = e.detail
            return error_response(
                dataset_id,
                message=detail.get("error"),
                available_datasets=detail.get("available_datasets"),
            )
        except ToolInputError as e:
            return json_compact(e.to_dict())

        return _tool_output(
            result,
            resource_uri=_SUMMARIZE_URI,
            enable_ui=enable_ui,
        )


# =============================================================
# register_all_tools — エントリポイント
# =============================================================


def register_all_tools(
    mcp: FastMCP,
    registry: DatasetRegistry,
    *,
    enable_ui: bool = True,
) -> None:
    """全ツールを MCP サーバーに登録する。

    ``enable_ui=False`` にすると UI ツールは AppConfig なし・プレーン JSON
    テキスト返却モードで登録される（MCP Apps 非対応クライアント向け）。
    """
    register_discovery_tool(mcp, registry, enable_ui=enable_ui)
    register_list_datasets(mcp, registry, enable_ui=enable_ui)
    register_data_tools(mcp, registry, enable_ui=enable_ui)


# =============================================================
# サーバー起動
# =============================================================


def _build_instructions() -> str:
    """サーバー instructions を生成する（静的テキストのみ）。

    データセット情報は list_datasets ツール経由でのみ提供し、
    system prompt には埋め込まない（prompt injection 対策）。
    """
    return """\
行政手続カタログ MCP Server — 日本政府の行政手続データを提供します。

## 推奨クエリパターン

1. list_datasets で利用可能なデータセットを確認
2. inspect_dataset で構造と品質を把握（デフォルトで品質詳細を含む）
   - fill_rate が低い・quality_warning があるフィールドを把握しておく
   - 品質に問題があるフィールドは後工程の集計時に注釈を付けること
3. query_records / summarize_records で dataset_id を指定してクエリ

## ツール選択ガイド

- **集計・クロス集計**（○○ごとの件数/合計/平均、ランキング）→ **summarize_records**
- **クロス集計は 1 回で実行**: 複数軸が必要な場合は summarize_records を分割せず、group_by=[\"軸1\", \"軸2\", ...] に全軸をまとめて渡す
- **個別レコード閲覧**（特定条件のレコード詳細）→ query_records

## データ利用における重要原則

- **推測禁止**: データに含まれない情報を推測・補完しないこと。不明な点は「不明」と明記する
- **数値引用**: 数値は必ずツール結果をそのまま引用し、暗算・推定・丸めをしないこと
- **出典明記**: 回答には必ず dataset_id, source_url を含めること
- **加工明示**: 集計・フィルタした場合は「○件から集計」等その旨を明記すること
- **免責表示**: 出力は政府の公式見解ではないことを利用者に示すこと
- **null尊重**: フィールドが null/空/「その他」の場合、そのまま提示する（解釈・補足しない）
- **概数注意**: 件数フィールドの値は概数・試算値を含む。ツール結果に注意事項（※）がある場合、必ず回答の脚注に含めること
"""


def _register_ui_resources(mcp: FastMCP) -> None:
    """MCP Apps UI リソースを登録する。"""

    @mcp.resource(_INSPECT_URI)
    def inspect_dataset_ui() -> str:
        """Structure and quality overview for inspect_dataset results."""
        return load_template("inspect_dataset")

    @mcp.resource(_LIST_URI)
    def list_datasets_ui() -> str:
        """Dataset list tiles for list_datasets results."""
        return load_template("list_datasets")

    @mcp.resource(_QUERY_URI)
    def query_records_ui() -> str:
        """Interactive data table for query_records results."""
        return load_template("query_records")

    @mcp.resource(_SUMMARIZE_URI)
    def summarize_records_ui() -> str:
        """Chart and table view for summarize_records results."""
        return load_template("summarize_records")


def _register_health_check(mcp: FastMCP, *, enable_ui: bool) -> None:
    """ヘルスチェックエンドポイントを登録する。"""

    @mcp.custom_route("/health", methods=["GET"])
    async def health_check(request):
        from mcp.types import LATEST_PROTOCOL_VERSION
        from starlette.responses import JSONResponse
        return JSONResponse({
            "status": "healthy",
            "server": "administrative-procedures-mcp-catalog",
            "ui_enabled": enable_ui,
            # ホスティング先でどの MCP 仕様版を話せるか確認できるようにする
            "mcp_protocol_version": LATEST_PROTOCOL_VERSION,
        })


def fastmcp_kwargs() -> dict[str, Any]:
    """FastMCP インスタンスに渡す設定用 kwargs を返す。

    cacheable list results は MCP spec 2026-07-28 の機能で、FastMCP 4.x 以降が
    ``cache_ttl`` / ``cache_scope`` を受け付ける。FastMCP 3.x では引数自体が
    存在しないため空 dict を返し、キャッシュ宣言を行わない。
    """
    import inspect

    from fastmcp import FastMCP as _FastMCP

    params = inspect.signature(_FastMCP.__init__).parameters
    result = {}
    if "cache_ttl" in params and "cache_scope" in params:
        result.update({"cache_ttl": CACHE_TTL_MS, "cache_scope": CACHE_SCOPE})
    if "mask_error_details" in params:
        result["mask_error_details"] = True
    return result


def create_mcp(
    *,
    data_dir: str | Path | None = None,
    no_ui: bool | None = None,
) -> FastMCP:
    """FastMCP インスタンスを構築する。

    環境変数で制御可能:
        ADMIN_PROCEDURES_DATA_DIR: datasets/ を含むベースディレクトリ
        MCP_NO_UI: "1" で MCP Apps UI を無効化

    Args:
        data_dir: datasets/ を含むベースディレクトリ (環境変数より優先)。
        no_ui: MCP Apps UI を無効化 (環境変数より優先)。
    """
    from fastmcp import FastMCP as _FastMCP

    # data_dir=None のときの ADMIN_PROCEDURES_DATA_DIR 解決は resolve_data_dir が行う
    if no_ui is None:
        no_ui = bool(os.environ.get("MCP_NO_UI"))
    enable_ui = not no_ui

    base_dir = resolve_data_dir(data_dir)
    registry = init_registry(base_dir)

    mcp_kwargs = fastmcp_kwargs()
    _mcp = _FastMCP(
        "administrative-procedures-mcp-catalog",
        instructions=_build_instructions(),
        **mcp_kwargs,
    )

    register_all_tools(_mcp, registry, enable_ui=enable_ui)

    if enable_ui:
        _register_ui_resources(_mcp)

    _register_health_check(_mcp, enable_ui=enable_ui)

    for entry in registry.list_datasets():
        logger.info("Registered dataset %s (LazyFrame)", entry.dataset_id)
    logger.info("UI resources: %s", "enabled" if enable_ui else "disabled")
    logger.info(
        "Cache hints (MCP 2026-07-28): %s",
        f"ttl={CACHE_TTL_MS}ms scope={CACHE_SCOPE}" if "cache_ttl" in mcp_kwargs
        else "unsupported by installed FastMCP (requires 4.x)",
    )

    return _mcp


# モジュールレベルの FastMCP インスタンス。
# fastmcp run / fastmcp call から参照される。
mcp = create_mcp()
