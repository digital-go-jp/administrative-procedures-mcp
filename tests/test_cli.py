"""CLI (apcli) 回帰テスト。

バリデーションが server.py と同等に動作し、
未知フィールドや非 groupable フィールドで構造化エラーを返すことを検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from admin_procedures.cli import main, _get_registry
from admin_procedures.loader import (
    CURRENT_SCHEMA_VERSION,
    DATA_DIR_ENV_VAR,
    FETCH_STATE_FILENAME,
)

DATASET_ID = "procedures-survey-r6"


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    """各テスト前にレジストリキャッシュをクリアする。"""
    _get_registry.cache_clear()
    yield
    _get_registry.cache_clear()


@pytest.fixture()
def runner():
    return CliRunner()


# ============================================================
# query: 未知 select フィールド
# ============================================================


class TestQueryUnknownSelect:
    def test_unknown_select_returns_structured_error(self, runner):
        result = runner.invoke(main, [
            "query", DATASET_ID, "--select", '["存在しないフィールド"]',
        ])
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert "error" in payload
        assert "存在しないフィールド" in payload["error"]

    def test_unknown_select_includes_available_fields(self, runner):
        result = runner.invoke(main, [
            "query", DATASET_ID, "--select", '["no_such_field"]',
        ])
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert "available_fields_sample" in payload


# ============================================================
# summarize: 未知 explode フィールド
# ============================================================


class TestSummarizeUnknownExplode:
    def test_unknown_explode_returns_structured_error(self, runner):
        result = runner.invoke(main, [
            "summarize", DATASET_ID, "--explode", "存在しないフィールド",
        ])
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert "error" in payload
        assert "存在しないフィールド" in payload["error"]

    def test_unknown_explode_includes_available_fields(self, runner):
        result = runner.invoke(main, [
            "summarize", DATASET_ID, "--explode", "no_such_field",
        ])
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert "available_fields_sample" in payload


# ============================================================
# summarize: 非 groupable フィールド
# ============================================================


class TestSummarizeNonGroupable:
    def test_non_groupable_field_returns_structured_error(self, runner):
        """measure フィールド (総手続件数) は groupable=false なので拒否される。"""
        result = runner.invoke(main, [
            "summarize", DATASET_ID,
            "--group-by", '["総手続件数"]',
        ])
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert "error" in payload
        assert "groupable" in payload["error"]

    def test_non_groupable_error_includes_groupable_fields(self, runner):
        result = runner.invoke(main, [
            "summarize", DATASET_ID,
            "--group-by", '["総手続件数"]',
        ])
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert "groupable_fields" in payload
        assert isinstance(payload["groupable_fields"], list)
        assert len(payload["groupable_fields"]) > 0


# ============================================================
# summarize: 未知 group_by フィールド
# ============================================================


class TestSummarizeUnknownGroupBy:
    def test_unknown_group_by_returns_structured_error(self, runner):
        result = runner.invoke(main, [
            "summarize", DATASET_ID,
            "--group-by", '["存在しないフィールド"]',
        ])
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert "error" in payload


# ============================================================
# 正常系: 基本的なコマンドが動作する
# ============================================================


class TestCLIBasicCommands:
    def test_list(self, runner):
        result = runner.invoke(main, ["list"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert "datasets" in payload

    def test_inspect(self, runner):
        result = runner.invoke(main, ["inspect", DATASET_ID])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert "columns" in payload

    def test_query(self, runner):
        result = runner.invoke(main, ["query", DATASET_ID, "--limit", "2"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert "total" in payload

    def test_summarize(self, runner):
        result = runner.invoke(main, [
            "summarize", DATASET_ID,
            "--group-by", '["所管府省庁"]',
        ])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert "total_group_count" in payload

    def test_unknown_dataset_returns_error(self, runner):
        result = runner.invoke(main, ["inspect", "no-such-dataset"])
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert "error" in payload
        assert "available_datasets" in payload


# ============================================================
# add: CSV 取り込み（scaffold）
# ============================================================


_SAMPLE_CSV = (
    "自治体名,分類,件数,関連士業\n"
    "札幌市,福祉,120,行政書士;社会保険労務士\n"
    "仙台市,税務,80,税理士\n"
    "横浜市,福祉,300,行政書士\n"
)


@pytest.fixture()
def csv_workspace(tmp_path):
    """リポジトリ外の作業ディレクトリと CSV を用意する。"""
    csv_path = tmp_path / "sample.csv"
    csv_path.write_text(_SAMPLE_CSV, encoding="utf-8")
    return tmp_path, csv_path


class TestAddCommand:
    def test_writes_outside_repository(self, runner, csv_workspace):
        """--data-dir で指定した場所に出力し、リポジトリ内を汚さないこと。"""
        workspace, csv_path = csv_workspace

        result = runner.invoke(main, [
            "add", "trial", "--csv", str(csv_path), "--data-dir", str(workspace),
        ])

        assert result.exit_code == 0
        assert (workspace / "datasets" / "trial" / "dataset.yaml").exists()
        assert (workspace / "datasets" / "trial" / "data.parquet").exists()
        # 同梱データセットのディレクトリに漏れていないこと
        repo_datasets = Path(__file__).resolve().parent.parent / "datasets"
        assert not (repo_datasets / "trial").exists()

    def test_generated_yaml_has_schema_version(self, runner, csv_workspace):
        """生成 YAML に schema_version が入り、ロード時に警告が出ないこと。"""
        workspace, csv_path = csv_workspace
        runner.invoke(main, [
            "add", "trial", "--csv", str(csv_path), "--data-dir", str(workspace),
        ])

        config = yaml.safe_load(
            (workspace / "datasets" / "trial" / "dataset.yaml").read_text(encoding="utf-8"),
        )
        assert config["schema_version"] == CURRENT_SCHEMA_VERSION

    def test_scaffolded_dataset_is_queryable_without_yaml_editing(
        self, runner, csv_workspace, monkeypatch,
    ):
        """desc が空のままでも inspect / summarize が動くこと。

        YAML 補完は品質向上のための任意工程であり、必須ではない。
        """
        workspace, csv_path = csv_workspace
        runner.invoke(main, [
            "add", "trial", "--csv", str(csv_path), "--data-dir", str(workspace),
        ])

        monkeypatch.setenv(DATA_DIR_ENV_VAR, str(workspace))
        _get_registry.cache_clear()

        inspected = json.loads(runner.invoke(main, ["inspect", "trial"]).output)
        assert inspected["record_count"] == 3

        summarized = json.loads(runner.invoke(main, [
            "summarize", "trial", "--group-by", '["分類"]', "--metrics", '["count"]',
        ]).output)
        assert dict(summarized["rows"]) == {"福祉": 2, "税務": 1}

    def test_env_var_selects_data_dir(self, runner, csv_workspace, monkeypatch):
        """ADMIN_PROCEDURES_DATA_DIR だけで出力先と読み込み先が決まること。"""
        workspace, csv_path = csv_workspace
        monkeypatch.setenv(DATA_DIR_ENV_VAR, str(workspace))
        _get_registry.cache_clear()

        result = runner.invoke(main, ["add", "trial", "--csv", str(csv_path)])
        assert result.exit_code == 0
        assert (workspace / "datasets" / "trial" / "dataset.yaml").exists()

        listed = json.loads(runner.invoke(main, ["list"]).output)
        assert [d["dataset_id"] for d in listed["datasets"]] == ["trial"]

    def test_missing_csv_for_new_dataset_is_an_error(self, runner, tmp_path):
        """dataset.yaml も --csv も無い場合は入力エラーになること。"""
        result = runner.invoke(main, ["add", "trial", "--data-dir", str(tmp_path)])
        assert result.exit_code == 1
        assert "--csv" in result.output


# ============================================================
# fetch: 配布元からの取得とデータ未取得時の案内
# ============================================================


class TestFetchAndMissingData:
    def _dataset_without_data(self, workspace, runner, csv_path):
        """scaffold 済みだが data.parquet が無い状態を作る。"""
        runner.invoke(main, [
            "add", "trial", "--csv", str(csv_path), "--data-dir", str(workspace),
        ])
        (workspace / "datasets" / "trial" / "data.parquet").unlink()
        return workspace / "datasets" / "trial"

    def test_missing_data_gives_actionable_error(
        self, runner, csv_workspace, monkeypatch,
    ):
        """データ未取得時は「空のデータセット」ではなく次の一手を示すこと。"""
        workspace, csv_path = csv_workspace
        self._dataset_without_data(workspace, runner, csv_path)

        monkeypatch.setenv(DATA_DIR_ENV_VAR, str(workspace))
        _get_registry.cache_clear()

        result = runner.invoke(main, ["inspect", "trial"])
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert "データファイルがまだありません" in payload["error"]
        assert "apcli fetch trial" in payload["hint"]
        assert "apcli add trial" in payload["hint"]

    def test_fetch_state_surfaces_in_provenance(
        self, runner, csv_workspace, monkeypatch,
    ):
        """.fetch.json の取得日が provenance の fetched_at に出ること。

        as_of_date（データの基準時点）とは別項目であること。
        """
        workspace, csv_path = csv_workspace
        runner.invoke(main, [
            "add", "trial", "--csv", str(csv_path), "--data-dir", str(workspace),
        ])
        (workspace / "datasets" / "trial" / FETCH_STATE_FILENAME).write_text(
            json.dumps({"fetched_at": "2026-08-03", "filename": "x.xlsx"}),
            encoding="utf-8",
        )

        monkeypatch.setenv(DATA_DIR_ENV_VAR, str(workspace))
        _get_registry.cache_clear()

        payload = json.loads(runner.invoke(main, ["query", "trial", "--limit", "1"]).output)
        assert payload["provenance"]["fetched_at"] == "2026-08-03"
        assert "as_of_date" not in payload["provenance"]

    def test_fetch_without_asset_pattern_is_an_error(
        self, runner, csv_workspace, monkeypatch,
    ):
        """asset_pattern 未定義のデータセットは自動取得できず、手順を案内すること。"""
        workspace, csv_path = csv_workspace
        runner.invoke(main, [
            "add", "trial", "--csv", str(csv_path), "--data-dir", str(workspace),
        ])

        monkeypatch.setenv(DATA_DIR_ENV_VAR, str(workspace))
        result = runner.invoke(main, ["fetch", "trial"])

        assert result.exit_code == 1
        assert "asset_pattern" in result.output
        assert "apcli add trial --csv" in result.output

    def test_fetch_forwards_allowed_hosts(self, runner, monkeypatch):
        """--allowed-host が fetch() に渡ること。未指定時は None になること。"""
        import admin_procedures.prepare_dataset as prepare_dataset

        received = {}

        def fake_fetch(dataset_id, base_dir=None, allowed_hosts=None, **kwargs):
            received["allowed_hosts"] = allowed_hosts

        monkeypatch.setattr(prepare_dataset, "fetch", fake_fetch)

        runner.invoke(main, [
            "fetch", "trial",
            "--allowed-host", "a.example.go.jp",
            "--allowed-host", "b.example.go.jp",
        ])
        assert received["allowed_hosts"] == ["a.example.go.jp", "b.example.go.jp"]

        runner.invoke(main, ["fetch", "trial"])
        assert received["allowed_hosts"] is None


# ============================================================
# add --force-scaffold: 補完済み YAML の保護
# ============================================================


def _curate(yaml_path):
    """scaffold 済み YAML に補完内容を書き足す（人手補完の再現）。"""
    config = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    config["title"] = "補完済みデータセット"
    config["fields"][0]["desc"] = "自治体の名称。"
    config["fields"][1]["notes"] = ["区分は調査時点のもの。"]
    yaml_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8",
    )


class TestForceScaffoldGuard:
    def _curated_dataset(self, runner, workspace, csv_path):
        runner.invoke(main, [
            "add", "trial", "--csv", str(csv_path), "--data-dir", str(workspace),
        ])
        yaml_path = workspace / "datasets" / "trial" / "dataset.yaml"
        _curate(yaml_path)
        return yaml_path

    def test_refuses_to_overwrite_curated_yaml_noninteractively(
        self, runner, csv_workspace,
    ):
        """非対話環境では補完済み YAML を破壊せず中断すること。

        スクリプトやエージェントからの実行で desc/notes を失わないようにする。
        """
        workspace, csv_path = csv_workspace
        yaml_path = self._curated_dataset(runner, workspace, csv_path)

        result = runner.invoke(main, [
            "add", "trial", "--csv", str(csv_path),
            "--data-dir", str(workspace), "--force-scaffold",
        ])

        assert result.exit_code == 1
        assert "--yes" in result.output
        # 補完内容が残っていること
        config = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert config["title"] == "補完済みデータセット"
        assert config["fields"][0]["desc"] == "自治体の名称。"

    def test_yes_overwrites_but_keeps_backup(self, runner, csv_workspace):
        """--yes 指定時は上書きするが .bak に退避すること。"""
        workspace, csv_path = csv_workspace
        yaml_path = self._curated_dataset(runner, workspace, csv_path)

        result = runner.invoke(main, [
            "add", "trial", "--csv", str(csv_path),
            "--data-dir", str(workspace), "--force-scaffold", "--yes",
        ])

        assert result.exit_code == 0
        regenerated = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert not regenerated["fields"][0]["desc"]  # 骨組みに戻っている

        backup = yaml.safe_load(
            yaml_path.with_suffix(".yaml.bak").read_text(encoding="utf-8"),
        )
        assert backup["title"] == "補完済みデータセット"
        assert backup["fields"][0]["desc"] == "自治体の名称。"

    def test_skeleton_yaml_is_regenerated_without_prompting(
        self, runner, csv_workspace,
    ):
        """補完内容が無ければ確認なしで再生成すること（失うものが無いため）。"""
        workspace, csv_path = csv_workspace
        runner.invoke(main, [
            "add", "trial", "--csv", str(csv_path), "--data-dir", str(workspace),
        ])

        result = runner.invoke(main, [
            "add", "trial", "--csv", str(csv_path),
            "--data-dir", str(workspace), "--force-scaffold",
        ])

        assert result.exit_code == 0
        assert "--yes" not in result.output

    def test_curated_summary_counts_completed_content(self, runner, csv_workspace):
        """curated_summary が補完内容を数えられること。"""
        from admin_procedures.prepare_dataset import curated_summary

        workspace, csv_path = csv_workspace
        yaml_path = self._curated_dataset(runner, workspace, csv_path)

        summary = curated_summary(
            yaml.safe_load(yaml_path.read_text(encoding="utf-8")),
        )
        assert summary["desc"] == 1
        assert summary["notes"] == 1


# ============================================================
# scaffold: 型推定とデータ保全
# ============================================================


_MESSY_ROWS = [
    "団体コード,自治体名,人口,予算額,執行率",
    '011002,札幌市,1973395,"1,234,567",0.87',
    '041009,仙台市,1096704,"987,654",0.92',
    '141003,横浜市,3777491,"3,456,789",0.78',
]


def _scaffold_messy(runner, workspace, csv_path):
    csv_path.write_text("\n".join(_MESSY_ROWS) + "\n", encoding="utf-8")
    runner.invoke(main, [
        "add", "messy", "--csv", str(csv_path), "--data-dir", str(workspace),
    ])
    import polars as pl
    config = yaml.safe_load(
        (workspace / "datasets" / "messy" / "dataset.yaml").read_text(encoding="utf-8"),
    )
    df = pl.read_parquet(workspace / "datasets" / "messy" / "data.parquet")
    return {f["name"]: f for f in config["fields"]}, df


class TestScaffoldTypeInference:
    def test_thousands_separator_is_preserved(self, runner, csv_workspace):
        """桁区切り付きの数値が null にならないこと。

        分類は数値と判定しつつ変換でカンマを除去しないと全行 null になる。
        """
        workspace, csv_path = csv_workspace
        fields, df = _scaffold_messy(runner, workspace, csv_path)

        assert fields["予算額"]["role"] == "measure"
        assert df["予算額"].to_list() == [1234567, 987654, 3456789]

    def test_decimal_column_becomes_float(self, runner, csv_workspace):
        """小数を integer に丸めず float として保持すること。"""
        workspace, csv_path = csv_workspace
        fields, df = _scaffold_messy(runner, workspace, csv_path)

        assert fields["執行率"]["data_type"] == "float"
        assert df["執行率"].to_list() == [0.87, 0.92, 0.78]

    def test_code_column_keeps_leading_zeros(self, runner, csv_workspace):
        """先頭ゼロのあるコード列を measure にせず文字列で保持すること。

        自治体コードや郵便番号を数値化すると桁が落ち、集計対象にもなる。
        """
        workspace, csv_path = csv_workspace
        fields, df = _scaffold_messy(runner, workspace, csv_path)

        assert fields["団体コード"]["role"] != "measure"
        assert df["団体コード"].to_list() == ["011002", "041009", "141003"]

    def test_plain_integer_column_still_measure(self, runner, csv_workspace):
        """通常の数値列は従来どおり measure/integer であること。"""
        workspace, csv_path = csv_workspace
        fields, df = _scaffold_messy(runner, workspace, csv_path)

        assert fields["人口"]["role"] == "measure"
        assert fields["人口"]["data_type"] == "integer"
        assert df["人口"].to_list() == [1973395, 1096704, 3777491]


class TestScaffoldEncoding:
    def test_detects_cp932(self, runner, csv_workspace):
        """CP932 (Shift_JIS) の CSV を自動判定して読めること。"""
        import polars as pl

        workspace, csv_path = csv_workspace
        sjis = workspace / "sjis.csv"
        sjis.write_bytes(("\n".join(_MESSY_ROWS) + "\n").encode("cp932"))

        result = runner.invoke(main, [
            "add", "sjis", "--csv", str(sjis), "--data-dir", str(workspace),
        ])

        assert result.exit_code == 0
        df = pl.read_parquet(workspace / "datasets" / "sjis" / "data.parquet")
        assert df["自治体名"].to_list() == ["札幌市", "仙台市", "横浜市"]

    def test_undetectable_encoding_reports_candidates(self, runner, csv_workspace):
        """判定できない場合は候補と --encoding を案内すること。"""
        workspace, csv_path = csv_workspace
        broken = workspace / "broken.csv"
        broken.write_bytes(b"\x80\x81\x82\xff\xfe\x81\x40\xff")

        result = runner.invoke(main, [
            "add", "broken", "--csv", str(broken), "--data-dir", str(workspace),
        ])

        assert result.exit_code == 1
        assert "--encoding" in result.output


class TestFetchHardening:
    """apcli fetch の取得元検証 (https 強制・ファイル名正規化)。"""

    def test_http_page_url_is_rejected(self):
        from admin_procedures.prepare_dataset import FetchError, _find_asset_url

        with pytest.raises(FetchError, match="https"):
            _find_asset_url("http://example.com/page", r"\.csv$")

    def test_http_download_url_is_rejected(self, tmp_path):
        from admin_procedures.prepare_dataset import FetchError, _download

        with pytest.raises(FetchError, match="https"):
            _download("http://example.com/data.csv", tmp_path / "data.csv")
        assert not (tmp_path / "data.csv").exists()

    def test_asset_filename_strips_directories_and_query(self):
        from admin_procedures.prepare_dataset import _asset_filename

        assert _asset_filename(
            "https://example.com/a/b/data%20file.csv?v=2#frag") == "data file.csv"

    def test_asset_filename_rejects_unusable_names(self):
        from admin_procedures.prepare_dataset import FetchError, _asset_filename

        for url in ("https://example.com/", "https://example.com/a/.."):
            with pytest.raises(FetchError, match="ファイル名"):
                _asset_filename(url)

    def test_invalid_asset_pattern_fails_before_network(self, monkeypatch):
        """不正な正規表現はページ取得前に FetchError になること。"""
        import urllib.request

        from admin_procedures.prepare_dataset import FetchError, _find_asset_url

        def _no_network(*_args, **_kwargs):
            raise AssertionError("ネットワークに出てはいけない")

        monkeypatch.setattr(urllib.request, "build_opener", _no_network)

        with pytest.raises(FetchError, match="asset_pattern"):
            _find_asset_url("https://example.test/page", "data(*.csv")

    def test_allowed_hosts_uses_normalized_exact_match(self):
        from admin_procedures.prepare_dataset import (
            FetchError,
            _normalize_allowed_hosts,
            _validate_fetch_url,
        )

        allowed_hosts = ["DATA.INTRA.EXAMPLE.GO.JP."]
        normalized = _normalize_allowed_hosts(allowed_hosts)
        _, hostname, _ = _validate_fetch_url(
            "https://data.intra.example.go.jp/file",
            allowed_hosts=normalized,
        )
        assert hostname == "data.intra.example.go.jp"

        with pytest.raises(FetchError, match="許可リスト"):
            _validate_fetch_url(
                "https://example.go.jp/file",
                allowed_hosts=normalized,
            )

    def test_allowed_hosts_normalizes_ipv6(self):
        from admin_procedures.prepare_dataset import (
            _normalize_allowed_hosts,
            _validate_fetch_url,
        )

        normalized = _normalize_allowed_hosts(["[FD00:0:0:0:0:0:0:1]"])
        _, hostname, _ = _validate_fetch_url(
            "https://[fd00::1]/file",
            allowed_hosts=normalized,
        )

        assert normalized == frozenset({"fd00::1"})
        assert hostname == "fd00::1"

    @pytest.mark.parametrize("host_with_port", ["example.go.jp:443", "[::1]:8443"])
    def test_allowed_hosts_rejects_hostname_with_port(self, host_with_port):
        from admin_procedures.prepare_dataset import FetchError, _normalize_allowed_hosts

        with pytest.raises(FetchError, match="ポート"):
            _normalize_allowed_hosts([host_with_port])

    def test_allowed_hosts_rejects_invalid_bracketed_ipv6(self):
        from admin_procedures.prepare_dataset import FetchError, _normalize_allowed_hosts

        with pytest.raises(FetchError, match="IPv6"):
            _normalize_allowed_hosts(["[not-an-ip]"])


class TestFetchStreamingDownload:
    """_fetch_url_with_limit / _download のストリーミング書き込みとタイムアウト伝播。"""

    @staticmethod
    def _install_fake_opener(monkeypatch, chunks):
        """urllib opener を置き換え、ネットワークなしでチャンク応答を返す。"""
        import urllib.request

        class FakeResponse:
            def __init__(self, response_chunks):
                self._chunks = iter(response_chunks)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                return next(self._chunks)

        class FakeOpener:
            def __init__(self):
                self.calls = []

            def open(self, url, timeout):
                self.calls.append((url, timeout))
                return FakeResponse(chunks)

        opener = FakeOpener()
        monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: opener)
        return opener

    def test_dest_path_writes_to_file(self, tmp_path, monkeypatch):
        """レスポンスをファイルへ書き込み、指定タイムアウトとサイズを返す。"""
        from admin_procedures.prepare_dataset import _fetch_url_with_limit

        opener = self._install_fake_opener(monkeypatch, [b"abc", b"def", b""])
        dest = tmp_path / "stream.bin"

        size = _fetch_url_with_limit(
            "https://example.test/data", max_size=100, timeout=17, dest_path=dest,
        )

        assert size == 6
        assert dest.read_bytes() == b"abcdef"
        assert opener.calls == [("https://example.test/data", 17)]

    def test_download_is_atomic_and_forwards_timeout(self, tmp_path, monkeypatch):
        from admin_procedures.prepare_dataset import _download

        opener = self._install_fake_opener(monkeypatch, [b"new-", b"data", b""])
        dest = tmp_path / "data" / "file.bin"
        dest.parent.mkdir()
        dest.write_bytes(b"old-data")

        size = _download("https://example.test/file", dest, timeout=29)

        assert size == 8
        assert dest.read_bytes() == b"new-data"
        assert opener.calls == [("https://example.test/file", 29)]
        assert list(dest.parent.glob("*.part")) == []

    def test_download_size_error_preserves_dest_and_removes_part(
        self, tmp_path, monkeypatch,
    ):
        import admin_procedures.prepare_dataset as prepare_dataset

        from admin_procedures.prepare_dataset import FetchError, _download

        self._install_fake_opener(monkeypatch, [b"ab", b"cd", b""])
        monkeypatch.setattr(prepare_dataset, "_MAX_DOWNLOAD_BYTES", 3)
        dest = tmp_path / "data" / "file.bin"
        dest.parent.mkdir()
        dest.write_bytes(b"old-data")

        with pytest.raises(FetchError, match="大きすぎます"):
            _download("https://example.test/file", dest)

        assert dest.read_bytes() == b"old-data"
        assert list(dest.parent.glob("*.part")) == []

    def test_download_rejects_http(self, tmp_path):
        """_download は http URL を拒否し、一時ファイルを作成しない。"""
        from admin_procedures.prepare_dataset import FetchError, _download

        dest = tmp_path / "data" / "file.bin"

        with pytest.raises(FetchError, match="https"):
            _download("http://example.com/file", dest)

        # dest も一時ファイルも作成されないこと
        assert not dest.exists()
        assert list(dest.parent.glob("*.part")) == []
