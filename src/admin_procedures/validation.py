"""admin_procedures.validation — 入力検証と信頼境界。

パス containment、メタデータサニタイズ、URL 検証を一箇所に集約。

Functions:
    validate_dataset_id         -- dataset_id の検証
    resolve_under               -- パス containment の検査
    validate_metadata_string    -- メタデータ文字列のサニタイズ
"""

import re
from pathlib import Path

_DATASET_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,63})\Z")


def validate_dataset_id(value: str) -> str:
    """dataset_id を検証する。

    - 小文字英数字、ドット、アンダースコア、ハイフンのみ
    - 最初は英数字
    - 最大 64 文字
    - ".." や "/" や "\\" を禁止

    Args:
        value: 検証する dataset_id

    Returns:
        検証済み dataset_id

    Raises:
        ValueError: 不正な形式の場合
    """
    if not isinstance(value, str):
        raise ValueError(f"dataset_id は文字列である必要があります")
    if not _DATASET_ID_RE.fullmatch(value):
        raise ValueError(f"不正な dataset_id: {value!r}")
    if ".." in value:
        raise ValueError(f"dataset_id に '..' は含められません")
    if "/" in value or "\\" in value:
        raise ValueError(f"dataset_id にパスセパレータは含められません")
    return value


def resolve_under(root: Path, raw_path: str, *, label: str) -> Path:
    """パス containment を検査して解決する。

    raw_path が root の外に出ようとしていないか確認。
    シンボリックリンクによる脱出も防ぐため resolve() を使用。

    Args:
        root: 許可する基準ディレクトリ
        raw_path: 解決するパス（相対パス想定）
        label: エラーメッセージに使用するラベル

    Returns:
        root 以下に解決された Path

    Raises:
        ValueError: 絶対パスまたは root 外へのパスの場合
    """
    path = Path(raw_path)
    if path.is_absolute():
        raise ValueError(f"{label} に絶対パスは指定できません")

    root = root.resolve()
    resolved = (root / path).resolve()

    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"{label} が許可ディレクトリ外を指しています: {resolved}")

    return resolved


def validate_metadata_string(
    value: str | None,
    *,
    label: str,
    max_len: int = 256,
) -> str:
    """メタデータ文字列を検証してサニタイズする。

    - null を空文字列に正規化
    - 長さを max_len 以内に制限
    - 改行・制御文字を拒否（prompt injection 防止）

    Args:
        value: 検証する文字列（None 可）
        label: エラーメッセージに使用するラベル
        max_len: 最大長（デフォルト: 256）

    Returns:
        検証済みの文字列（前後の空白を trim）

    Raises:
        ValueError: 型、長さ、文字が不正な場合
    """
    if value is None:
        return ""

    if not isinstance(value, str):
        raise ValueError(f"{label} は文字列である必要があります")

    if len(value) > max_len:
        raise ValueError(f"{label} は {max_len} 文字以内です（入力: {len(value)} 文字）")

    # 改行・制御文字を拒否（ESC も含む）
    forbidden = {'\n', '\r', '\t', '\x00', '\x1b'}
    if any(c in value for c in forbidden):
        raise ValueError(f"{label} に無効な文字が含まれています")

    return value.strip()
