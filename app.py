"""HTTP 请求入口：路由、JSON 编解码、静态页面。

业务规则见 rules.py，数据与事务见 store.py；本文件不含业务判定。
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from errors import BusinessError
from store import BASE_DIR, DEFAULT_DB, PreservationStore

WEB_DIR = BASE_DIR / "web"


class Handler(BaseHTTPRequestHandler):
    server_version = "Preservation/1.0"

    def _store(self) -> PreservationStore:
        return self.server.store  # type: ignore[attr-defined]

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict):
            raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, rel: str, content_type: str) -> None:
        target = (WEB_DIR / rel).resolve()
        if WEB_DIR not in target.parents or not target.is_file():
            raise BusinessError("页面不存在", 404, "not_found")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    # 路由：只做参数提取与转发
    # ------------------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]
        user = self.headers.get("X-User-Id", "")
        if method == "GET" and path == "/":
            return self._static("index.html", "text/html; charset=utf-8")
        if method == "GET" and path == "/static/app.js":
            return self._static("app.js", "application/javascript; charset=utf-8")
        if method == "GET" and path == "/static/style.css":
            return self._static("style.css", "text/css; charset=utf-8")
        if method == "GET" and path == "/health":
            return self._send(200, {"ok": True})

        store = self._store()
        d = self._body() if method in ("POST", "PUT", "PATCH") else {}

        # -- 档案与版本（原有接口） -------------------------------------
        if parts == ["api", "archives"] and method == "POST":
            return self._send(201, store.create_archive(
                user, d.get("name", ""), d.get("retention_until", ""), bool(d.get("restricted", True))))
        if len(parts) == 4 and parts[:2] == ["api", "archives"] and method == "POST":
            archive_id = int(parts[2])
            if parts[3] == "versions":
                return self._send(201, store.ingest_version(user, archive_id, d.get("files")))
            if parts[3] == "members":
                return self._send(201, store.grant(user, archive_id, d.get("user_id", ""), d.get("permission", "")))
            if parts[3] == "retention":
                return self._send(200, store.update_retention(user, archive_id, d.get("retention_until", "")))
        if len(parts) == 4 and parts[:2] == ["api", "archives"] and parts[3] == "status" and method == "GET":
            return self._send(200, store.archive_status(user, int(parts[2])))
        if len(parts) == 3 and parts[:2] == ["api", "versions"] and method == "GET":
            return self._send(200, store.get_version(user, int(parts[2])))
        if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "copies" and method == "POST":
            return self._send(201, store.add_copy(user, int(parts[2]), d.get("location", "")))
        if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "migrate" and method == "POST":
            return self._send(201, store.migrate(
                user, int(parts[2]), d.get("source_path", ""), d.get("target_path", ""),
                d.get("target_format", ""), d.get("content_b64", "")))
        if len(parts) == 4 and parts[:2] == ["api", "copies"] and parts[3] == "verify" and method == "POST":
            return self._send(200, store.verify_copy(user, int(parts[2])))
        if len(parts) == 4 and parts[:2] == ["api", "copies"] and parts[3] == "simulate-corruption" and method == "POST":
            return self._send(200, store.simulate_corruption(user, int(parts[2]), d.get("path", "")))

        # -- 到期处置台 ---------------------------------------------------
        if parts == ["api", "disposition", "console"] and method == "GET":
            return self._send(200, store.console(user))
        if parts == ["api", "disposition", "candidates"] and method == "GET":
            return self._send(200, store.list_due_candidates(user))
        if parts == ["api", "disposition", "batches"] and method == "POST":
            return self._send(201, store.create_batch(user, d.get("name", ""), d.get("archive_ids")))
        if parts == ["api", "disposition", "batches"] and method == "GET":
            return self._send(200, store.list_batches(user))
        if len(parts) == 5 and parts[:3] == ["api", "disposition", "batches"]:
            batch_id = int(parts[3])
            action = parts[4]
            if action == "archives" and method == "POST":
                return self._send(201, store.add_archives(user, batch_id, d.get("archive_ids")))
            if action == "check" and method == "POST":
                return self._send(200, store.check_batch(user, batch_id))
            if action == "confirm" and method == "POST":
                return self._send(200, store.confirm_items(user, batch_id, d.get("item_ids")))
            if action == "execute" and method == "POST":
                return self._send(200, store.execute_batch(user, batch_id))
        if (len(parts) == 7 and parts[:3] == ["api", "disposition", "batches"]
                and parts[4] == "items" and method == "POST"):
            batch_id, item_id = int(parts[3]), int(parts[5])
            if parts[6] == "freeze":
                return self._send(200, store.freeze_item(user, batch_id, item_id, d.get("reason", "")))
            if parts[6] == "unfreeze":
                return self._send(200, store.unfreeze_item(user, batch_id, item_id))
        if (len(parts) == 6 and parts[:3] == ["api", "disposition", "batches"]
                and parts[4] == "items" and method == "DELETE"):
            return self._send(200, store.remove_item(user, int(parts[3]), int(parts[5])))
        if len(parts) == 4 and parts[:3] == ["api", "disposition", "batches"] and method == "GET":
            return self._send(200, store.batch_detail(user, int(parts[3])))

        raise BusinessError("接口不存在", 404, "not_found")

    def _handle(self, method: str) -> None:
        try:
            self._dispatch(method)
        except BusinessError as exc:
            payload = {"error": {"code": exc.code, "message": exc.message}}
            if exc.details is not None:
                payload["error"]["details"] = exc.details
            self._send(exc.status, payload)
        except (ValueError, TypeError):
            self._send(400, {"error": {"code": "invalid_path", "message": "路径参数格式错误"}})
        except Exception as exc:  # noqa: BLE001 - 示例服务兜底
            self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def do_DELETE(self): self._handle("DELETE")
    def log_message(self, fmt, *args): print(f"{self.address_string()} - {fmt % args}")


class PreservationServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store):
        self.store = store
        super().__init__(address, Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="数字档案长期保存服务")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--port", type=int, default=8102)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    store = PreservationStore(args.db)
    store.init_schema()
    if args.seed:
        store.seed()
    if args.init or args.seed:
        print(f"数据库已初始化: {args.db}")
        return
    print(f"数字档案服务运行于 http://127.0.0.1:{args.port}")
    server = PreservationServer(("127.0.0.1", args.port), store)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
