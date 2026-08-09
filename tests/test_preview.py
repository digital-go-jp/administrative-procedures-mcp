"""preview.py (MCP Apps プレビューホスト) のテスト。"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from admin_procedures.preview import (
    TOOL_TEMPLATES,
    build_call_result,
    call_tool,
    make_server,
)
from admin_procedures.response import DatasetResolveError

DATASET_ID = "procedures-survey-r6"


# ============================================================
# call_tool
# ============================================================


def test_call_tool_list_datasets(registry):
    data = call_tool(registry, "list_datasets", {})
    ids = [d["dataset_id"] for d in data["datasets"]]
    assert DATASET_ID in ids


def test_call_tool_summarize_columnar(registry):
    data = call_tool(registry, "summarize_records", {
        "dataset_id": DATASET_ID,
        "group_by": ["所管府省庁"],
        "metrics": ["count"],
    })
    assert "columns" in data and "rows" in data
    assert data["dataset_id"] == DATASET_ID  # UI のドリルダウンが参照する


def test_call_tool_coerces_json_string_params(registry):
    """UI の callServerTool は where を JSON 文字列で送る (MCP サーバーと同じ coercion)。"""
    data = call_tool(registry, "query_records", {
        "dataset_id": DATASET_ID,
        "where": json.dumps({"所管府省庁": "厚生労働省"}),
        "limit": 10,
    })
    assert data["total"] == 2


def test_call_tool_unknown_tool(registry):
    with pytest.raises(ValueError, match="未知のツール"):
        call_tool(registry, "no_such_tool", {})


def test_call_tool_missing_dataset_id(registry):
    with pytest.raises(ValueError, match="dataset_id"):
        call_tool(registry, "inspect_dataset", {})


def test_call_tool_dataset_not_found(registry):
    with pytest.raises(DatasetResolveError):
        call_tool(registry, "inspect_dataset", {"dataset_id": "nope"})


def test_build_call_result_success(registry):
    result = build_call_result(registry, "list_datasets", {})
    assert result["isError"] is False
    assert result["structuredContent"]["total"] >= 1
    assert result["content"][0]["type"] == "text"
    assert result["_meta"]["ui"]["resourceUri"].endswith("/list_datasets")


def test_build_call_result_tool_error_is_not_protocol_error(registry):
    result = build_call_result(registry, "inspect_dataset", {"dataset_id": "nope"})
    assert result["isError"] is True
    assert "error" in result["structuredContent"]


# ============================================================
# HTTP サーバー
# ============================================================


@pytest.fixture()
def http_server(registry):
    server = make_server(registry, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base
    server.shutdown()
    server.server_close()


def _get(base, path):
    with urllib.request.urlopen(base + path) as res:
        return res.status, res.read().decode("utf-8")


def _post_json(base, path, payload):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as res:
        return res.status, json.loads(res.read().decode("utf-8"))


def test_http_host_page(http_server):
    status, body = _get(http_server, "/")
    assert status == 200
    assert "<title>apcli</title>" in body
    assert "INLINE_BASE_CSS" not in body  # base.css が注入済み


def test_http_host_omits_truncated_history_result(http_server):
    """容量制限した履歴を「0件」の結果 UI として復元しない。"""
    _, body = _get(http_server, "/")
    omitted_branch = body.index("e.summary._truncated")
    iframe_branch = body.index(
        "e.summary && e.ok !== false && TOOL_TEMPLATES[e.tool]",
        omitted_branch,
    )
    assert omitted_branch < iframe_branch
    assert "結果の詳細は履歴容量を抑えるため省略しました" in body


def test_http_host_prunes_orphaned_session_storage(http_server):
    """一覧から外れたセッション本体を localStorage に残さない。"""
    _, body = _get(http_server, "/")
    assert 'var LS_SESSION_PREFIX = "apcli-preview:session:"' in body
    assert "staleKeys.push(key)" in body
    assert "staleKeys.forEach(function(key) { localStorage.removeItem(key); });" in body


def test_http_ui_templates(http_server):
    for name in TOOL_TEMPLATES.values():
        status, body = _get(http_server, f"/ui/{name}")
        assert status == 200
        assert "onData(" in body  # base.js が注入済み


def test_http_ui_unknown_template(http_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(http_server, "/ui/../secret")
    assert exc.value.code == 404


def test_http_describe(http_server):
    status, body = _get(http_server, "/api/describe")
    names = [t["name"] for t in json.loads(body)["tools"]]
    assert sorted(names) == sorted(TOOL_TEMPLATES)


def test_http_call_success(http_server):
    status, data = _post_json(http_server, "/api/call", {
        "name": "summarize_records",
        "arguments": {"dataset_id": DATASET_ID, "group_by": ["所管府省庁"]},
    })
    assert status == 200
    assert data["isError"] is False
    assert "columns" in data["structuredContent"]


def test_http_call_unknown_tool(http_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post_json(http_server, "/api/call", {"name": "nope", "arguments": {}})
    assert exc.value.code == 400


def test_http_call_bad_json(http_server):
    req = urllib.request.Request(
        http_server + "/api/call",
        data=b"not json",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 400


def test_http_version_etag(http_server):
    status, body = _get(http_server, "/api/version")
    assert status == 200
    etag = json.loads(body)["etag"]
    assert len(etag) == 40  # sha1 hex
    # 同一内容なら安定している (watch モードの誤発火防止)
    _, body2 = _get(http_server, "/api/version")
    assert json.loads(body2)["etag"] == etag


def test_http_turn_log_appends(http_server, tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PROCEDURES_EVAL_DIR", str(tmp_path))
    payload = {"q": "テスト質問", "calls": [{"tool": "summarize_records",
                                            "query": "集計軸: 所管府省庁"}], "answer": "回答"}
    status, data = _post_json(http_server, "/api/turn-log", payload)
    assert status == 200 and data["ok"] is True
    _post_json(http_server, "/api/turn-log", payload)
    lines = (tmp_path / "results" / "turns.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["q"] == "テスト質問"
    assert "at" in rec  # タイムスタンプが付与される


def test_http_unknown_path(http_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(http_server, "/api/unknown")
    assert exc.value.code == 404


# ============================================================
# リクエスト検証 (DNS リバインディング / CSRF 対策)
# ============================================================


def _raw_request(base, method, path, headers, body=None):
    """任意の Host ヘッダーを送るため urllib ではなく http.client を使う。"""
    import http.client

    host, port = base[len("http://"):].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        return res.status, res.read().decode("utf-8"), \
            {k.lower(): v for k, v in res.getheaders()}
    finally:
        conn.close()


def test_host_header_allowed():
    from admin_procedures.preview import host_header_allowed

    assert host_header_allowed("127.0.0.1:8765", "127.0.0.1")
    assert host_header_allowed("localhost:8765", "127.0.0.1")
    assert host_header_allowed("Localhost", "127.0.0.1")
    assert host_header_allowed("[::1]:8765", "127.0.0.1")
    # DNS リバインディング: 攻撃者ドメインが 127.0.0.1 に解決されても Host で弾く
    assert not host_header_allowed("attacker.example:8765", "127.0.0.1")
    assert not host_header_allowed("", "127.0.0.1")
    assert not host_header_allowed(None, "127.0.0.1")
    # 明示バインドしたホスト名は許可
    assert host_header_allowed("192.168.1.10:8765", "192.168.1.10")
    # ワイルドカードバインドは正当なホスト名を列挙できないため照合しない
    assert host_header_allowed("anything.example", "0.0.0.0")


def test_http_rejects_foreign_host_get(http_server):
    status, _, _ = _raw_request(http_server, "GET", "/", {"Host": "attacker.example"})
    assert status == 403


def test_http_rejects_foreign_host_post(http_server):
    status, _, _ = _raw_request(
        http_server, "POST", "/api/call",
        {"Host": "attacker.example", "Content-Type": "application/json"},
        body=json.dumps({"name": "list_datasets"}),
    )
    assert status == 403


def test_http_rejects_form_content_type(http_server):
    """HTML form が送れる Content-Type (CSRF 経路) は 415 で拒否する。"""
    status, _, _ = _raw_request(
        http_server, "POST", "/api/call",
        {"Host": "127.0.0.1", "Content-Type": "text/plain"},
        body=json.dumps({"name": "list_datasets"}),
    )
    assert status == 415


def test_http_security_headers(http_server):
    _, _, headers = _raw_request(http_server, "GET", "/", {"Host": "127.0.0.1"})
    assert headers.get("x-content-type-options") == "nosniff"
    assert "connect-src 'self'" in headers.get("content-security-policy", "")
    # JSON API にも nosniff は付く (CSP は HTML 応答のみ)
    _, _, api_headers = _raw_request(
        http_server, "GET", "/api/describe", {"Host": "127.0.0.1"})
    assert api_headers.get("x-content-type-options") == "nosniff"
