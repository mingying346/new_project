"""连通性测试：确认平台大模型接口可用（只发一条极短请求，控制花费）。"""
import json
import sys
import urllib.request
from pathlib import Path

cfg = json.loads(Path("app/llm_config.local.json").read_text(encoding="utf-8"))
body = {
    "model": cfg["model"],
    "messages": [{"role": "user", "content": "只回复两个字：可用"}],
    "temperature": 0,
    "max_tokens": 16,
    "stream": False,
}
req = urllib.request.Request(
    cfg["base_url"] + "/chat/completions",
    data=json.dumps(body).encode("utf-8"),
    headers={"content-type": "application/json",
             "authorization": "Bearer " + cfg["api_key"]},
    method="POST")
try:
    with urllib.request.urlopen(req, timeout=cfg.get("timeout_seconds", 60)) as r:
        d = json.loads(r.read().decode("utf-8"))
    print("HTTP 200")
    print("模型回答 :", d["choices"][0]["message"]["content"])
    print("实际模型 :", d.get("model"))
    print("token 用量:", d.get("usage"))
except Exception as exc:
    print("调用失败:", type(exc).__name__, exc)
    body = getattr(exc, "read", lambda: b"")()
    if body:
        print("响应体:", body.decode("utf-8", "replace")[:500])
    sys.exit(1)
