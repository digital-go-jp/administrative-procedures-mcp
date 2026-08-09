#!/usr/bin/env python3
"""
prepare_dataset.py — CSV からデータセットを準備する統合スクリプト。

dataset.yaml の有無で自動的にモードを切り替える:
  - YAML なし → scaffold: CSV 分析 → YAML 生成 → Parquet 変換
  - YAML あり → convert: YAML 設定に基づく Parquet 変換

通常は `apcli add` を使う。本モジュールは同じ処理の直接実行用エントリポイント。

使い方:
  # 新規データセット（scaffold → convert）
  apcli add my-dataset --csv path/to/my_data.csv

  # 既存データセット（convert のみ）
  apcli add my-dataset
  apcli add my-dataset --csv path/to/updated.csv

  # 強制 scaffold（YAML 再生成）
  apcli add my-dataset --csv path/to/my_data.csv --force-scaffold

出力先は datasets/ を含むベースディレクトリ配下。解決順は
--data-dir 引数 → ADMIN_PROCEDURES_DATA_DIR → パッケージ相対 → カレントディレクトリ。
"""

from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import os
import re
import socket
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import polars as pl
import yaml

DATASETS_DIR_NAME = "datasets"
SOURCE_DIR_NAME = "source-data"

from admin_procedures.loader import FETCH_STATE_FILENAME  # noqa: E402
from admin_procedures.validation import resolve_under  # noqa: E402


def resolve_base_dir(explicit: str | Path | None = None) -> Path:
    """datasets/ を置くベースディレクトリを解決する。

    解決順:
        1. explicit 引数 (--data-dir)
        2. ADMIN_PROCEDURES_DATA_DIR 環境変数
        3. loader.resolve_data_dir() と同じ探索（パッケージ相対 → CWD）
        4. カレントディレクトリ（datasets/ がまだ存在しない新規作成ケース）

    loader 側と揃えることで、生成したデータセットをそのまま
    apcli / MCP サーバーから読めるようにする。
    """
    from admin_procedures.loader import DATA_DIR_ENV_VAR, resolve_data_dir

    try:
        return resolve_data_dir(explicit)
    except FileNotFoundError:
        # datasets/ がまだ無い = 新規作成。作成先だけ自前で決める。
        if explicit is not None:
            return Path(explicit)
        env = os.environ.get(DATA_DIR_ENV_VAR)
        return Path(env) if env else Path.cwd()


# ============================================================
# 共通: 表形式ファイルの読み込み (CSV / XLSX)
# ============================================================

_EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xlsb"}

# 日本の公的機関が配布する CSV は CP932 (Shift_JIS) が今も多い。
_ENCODING_CANDIDATES = ("utf-8-sig", "cp932", "euc-jp")

# 文字化けの検出用。
# CP932 はほぼ任意のバイト列をデコードできてしまい、未定義バイトを
# 私用領域 (U+F8F0-F8F3) に写す。そのためデコード成功だけでは判定にならず、
# 制御文字と私用領域の出現を文字化けの signal として併用する。
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_PRIVATE_USE_RE = re.compile(r"[-]")


def _looks_like_text(s: str) -> bool:
    """デコード結果がテキストとして妥当か判定する。"""
    sample = s[:8192]
    return not (_CONTROL_RE.search(sample) or _PRIVATE_USE_RE.search(sample))


def _sniff_bom(path: Path) -> str | None:
    """BOM からエンコーディングを判定する。Excel の Unicode 出力対策。"""
    import codecs

    head = path.open("rb").read(4)
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if head.startswith(codecs.BOM_UTF16_LE) or head.startswith(codecs.BOM_UTF16_BE):
        return "utf-16"
    return None


def detect_encoding(path: Path, explicit: str | None = None) -> str:
    """CSV のエンコーディングを判定する。

    explicit が渡された場合はそれを優先する（誤判定時の逃げ道）。
    どれでも妥当に読めない場合は ValueError で候補を案内する。
    """
    if explicit and explicit not in _ENCODING_CANDIDATES:
        return explicit  # 利用者が明示指定したものを尊重する

    bom = _sniff_bom(path)
    if bom:
        return bom

    for enc in _ENCODING_CANDIDATES:
        try:
            with open(path, encoding=enc) as f:
                text = f.read()
        except (UnicodeDecodeError, LookupError):
            continue
        if _looks_like_text(text):
            return enc

    raise ValueError(
        f"文字コードを判定できません: {path.name}\n"
        f"  試した候補: {', '.join(_ENCODING_CANDIDATES)}\n"
        f"  --encoding で明示してください (例: --encoding shift_jis)",
    )


def _read_table(
    path: Path,
    header_rows: int,
    encoding: str = "utf-8-sig",
) -> tuple[list[str], list[list[str]]]:
    """CSV / Excel を (最終ヘッダー行, データ行) として読む。

    公的機関の配布は Excel が多いため、CSV と同じ経路で扱えるようにする。
    値はすべて文字列に正規化し、後段の型変換は既存ロジックに委ねる。
    """
    if path.suffix.lower() in _EXCEL_SUFFIXES:
        try:
            df = pl.read_excel(path, has_header=False)
        except ModuleNotFoundError as e:  # fastexcel 未導入
            raise ValueError(
                f"Excel ファイルの読み込みには fastexcel が必要です: {path.name}\n"
                f'  pip install -e ".[excel]"  または  pip install fastexcel',
            ) from e
        rows = [
            ["" if v is None else str(v) for v in row]
            for row in df.rows()
        ]
    else:
        resolved = detect_encoding(path, encoding if encoding != "utf-8-sig" else None)
        if resolved != "utf-8-sig":
            print(f"  Encoding: {resolved} を検出しました")
        with open(path, encoding=resolved, newline="") as f:
            rows = list(csv.reader(f))

    if len(rows) < header_rows:
        return [], []
    header = [str(c).strip() for c in rows[header_rows - 1]] if header_rows else []
    return header, rows[header_rows:]


# ============================================================
# 共通: 表形式ファイル → Parquet 変換
# ============================================================


def _cast_numeric_columns(
    df: pl.DataFrame,
    numeric_columns: dict[str, str],
) -> tuple[pl.DataFrame, list[tuple[str, int, int]]]:
    """数値カラムを型変換し、変換で失われた値を報告する。

    桁区切り (1,234,567) は除去してから変換する。除去しないと strict=False の
    キャストが全行 null になり、データが黙って消える。

    Returns:
        (変換後 DataFrame, [(カラム名, 変換前の非空件数, 変換後の非空件数), ...])
        報告対象は値が半分以上失われたカラムのみ。
    """
    losses: list[tuple[str, int, int]] = []
    for col, data_type in numeric_columns.items():
        if col not in df.columns:
            continue
        before = df[col].drop_nulls().len()
        target = pl.Float64 if data_type == "float" else pl.Int64
        df = df.with_columns(
            pl.col(col).cast(pl.Utf8)
            .str.replace_all(",", "", literal=True)
            .cast(target, strict=False)
            .alias(col),
        )
        after = df[col].drop_nulls().len()
        if before > 0 and after * 2 < before:
            losses.append((col, before, after))
    return df, losses


def _csv_to_parquet(
    csv_path: Path,
    output_path: Path,
    header_rows: int,
    column_specs: list[tuple[str, int]],
    numeric_columns: dict[str, str],
    encoding: str = "utf-8-sig",
) -> int:
    """CSV / Excel を読み込み Parquet に変換する。行数を返す。

    Args:
        column_specs: [(カラム名, csv_col_index), ...] の順序付きリスト
        numeric_columns: {カラム名: "integer" | "float"} の変換指定
    """
    max_col_idx = max(idx for _, idx in column_specs) + 1

    _, rows = _read_table(csv_path, header_rows, encoding)

    for row in rows:
        while len(row) < max_col_idx:
            row.append("")

    # Polars DataFrame 構築
    columns: dict[str, list[str | None]] = {}
    for col_name, col_idx in column_specs:
        columns[col_name] = [
            row[col_idx] if col_idx < len(row) and row[col_idx] else None
            for row in rows
        ]
    df = pl.DataFrame(columns)

    # 数値カラム変換 (非数値/空値は null のまま保持)
    df, losses = _cast_numeric_columns(df, numeric_columns)
    for col, before, after in losses:
        print(
            f"  警告: '{col}' は数値変換で {before} 件中 {before - after} 件が "
            f"null になりました。data_type が実際の値と合っていない可能性があります。",
        )

    # Parquet 書き出し
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(output_path, compression="snappy")

    return len(rows)


# ============================================================
# Fetch モード: 配布元から取得 → Parquet
# ============================================================


class FetchError(Exception):
    """配布元からの取得に失敗した。手動取得の手順を message に含める。"""


# 想定外の巨大応答からの防御 (配布 CSV/Excel は大きくても数十 MB)
_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024


def _require_https(url: str, *, context: str) -> None:
    """取得先 URL が https であることを検証する。

    dataset.yaml の source.url も配布ページ内の href も入力データであり、
    平文 http だと経路上でファイルを差し替えられる。
    """
    from urllib.parse import urlsplit

    if urlsplit(url).scheme != "https":
        raise FetchError(
            f"{context}が https ではないため取得を中止しました: {url}\n"
            f"  必要であれば手動でダウンロードし、apcli add --csv で取り込んでください。",
        )


def _asset_filename(asset_url: str) -> str:
    """URL のパス末尾から保存ファイル名を決める (クエリ・空名・特殊名を排除)。"""
    from urllib.parse import unquote, urlsplit

    name = Path(unquote(urlsplit(asset_url).path)).name
    if not name or name in (".", ".."):
        raise FetchError(f"配布ファイル名を URL から特定できません: {asset_url}")
    return name


def _find_asset_url(page_url: str, pattern: str, timeout: int = 60, allowed_hosts: frozenset[str] | None = None) -> str:
    """配布ページを取得し、パターンに一致するファイルの URL を返す。

    直リンクを YAML に焼き込まないのは、配布元がファイルを改訂するたびに
    日付とパス中のハッシュが変わり、リンクが必ず腐るため。
    ページのスラッグとファイル名の命名規則のほうが寿命が長い。

    セキュリティ：
    - 配布ページ URL を HTTPS のみで検証
    - ページサイズを 20 MiB に制限
    - リダイレクトを自動追従しない
    （dataset.yaml は信頼済み入力として扱う）
    """
    import re
    import urllib.parse

    # SSRF 対策：URL 検証
    try:
        _validate_fetch_url(page_url, allowed_hosts=allowed_hosts)
    except FetchError as e:
        raise FetchError(f"配布ページ URL が不正です: {e}") from e

    # パターンはページ取得前に検証し、不正ならネットワークに出ない
    try:
        compiled_pattern = re.compile(pattern)
    except re.error as e:
        raise FetchError(
            f"source.asset_pattern が正規表現として不正です: {pattern!r}\n  {e}",
        ) from e

    # URL 取得（サイズ制限、ストリーミング、リダイレクト無効）
    # 配布ページは通常数 MiB、念のため 20 MiB の余裕を持たせる
    try:
        html_bytes = _fetch_url_with_limit(page_url, max_size=20 * 1024 * 1024, timeout=timeout)
        html = html_bytes.decode("utf-8", "replace")
    except FetchError:
        raise

    hrefs = re.findall(r'href="([^"]+)"', html)
    matched = [h for h in hrefs if compiled_pattern.search(h)]
    if not matched:
        raise FetchError(
            f"配布ページにパターン {pattern!r} に一致するファイルが見つかりません。\n"
            f"  ページ構成かファイル名が変わった可能性があります: {page_url}",
        )
    asset_url = urllib.parse.urljoin(page_url, matched[0])
    _require_https(asset_url, context="配布ファイルのリンク")
    if urllib.parse.urlsplit(asset_url).netloc != urllib.parse.urlsplit(page_url).netloc:
        # 公式ページが外部 CDN を指すケースはあり得るため中止はしないが、明示する
        print(f"  注意: 配布ファイルのホストが配布ページと異なります: "
              f"{urllib.parse.urlsplit(asset_url).netloc}")
    return asset_url


def _download(url: str, dest: Path, timeout: int = 300, allowed_hosts: frozenset[str] | None = None) -> int:
    """URL をダウンロードして dest に保存し、バイト数を返す。

    セキュリティ：
    - ダウンロード URL を HTTPS のみで検証
    - リダイレクトを自動追従しない
    - ファイルサイズを 500 MiB に制限
    （dataset.yaml は信頼済み入力として扱う）

    実装上の特徴：
    - チャンクをファイルへ直接ストリーミング書き込みするため、
      ピークメモリはチャンクサイズ（64 KiB）程度に抑えられる。
    - 一時ファイル（<dest>.<pid>.part）へ書き込み、成功時のみ dest へ
      アトミックに配置する。失敗時は一時ファイルを削除し、
      不完全なファイルが dest に残らないようにする。
    """
    # URL 検証
    try:
        _validate_fetch_url(url, allowed_hosts=allowed_hosts)
    except FetchError as e:
        raise FetchError(f"ダウンロード URL が不正です: {e}") from e

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_name(f"{dest.name}.{os.getpid()}.part")

    try:
        size = _fetch_url_with_limit(
            url, max_size=_MAX_DOWNLOAD_BYTES, timeout=timeout, dest_path=tmp_path,
        )
    except FetchError:
        tmp_path.unlink(missing_ok=True)
        raise
    except (OSError, socket.timeout) as e:
        tmp_path.unlink(missing_ok=True)
        raise FetchError(f"ファイルを取得できません: {url}\n  {e}") from e
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    tmp_path.replace(dest)
    return size


# ============================================================
# セキュリティ（dataset.yaml は信頼済み入力として扱う）
# ============================================================


def _normalize_hostname(value: str) -> str:
    """ホスト名 1 件を完全一致比較用の表記に正規化する。

    小文字化、トレーリングドット削除、IPv6 のブラケット削除と圧縮表記化を行う。
    ホスト名として不正な場合は ValueError（呼び出し側で FetchError に変換する）。
    """
    candidate = value.strip().lower().rstrip(".")
    bracketed = candidate.startswith("[") and candidate.endswith("]")
    if bracketed:
        candidate = candidate[1:-1]
    if not candidate or any(char in candidate for char in "/?#@"):
        raise ValueError("スキームやパスを含めないでください")
    # ブラケット表記は IPv6 リテラル (RFC 3986)。ブラケットなしの ":" はポート指定とみなす
    if bracketed or ":" in candidate:
        try:
            candidate = ipaddress.IPv6Address(candidate).compressed
        except ValueError as e:
            if bracketed:
                raise ValueError("IPv6 アドレスが不正です") from e
            raise ValueError("ポートは指定できません（IPv6 アドレスは [] で囲んでください）") from e
    return candidate


def _normalize_allowed_hosts(allowed_hosts: list[str] | None) -> frozenset[str] | None:
    """許可ホストのリストを完全一致比較用の frozenset に正規化する。"""
    if allowed_hosts is None:
        return None

    normalized: set[str] = set()
    for host in allowed_hosts:
        if not isinstance(host, str):
            raise FetchError(f"ホスト名は文字列で指定してください: {host!r}")
        try:
            normalized.add(_normalize_hostname(host))
        except ValueError as e:
            raise FetchError(f"許可ホストが不正です: {host!r}（{e}）") from e
    return frozenset(normalized)


def _validate_fetch_url(url: str, allow_https_only: bool = True, allowed_hosts: frozenset[str] | None = None) -> tuple[str, str, int]:
    """フェッチ対象 URL を検証する。

    Args:
        allowed_hosts: 正規化済みのホスト名セット（_normalize_allowed_hosts から）

    Returns:
        (url, hostname, port): 検査済みURL、ホスト名、接続ポート

    注: dataset.yaml は信頼済み入力として扱う。
    - HTTPS のみ許可（allow_https_only=True の場合）
    - allowed_hosts が指定されている場合、そのホストのみ許可（閉域網など接続先を制限したい環境向け）
    """
    parsed = urlparse(url)

    # スキーム検証
    if allow_https_only and parsed.scheme != "https":
        raise FetchError(f"配布元は https のみです: {url}")
    if parsed.scheme not in ("http", "https"):
        raise FetchError(f"不正なスキーム: {parsed.scheme}")

    # ホスト取得
    hostname = parsed.hostname
    if not hostname:
        raise FetchError(f"ホスト名が不正です: {url}")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    # allowed_hosts が指定されている場合、正規化済みホスト名との完全一致だけを許可
    if allowed_hosts is not None:
        try:
            normalized_hostname = _normalize_hostname(hostname)
        except ValueError as e:
            raise FetchError(f"ホスト名が不正です: {hostname!r}（{e}）") from e
        if normalized_hostname not in allowed_hosts:
            raise FetchError(f"ホストが許可リストにありません: {hostname}")

    return url, hostname, port


def _fetch_url_with_limit(
    url: str,
    max_size: int = 8 * 1024 * 1024,
    *,
    timeout: int = 30,
    dest_path: Path | None = None,
) -> bytes | int:
    """URL からコンテンツを取得し、サイズ上限を適用する。

    Args:
        url: 取得対象 URL
        max_size: コンテンツサイズ上限（デフォルト 8 MiB）
        timeout: 接続・読み込みタイムアウト秒数（デフォルト 30 秒）
        dest_path: 指定時はレスポンスボディをこのパスへストリーミング書き込みし、
            書き込みバイト数 (int) を返す。bytes をメモリに保持しない。
            未指定時は従来通りレスポンスボディ全体を bytes で返す。

    Returns:
        dest_path 指定時は書き込みバイト数 (int)、未指定時はレスポンスボディ (bytes)

    特性:
    - リダイレクトを自動追従しない（SSRF 対策）
    - サイズ制限・タイムアウト設定・ストリーミング読み込み
    """
    import urllib.request
    import urllib.error

    bytes_read = 0
    chunks: list[bytes] | None = None
    dest_file = None

    # リダイレクト無効化: urllib.error.HTTPError で 3xx を検出
    class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            # 3xx リダイレクトを拒否
            raise FetchError(f"リダイレクトは許可されていません（{code}）: {newurl}")

    try:
        opener = urllib.request.build_opener(NoRedirectHandler)
        with opener.open(url, timeout=timeout) as resp:
            if dest_path is not None:
                dest_file = open(dest_path, "wb")
            else:
                chunks = []
            try:
                while True:
                    chunk = resp.read(65536)  # 64 KiB ずつ読み込み
                    if not chunk:
                        break
                    bytes_read += len(chunk)
                    if bytes_read > max_size:
                        raise FetchError(
                            f"コンテンツサイズが大きすぎます（上限: {max_size / (1024*1024):.0f} MiB）: {url}"
                        )
                    if dest_file is not None:
                        dest_file.write(chunk)
                    else:
                        chunks.append(chunk)
            finally:
                if dest_file is not None:
                    dest_file.close()
    except FetchError:
        raise
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            raise FetchError(f"リダイレクトは許可されていません（{e.code}）: {url}") from e
        raise FetchError(f"HTTP エラー: {url} ({e.code})") from e
    except urllib.error.URLError as e:
        raise FetchError(f"URL 取得失敗: {url} ({e})") from e
    except socket.timeout as e:
        raise FetchError(f"タイムアウト: {url}") from e

    if dest_path is not None:
        return bytes_read
    return b"".join(chunks)


def fetch(
    dataset_id: str,
    base_dir: Path | None = None,
    keep_source: bool = True,
    allowed_hosts: list[str] | None = None,
) -> None:
    """配布元から最新データを取得し、dataset.yaml に基づいて Parquet を生成する。

    dataset.yaml の source.url（配布ページ）と source.asset_pattern
    （ファイル名パターン）を使う。取得日は as_of_date に記録し、
    provenance に「いつ時点のデータか」が出るようにする。

    Args:
        dataset_id: データセット ID
        base_dir: ベースディレクトリ
        keep_source: CSV ファイルを保持するか
        allowed_hosts: 接続許可ホスト名リスト。指定時はこのホストのみアクセス許可。
    """
    from admin_procedures.loader import load_dataset_yaml
    from admin_procedures.validation import validate_dataset_id

    # dataset_id 検証
    try:
        dataset_id = validate_dataset_id(dataset_id)
    except ValueError as e:
        raise ValueError(f"Invalid dataset_id: {e}") from e

    base = resolve_base_dir(base_dir)
    dataset_dir = base / DATASETS_DIR_NAME / dataset_id
    if not (dataset_dir / "dataset.yaml").exists():
        raise ValueError(
            f"dataset.yaml が見つかりません: {dataset_dir}\n"
            f"  新規データセットは `apcli add {dataset_id} --csv <file>` で作成してください。",
        )

    config = load_dataset_yaml(dataset_dir)
    source = config.get("source", {})
    page_url = source.get("url", "")
    pattern = source.get("asset_pattern", "")

    # 正規化を一度だけ実行し、以降の検証で再実行しない
    normalized_allowed_hosts = _normalize_allowed_hosts(allowed_hosts)

    if not page_url or not pattern:
        raise FetchError(
            f"dataset.yaml に source.url と source.asset_pattern がありません。\n"
            f"  自動取得を使わない場合は、配布ファイルを手元に用意して\n"
            f"  `apcli add {dataset_id} --csv <file>` を実行してください。",
        )

    print(f"[fetch] Dataset: {dataset_id}")
    print(f"  Page:    {page_url}")

    asset_url = _find_asset_url(page_url, pattern, allowed_hosts=normalized_allowed_hosts)
    filename = _asset_filename(asset_url)
    dest = base / SOURCE_DIR_NAME / filename

    print(f"  Asset:   {filename}")
    size = _download(asset_url, dest, allowed_hosts=normalized_allowed_hosts)
    print(f"  Saved:   {dest} ({size / 1024 / 1024:.1f} MB)")

    # 取得日を記録 — provenance の fetched_at として出る
    _record_fetch_metadata(dataset_dir, filename, asset_url)

    convert(dataset_id, csv_override=dest, base_dir=base)

    if not keep_source:
        dest.unlink(missing_ok=True)


def _record_fetch_metadata(dataset_dir: Path, filename: str, asset_url: str) -> None:
    """取得元・取得日をサイドカー (.fetch.json) に記録する。

    dataset.yaml には書かない。YAML は配布される定義ファイルで、
    取得状態はローカル固有の情報だからである。YAML に書き戻すと
    fetch のたびに全利用者の作業ツリーが汚れる。

    取得日は ``fetched_at`` として扱う。``as_of_date``（データの基準時点）
    とは別物で、混同すると「いつ時点のデータか」を偽ることになる。
    """
    import datetime

    payload = {
        "fetched_at": datetime.date.today().isoformat(),
        "filename": filename,
        "fetched_from": asset_url,
    }
    path = dataset_dir / FETCH_STATE_FILENAME
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(f"  State:   {path}")


# ============================================================
# Convert モード: 既存 YAML → Parquet
# ============================================================


def convert(
    dataset_id: str,
    csv_override: Path | None = None,
    base_dir: Path | None = None,
) -> None:
    """dataset.yaml に基づいて CSV → Parquet 変換を実行する。"""
    from admin_procedures.loader import (
        build_field_map,
        load_dataset_yaml,
    )

    base = resolve_base_dir(base_dir)
    dataset_dir = base / DATASETS_DIR_NAME / dataset_id
    config = load_dataset_yaml(dataset_dir)

    source = config.get("source", {})
    header_rows = source.get("csv_header_rows", 1)

    # CSV ファイル解決
    if csv_override:
        csv_file = csv_override
    else:
        csv_filename = source.get("csv_filename", "")
        source_dir = base / SOURCE_DIR_NAME
        try:
            csv_file = resolve_under(source_dir, csv_filename, label="csv_filename")
        except ValueError as e:
            raise ValueError(f"CSV filename validation failed: {e}") from e

    if not csv_file.exists():
        raise ValueError(f"CSV file not found: {csv_file}")

    # フィールド情報を取得
    field_map = build_field_map(config)
    active_fields = [f for f in field_map if f.csv_col_index >= 0]

    # 数値カラムと型を判定 (YAML の data_type を優先し、未指定の measure は integer)
    numeric_columns: dict[str, str] = {}
    active_names = {f.ja for f in active_fields}
    for fd in config.get("fields", []):
        if fd["name"] not in active_names:
            continue
        data_type = fd.get("data_type")
        if data_type in ("integer", "float"):
            numeric_columns[fd["name"]] = data_type
        elif data_type is None and fd.get("role") == "measure":
            numeric_columns[fd["name"]] = "integer"

    # 出力先（パストラバーサル対策）
    data_file = config.get("data_file", "data.parquet")
    try:
        output_path = resolve_under(dataset_dir, data_file, label="data_file")
    except ValueError as e:
        raise ValueError(f"Output data_file validation failed: {e}") from e

    column_specs = [(f.ja, f.csv_col_index) for f in active_fields]

    print(f"[convert] Dataset: {dataset_id}")
    print(f"  CSV:     {csv_file}")
    print(f"  Headers: {header_rows} row(s)")
    print(f"  Fields:  {len(active_fields)} (of {len(field_map)} defined)")

    n_rows = _csv_to_parquet(
        csv_file, output_path, header_rows, column_specs, numeric_columns,
    )

    csv_size = csv_file.stat().st_size / 1024
    parquet_size = output_path.stat().st_size / 1024
    print(f"  Records: {n_rows}")
    print(f"  Output:  {output_path}")
    print(f"  CSV:     {csv_size:.0f} KB → Parquet: {parquet_size:.0f} KB")
    if csv_size > 0:
        print(f"  Ratio:   {(1 - parquet_size / csv_size) * 100:.1f}% smaller")


# ============================================================
# Scaffold モード: CSV 分析 → YAML 生成 → Parquet 変換
# ============================================================

DIM_THRESHOLD = 30
SEMICOLON_RATIO = 0.10
NUMERIC_RATIO = 0.9

# 桁区切り付きの数値 (1,234,567)。分類だけでなく変換時にも除去する必要がある。
_THOUSANDS_RE = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")
_INTEGER_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?(\d+\.\d*|\.\d+|\d+[eE][+-]?\d+|\d*\.\d+[eE][+-]?\d+)$")
# 先頭ゼロ (011002)。数値化すると桁が落ちるため、コード列の決定的な証拠になる。
_LEADING_ZERO_RE = re.compile(r"^0\d+$")
# コード・識別子を示す列名のヒント
_CODE_NAME_RE = re.compile(
    r"コード|番号|符号|ｺｰﾄﾞ|code|(^|[^a-z])id($|[^a-z])|no\.?$|№",
    re.IGNORECASE,
)


def _numeric_kind(value: str) -> str | None:
    """1つの値の数値種別を返す。'integer' / 'float' / None。"""
    s = value.strip()
    if not s:
        return None
    if _THOUSANDS_RE.fullmatch(s):
        return "float" if "." in s else "integer"
    s = s.replace(",", "")
    if _INTEGER_RE.fullmatch(s):
        return "integer"
    if _FLOAT_RE.fullmatch(s):
        return "float"
    return None


def _looks_like_code(col_name: str, values: list[str]) -> bool:
    """量ではなく符号として扱うべき列か判定する。

    自治体コード・郵便番号・電話番号などを measure にすると、
    先頭ゼロが落ちたうえに集計対象になってしまう。
    """
    if any(_LEADING_ZERO_RE.fullmatch(v.strip()) for v in values):
        return True
    return bool(_CODE_NAME_RE.search(col_name))


def _analyze_csv(
    csv_path: Path,
    header_rows: int = 1,
    encoding: str = "utf-8-sig",
) -> list[dict[str, Any]]:
    """CSV / Excel を分析し、フィールドごとの統計情報を返す。"""
    headers, rows = _read_table(csv_path, header_rows, encoding)

    n_rows = len(rows)
    if n_rows == 0:
        return []

    n_cols = len(headers)
    analysis: list[dict[str, Any]] = []

    for col_idx in range(n_cols):
        col_name = headers[col_idx].strip() if col_idx < len(headers) else f"col_{col_idx}"
        values = [
            row[col_idx].strip() if col_idx < len(row) else ""
            for row in rows
        ]
        non_empty = [v for v in values if v]
        unique_values = set(non_empty)
        n_unique = len(unique_values)

        kinds = [_numeric_kind(v) for v in non_empty]
        numeric_count = sum(1 for k in kinds if k is not None)
        is_numeric = (
            len(non_empty) > 0
            and numeric_count / len(non_empty) > NUMERIC_RATIO
            and not _looks_like_code(col_name, non_empty)
        )
        # 1つでも小数があれば float。integer に丸めると値が null になる
        numeric_type = "float" if "float" in kinds else "integer"

        semicolon_count = sum(1 for v in non_empty if ";" in v)
        semicolon_ratio = semicolon_count / len(non_empty) if non_empty else 0

        all_unique = n_unique == n_rows and n_rows > 10

        flag_chars = {"○", "●", "×", "◯"}
        is_flag = len(unique_values) > 0 and unique_values.issubset(flag_chars)

        analysis.append({
            "col_idx": col_idx,
            "col_name": col_name,
            "n_rows": n_rows,
            "n_non_empty": len(non_empty),
            "n_unique": n_unique,
            "is_numeric": is_numeric,
            "numeric_type": numeric_type,
            "is_code": _looks_like_code(col_name, non_empty),
            "semicolon_ratio": semicolon_ratio,
            "all_unique": all_unique,
            "is_flag": is_flag,
            "sample_values": sorted(unique_values)[:20],
        })

    return analysis


def _generate_en_name(ja_name: str, col_idx: int) -> str:
    known = {
        "手続ID": "procedure_id",
        "ID": "id",
        "名称": "name",
        "名前": "name",
    }
    for key, val in known.items():
        if key in ja_name:
            return val
    return f"field_{col_idx}"


def _classify_field(info: dict[str, Any]) -> dict[str, Any]:
    col_name = info["col_name"]
    col_idx = info["col_idx"]
    n_unique = info["n_unique"]
    is_numeric = info["is_numeric"]
    semicolon_ratio = info["semicolon_ratio"]
    all_unique = info["all_unique"]
    is_flag = info["is_flag"]

    en = _generate_en_name(col_name, col_idx)

    field: dict[str, Any] = {"name": col_name}

    if all_unique and ("ID" in col_name or "id" in col_name.lower()):
        field["role"] = "id"
        field["_reason"] = "全行ユニーク & ID を含む"
    elif is_numeric:
        field["role"] = "measure"
        field["data_type"] = info.get("numeric_type", "integer")
        field["_reason"] = f"数値列 ({info['n_non_empty']} 件, {field['data_type']})"
    elif info.get("is_code"):
        # 数値に見えてもコード・符号は集計対象にしない。文字列のまま保持する
        field["role"] = "dim" if n_unique <= DIM_THRESHOLD else "attr"
        field["_reason"] = f"コード・識別子として扱う ({n_unique} 種)"
    elif is_flag:
        field["role"] = "dim"
        field["_reason"] = "○/●/× フラグ"
    elif semicolon_ratio > SEMICOLON_RATIO:
        field["multi_value"] = True
        field["role"] = "attr"
        field["codelist"] = "auto_split"
        field["_reason"] = f"セミコロン区切り ({semicolon_ratio:.0%}, {n_unique} 種)"
    elif n_unique <= DIM_THRESHOLD:
        field["role"] = "dim"
        field["codelist"] = "inline"
        field["_reason"] = f"低カーディナリティ ({n_unique} 種)"
    else:
        field["role"] = "attr"
        field["_reason"] = f"カーディナリティ ({n_unique} 種)"

    field["_sample"] = info["sample_values"][:5]
    return field


def _generate_classification_report(analysis: list[dict[str, Any]]) -> str:
    lines = ["# 自動分類レポート", ""]
    for info in analysis:
        fc = _classify_field(info)
        lines.append(f"  {info['col_name']}: {fc['role']} — {fc.get('_reason', '')}")
    lines.append("")
    return "\n".join(lines)


def _build_yaml_from_analysis(
    analysis: list[dict[str, Any]],
    csv_path: Path,
    header_rows: int,
) -> dict[str, Any]:
    fields_classified = [_classify_field(info) for info in analysis]

    id_fields = [f for f in fields_classified if f["role"] == "id"]
    id_field = id_fields[0]["name"] if id_fields else fields_classified[0]["name"]

    fields: list[dict[str, Any]] = []
    for fc, info in zip(fields_classified, analysis):
        field: dict[str, Any] = {
            "role": fc["role"],
            "name": fc["name"],
            "desc": "",
        }
        if "data_type" in fc:
            field["data_type"] = fc["data_type"]
        cl = fc.get("codelist")
        if cl == "inline" and info["sample_values"]:
            # インラインコードリスト: サンプル値をリストで直接埋め込む
            field["codelist"] = [str(val) for val in sorted(info["sample_values"])]
        elif cl in ("auto", "auto_split"):
            field["codelist"] = cl
        if fc.get("multi_value"):
            field["multi_value"] = True
        field["csv_col_index"] = info["col_idx"]
        fields.append(field)

    from admin_procedures.loader import CURRENT_SCHEMA_VERSION

    config: dict[str, Any] = {
        # loader が未指定時に警告を出すため、生成時点で必ず入れておく
        "schema_version": CURRENT_SCHEMA_VERSION,
        "title": "",
        "publisher": "",
        "tags": [],
        "update_frequency": "",
        "contact": "",
        "id_field": id_field,
        "source": {
            "url": "",
            "legal_basis": "",
            "note": "",
            "csv_filename": csv_path.name,
            "csv_header_rows": header_rows,
        },
        "data_file": "data.parquet",
        "as_of_date": "",
        "published_at": "",
        "fields": fields,
    }

    config["generic_values"] = ["その他"]

    return config


class _YamlDumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.Dumper, data: str) -> Any:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_YamlDumper.add_representer(str, _str_representer)


def _write_yaml(config: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(
            config, f,
            Dumper=_YamlDumper,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            width=120,
        )
    print(f"  YAML: {path}")


def scaffold(
    csv_path: Path,
    dataset_id: str,
    header_rows: int = 1,
    encoding: str = "utf-8-sig",
    base_dir: Path | None = None,
) -> None:
    """CSV を分析し、dataset.yaml + Parquet を自動生成する。"""
    from admin_procedures.validation import validate_dataset_id

    # dataset_id 検証
    try:
        dataset_id = validate_dataset_id(dataset_id)
    except ValueError as e:
        raise ValueError(f"Invalid dataset_id: {e}") from e

    output_dir = resolve_base_dir(base_dir) / DATASETS_DIR_NAME / dataset_id

    if not csv_path.exists():
        raise ValueError(f"CSV file not found: {csv_path}")

    print(f"[scaffold] Dataset: {dataset_id}")
    print(f"  CSV:         {csv_path}")
    print(f"  Header rows: {header_rows}")

    analysis = _analyze_csv(csv_path, header_rows=header_rows, encoding=encoding)
    if not analysis:
        raise ValueError("No data found in CSV.")

    print(f"  Columns:     {len(analysis)}")
    print(f"  Rows:        {analysis[0]['n_rows']}")
    print()

    report = _generate_classification_report(analysis)
    print(report)

    # YAML 生成
    config = _build_yaml_from_analysis(analysis, csv_path, header_rows)
    _write_yaml(config, output_dir / "dataset.yaml")

    # Parquet 変換（共通関数を使用）
    column_specs = [(info["col_name"], info["col_idx"]) for info in analysis]
    numeric_cols = {
        info["col_name"]: _classify_field(info).get("data_type", "integer")
        for info in analysis
        if _classify_field(info)["role"] == "measure"
    }
    parquet_path = output_dir / "data.parquet"

    n_rows = _csv_to_parquet(
        csv_path, parquet_path, header_rows, column_specs, numeric_cols, encoding,
    )

    parquet_size = parquet_path.stat().st_size / 1024
    print(f"  Parquet: {parquet_path} ({parquet_size:.0f} KB, {n_rows} records)")

    print(f"\nDone! Dataset is ready to query: apcli inspect {dataset_id}")
    print(f"  YAML: {output_dir / 'dataset.yaml'}")
    print("  Optional: fill in desc / notes / computed_measures to improve")
    print("  AI answer quality (see docs/dataset-yaml-guide.md).")


# ============================================================
# メイン
# ============================================================


def curated_summary(config: dict[str, Any]) -> dict[str, int]:
    """YAML に人手/AI で補完された内容の量を数える。

    scaffold はこれらを再生成できない（項目説明資料が必要）ため、
    上書き前の警告材料に使う。
    """
    fields = config.get("fields", [])
    return {
        "desc": sum(1 for f in fields if f.get("desc")),
        "notes": sum(1 for f in fields if f.get("notes")),
        "codelist_desc": sum(
            1 for f in fields
            if isinstance(f.get("codelist"), list)
            and any(isinstance(i, dict) for i in f["codelist"])
        ),
        "computed_measures": len(config.get("computed_measures", [])),
    }


def _backup_yaml(yaml_path: Path) -> Path:
    """既存 dataset.yaml を .bak に退避する。"""
    backup = yaml_path.with_suffix(".yaml.bak")
    backup.write_text(yaml_path.read_text(encoding="utf-8"), encoding="utf-8")
    return backup


def _confirm_overwrite(yaml_path: Path, *, assume_yes: bool) -> None:
    """補完済み YAML を scaffold で上書きしてよいか確認する。

    非対話環境では確認が取れないため、破壊せず ValueError を送出する。
    スクリプトやエージェントから実行されたときに、気付かないうちに
    desc/notes/computed_measures を失うことを防ぐ。
    """
    summary = curated_summary(yaml.safe_load(yaml_path.read_text(encoding="utf-8")))
    if not any(summary.values()):
        return  # 骨組みのみ。失われるものが無いので確認不要

    detail = ", ".join(f"{k}={v}" for k, v in summary.items() if v)
    message = (
        f"{yaml_path} には補完済みの内容があります ({detail})。\n"
        f"  scaffold で上書きすると、これらは失われます"
        f"（項目説明資料が無いと再生成できません）。"
    )

    if assume_yes:
        print(f"警告: {message}")
        return

    if not sys.stdin.isatty():
        raise ValueError(
            f"{message}\n"
            f"  非対話環境のため中断しました。意図的に上書きする場合は --yes を付けてください。",
        )

    print(f"警告: {message}")
    answer = input("  上書きして続行しますか? [y/N]: ").strip().lower()
    if answer not in ("y", "yes"):
        raise ValueError("中断しました。")


def run(
    dataset_id: str,
    *,
    csv: Path | None = None,
    header_rows: int = 1,
    encoding: str = "utf-8-sig",
    base_dir: Path | None = None,
    force_scaffold: bool = False,
    assume_yes: bool = False,
) -> None:
    """dataset.yaml の有無で scaffold / convert を自動選択して実行する。

    ``apcli add`` と ``python -m admin_procedures.prepare_dataset`` の共通実体。
    入力不備は ValueError で送出し、終了コードは呼び出し側に委ねる。
    """
    from admin_procedures.validation import validate_dataset_id

    # dataset_id 検証
    try:
        dataset_id = validate_dataset_id(dataset_id)
    except ValueError as e:
        raise ValueError(f"Invalid dataset_id: {e}") from e
    base = resolve_base_dir(base_dir)
    yaml_path = base / DATASETS_DIR_NAME / dataset_id / "dataset.yaml"

    if yaml_path.exists() and not force_scaffold:
        convert(dataset_id, csv_override=csv, base_dir=base)
        return

    if not csv:
        raise ValueError(
            "dataset.yaml が存在しないため --csv が必要です (scaffold モード)",
        )

    if yaml_path.exists():
        _confirm_overwrite(yaml_path, assume_yes=assume_yes)
        backup = _backup_yaml(yaml_path)
        print(f"  Backup:  {backup}")

    scaffold(
        csv, dataset_id,
        header_rows=header_rows, encoding=encoding, base_dir=base,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CSV からデータセットを準備する（scaffold + convert 統合）",
    )
    parser.add_argument("dataset_id", help="データセットID (datasets/ 配下のディレクトリ名)")
    parser.add_argument("--csv", type=Path, default=None, help="CSV ファイルパス")
    parser.add_argument("--header-rows", type=int, default=1, help="CSV ヘッダー行数 (scaffold 時、default: 1)")
    parser.add_argument("--encoding", default="utf-8-sig", help="CSV エンコーディング (scaffold 時)")
    parser.add_argument("--data-dir", type=Path, default=None,
                        help="datasets/ を置くベースディレクトリ (既定: ADMIN_PROCEDURES_DATA_DIR → 自動検出)")
    parser.add_argument("--force-scaffold", action="store_true", help="YAML が存在しても scaffold を強制実行")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="補完済み YAML を上書きする際の確認を省略する")
    args = parser.parse_args()

    try:
        run(
            args.dataset_id,
            csv=args.csv,
            header_rows=args.header_rows,
            encoding=args.encoding,
            base_dir=args.data_dir,
            force_scaffold=args.force_scaffold,
            assume_yes=args.yes,
        )
    except ValueError as e:
        parser.error(str(e))


if __name__ == "__main__":
    main()
