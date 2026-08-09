"""admin_procedures.query — フィルタ・集計エンジン・ページネーション。

WhereClause のパース、述語ベースのフィルタリング、
メトリクス集計、カーソルベースページネーションを提供する。

Classes:
    CursorCodec            -- opaque cursor トークンのエンコード/デコード
    FilterPredicate        -- where 句の述語基底クラス
    ContainsPredicate      -- $contains 演算子（部分一致）
    NotContainsPredicate   -- $not_contains 演算子（部分不一致）
    EqPredicate            -- $eq 演算子（完全一致）
    GtePredicate           -- $gte 演算子（以上）
    LtePredicate           -- $lte 演算子（以下）
    NePredicate            -- $ne 演算子（不一致）
    NotEmptyPredicate      -- $not_empty 演算子（非空）
    FullTextPredicate      -- 全文検索述語
    FieldQualityStats      -- 1 フィールドの品質統計

Functions:
    parse_where                 -- where 辞書を FilterPredicate リストに変換
    apply_filters               -- レコードリストに述語を適用
    apply_order_by              -- ソート適用
    paginate                    -- カーソルベースページネーション
    coerce_dict                 -- JSON 文字列 → dict 変換
    coerce_list                 -- JSON 文字列 → list 変換
    json_compact                -- コンパクト JSON シリアライズ
    apply_columnar              -- result 内 records/groups (DataFrame) を columnar 変換
    error_response              -- 標準エラーレスポンス生成
    build_fulltext_predicate    -- 全文検索述語の構築
    parse_metrics               -- メトリクス文字列の解析
    compute_aggregation         -- グループ別メトリクス集計
    having_column_names         -- 集計結果カラム名セット構築
    parse_having                -- having 辞書を述語リストに変換
    explode_records             -- セミコロン区切りフィールドの展開
    compute_field_display_info  -- フィールド表示可否の判定 (DataFrame 対応)
    compute_dataset_quality     -- 複数フィールドの品質統計計算
    get_quality_stats           -- 品質統計キャッシュ取得
    collect_field_notes         -- フィールドの notes.details 収集
    ensure_df                   -- LazyFrame → DataFrame 変換 (DataFrame はそのまま返す)
"""

from __future__ import annotations

import base64
import json
import operator
from dataclasses import dataclass
from typing import Any

import polars as pl

from admin_procedures.models import ComponentDef, ComputedMeasureDef, DataStructureDefinition, _clean_value, has_data

# リソース消費抑制のための入力制限
MAX_Q_LENGTH = 1024                 # 全文検索キーワード長
MAX_WHERE_FIELDS = 200              # where のフィールド数
MAX_WHERE_ARRAY_SIZE = 200          # where 配列（IN/contains）の要素数
MAX_WHERE_STRING_LENGTH = 10_000    # where 文字列値の長さ
MAX_GROUP_BY_FIELDS = 200           # group_by のフィールド数
MAX_METRICS = 200                   # metrics の個数
MAX_CURSOR_LENGTH = 2048            # cursor の長さ（バイト）
MAX_CURSOR_OFFSET = (1 << 63) - 1   # Polars の slice が受理できる符号付き64bit上限
MAX_QUERY_LIMIT = 5_000             # query_records の limit 上限
MAX_AGGREGATE_LIMIT = 10_000        # summarize_records の limit 上限


def ensure_df(data: Any) -> pl.DataFrame:
    """LazyFrame なら collect して DataFrame にする。DataFrame はそのまま返す。"""
    if isinstance(data, pl.LazyFrame):
        return data.collect()
    return data


# ============================================================
# カーソルベースページネーション
# ============================================================


class CursorCodec:
    """opaque cursor トークンのエンコード/デコード。

    内部形式: base64(json({"o": offset, "v": version}))
    version が異なるカーソルは拒否する。
    """

    @staticmethod
    def encode(offset: int, version: str) -> str:
        """オフセットとバージョンからカーソルトークンを生成する。

        Args:
            offset: データ内のオフセット位置。
            version: データセットバージョン（カーソル無効化用）。

        Returns:
            Base64 エンコードされた opaque カーソル文字列。
        """
        payload = json.dumps({"o": offset, "v": version}, separators=(",", ":"))
        return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")

    @staticmethod
    def decode(cursor: str, expected_version: str) -> int:
        """カーソルをデコードし offset を返す。不正時は ValueError。"""
        padded = cursor + "=" * (-len(cursor) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(padded))
            if not isinstance(payload, dict):
                raise ValueError("cursor payload is not an object")
            if "o" not in payload or "v" not in payload:
                raise ValueError("cursor payload is missing required keys")
            offset = payload["o"]
            if not isinstance(offset, int) or isinstance(offset, bool):
                raise ValueError("cursor offset is not an integer")
            if offset < 0:
                raise ValueError("cursor offset is negative")
            if offset > MAX_CURSOR_OFFSET:
                raise ValueError("cursor offset exceeds the supported range")
        except Exception as exc:
            raise ValueError("カーソル形式が不正です") from exc

        if payload.get("v") != expected_version:
            raise ValueError(
                f"Cursor was created for version '{payload.get('v')}' "
                f"but current version is '{expected_version}'"
            )
        return offset


def paginate(
    data: Any,
    limit: int,
    cursor: str | None,
    version: str,
    max_limit: int = 5000,
) -> tuple[pl.DataFrame, str | None, int]:
    """カーソルベースページネーションを適用する。data は polars DataFrame。

    Returns:
        (page_df, next_cursor_or_None, total_count)
    """
    limit = max(1, min(max_limit, limit))

    if cursor:
        offset = CursorCodec.decode(cursor, version)
    else:
        offset = 0

    data = ensure_df(data)
    total = len(data)
    page = data.slice(offset, limit)
    next_offset = offset + len(page)

    next_cursor = None
    if next_offset < total:
        next_cursor = CursorCodec.encode(next_offset, version)

    return page, next_cursor, total


# ============================================================
# 共通レスポンスヘルパー
# ============================================================


def _coerce_json(value: Any, expected_type: type, label: str) -> Any | None:
    """MCP クライアントが JSON 文字列で送ってくるパラメータをパースする汎用関数。"""
    if value is None:
        return None
    if isinstance(value, expected_type):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            hint = ""
            if expected_type is dict and any(
                kw in value.upper() for kw in ("AND", "OR", "IN (", "LIKE", "WHERE", "=")
            ):
                hint = (
                    " SQL 構文ではなく JSON オブジェクトで指定してください。"
                    ' 例: {"フィールド名": "値"}'
                    ' / {"フィールド名": ["値1", "値2"]}'
                    ' / {"フィールド名": {"$gte": 100}}'
                )
            raise ValueError(
                f"Invalid JSON string for {label} parameter: {value!r}.{hint}"
            )
        if not isinstance(parsed, expected_type):
            type_label = "JSON object" if expected_type is dict else "JSON array"
            raise ValueError(f"{type_label} が必要ですが、{type(parsed).__name__} が渡されました")
        return parsed
    raise ValueError(f"{expected_type.__name__} または JSON 文字列が必要ですが、{type(value).__name__} が渡されました")


def coerce_dict(value: dict[str, Any] | str | None) -> dict[str, Any] | None:
    """MCP クライアントが JSON 文字列で送ってくる dict パラメータをパースする。"""
    return _coerce_json(value, dict, "dict")


def coerce_list(value: list[str] | str | None) -> list[str] | None:
    """MCP クライアントが JSON 文字列で送ってくる list パラメータをパースする。"""
    return _coerce_json(value, list, "list")


def json_compact(obj: Any) -> str:
    """コンパクト JSON にシリアライズする。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def apply_columnar(result: dict[str, Any]) -> dict[str, Any]:
    """result 内の records/groups (DataFrame) を columnar format に変換した新しい dict を返す。

    キーの重複出力を避け、トークン消費とメモリを削減する。
    """
    out = dict(result)
    for key in ("records", "groups"):
        if key in out and isinstance(out[key], pl.DataFrame):
            df = ensure_df(out.pop(key))
            if df.is_empty():
                out.update({"columns": [], "rows": []})
            else:
                out.update({"columns": df.columns, "rows": df.rows()})
            break
    return out


def error_response(
    dataset_id: str,
    *,
    message: str | None = None,
    hint: str | None = None,
    available_datasets: list[str] | None = None,
) -> str:
    """データセット未発見時の標準エラーレスポンス。"""
    if message:
        result: dict[str, Any] = {"error": message}
    else:
        result = {"error": f"データセット '{dataset_id}' が見つかりません"}
    if hint:
        result["hint"] = hint
    else:
        result["hint"] = "list_datasets ツールでデータセット一覧を確認してください。"
        if available_datasets:
            result["available_datasets"] = available_datasets
    return json_compact(result)


# ============================================================
# フィルタ述語
# ============================================================

class FilterPredicate:
    """フィルタ述語の基底クラス。サブクラスで matches() を実装する。"""

    def matches(self, record: dict[str, Any]) -> bool:
        """レコードがこの述語に一致するか判定する。

        Args:
            record: 日本語キーのレコード辞書。

        Returns:
            一致する場合は True。
        """
        raise NotImplementedError

    def to_expr(self) -> Any:
        """polars Expr を返す。未実装の場合は None (フォールバック)。"""
        return None


class _SubstringPredicate(FilterPredicate):
    """部分一致/不一致の共通基底。negate=True で NOT 動作。"""

    _negate: bool = False

    def __init__(self, ja: str, substring: str | list[str]) -> None:
        self.ja = ja
        if isinstance(substring, list):
            self.substrings = [str(s).lower() for s in substring]
        else:
            self.substrings = [str(substring).lower()]

    def matches(self, record: dict[str, Any]) -> bool:
        val = str(record.get(self.ja, "") or "").lower()
        if self._negate:
            return all(s not in val for s in self.substrings)
        return any(s in val for s in self.substrings)

    def to_expr(self) -> Any:
        col = pl.col(self.ja).cast(pl.Utf8).fill_null("").str.to_lowercase()
        if self._negate:
            exprs = [~col.str.contains(s, literal=True) for s in self.substrings]
            return exprs[0] if len(exprs) == 1 else pl.all_horizontal(*exprs)
        exprs = [col.str.contains(s, literal=True) for s in self.substrings]
        return exprs[0] if len(exprs) == 1 else pl.any_horizontal(*exprs)


class ContainsPredicate(_SubstringPredicate):
    """部分一致（文字列）。単一値またはリスト（OR）を受け付ける。"""

    _negate = False


class NotContainsPredicate(_SubstringPredicate):
    """部分不一致（文字列）。単一値またはリスト（いずれも含まない）を受け付ける。"""

    _negate = True


class EqPredicate(FilterPredicate):
    """完全一致。単一値またはリスト（いずれかに一致）を受け付ける。"""

    def __init__(self, ja: str, value: Any, *, multi_value: bool = False) -> None:
        self.ja = ja
        self.multi_value = multi_value
        self.values: set[str] = _coerce_value_set(value)
        self.single = not isinstance(value, list)
        if self.single:
            self._original = value  # 数値完全一致用

    def matches(self, record: dict[str, Any]) -> bool:
        if self.single and not self.multi_value:
            # 数値など型付き完全一致
            return record.get(self.ja) == self._original
        raw = str(record.get(self.ja, "") or "")
        if self.multi_value:
            # セミコロン区切りフィールド: 各部分値で判定
            if not raw.strip():
                return False
            parts = [p.strip() for p in raw.split(";")]
            return any(p in self.values for p in parts if p)
        return raw in self.values

    def to_expr(self) -> Any:
        vals_list = sorted(self.values)
        if self.multi_value:
            # セミコロン区切り: split して各パーツが values に含まれるか
            col = pl.col(self.ja).cast(pl.Utf8).fill_null("")
            return col.str.split(";").list.eval(
                pl.element().str.strip_chars().is_in(vals_list)
            ).list.any()
        if self.single:
            v = self._original
            if isinstance(v, (int, float)):
                return pl.col(self.ja) == v
            return pl.col(self.ja).cast(pl.Utf8).fill_null("") == str(v)
        return pl.col(self.ja).cast(pl.Utf8).fill_null("").is_in(vals_list)


class _ComparisonPredicate(FilterPredicate):
    """数値比較述語の基底クラス。_op にoperator関数を設定して使う。"""

    _op: Any = None  # operator.ge / operator.le

    def __init__(self, ja: str, value: int | float) -> None:
        self.ja = ja
        self.value = value

    def matches(self, record: dict[str, Any]) -> bool:
        raw = record.get(self.ja)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return False
        return self._op(float(raw), self.value)

    def to_expr(self) -> Any:
        col = pl.col(self.ja).cast(pl.Float64, strict=False).fill_null(float("nan"))
        return self._op(col, self.value)


class GtePredicate(_ComparisonPredicate):
    """以上一致（>= 演算子、数値フィールド用）。"""
    _op = operator.ge


class LtePredicate(_ComparisonPredicate):
    """以下一致（<= 演算子、数値フィールド用）。"""
    _op = operator.le


def _coerce_value_set(value: Any) -> set[str]:
    """単一値またはリストを文字列セットに変換する。"""
    if isinstance(value, list):
        return {str(v) for v in value}
    return {str(value)}


class NePredicate(FilterPredicate):
    """不一致（!= 演算子）。単一値またはリストを受け付ける。"""

    def __init__(self, ja: str, value: Any) -> None:
        self.ja = ja
        self.values = _coerce_value_set(value)

    def matches(self, record: dict[str, Any]) -> bool:
        raw = str(record.get(self.ja, "") or "")
        return raw not in self.values

    def to_expr(self) -> Any:
        return ~pl.col(self.ja).cast(pl.Utf8).fill_null("").is_in(sorted(self.values))


class NotEmptyPredicate(FilterPredicate):
    """空でない値のみ一致。"""

    def __init__(self, ja: str) -> None:
        self.ja = ja

    def matches(self, record: dict[str, Any]) -> bool:
        val = record.get(self.ja)
        if val is None:
            return False
        return str(val).strip() != ""

    def to_expr(self) -> Any:
        col = pl.col(self.ja)
        return col.is_not_null() & (col.cast(pl.Utf8).fill_null("").str.strip_chars() != "")


# ============================================================
# 全文検索述語
# ============================================================


class FullTextPredicate(FilterPredicate):
    """複数フィールドの OR 部分一致。"""

    def __init__(
        self,
        target_ja_fields: list[str],
        keyword: str,
    ) -> None:
        self.target_ja_fields = target_ja_fields
        self.keyword = keyword.lower()

    def matches(self, record: dict[str, Any]) -> bool:
        for ja in self.target_ja_fields:
            raw = str(record.get(ja, "") or "")
            if self.keyword in raw.lower():
                return True
        return False

    def to_expr(self) -> Any:
        exprs = [
            pl.col(ja).cast(pl.Utf8).fill_null("").str.to_lowercase().str.contains(self.keyword, literal=True)
            for ja in self.target_ja_fields
        ]
        if not exprs:
            return pl.lit(False)
        return pl.any_horizontal(*exprs)


def build_fulltext_predicate(
    keyword: str,
    dsd: DataStructureDefinition,
    search_fields: list[str] | None = None,
) -> FullTextPredicate:
    """キーワードから FullTextPredicate を構築する。

    search_fields 未指定時は data_type="string" の全コンポーネントを対象にする。
    """
    if search_fields:
        components = []
        for en in search_fields:
            comp = dsd.get_component(en)
            if comp is None:
                raise ValueError(f"不明な検索フィールド '{en}'")
            components.append(comp)
    else:
        components = [c for c in dsd.components if c.data_type == "string"]

    ja_fields = [c.ja for c in components]
    return FullTextPredicate(ja_fields, keyword)


# ============================================================
# WhereClause パーサー
# ============================================================

def _validate_string_value(value: str, label: str, max_len: int = MAX_WHERE_STRING_LENGTH) -> None:
    """文字列値の長さを検証する。"""
    if len(value) > max_len:
        raise ValueError(f"{label} は {max_len} 文字以内です（入力: {len(value)} 文字）")


def _build_predicate(ja: str, op: str, val: Any, *, multi_value: bool = False) -> FilterPredicate:
    if op in ("$eq", "$in"):
        # 配列の場合、サイズを検証
        if isinstance(val, list):
            if len(val) > MAX_WHERE_ARRAY_SIZE:
                raise ValueError(
                    f"$in/$eq の配列は {MAX_WHERE_ARRAY_SIZE} 要素以内です（フィールド '{ja}': {len(val)} 要素）"
                )
            for v in val:
                if isinstance(v, str):
                    _validate_string_value(v, f"$in/$eq の配列要素（フィールド '{ja}'）")
        elif isinstance(val, str):
            _validate_string_value(val, f"$eq（フィールド '{ja}'）")
        return EqPredicate(ja, val, multi_value=multi_value)
    if op == "$ne":
        # $ne も配列をサポート。サイズ・文字列長を検証
        if isinstance(val, list):
            if len(val) > MAX_WHERE_ARRAY_SIZE:
                raise ValueError(
                    f"$ne の配列は {MAX_WHERE_ARRAY_SIZE} 要素以内です（フィールド '{ja}': {len(val)} 要素）"
                )
            for v in val:
                if isinstance(v, str):
                    _validate_string_value(v, f"$ne の配列要素（フィールド '{ja}'）")
        elif isinstance(val, str):
            _validate_string_value(val, f"$ne（フィールド '{ja}'）")
        return NePredicate(ja, val)
    if op == "$contains":
        # 文字列または文字列配列のみ許可
        if isinstance(val, list):
            if len(val) == 0:
                raise ValueError(f"$contains の配列は 1 要素以上必要です（フィールド '{ja}'）")
            if len(val) > MAX_WHERE_ARRAY_SIZE:
                raise ValueError(
                    f"$contains の配列は {MAX_WHERE_ARRAY_SIZE} 要素以内です（フィールド '{ja}': {len(val)} 要素）"
                )
            if not all(isinstance(v, str) for v in val):
                raise ValueError(f"$contains の配列は文字列のみ許可です（フィールド '{ja}'）")
            for v in val:
                _validate_string_value(v, f"$contains の配列要素（フィールド '{ja}'）")
        elif not isinstance(val, str):
            raise ValueError(f"$contains は文字列または文字列配列のみ許可です（フィールド '{ja}'）")
        else:
            _validate_string_value(val, f"$contains（フィールド '{ja}'）")
        return ContainsPredicate(ja, val)
    if op == "$not_contains":
        # 文字列のみ許可
        if not isinstance(val, str):
            raise ValueError(f"$not_contains は文字列のみ許可です（フィールド '{ja}'）")
        _validate_string_value(val, f"$not_contains（フィールド '{ja}'）")
        return NotContainsPredicate(ja, val)
    if op == "$gte":
        # int | float のみ許可
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            raise ValueError(f"$gte は数値（int/float）のみ許可です（フィールド '{ja}'、入力: {type(val).__name__}）")
        return GtePredicate(ja, val)
    if op == "$lte":
        # int | float のみ許可
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            raise ValueError(f"$lte は数値（int/float）のみ許可です（フィールド '{ja}'、入力: {type(val).__name__}）")
        return LtePredicate(ja, val)
    if op == "$not_empty":
        # $not_empty は値を取らない。値が指定されたら拒否
        if val is not None:
            raise ValueError(f"$not_empty は値を取りません（フィールド '{ja}'）")
        return NotEmptyPredicate(ja)
    raise ValueError(
        f"不明な演算子 '{op}'。"
        f"使用可能: $eq, $ne, $contains, $not_contains, $gte, $lte, $not_empty"
    )


def parse_where(
    where: dict[str, Any] | None,
    dsd: DataStructureDefinition,
) -> list[FilterPredicate]:
    """where dict をフィルタ述語リストに変換する。"""
    if not where:
        return []

    if len(where) > MAX_WHERE_FIELDS:
        raise ValueError(
            f"where は {MAX_WHERE_FIELDS} フィールド以内です（入力: {len(where)}）"
        )

    predicates: list[FilterPredicate] = []
    for field_name, condition in where.items():
        comp = dsd.get_component(field_name)
        if comp is None:
            raise ValueError(f"不明なフィールド '{field_name}'")
        ja = comp.ja

        if isinstance(condition, list):
            # 配列 → いずれかに一致（multi_value フィールドはセミコロン分割で判定）
            if len(condition) > MAX_WHERE_ARRAY_SIZE:
                raise ValueError(
                    f"where の配列は {MAX_WHERE_ARRAY_SIZE} 要素以内です（フィールド '{field_name}': {len(condition)} 要素）"
                )
            for elem in condition:
                if isinstance(elem, str):
                    _validate_string_value(elem, f"where の配列要素（フィールド '{field_name}'）")
            predicates.append(EqPredicate(ja, condition, multi_value=comp.multi_value))
        elif isinstance(condition, str):
            # 文字列 → 部分一致
            _validate_string_value(condition, f"where（フィールド '{field_name}'）")
            predicates.append(ContainsPredicate(ja, condition))
        elif isinstance(condition, (int, float)):
            # 数値 → 完全一致
            predicates.append(EqPredicate(ja, condition))
        elif isinstance(condition, dict):
            for op, val in condition.items():
                predicates.append(_build_predicate(ja, op, val, multi_value=comp.multi_value))
        else:
            raise ValueError(
                f"Invalid condition type for '{field_name}': {type(condition).__name__}"
            )

    return predicates


# ============================================================
# フィルタ適用
# ============================================================

def apply_filters(
    data: Any,
    predicates: list[FilterPredicate],
) -> Any:
    """全述語を AND 結合でフィルタリングする。DataFrame / list[dict] 両対応。

    DataFrame は Polars Expr で高速フィルタ。
    list[dict] (having 句の集計結果など) は Python パスで処理。
    """
    if not predicates:
        return data

    if isinstance(data, list):
        result = data
        for pred in predicates:
            result = [rec for rec in result if pred.matches(rec)]
        return result

    exprs = [p.to_expr() for p in predicates]
    combined = exprs[0]
    for e in exprs[1:]:
        combined = combined & e
    return data.filter(combined)


# ============================================================
# ソート
# ============================================================

def apply_order_by(
    data: Any,
    order_by: str | None,
    dsd: DataStructureDefinition,
) -> Any:
    """フィールドでソートする。'-' 接頭辞で降順。data は polars DataFrame。"""
    if not order_by:
        return data
    desc = order_by.startswith("-")
    field_name = order_by.lstrip("-")
    comp = dsd.get_component(field_name)
    if comp is None:
        raise ValueError(f"不明な order_by フィールド '{field_name}'")
    ja = comp.ja

    if ja not in data.columns:
        return data
    return data.sort(ja, descending=desc, nulls_last=True)


# ============================================================
# 表示可能 / 非表示フィールド
# ============================================================

def compute_field_display_info(
    data: pl.DataFrame,
    all_fields: list[str],
    generic_values: set[str] | None = None,
) -> tuple[list[str], dict[str, str]]:
    """DataFrame を走査し、各フィールドの表示可否を判定する。

    - 1 件でも非 null・非ジェネリック値があれば displayable
    - 全件 null なら suppressed (理由: "all_null")
    - 全件ジェネリック値のみなら suppressed (理由: "generic_value_only (...)")

    Args:
        generic_values: 表示抑制する汎用値の set (YAML 設定から)。

    Returns:
        (displayable_fields, suppressed_fields)
    """
    if generic_values is None:
        generic_values = set()

    data = ensure_df(data)
    displayable: list[str] = []
    suppressed: dict[str, str] = {}
    n = len(data)

    for col_name in all_fields:
        if col_name not in data.columns:
            suppressed[col_name] = "all_null"
            continue

        col = data[col_name]
        non_null = col.drop_nulls()

        # 空文字列も除外
        if non_null.dtype == pl.Utf8:
            non_null = non_null.filter(non_null.str.strip_chars() != "")

        if len(non_null) == 0:
            suppressed[col_name] = "all_null"
        elif generic_values and non_null.dtype == pl.Utf8:
            # 全値がジェネリック値のみかチェック (Polars 内で完結)
            stripped = non_null.str.strip_chars()
            all_generic = stripped.is_in(list(generic_values)).sum() == len(stripped)
            if all_generic:
                gv_str = ", ".join(sorted(generic_values))
                suppressed[col_name] = f"generic_value_only ({gv_str})"
            else:
                displayable.append(col_name)
        else:
            displayable.append(col_name)

    return displayable, suppressed


# ============================================================
# 集計エンジン
# ============================================================

VALID_METRICS = {"count", "sum", "avg", "min", "max"}


def parse_metrics(
    metrics: list[str],
    dsd: DataStructureDefinition,
) -> list[tuple[str, str | None, ComputedMeasureDef | None]]:
    """メトリクス文字列をパースする。

    "count" → ("count", None, None)
    "sum:total_volume" → ("sum", "総手続件数", None)  # ja 名に変換
    "avg:online_rate" → ("computed_avg", None, ComputedMeasureDef)
    """
    parsed: list[tuple[str, str | None, ComputedMeasureDef | None]] = []
    for m in metrics:
        if ":" in m:
            metric_type, field_name = m.split(":", 1)
        else:
            metric_type = m
            field_name = None

        if metric_type not in VALID_METRICS:
            raise ValueError(
                f"不明な集計種別 '{metric_type}'。"
                f"使用可能: {', '.join(sorted(VALID_METRICS))}"
            )

        if field_name:
            comp = dsd.get_component(field_name)
            if comp is not None:
                if not comp.aggregatable:
                    raise ValueError(
                        f"フィールド '{field_name}' は集計対象外です。"
                        f"aggregatable=true の数値項目のみ集計可能です。"
                    )
                parsed.append((metric_type, comp.ja, None))
            else:
                # 算出数値項目を探す
                cm = dsd.get_computed_measure(field_name)
                if cm is None:
                    raise ValueError(f"不明なフィールド '{field_name}'")
                if metric_type != "avg":
                    raise ValueError(
                        f"算出数値項目 '{field_name}' は 'avg'（加重平均）のみ対応しています。"
                        f"'avg:{field_name}' を使用してください。"
                    )
                parsed.append(("computed_avg", None, cm))
        else:
            if metric_type != "count":
                raise ValueError(
                    f"Metric '{metric_type}' requires a field. "
                    f"Use '{metric_type}:field_name'."
                )
            parsed.append(("count", None, None))

    return parsed


_SIMPLE_AGG_FN = {"sum": "sum", "avg": "mean", "min": "min", "max": "max"}


def compute_aggregation(
    data: Any,
    group_by_components: list[ComponentDef],
    parsed_metrics: list[tuple[str, str | None, ComputedMeasureDef | None]],
    dsd: DataStructureDefinition,
) -> pl.DataFrame:
    """グループ別集計を Polars で実行する。data は polars DataFrame。"""

    df = data
    group_by_ja = [c.ja for c in group_by_components]

    agg_exprs: list[Any] = []
    for metric_type, target_ja, cm in parsed_metrics:
        if metric_type == "count":
            agg_exprs.append(pl.len().alias("count"))
        elif metric_type == "computed_avg" and cm is not None:
            if cm.mode == "count_where":
                cond_ja = dsd.get_component(cm.condition_field).ja
                cond_col = pl.col(cond_ja).cast(pl.Utf8).fill_null("")
                matched = cond_col.is_in(list(cm.condition_values)).sum().cast(pl.Float64)
                agg_exprs.append(
                    (matched / pl.len()).round(4).alias(f"avg:{cm.name}")
                )
            else:  # sum_ratio
                num_ja = dsd.get_component(cm.numerator).ja
                den_ja = dsd.get_component(cm.denominator).ja
                num_sum = pl.col(num_ja).cast(pl.Float64, strict=False).sum()
                den_sum = pl.col(den_ja).cast(pl.Float64, strict=False).sum()
                agg_exprs.append(
                    pl.when(den_sum != 0).then((num_sum / den_sum).round(4))
                    .otherwise(pl.lit(None))
                    .alias(f"avg:{cm.name}")
                )
        elif target_ja and metric_type in _SIMPLE_AGG_FN:
            num_col = pl.col(target_ja).cast(pl.Float64, strict=False)
            fn_name = _SIMPLE_AGG_FN[metric_type]
            agg_exprs.append(getattr(num_col, fn_name)().alias(f"{metric_type}:{target_ja}"))
            agg_exprs.append(num_col.is_null().sum().alias(f"{metric_type}:{target_ja}:null_excluded"))

    if not agg_exprs:
        return pl.DataFrame()

    df = ensure_df(df)
    if group_by_ja:
        cast_exprs = [pl.col(ja).cast(pl.Utf8).fill_null("").alias(ja) for ja in group_by_ja]
        result_df = df.with_columns(cast_exprs).group_by(group_by_ja).agg(agg_exprs)
    else:
        result_df = df.select(agg_exprs)

    # 後処理: avg → round(2), sum → int, null_excluded → int
    post_cols: list[Any] = []
    for metric_type, target_ja, cm in parsed_metrics:
        if target_ja and metric_type in ("avg", "sum"):
            col_name = f"{metric_type}:{target_ja}"
            if col_name in result_df.columns:
                if metric_type == "avg":
                    post_cols.append(pl.col(col_name).round(2))
                else:
                    post_cols.append(pl.col(col_name).cast(pl.Int64, strict=False))
    for c in result_df.columns:
        if c.endswith(":null_excluded"):
            post_cols.append(pl.col(c).cast(pl.Int64))
    if post_cols:
        result_df = result_df.with_columns(post_cols)

    return result_df


# ============================================================
# Having 句（後集計フィルタ）
# ============================================================


def having_column_names(
    parsed_metrics: list[tuple[str, str | None, ComputedMeasureDef | None]],
    dsd: DataStructureDefinition,
) -> set[str]:
    """集計結果のカラム名セットを構築する。"""
    cols: set[str] = set()
    for metric_type, target_ja, cm in parsed_metrics:
        if metric_type == "count":
            cols.add("count")
        elif metric_type == "computed_avg" and cm is not None:
            cols.add(f"avg:{cm.name}")
        elif target_ja:
            cols.add(f"{metric_type}:{target_ja}")
    return cols


def parse_having(
    having: dict[str, Any] | None,
    valid_columns: set[str],
) -> list[FilterPredicate]:
    """having dict を述語リストに変換する。集計結果カラム名で動作。"""
    if not having:
        return []
    predicates: list[FilterPredicate] = []
    for col, condition in having.items():
        if col not in valid_columns:
            raise ValueError(
                f"不明な having カラム '{col}'。"
                f"使用可能: {', '.join(sorted(valid_columns))}"
            )
        if isinstance(condition, dict):
            for op, val in condition.items():
                predicates.append(_build_predicate(col, op, val))
        elif isinstance(condition, (int, float)):
            predicates.append(EqPredicate(col, condition))
        else:
            raise ValueError("having 条件は数値または演算子辞書で指定してください。")
    return predicates


# ============================================================
# Explode（セミコロン区切りフィールドの展開）
# ============================================================


def explode_records(
    data: Any,
    explode_ja: str,
) -> Any:
    """セミコロン区切りフィールドを分割し、レコードを複製する。data は polars DataFrame。

    例: {"event": "A;B", "vol": 100} → [{"event": "A", ...}, {"event": "B", ...}]
    空値レコードは除外する。
    """

    data = ensure_df(data)
    if explode_ja not in data.columns:
        return data.clear()
    return data.with_columns(
        pl.col(explode_ja).cast(pl.Utf8).fill_null("").str.split(";")
    ).explode(explode_ja).with_columns(
        pl.col(explode_ja).str.strip_chars()
    ).filter(
        pl.col(explode_ja) != ""
    )


# ============================================================
# データ品質計算エンジン
# ============================================================


@dataclass
class FieldQualityStats:
    """1フィールドの品質統計。"""

    field_name: str
    field_ja: str
    role: str
    total_records: int
    non_null_count: int
    null_count: int
    empty_count: int
    fill_rate: float

    # 数値フィールド用
    numeric_min: int | None = None
    numeric_max: int | None = None
    numeric_mean: float | None = None
    zero_count: int | None = None

    # 文字列フィールド用
    cardinality: int | None = None
    top_values: list[tuple[str, int]] | None = None



def _make_base_stats(
    comp: ComponentDef,
    total: int,
    non_null_count: int,
    null_count: int,
    empty_count: int,
) -> FieldQualityStats:
    """FieldQualityStats の基本フィールドを構築する。"""
    return FieldQualityStats(
        field_name=comp.ja, field_ja=comp.ja, role=comp.role.value,
        total_records=total, non_null_count=non_null_count,
        null_count=null_count, empty_count=empty_count,
        fill_rate=round(non_null_count / total, 4) if total > 0 else 0.0,
    )


def _field_quality_polars(data: Any, comp: ComponentDef) -> "FieldQualityStats":
    """Polars DataFrame 向け品質統計計算。"""
    data = ensure_df(data)
    ja = comp.ja
    total = len(data)
    if ja not in data.columns:
        return _make_base_stats(comp, total, 0, total, 0)
    col = data[ja]
    null_count = col.null_count()
    str_col = col.cast(pl.Utf8, strict=False).fill_null("")
    stripped = str_col.str.strip_chars()
    empty_count = ((col.is_not_null()) & (stripped == "")).sum()
    non_null_count = total - null_count - empty_count

    stats = _make_base_stats(comp, total, non_null_count, null_count, empty_count)

    if comp.data_type in ("integer", "float") and non_null_count > 0:
        valid = stripped.filter(stripped != "")
        num_col = valid.cast(pl.Float64, strict=False).drop_nulls()
        if len(num_col) > 0:
            is_int = comp.data_type == "integer"
            cast_fn = int if is_int else (lambda v: round(float(v), 4))
            stats.numeric_min = cast_fn(num_col.min())
            stats.numeric_max = cast_fn(num_col.max())
            stats.numeric_mean = round(float(num_col.mean()), 2)
            stats.zero_count = int((num_col == 0).sum())
    elif comp.data_type == "string" and non_null_count > 0:
        valid = stripped.filter(stripped != "")
        vc = valid.value_counts()
        stats.cardinality = len(vc)
        sorted_vc = vc.sort("count", descending=True).head(5)
        stats.top_values = [
            (row[ja], row["count"]) for row in sorted_vc.iter_rows(named=True)
        ]
    return stats


def compute_dataset_quality(
    data: Any,
    dsd: DataStructureDefinition,
    fields: list[str] | None = None,
) -> list[FieldQualityStats]:
    """複数フィールドの品質統計を計算する。"""
    if fields:
        components = []
        for en in fields:
            comp = dsd.get_component(en)
            if comp is None:
                raise ValueError(f"不明なフィールド '{en}'")
            components.append(comp)
    else:
        components = list(dsd.components)

    return [_field_quality_polars(data, comp) for comp in components]



def get_quality_stats(ver: Any) -> dict[str, FieldQualityStats]:
    """DatasetVersion の品質統計を返す。初回アクセス時に全フィールド一括計算してキャッシュする。

    Args:
        ver: DatasetVersion インスタンス (data, dsd, _quality_stats_cache を持つ)。

    Returns:
        {field_name: FieldQualityStats} の辞書。
    """
    if ver._quality_stats_cache is not None:
        return ver._quality_stats_cache
    cache: dict[str, FieldQualityStats] = {}
    if has_data(ver.data) and ver.dsd:
        for qs in compute_dataset_quality(ver.data, ver.dsd):
            cache[qs.field_name] = qs
    ver._quality_stats_cache = cache
    return cache



# ============================================================
# フィールド注意事項 (notes.details) 収集
# ============================================================


def collect_field_notes(
    dsd: DataStructureDefinition,
    involved_fields: list[str] | None = None,
) -> dict[str, list[str]]:
    """関連フィールドの notes.details を収集してフィールド名→注意事項リストの辞書を返す。

    Args:
        dsd: データ構造定義。
        involved_fields: 収集対象フィールド名リスト。
            None の場合は全コンポーネント + 全算出数値項目を対象とする。

    Returns:
        {field_name: [detail1, detail2, ...]} 形式の辞書。
    """
    result: dict[str, list[str]] = {}

    def _add(name: str, details: tuple[str, ...]) -> None:
        existing = result.get(name)
        if existing is None:
            result[name] = list(details)
        else:
            for c in details:
                if c not in existing:
                    existing.append(c)

    if involved_fields is None:
        for comp in dsd.components:
            if comp.notes.details:
                _add(comp.ja, comp.notes.details)
        for cm in dsd.computed_measures:
            if cm.notes.details:
                _add(cm.name, cm.notes.details)
    else:
        for field_name in involved_fields:
            comp = dsd.get_component(field_name)
            if comp and comp.notes.details:
                _add(comp.ja, comp.notes.details)
                continue
            cm = dsd.get_computed_measure(field_name)
            if cm and cm.notes.details:
                _add(cm.name, cm.notes.details)

    return result
