#!/usr/bin/env bash
# clone 直後の初期セットアップ（ユーザー同意型）
#   1. 実行内容を説明してから、ユーザーの同意を取る
#   2. 依存インストール（uv があれば uv、無ければ venv + pip）
#   3. 調査結果データを配布元から取得
#   4. 使える接続方法を検出して案内
set -euo pipefail

cd "$(dirname "$0")"

DATASET_ID="procedures-survey-r6"
bold() { printf '\033[1m%s\033[0m\n' "$1"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ask() {
  local prompt="$1"
  local default="${2:-n}"
  local response
  printf '%s (y/n) [%s]: ' "$prompt" "$default"
  read -r response || response="$default"
  case "$response" in
    [yY]) return 0 ;;
    [nN]) return 1 ;;
    "") [ "$default" = "y" ]; return ;;
    *) echo "y または n で答えてください"; ask "$prompt" "$default" ;;
  esac
}

# ==================== オプション解析 ====================
AUTO_MODE=false
SKIP_DATA=false
APCLI=()  # Step 1 (依存インストール) を実行した場合のみ中身が入る

while [ $# -gt 0 ]; do
  case "$1" in
    --yes)        AUTO_MODE=true; shift ;;
    --skip-data)  SKIP_DATA=true; shift ;;
    --help)
      cat <<'HELP'
使用法: ./setup.sh [オプション]

オプション:
  --yes          対話プロンプトをスキップ（すべて y で応答）
  --skip-data    データ取得をスキップ
  --help         このヘルプを表示

例:
  ./setup.sh              対話的にセットアップ
  ./setup.sh --yes        自動モードでセットアップ
  ./setup.sh --skip-data  データ取得をスキップしてセットアップ
HELP
      exit 0
      ;;
    *)
      echo "unknown option: $1"
      echo "run './setup.sh --help' for usage"
      exit 1
      ;;
  esac
done

# ==================== セットアップ内容の案内と確認 ====================
cat <<'EOF'
このセットアップスクリプトは以下を実行します：

【Step 1】依存パッケージのインストール
  - Python 仮想環境を初期化
  - Polars・FastMCP などの依存パッケージをインストール

【Step 2】調査結果データの取得（オプション）
  - デジタル庁の配布ページから最新データを自動取得
  - 〜100MB のデータが datasets/ に保存されます
  - 後で apcli fetch で手動取り込みもできます

【Step 3】接続方法の案内
  - CLI（apcli）コマンドのテスト
  - Claude Desktop / Claude Code / ChatGPT との連携方法

このセットアップによりマシン設定は変更されません。
（既存環境に .venv/ ディレクトリが追加されるだけです）
EOF

echo
if [ "$AUTO_MODE" = false ]; then
  if ! ask "セットアップを進めますか？" "y"; then
    echo "セットアップをキャンセルしました"
    exit 0
  fi
fi

# ==================== Step 1: 依存インストール ====================
step "1/3 依存パッケージをインストール"
echo "実行内容:"
echo "  - 仮想環境の初期化"
echo "  - Polars / FastMCP / Click などをインストール"
echo

SKIP_STEP1=true
if [ "$AUTO_MODE" = false ]; then
  if ask "進めますか？" "y"; then
    SKIP_STEP1=false
  fi
else
  SKIP_STEP1=false
fi

if [ "$SKIP_STEP1" = false ]; then
  if command -v uv >/dev/null 2>&1; then
    echo "✓ uv が見つかりました"
    uv sync --extra excel
    APCLI=(uv run apcli)
  else
    echo "✓ uv が見つかりません。venv + pip を使用します"
    if [ ! -d .venv ]; then
      echo "  → .venv/ を作成します"
      python3 -m venv .venv
    fi
    # venv/bin（Unix）と venv\Scripts（Windows）の両方に対応
    if [ -f ".venv/bin/python" ]; then
      VENV_PYTHON="./.venv/bin/python"
    elif [ -f ".venv/Scripts/python.exe" ]; then
      VENV_PYTHON="./.venv/Scripts/python.exe"
    else
      VENV_PYTHON="python"
    fi
    echo "  → pip をアップグレード"
    $VENV_PYTHON -m pip install --upgrade pip
    echo "  → administrative-procedures-mcp をインストール"
    $VENV_PYTHON -m pip install -e ".[excel]"
    APCLI=($VENV_PYTHON -m admin_procedures.cli)
  fi
  echo "✓ 依存インストール完了"
fi

# ==================== Step 2: データ取得 ====================
if [ "$SKIP_DATA" = false ]; then
  step "2/3 調査結果データを取得"
  echo "実行内容:"
  echo "  - デジタル庁の配布ページから最新データを自動取得"
  echo "  - Parquet フォーマットに変換して保存"
  echo "  - 〜100MB のデータが datasets/ に保存されます"
  echo

  SKIP_FETCH=true
  if [ "$AUTO_MODE" = false ]; then
    if ask "進めますか？" "y"; then
      SKIP_FETCH=false
    fi
  else
    SKIP_FETCH=false
  fi

  if [ "$SKIP_FETCH" = false ]; then
    if [ "${#APCLI[@]}" -eq 0 ]; then
      cat <<'EOF'

⚠ Step 1（依存パッケージのインストール）をスキップしたため、データ取得もスキップします。
  apcli コマンドは依存パッケージが入っていないと実行できません。

【方法1】依存インストール後に取得
  uv sync --extra excel
  uv run apcli fetch procedures-survey-r6

【方法2】配布ページから手動ダウンロード
  1. デジタル庁の配布ページ（https://www.digital.go.jp/resources/procedures-survey-results）にアクセス
  2. CSV/Excel ファイルを保存
  3. 依存インストール後に以下のコマンドで取り込み：
     uv run apcli add procedures-survey-r6 --csv <保存したファイル>

セットアップの残りは続行できます。
EOF
    elif "${APCLI[@]}" fetch "$DATASET_ID"; then
      echo "✓ データ取得完了"
    else
      cat <<'EOF'

⚠ 自動取得に失敗しました。以下のいずれかを試してください：

【方法1】配布ページから手動ダウンロード
  1. デジタル庁の配布ページ（https://www.digital.go.jp/resources/procedures-survey-results）にアクセス
  2. CSV ファイルを保存
  3. 以下のコマンドで取り込み：
     uv run apcli add procedures-survey-r6 --csv <保存したファイル>

【方法2】スキップして後で実行
  uv run apcli fetch procedures-survey-r6

セットアップの残りは続行できます。
EOF
    fi
  fi
else
  step "2/3 調査結果データを取得"
  echo "✓ スキップ（--skip-data オプション）"
  echo "後で以下のコマンドで取得できます："
  echo "  uv run apcli fetch procedures-survey-r6"
fi

# ==================== Step 3: 接続方法の案内 ====================
step "3/3 接続方法"
echo

bold "LLM を使わずコマンドラインで試す（すぐ動きます）"
cat <<'EOF'
  uv run apcli list
  uv run apcli inspect procedures-survey-r6
  uv run apcli summarize procedures-survey-r6 --group-by '["所管府省庁"]' --metrics '["count"]'
  uv run apcli summarize procedures-survey-r6 --group-by '["所管府省庁"]' --metrics '["count"]' --html > report.html
EOF

echo
if command -v claude >/dev/null 2>&1; then
  bold "Claude Code が見つかりました（追加設定は不要です）"
  echo "  このディレクトリで claude を起動すると .mcp.json が読み込まれます。"
else
  bold "Claude Code"
  echo "  インストールすると、このディレクトリの .mcp.json だけで接続できます。"
fi

echo
# Claude Desktop のインストール場所を OS 別に検出
CLAUDE_FOUND=false
if [ -d "$HOME/Library/Application Support/Claude" ]; then
  CLAUDE_FOUND=true
elif [ -d "$HOME/.config/Claude" ]; then
  CLAUDE_FOUND=true
elif [ -n "${APPDATA:-}" ] && [ -d "${APPDATA}/Claude" ]; then
  CLAUDE_FOUND=true
fi

if [ "$CLAUDE_FOUND" = true ]; then
  bold "Claude Desktop が見つかりました"
else
  bold "Claude Desktop"
fi
echo "  設定ファイルを手で編集せずに登録できます:"
echo "    uv run apcli install desktop"
echo "  他のクライアント: uv run apcli install cursor / gemini-cli / goose"
echo "  設定内容だけ確認: uv run apcli install json"

echo
bold "セットアップ完了 ✓"
