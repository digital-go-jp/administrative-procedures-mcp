"""admin_procedures.response — MCP 非依存の共有レイヤー。

server.py および cli.py から使用されるレスポンス構築・実行パイプライン・ツール定義。
fastmcp / mcp への依存はゼロ。query/summarize の実処理はここに集約し、
server.py は入力 coercion と ToolResult 変換のみを担当する。

Classes:
    DatasetResolveError            -- データセット解決失敗時の例外
    ToolInputError                 -- ツール入力バリデーションエラー

Functions:
    resolve_dataset                -- dataset_id → (entry, ver, dsd) の解決
    parse_and_filter               -- where/q パラメータの解析とフィルタ適用
    strip_matching_quotes          -- 余分な囲みクォートの除去
    suggest_similar_names          -- 類似候補名の提案
    build_unknown_field_payload    -- 未知フィールドエラーペイロード構築
    sort_groups_by_primary_metric  -- 集計結果の降順ソート
    build_inspect_response         -- inspect_dataset 用レスポンス構築
    build_list_response            -- list_datasets 用レスポンス構築
    build_query_response           -- query_records 用レスポンス構築
    build_summarize_response       -- summarize_records 用レスポンス構築
    execute_query                  -- query_records 全パイプライン (バリデーション含む)
    execute_summarize              -- summarize_records 全パイプライン (バリデーション含む)
    get_tool_def                   -- 名前でツール定義を取得

Data:
    TOOL_DEFINITIONS               -- 全ツールの名前・説明・パラメータスキーマ (CLI describe 用)
"""

from __future__ import annotations

import difflib
import functools
import logging
import re
from typing import Any, Callable

import polars as pl

from admin_procedures.models import (
    ComponentDef,
    ComponentRole,
    DatasetEntry,
    DatasetRegistry,
    DatasetVersion,
    DataStructureDefinition,
    _normalize_lookup_key,
    build_provenance,
    has_data,
    track_field_resolutions,
)
from admin_procedures.query import (
    apply_columnar,
    apply_filters,
    apply_order_by,
    build_fulltext_predicate,
    collect_field_notes,
    compute_aggregation,
    compute_field_display_info,
    ensure_df,
    explode_records,
    get_quality_stats,
    having_column_names,
    paginate,
    parse_having,
    parse_metrics,
    parse_where,
    MAX_Q_LENGTH,
    MAX_WHERE_FIELDS,
    MAX_WHERE_ARRAY_SIZE,
    MAX_GROUP_BY_FIELDS,
    MAX_METRICS,
    MAX_CURSOR_LENGTH,
    MAX_QUERY_LIMIT,
    MAX_AGGREGATE_LIMIT,
)

_FIELD_HINT_LIMIT = 20
DEFAULT_QUERY_LIMIT = 50


# ============================================================
# 共通エラー・解決
# ============================================================


class DatasetResolveError(Exception):
    """データセット解決時のエラー。detail に構造化エラー情報を持つ。"""

    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(str(detail))

    def to_dict(self) -> dict[str, Any]:
        return self.detail


class ToolInputError(Exception):
    """ツール入力バリデーションエラー。構造化エラー情報を持つ。

    execute_query / execute_summarize 内で発生するバリデーションエラーを
    統一的に表現する。server.py は json_compact(e.to_dict()) でシリアライズし、
    cli.py は json.dumps(e.to_dict()) で整形出力する。
    """

    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = detail
        super().__init__(str(detail))

    def to_dict(self) -> dict[str, Any]:
        return self.detail


logger = logging.getLogger(__name__)


def resolve_dataset(
    registry: DatasetRegistry,
    dataset_id: str,
) -> tuple[DatasetEntry, DatasetVersion, DataStructureDefinition]:
    """dataset + DSD を解決する。失敗時は DatasetResolveError。

    server.py から使用される共通の解決ロジック。
    """
    resolved = registry.resolve(dataset_id)
    if not resolved:
        available = [e.dataset_id for e in registry.list_datasets()]
        raise DatasetResolveError({
            "error": f"データセット '{dataset_id}' が見つかりません",
            "available_datasets": available,
        })
    entry, ver = resolved
    if not ver.dsd:
        if not has_data(ver.data):
            # データファイルは配布せず利用者側で取得する運用のため、
            # 「空のデータセット」ではなく次に打つべきコマンドを示す
            raise DatasetResolveError({
                "error": f"データセット '{dataset_id}' のデータファイルがまだありません。",
                "hint": (
                    f"配布元から取得する: apcli fetch {dataset_id} / "
                    f"手元のファイルから取り込む: apcli add {dataset_id} --csv <file>"
                ),
            })
        raise DatasetResolveError({"error": "データ構造定義 (DSD) が利用できません"})
    return entry, ver, ver.dsd


def parse_and_filter(
    where: dict | None,
    dsd: DataStructureDefinition,
    data: Any,
) -> Any:
    """where パース + フィルタ適用。パースエラー時は ValueError を送出する。

    data は polars DataFrame。戻り値も polars DataFrame。
    各レイヤーで ValueError を独自のエラー形式に変換すること。
    """
    predicates = parse_where(where, dsd)
    return apply_filters(data, predicates)



# =============================================================
# 文字列・フィールドユーティリティ
# =============================================================


_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def decode_unicode_escapes(value: str) -> str:
    """文字列中のリテラル ``\\uXXXX`` エスケープを実際の文字にデコードする。

    LLM が日本語フィールド名を ``\\u624b\\u7d9a...`` のように
    二重エスケープして送信するケースに対応する。
    """
    if "\\u" not in value:
        return value
    return _UNICODE_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), value)


def strip_matching_quotes(value: str | None) -> str | None:
    """文字列パラメータの余分な囲みクォートを除去する。"""
    if value is None:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value



def suggest_similar_names(
    value: str,
    candidates: list[str],
    *,
    limit: int = 3,
) -> list[str]:
    """候補名の一覧から類似候補を返す。"""
    by_normalized: dict[str, list[str]] = {}
    for name in candidates:
        by_normalized.setdefault(_normalize_lookup_key(name), []).append(name)

    target = _normalize_lookup_key(value)
    exact = by_normalized.get(target, [])
    if exact:
        return exact[:limit]

    matches = difflib.get_close_matches(
        target,
        list(by_normalized.keys()),
        n=limit,
        cutoff=0.55,
    )
    suggestions: list[str] = []
    for match in matches:
        suggestions.extend(by_normalized[match])
    return suggestions[:limit]



# =============================================================
# コンポーネントサマリー
# =============================================================


_ROLE_SHORT = {
    ComponentRole.IDENTIFIER: "id",
    ComponentRole.DIMENSION: "dim",
    ComponentRole.MEASURE: "measure",
    ComponentRole.ATTRIBUTE: "attr",
}


def _build_field_row(
    comp: ComponentDef,
    dsd: DataStructureDefinition,
    quality_stats: dict[str, Any],
) -> list[Any]:
    """1コンポーネントを columnar の1行に変換する。"""
    qs = quality_stats.get(comp.ja)
    fill_rate = qs.fill_rate if qs else None

    # codelist — YAML 定義 (is_static) のみ返す。auto/auto_split は件数が大きいため省略
    codelist = None
    if comp.codelist_ref:
        cl = dsd.get_codelist(comp.codelist_ref)
        if cl and cl.is_static:
            codelist = [item.value for item in cl.items]

    row: list[Any] = [
        comp.ja,                                        # id
        _ROLE_SHORT.get(comp.role, "attr"),              # role
        comp.description or None,                        # desc
        fill_rate,                                       # fill_rate
        1 if comp.groupable else None,                   # groupable
        codelist,                                        # codelist
        1 if comp.multi_value else None,                 # multi_value
        comp.data_type if comp.data_type != "string" else None,  # type
        1 if comp.aggregatable else None,                # aggregatable
    ]
    # 末尾の null を切り捨て
    while row and row[-1] is None:
        row.pop()
    return row


# =============================================================
# レスポンスメタデータ
# =============================================================


def _available_fields_payload(
    dsd: DataStructureDefinition, *, limit: int = _FIELD_HINT_LIMIT
) -> dict[str, Any]:
    """利用可能フィールドのサンプルリストを含むペイロードを返す。"""
    all_fields = dsd.all_field_names()
    return {
        "available_fields_sample": all_fields[:limit],
        "available_fields_total": len(all_fields),
        "hint_detail": "inspect_dataset を呼び出すと、フィールド定義や利用方法を確認できます。",
    }


def build_unknown_field_payload(
    field: str,
    dsd: DataStructureDefinition,
    *,
    label: str = "field",
) -> dict[str, Any]:
    """未知フィールドエラー用の共通ペイロードを返す。"""
    # LLM が \\uXXXX 二重エスケープで送信するケースをデコード
    field = decode_unicode_escapes(field)
    payload = {
        "error": f"不明な{label}: '{field}'",
        "hint": "このレスポンスの 'maybe_fields' と 'available_fields_sample' を参照してください。",
    }
    suggestions = suggest_similar_names(field, dsd.all_field_names())
    if suggestions:
        payload["maybe_fields"] = suggestions
    if field and all(ord(ch) < 128 for ch in field if not ch.isspace()):
        payload["field_id_hint"] = (
            "このデータセットの field ID は主に日本語です。"
            "inspect_dataset の fields に出る id をそのまま使用してください。"
        )
    payload.update(_available_fields_payload(dsd))
    return payload


def _build_response_meta(
    entry: DatasetEntry,
    ver: DatasetVersion,
) -> dict[str, Any]:
    """field_metadata + provenance のレスポンス共通メタデータを生成する。"""
    dsd = ver.dsd
    field_meta: dict[str, dict[str, Any]] = {}
    if dsd is not None:
        for comp in dsd.components:
            meta: dict[str, Any] = {"role": comp.role.value, "type": comp.data_type}
            if comp.multi_value:
                meta["multi_value"] = True
            if comp.description:
                meta["desc"] = comp.description
            field_meta[comp.ja] = meta
        for cm in dsd.computed_measures:
            cm_meta: dict[str, Any] = {"role": "measure", "type": cm.data_type}
            if cm.description:
                cm_meta["desc"] = cm.description
            field_meta[cm.name] = cm_meta
    return {
        "field_metadata": field_meta,
        "provenance": build_provenance(entry, ver),
    }


# =============================================================
# メトリクスフィールド抽出
# =============================================================


def _extract_metric_fields(
    parsed_metrics: list[tuple[str, str | None, Any]],
    dsd: DataStructureDefinition,
) -> list[str]:
    """parsed_metrics からメトリクスに関与するフィールド名リストを抽出する。"""
    fields: list[str] = []
    for _, target_ja, cm in parsed_metrics:
        if cm is not None:
            fields.append(cm.name)
            if cm.mode == "count_where":
                fields.append(cm.condition_field)
            else:
                fields.append(cm.numerator)
                fields.append(cm.denominator)
        elif target_ja:
            fields.append(target_ja)
    return fields


# =============================================================
# 集計ソート
# =============================================================


def sort_groups_by_primary_metric(groups: pl.DataFrame) -> pl.DataFrame:
    """先頭のメトリクス列を基準に集計結果を降順ソートする。"""
    if groups.is_empty():
        return groups

    metric_columns = [
        col
        for col in groups.columns
        if (col == "count" or col.startswith(("sum:", "avg:", "min:", "max:")))
        and not col.endswith(":null_excluded")
    ]
    if not metric_columns:
        return groups

    sort_key = metric_columns[0]
    return groups.sort(sort_key, descending=True, nulls_last=True)


# =============================================================
# レスポンスビルダー
# =============================================================



def _build_computed_measures_payload(dsd: DataStructureDefinition) -> list[dict[str, Any]]:
    return [
        {
            "id": cm.name,
            "type": cm.data_type,
            "formula": (
                f"count({cm.condition_field} in {list(cm.condition_values)}) / count(all)"
                if cm.mode == "count_where"
                else f"{cm.numerator} / {cm.denominator}"
            ),
            "aggregation": (
                "種類数比率 (該当数/全数)"
                if cm.mode == "count_where"
                else "加重平均 (sum(分子)/sum(分母))"
            ),
            "metrics": ["avg"],
            **({"description": cm.description} if cm.description else {}),
        }
        for cm in dsd.computed_measures
    ]


def _build_quality_summary(quality_stats: dict[str, Any]) -> dict[str, int] | None:
    if not quality_stats:
        return None
    rates = {name: stats.fill_rate for name, stats in quality_stats.items()}
    return {
        "fully_populated": sum(1 for rate in rates.values() if rate >= 1.0),
        "mostly_populated": sum(1 for rate in rates.values() if 0.5 <= rate < 1.0),
        "sparse": sum(1 for rate in rates.values() if rate < 0.5),
    }


_INSPECT_COLUMNS = [
    "id", "role", "desc", "fill_rate", "groupable",
    "codelist", "multi_value", "type",
    "aggregatable",
]


def build_inspect_response(
    entry: DatasetEntry,
    ver: DatasetVersion,
    dsd: DataStructureDefinition,
    *,
    dataset_id: str,
) -> dict[str, Any]:
    """inspect_dataset の共通レスポンス本体を構築する。

    columnar 形式 (columns + rows) でフィールド情報を返す。
    数値フィールドの統計は numeric_stats に分離して付与。
    """
    quality_stats = get_quality_stats(ver)

    all_components = list(dsd.components)
    rows = [
        _build_field_row(comp, dsd, quality_stats)
        for comp in all_components
    ]

    response: dict[str, Any] = {
        "dataset_id": dataset_id,
        "title": entry.title,
        "record_count": len(ensure_df(ver.data)) if has_data(ver.data) else 0,
        "schema_version": entry.schema_version,
        "columns": list(_INSPECT_COLUMNS),
        "rows": rows,
    }

    # 数値統計
    numeric_stats: dict[str, dict[str, Any]] = {}
    for comp in all_components:
        qs = quality_stats.get(comp.ja)
        if qs and qs.numeric_min is not None:
            numeric_stats[comp.ja] = {
                "min": qs.numeric_min,
                "max": qs.numeric_max,
                "mean": qs.numeric_mean,
                "zero_count": qs.zero_count,
            }
    if numeric_stats:
        response["numeric_stats"] = numeric_stats

    if dsd.computed_measures:
        response["computed_measures"] = _build_computed_measures_payload(dsd)

    quality_summary = _build_quality_summary(quality_stats)
    if quality_summary:
        response["quality_summary"] = quality_summary

    return response


def build_list_response(
    registry: DatasetRegistry,
    *,
    q: str | None = None,
    publisher: str | None = None,
) -> dict[str, Any]:
    """list_datasets の共通レスポンスを構築する。"""
    q_lower = q.lower() if q else None
    pub_lower = publisher.lower() if publisher else None

    datasets: list[dict[str, Any]] = []
    for entry in registry.list_datasets():
        if q_lower:
            haystack = f"{entry.dataset_id} {entry.title}".lower()
            if q_lower not in haystack:
                continue
        if pub_lower and pub_lower not in (entry.publisher or "").lower():
            continue

        item: dict[str, Any] = {
            "dataset_id": entry.dataset_id,
            "title": entry.title,
            "publisher": entry.publisher,
        }
        if entry.record_count:
            item["record_count"] = entry.record_count
        if entry.schema_version:
            item["schema_version"] = entry.schema_version
        datasets.append(item)

    return {
        "datasets": datasets,
        "total": len(datasets),
    }


def build_query_response(
    entry: DatasetEntry,
    ver: DatasetVersion,
    dsd: DataStructureDefinition,
    *,
    dataset_id: str,
    total: int,
    records: pl.DataFrame,
    next_cursor: str | None,
    hint: str | None = None,
    query_params: dict[str, Any] | None = None,
    selected_fields: list[str] | None = None,
) -> dict[str, Any]:
    """query_records の共通レスポンス本体を構築する。"""
    response: dict[str, Any] = {
        "dataset_id": dataset_id,
        "total": total,
        "next_cursor": next_cursor,
        "records": records,
        **_build_response_meta(entry, ver),
    }
    if hint:
        response["hint"] = hint
    if query_params:
        response["query_params"] = query_params
    field_notes = collect_field_notes(dsd, selected_fields)
    if field_notes:
        response["notes"] = field_notes
    return response


def build_summarize_response(
    entry: DatasetEntry,
    ver: DatasetVersion,
    dsd: DataStructureDefinition,
    *,
    dataset_id: str,
    groups: pl.DataFrame,
    total_group_count: int,
    query_params: dict[str, Any] | None = None,
    metric_fields: list[str] | None = None,
    exploded_field: str | None = None,
    pre_explode_records: int | None = None,
) -> dict[str, Any]:
    """summarize_records の共通レスポンス本体を構築する。"""
    response: dict[str, Any] = {
        "dataset_id": dataset_id,
        "total_group_count": total_group_count,
        "groups": groups,
        **_build_response_meta(entry, ver),
    }
    if query_params:
        response["query_params"] = query_params
    field_notes = collect_field_notes(dsd, metric_fields)
    if field_notes:
        response["notes"] = field_notes
    if exploded_field is not None:
        response["exploded_field"] = exploded_field
        response["pre_explode_records"] = pre_explode_records
    return response


# ============================================================
# パイプライン実行 — 共通ヘルパー
# ============================================================


_WHERE_HINT = (
    "where は JSON オブジェクトで指定してください。"
    '例: {"フィールド名": "部分一致キーワード"}'
    ' / {"フィールド名": ["完全一致値1", "完全一致値2"]}'
    ' / {"フィールド名": {"$gte": 100}}。'
    "正確な値がわからない場合は文字列で部分一致検索するか、q パラメータで全文検索してください。"
)


def _resolve_where_key(dsd: DataStructureDefinition, key: str) -> str | None:
    """where キーを日本語フィールド名に解決する。"""
    comp = dsd.get_component(key)
    return comp.ja if comp else None


def _build_query_params(**kwargs: Any) -> dict[str, Any]:
    """非 None / 非デフォルトのパラメータだけを dict にまとめる。"""
    return {k: v for k, v in kwargs.items() if v}


def _extract_field_from_message(message: str, prefix: str) -> str | None:
    """エラーメッセージから "不明な○○ 'フィールド名'" パターンのフィールド名を抽出する。"""
    if message.startswith(prefix) and message.endswith("'"):
        return message[len(prefix):-1]
    return None


def _wrap_field_error(
    exc: ValueError,
    dsd: DataStructureDefinition,
    *,
    prefix: str = "不明なフィールド '",
    label: str = "field",
    hint: str | None = None,
) -> ToolInputError:
    """ValueError を ToolInputError に変換する。未知フィールドパターンならば候補付き。"""
    message = str(exc)
    field = _extract_field_from_message(message, prefix)
    if field is not None:
        return ToolInputError(build_unknown_field_payload(field, dsd, label=label))
    payload: dict[str, Any] = {"error": message}
    if hint:
        payload["hint"] = hint
    return ToolInputError(payload)


def _build_invalid_metrics_payload(
    message: str,
    dsd: DataStructureDefinition,
) -> dict[str, Any]:
    """metrics パラメータのエラーペイロードを構築する。"""
    payload: dict[str, Any] = {
        "error": message,
        "hint": (
            "metrics には inspect_dataset の fields で role=measure のフィールド id をそのまま使用してください。"
            "分析前に inspect_dataset を呼ぶと安全です。"
        ),
    }
    if dsd.measures:
        payload["metric_examples"] = ["count", f"sum:{dsd.measures[0].ja}"]
    if dsd.computed_measures:
        payload.setdefault("metric_examples", ["count"])
        payload["metric_examples"].append(f"avg:{dsd.computed_measures[0].name}")
    payload.update(_available_fields_payload(dsd))
    field = _extract_field_from_message(message, "不明なフィールド '")
    if field is not None:
        suggestions = suggest_similar_names(field, dsd.all_field_names())
        if suggestions:
            payload["maybe_fields"] = suggestions
        if field and all(ord(ch) < 128 for ch in field if not ch.isspace()):
            payload["field_id_hint"] = (
                "このデータセットの field ID は主に日本語です。"
                "inspect_dataset で確認してください。"
            )
        cm_suggestions = [
            cm.name for cm in dsd.computed_measures
            if field.strip().casefold() in cm.name.casefold()
        ]
        if cm_suggestions:
            payload["maybe_metrics"] = [f"avg:{name}" for name in cm_suggestions[:3]]
    return payload


def _build_invalid_having_payload(
    message: str,
    valid_columns: set[str],
    dsd: DataStructureDefinition,
) -> dict[str, Any]:
    """having パラメータのエラーペイロードを構築する。"""
    payload: dict[str, Any] = {
        "error": message,
        "hint": (
            "having のキーには summarize_records の返却列名をそのまま使用してください。"
            "まず summarize_records を metrics/group_by のみで呼ぶか、"
            "inspect_dataset で role=measure のフィールドと computed_measures を確認してください。"
        ),
        "valid_having_columns": sorted(valid_columns),
    }
    payload.update(_available_fields_payload(dsd))

    prefix = "不明な having カラム '"
    if message.startswith(prefix):
        suffix = message[len(prefix):]
        col = suffix.split("'", 1)[0]
        suggestions = suggest_similar_names(col, sorted(valid_columns))
        if not suggestions and ":" in col:
            metric_type, field = col.split(":", 1)
            field_suggestions = suggest_similar_names(field, dsd.all_field_names())
            suggestions = [f"{metric_type}:{name}" for name in field_suggestions]
            if metric_type == "avg":
                computed_matches = suggest_similar_names(
                    field,
                    [cm.name for cm in dsd.computed_measures],
                )
                suggestions.extend(f"avg:{name}" for name in computed_matches)
        if suggestions:
            payload["maybe_having_columns"] = suggestions[:3]
        field_part = col.split(":", 1)[-1]
        if field_part and all(ord(ch) < 128 for ch in field_part if not ch.isspace()):
            payload["field_id_hint"] = (
                "having で参照する列名の field 部分も主に日本語です。"
                "metrics に指定した field ID と同じ表記を使ってください。"
            )

    return payload


# ============================================================
# パイプライン実行 (CLI / server.py 共用)
# ============================================================


def _disclose_field_resolutions(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """フィールド名の暗黙補正を resolved_fields としてレスポンスに開示する。

    NFKC 正規化や difflib 近似一致でフィールド名が解決された場合、
    {入力名: 解釈された正式名} をレスポンス先頭に付与する。
    推測禁止・加工明示の原則に基づき、補正を無言で行わないための機構。
    """
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        with track_field_resolutions() as resolved:
            result = fn(*args, **kwargs)
            if resolved:
                return {"resolved_fields": dict(resolved), **result}
            return result
    return wrapper


@_disclose_field_resolutions
def execute_query(
    registry: DatasetRegistry,
    dataset_id: str,
    *,
    q: str | None = None,
    search_fields: list[str] | None = None,
    select: list[str] | None = None,
    where: dict | None = None,
    order_by: str | None = None,
    limit: int | None = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """query_records の全パイプラインを実行し columnar dict を返す。

    エラー時は ToolInputError を送出する。
    DatasetResolveError はそのまま伝播する。
    フィールド名を自動補正した場合は resolved_fields に対応を付与する。
    """
    entry, ver, dsd = resolve_dataset(registry, dataset_id)

    # DoS 対策：入力検証を先に完全実施してから collect
    # これにより、不正な入力早期段階で拒否し、大容量データのメモリ展開を避ける
    if q and len(q) > MAX_Q_LENGTH:
        raise ToolInputError({
            "error": f"q は {MAX_Q_LENGTH} 文字以内です（入力: {len(q)} 文字）"
        })
    if cursor and len(cursor.encode()) > MAX_CURSOR_LENGTH:
        raise ToolInputError({
            "error": f"cursor は {MAX_CURSOR_LENGTH} バイト以内です"
        })
    if search_fields and len(search_fields) > len(dsd.all_field_names()):
        raise ToolInputError({
            "error": f"search_fields はフィールド数以内です"
        })
    if select and len(select) > len(dsd.all_field_names()):
        raise ToolInputError({
            "error": f"select はフィールド数以内です"
        })

    # データの入力は完了、ここで collect（LazyFrame を遅延させる）
    data = ensure_df(ver.data)

    # where フィルタ（入力検証後に実行）
    try:
        filtered = parse_and_filter(where, dsd, data)
    except ValueError as e:
        raise _wrap_field_error(e, dsd, hint=_WHERE_HINT) from e

    # 全文検索
    if q and q.strip():
        try:
            filtered = apply_filters(
                filtered,
                [build_fulltext_predicate(q.strip(), dsd, search_fields=search_fields)],
            )
        except ValueError as e:
            raise _wrap_field_error(
                e, dsd, prefix="不明な検索フィールド '", label="search field",
            ) from e

    # ソート
    try:
        filtered = apply_order_by(filtered, order_by, dsd)
    except ValueError as e:
        raise _wrap_field_error(
            e, dsd, prefix="不明な order_by フィールド '", label="order_by field",
        ) from e

    # ページネーション
    limit = max(1, min(MAX_QUERY_LIMIT, limit or DEFAULT_QUERY_LIMIT))
    try:
        page, next_cursor, total = paginate(
            filtered, limit, cursor, ver.version, max_limit=MAX_QUERY_LIMIT,
        )
    except ValueError as e:
        raise ToolInputError({"error": str(e)}) from e

    # select バリデーション
    missing = [f for f in (select or []) if dsd.get_component(f) is None]
    if missing:
        payload = build_unknown_field_payload(missing[0], dsd, label="select field")
        if len(missing) > 1:
            payload["error"] = f"不明な select フィールド: {', '.join(missing)}"
        raise ToolInputError(payload)

    # 列選択 + 表示フィルタ
    target_fields = select or dsd.all_field_names()
    available_cols = [f for f in target_fields if f in page.columns]
    page_selected = page.select(available_cols) if available_cols else page

    displayable_fields, _ = compute_field_display_info(
        page_selected, available_cols, generic_values=entry.generic_values or None,
    )
    records = page_selected.select(displayable_fields) if displayable_fields else pl.DataFrame()

    # zero-result ヒント
    hint = None
    if total == 0:
        hints = []
        if where:
            if any(isinstance(v, list) for v in where.values()):
                hints.append("完全一致値が正しいか確認してください。文字列で部分一致に切り替えると見つかる場合があります。")
            if any(isinstance(v, str) for v in where.values()):
                hints.append("部分一致のキーワードを短くしてみてください。")
            if len(where) > 1:
                hints.append("フィルタを減らして検索範囲を広げてみてください。")
        hints.append("where の代わりに q パラメータで全文検索すると、フィールドを横断してヒットする場合があります。")
        hint = " ".join(hints)

    query_params = _build_query_params(
        q=q, where=where, select=select, order_by=order_by,
        limit=limit if limit != 50 else None,
    )

    result = build_query_response(
        entry, ver, dsd,
        dataset_id=dataset_id,
        total=total,
        records=records,
        next_cursor=next_cursor,
        hint=hint,
        query_params=query_params,
        selected_fields=select,
    )
    return apply_columnar(result)


@_disclose_field_resolutions
def execute_summarize(
    registry: DatasetRegistry,
    dataset_id: str,
    *,
    metrics: list[str] | None = None,
    group_by: list[str] | None = None,
    where: dict | None = None,
    having: dict | None = None,
    explode: str | None = None,
    limit: int | None = 200,
) -> dict[str, Any]:
    """summarize_records の全パイプラインを実行し columnar dict を返す。

    エラー時は ToolInputError を送出する。
    DatasetResolveError はそのまま伝播する。
    フィールド名を自動補正した場合は resolved_fields に対応を付与する。
    """
    entry, ver, dsd = resolve_dataset(registry, dataset_id)

    metrics_list = metrics or ["count"]
    group_by_list = list(group_by or [])

    # 入力制限チェック
    if len(metrics_list) > MAX_METRICS:
        raise ToolInputError({
            "error": f"metrics は {MAX_METRICS} 個以内です（入力: {len(metrics_list)} 個）"
        })
    if len(group_by_list) > MAX_GROUP_BY_FIELDS:
        raise ToolInputError({
            "error": f"group_by は {MAX_GROUP_BY_FIELDS} フィールド以内です（入力: {len(group_by_list)} フィールド）"
        })

    # メトリクスバリデーション
    try:
        parsed_metrics = parse_metrics(metrics_list, dsd)
    except ValueError as e:
        raise ToolInputError(_build_invalid_metrics_payload(str(e), dsd)) from e

    # group_by コンポーネント解決 + groupable チェック
    group_by_components: list[ComponentDef] = []
    for field_name in group_by_list:
        comp = dsd.get_component(field_name)
        if comp is None:
            raise ToolInputError(build_unknown_field_payload(field_name, dsd))
        group_by_components.append(comp)

    # explode 解決
    explode_comp = None
    if explode:
        explode_comp = dsd.get_component(explode)
        if explode_comp is None:
            raise ToolInputError(build_unknown_field_payload(explode, dsd, label="explode field"))
        if explode not in group_by_list:
            group_by_list.append(explode)
            group_by_components.append(explode_comp)

    # groupable 制約チェック (explode 対象は除外)
    for field_name, comp in zip(group_by_list, group_by_components):
        if not comp.groupable and field_name != explode:
            raise ToolInputError({
                "error": f"フィールド '{field_name}' は groupable ではありません。",
                "hint": "groupable=true のフィールドのみ使用可能です。このレスポンスの 'groupable_fields' を参照してください。",
                "groupable_fields": [c.ja for c in dsd.components if c.groupable],
            })

    # auto-explode
    if not explode:
        for field_name, comp in zip(group_by_list, group_by_components):
            if comp.multi_value:
                explode = field_name
                explode_comp = comp
                logger.info(
                    "Auto-explode: '%s' in group_by is multi_value, using as explode",
                    field_name,
                )
                break

    # フィルタ (explode 対象フィールドの条件を分離)
    if explode_comp is not None and where:
        explode_ja = explode_comp.ja
        where_pre = {k: v for k, v in where.items()
                     if _resolve_where_key(dsd, k) != explode_ja}
        where_post = {k: v for k, v in where.items()
                      if _resolve_where_key(dsd, k) == explode_ja}
    else:
        where_pre = where
        where_post = None

    # データの入力は完了、ここで collect（LazyFrame を遅延させる）
    # 続く where と having の検証は collect 後に実行
    data = ensure_df(ver.data)
    try:
        filtered = parse_and_filter(where_pre, dsd, data)
    except ValueError as e:
        raise _wrap_field_error(e, dsd) from e
    pre_explode_count = len(filtered)

    if explode_comp is not None:
        filtered = explode_records(filtered, explode_comp.ja)

    if where_post:
        try:
            filtered = parse_and_filter(where_post, dsd, filtered)
        except ValueError as e:
            raise _wrap_field_error(e, dsd) from e

    groups = compute_aggregation(filtered, group_by_components, parsed_metrics, dsd)

    # Having フィルタ
    if having:
        valid_cols = having_column_names(parsed_metrics, dsd)
        try:
            having_preds = parse_having(having, valid_cols)
        except ValueError as e:
            raise ToolInputError(
                _build_invalid_having_payload(str(e), valid_cols, dsd),
            ) from e
        groups = apply_filters(groups, having_preds)

    groups = sort_groups_by_primary_metric(groups)

    normalized_limit = max(1, min(MAX_AGGREGATE_LIMIT, limit or 200))
    total_group_count = len(groups)
    if total_group_count > normalized_limit:
        groups = groups.head(normalized_limit)

    query_params = _build_query_params(
        group_by=group_by_list or None,
        where=where,
        having=having,
        explode=explode,
        metrics=metrics_list if metrics_list != ["count"] else None,
    )

    metric_fields = _extract_metric_fields(parsed_metrics, dsd)
    result = build_summarize_response(
        entry, ver, dsd,
        dataset_id=dataset_id,
        groups=groups,
        total_group_count=total_group_count,
        query_params=query_params,
        metric_fields=metric_fields,
        exploded_field=explode if explode_comp else None,
        pre_explode_records=pre_explode_count if explode_comp else None,
    )
    return apply_columnar(result)


# ============================================================
# ツール定義 (CLI describe / エージェント向け)
# ============================================================

def _p(type: str, desc: str, *, default=None) -> dict:
    """パラメータプロパティの短縮ヘルパー (JSON Schema 準拠)。"""
    d: dict = {"type": type, "description": desc}
    if default is not None:
        d["default"] = default
    return d


def _params(properties: dict, required: list[str] | None = None) -> dict:
    """JSON Schema 準拠の parameters オブジェクトを構築する。"""
    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


_WHERE_DESC = "フィルタ条件。文字列=部分一致、配列=IN、$gte/$lte=範囲、$ne=不等、$not_contains=部分不一致、$not_empty=非空。"

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "list_datasets",
        "description": "利用可能なデータセットの一覧を返す。dataset_id が不明な場合はまずこれを呼ぶ。",
        "cli": "apcli list",
        "parameters": _params({
            "q": _p("string", "キーワード検索（部分一致）。"),
            "publisher": _p("string", "発行者名でフィルタ（部分一致）。"),
        }),
    },
    {
        "name": "inspect_dataset",
        "description": "データセットの構造と品質を検査する。query/summarize の前にフィールド名・品質を確認する。",
        "cli": "apcli inspect <dataset_id>",
        "parameters": _params(
            {"dataset_id": _p("string", "データセット識別子。")},
            required=["dataset_id"],
        ),
    },
    {
        "name": "query_records",
        "description": "レコードをフィルタ・選択・ソートして取得する。カーソルページネーション対応。",
        "cli": "apcli query <dataset_id> [options]",
        "parameters": _params({
            "dataset_id": _p("string", "データセット識別子。"),
            "q": _p("string", "全文検索キーワード（部分一致 OR、where と AND 結合）。"),
            "search_fields": _p("array", "全文検索対象フィールド名。"),
            "select": _p("array", "出力フィールド名（None=全フィールド）。"),
            "where": _p("object", _WHERE_DESC),
            "order_by": _p("string", "ソートフィールド（'-' プレフィックスで降順）。"),
            "limit": _p("integer", "最大レコード数（1-5000）。", default=50),
            "cursor": _p("string", "ページネーションカーソル。"),
        }, required=["dataset_id"]),
    },
    {
        "name": "summarize_records",
        "description": "集計統計を計算する（GROUP BY × metrics）。件数・合計・平均が必要なときに使う。",
        "cli": "apcli summarize <dataset_id> [options]",
        "parameters": _params({
            "dataset_id": _p("string", "データセット識別子。"),
            "metrics": _p("array", "集計メトリクス。count/sum:field/avg:field/min:field/max:field。", default=["count"]),
            "group_by": _p("array", "グループ化フィールド（クロス集計: 複数指定、空=全体1グループ）。"),
            "where": _p("object", "フィルタ条件（query_records と同じ構文）。"),
            "having": _p("object", "集計後フィルタ（例: {\"count\": {\"$gte\": 10}}）。"),
            "explode": _p("string", "multi_value フィールドを展開（自動的に group_by に追加）。"),
            "limit": _p("integer", "最大グループ数（上限 10,000）。", default=200),
        }, required=["dataset_id"]),
    },
]

_TOOL_BY_NAME = {td["name"]: td for td in TOOL_DEFINITIONS}

USAGE_GUIDE: dict = {
    "workflow": [
        "1. apcli list — dataset_id を確認",
        "2. apcli inspect <dataset_id> — フィールド名・型・品質を確認",
        "3. apcli query / summarize — データ取得・集計",
    ],
    "inspect_fields": {
        "columns": "id, role, desc, fill_rate, groupable, codelist, multi_value, type, aggregatable",
        "role": "id=識別子, dim=分析軸, measure=数値項目, attr=属性",
        "groupable": "1 のフィールドのみ --group-by に使用可能",
        "aggregatable": "1 のフィールドのみ sum/avg/min/max に使用可能",
        "multi_value": "1 のフィールドはセミコロン区切り。group_by で自動展開",
        "codelist": "一部のみ返す。値が不明なら -q で全文検索",
        "computed_measures": "加重平均 avg:<name> で集計可能",
        "trailing_null": "末尾 null は省略（行の長さは可変）",
    },
    "where_syntax": {
        "string": "部分一致",
        "array": "IN（完全一致のいずれか）",
        "$gte/$lte": "範囲",
        "$ne": "不等",
        "$not_contains": "部分不一致",
        "$not_empty": "非空",
        "multiple_keys": "複合条件（AND）",
    },
    "tips": [
        "値がわからないときは -q で全文検索が最も確実",
        "--select で出力フィールドを絞るとトークン節約",
        "summarize の結果は主要メトリクスの降順で自動ソート済み",
        "notes がある場合は回答の脚注に含めること",
        "数値はツール結果をそのまま引用し、欠損値を推測しないこと",
    ],
    "output_format": "columnar 形式: columns(配列) + rows(配列の配列)。各行は columns の順に値が並ぶ",
}


def get_tool_def(name: str) -> dict | None:
    """名前でツール定義を取得する。"""
    return _TOOL_BY_NAME.get(name)


def get_full_description() -> dict:
    """全ツール定義 + 使い方ガイドを返す。"""
    return {"tools": TOOL_DEFINITIONS, "usage_guide": USAGE_GUIDE}
