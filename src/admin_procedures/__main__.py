"""Allow running as `python -m admin_procedures`."""

import os

from admin_procedures.server import mcp

if port := os.environ.get("ADMIN_PROCEDURES_PORT"):
    # HTTP transport での起動
    # ホスト決定: ADMIN_PROCEDURES_HOST > ADMIN_PROCEDURES_PUBLIC の順で優先
    if os.environ.get("ADMIN_PROCEDURES_HOST"):
        host = os.environ["ADMIN_PROCEDURES_HOST"]
    elif os.environ.get("ADMIN_PROCEDURES_PUBLIC") == "1":
        host = "0.0.0.0"
    else:
        host = "127.0.0.1"
    mcp.run(transport="streamable-http", host=host, port=int(port))
else:
    mcp.run()
