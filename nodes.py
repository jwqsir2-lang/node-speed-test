# -*- coding: utf-8 -*-
"""节点导入：从 文件 / 订阅 URL / 剪贴板文本 / 内存字符串 加载节点，统一成 Clash proxy dict 列表。"""
import base64
import json
import os
import re
import urllib.request
import yaml


def _norm(p):
    if not isinstance(p, dict):
        return None
    if not p.get("name") or not p.get("server"):
        return None
    try:
        p["port"] = int(p["port"])
    except (TypeError, ValueError):
        return None
    return p


def from_clash_yaml(text):
    d = yaml.safe_load(text)
    out = []
    if isinstance(d, dict):
        for p in d.get("proxies") or []:
            p = _norm(p)
            if p:
                out.append(p)
    return out


def _sb_transport(o, p):
    """sing-box transport -> clash network 配置。"""
    t = o.get("transport") or {}
    if not isinstance(t, dict):
        return
    tt = t.get("type")
    if tt == "ws":
        p["network"] = "ws"
        ws = {"path": t.get("path", "/")}
        hdrs = t.get("headers") or {}
        if isinstance(hdrs, dict) and hdrs:
            ws["headers"] = {str(k): (v[0] if isinstance(v, list) else v) for k, v in hdrs.items()}
        p["ws-opts"] = ws
    elif tt == "grpc":
        p["network"] = "grpc"
        p["grpc-opts"] = {"grpc-service-name": t.get("service_name", "")}
    elif tt == "http":
        p["network"] = "http"
        ho = {"path": [t.get("path", "/")]}
        hdrs = t.get("headers") or {}
        if isinstance(hdrs, dict) and hdrs:
            ho["headers"] = {str(k): (v if isinstance(v, list) else [v]) for k, v in hdrs.items()}
        p["http-opts"] = ho
    elif tt == "httpupgrade":
        # mihomo 用 ws + Connection/Upgrade 头模拟 httpupgrade
        p["network"] = "ws"
        ws = {"path": t.get("path", "/")}
        hdrs = dict(t.get("headers") or {})
        hdrs.setdefault("Connection", "Upgrade")
        hdrs.setdefault("Upgrade", "websocket")
        ws["headers"] = {str(k): (v[0] if isinstance(v, list) else v) for k, v in hdrs.items()}
        p["ws-opts"] = ws


def from_singbox_json(text):
    """sing-box outbound -> clash proxy（完整转换，mihomo 直接可用）。"""
    d = json.loads(text)
    out = []
    for o in d.get("outbounds") or []:
        if not isinstance(o, dict) or o.get("tag") is None or o.get("server") is None:
            continue
        t = o.get("type")
        if t in ("direct", "block", "dns", "selector", "urltest", "compat outbound"):
            continue
        p = {"name": o["tag"], "type": t, "server": o["server"],
             "port": o.get("server_port") or o.get("port")}

        # ---- TLS（trojan/vless/hysteria2/anytls 等都在 tls 字段里） ----
        tls = o.get("tls") or {}
        if isinstance(tls, dict) and tls.get("enabled"):
            p["tls"] = True
            if tls.get("server_name"):
                p["sni"] = tls["server_name"]
            if tls.get("insecure"):
                p["skip-cert-verify"] = True
            alpn = tls.get("alpn")
            if alpn:
                p["alpn"] = alpn if isinstance(alpn, list) else [alpn]
            r = tls.get("reality") or {}
            if isinstance(r, dict) and r.get("public_key"):
                p["reality-opts"] = {"public-key": r.get("public_key"),
                                     "short-id": (r.get("short_id") or [""])[0]
                                     if isinstance(r.get("short_id"), list)
                                     else (r.get("short_id") or "")}
            if tls.get("fingerprint"):
                p["client-fingerprint"] = tls["fingerprint"]

        # ---- 各协议特有字段 ----
        if t == "vmess":
            p["uuid"] = o.get("uuid", "")
            p["alterId"] = o.get("alter_id", 0)
            p["cipher"] = o.get("security", "auto")
        elif t == "vless":
            p["uuid"] = o.get("uuid", "")
            if o.get("flow"):
                p["flow"] = o["flow"]
        elif t in ("trojan", "shadowsocks", "hysteria2", "anytls", "hysteria", "tuic"):
            if o.get("password") is not None:
                p["password"] = o["password"]
            if t == "shadowsocks":
                p["cipher"] = o.get("cipher", "aes-128-gcm")
            if t in ("hysteria2", "hysteria"):
                obfs = o.get("obfs") or {}
                if isinstance(obfs, dict) and obfs.get("type") == "salamander":
                    p["obfs"] = "salamander"
                    p["obfs-password"] = obfs.get("password", "")
                if o.get("up_mbps"):
                    p["up"] = f"{o['up_mbps']} Mbps"
                if o.get("down_mbps"):
                    p["down"] = f"{o['down_mbps']} Mbps"
            if t == "tuic":
                p["uuid"] = o.get("uuid", "")
                p["congestion-controller"] = o.get("congestion_control", "bbr")
                p["udp-relay-mode"] = o.get("udp_relay_mode", "native")

        # ---- transport（ws/grpc/http） ----
        _sb_transport(o, p)

        # http/socks 代理类型
        if t in ("http", "socks"):
            if o.get("username"):
                p["username"] = o["username"]
                p["password"] = o.get("password", "")

        n = _norm(p)
        out.append(n if n else p)
    return out


def from_base64(text):
    """订阅常见的 base64 内容（可能是 v2rayN 的 vmess/ss 链接列表，也可能是 base64 的 clash yaml）。"""
    s = text.strip()
    try:
        raw = base64.b64decode(s + "=" * (-len(s) % 4), validate=False).decode("utf-8", "replace")
    except Exception:
        return []
    if "proxies:" in raw and ("server" in raw or "name:" in raw):
        return from_clash_yaml(raw)
    # v2ray 链接列表（vmess://...）暂不支持测速所需字段，返回空
    return []


def parse_node_lines(text):
    """从一段文字里抠出所有看起来像节点的行/JSON/YAML 片段。"""
    text = text or ""
    out = []
    # 1) 完整 clash yaml / sing-box json
    if "proxies:" in text:
        out += from_clash_yaml(text)
    if '"outbounds"' in text:
        out += from_singbox_json(text)
    # 2) 单个节点 JSON 片段（如 {"type":"http","server":"1.2.3.4","port":443}）
    for m in re.finditer(r"\{[^{}]*?\"(?:server|type)\"[^{}]*?\}", text, re.S):
        try:
            j = json.loads(m.group(0))
            if isinstance(j, dict) and j.get("server") and j.get("port"):
                j.setdefault("name", j.get("tag") or f"{j['server']}:{j['port']}")
                p = _norm(j)
                if p:
                    out.append(p)
                continue
        except Exception:
            pass
        # 3) host:port 形式
    for m in re.finditer(r"(?<![\d.])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{2,5})(?![\d])", text):
        host, port = m.group(1), int(m.group(2))
        out.append({"name": f"{host}:{port}", "type": "http", "server": host, "port": port})
    # 去重：同名同地址保留第一个
    seen, res = set(), []
    for p in out:
        k = (p.get("name"), p.get("server"), p.get("port"))
        if k in seen:
            continue
        seen.add(k)
        res.append(p)
    return res


def load_proxies(src):
    """src: 文件路径 / http(s) 订阅 URL / 直接的文本内容。返回 proxy dict 列表。"""
    if not src:
        return []
    s = str(src).strip()
    if s.startswith("http://") or s.startswith("https://"):
        req = urllib.request.Request(s, headers={
            "User-Agent": "clash-verge/v2.2.3",
            "Accept": "application/yaml,text/yaml,application/json,*/*",
        })
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8", "replace")
    elif os.path.exists(s):
        with open(s, "r", encoding="utf-8") as f:
            text = f.read()
    else:
        text = s  # 当作直接给的文本
    out = parse_node_lines(text)
    if not out and re.fullmatch(r"[A-Za-z0-9+/=_\s-]+", text or ""):
        out = from_base64(text)
    return out


if __name__ == "__main__":
    import sys
    nodes = load_proxies(sys.argv[1] if len(sys.argv) > 1 else "")
    print(f"{len(nodes)} nodes")
    for n in nodes[:5]:
        print(" ", n.get("name"), n.get("type"), n.get("server"), n.get("port"),
              "tls" if n.get("tls") else "", n.get("username") or "")
