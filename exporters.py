# -*- coding: utf-8 -*-
"""导出器：Clash YAML / sing-box JSON / base64 订阅 / CSV / Markdown。"""
import base64
import csv
import io
import json

import yaml

# 导出时保留的节点字段（白名单，避免把 _idx 之类的内部字段带出去）
KEEP = ("name", "type", "server", "port", "tls", "skip-cert-verify", "skip_cert_verify",
        "servername", "username", "password", "uuid", "alterId", "cipher", "network",
        "ws-opts", "ws-path", "udp", "sni", "ip", "ipv6", "peername", "fingerprint")


def _clean(node):
    return {k: v for k, v in node.items() if k in KEEP}


def to_clash_yaml(nodes):
    """导出为 Clash/mihomo 可直接导入的配置（只含可用节点，按速度排序）。"""
    out = {
        "mixed-port": 7890,
        "mode": "rule",
        "log-level": "warning",
        "external-controller": "127.0.0.1:9090",
        "proxies": [_clean(n) for n in nodes],
        "proxy-groups": [
            {"name": "🚀 节点选择", "type": "select", "proxies": [n["name"] for n in nodes]},
            {"name": "♻️ 自动测速", "type": "url-test",
             "proxies": [n["name"] for n in nodes],
             "url": "http://www.gstatic.com/generate_204", "interval": 300},
        ],
        "rules": ["MATCH,🚀 节点选择"],
    }
    return yaml.dump(out, allow_unicode=True, sort_keys=False)


def _to_singbox_outbound(n):
    """Clash proxy dict -> sing-box outbound。"""
    t = n.get("type")
    base = {"tag": n["name"], "type": t, "server": n["server"],
            "server_port": int(n["port"])}
    if n.get("tls"):
        tls = {"enabled": True}
        if n.get("servername") or n.get("sni"):
            tls["server_name"] = n.get("servername") or n.get("sni")
        if n.get("skip-cert-verify") or n.get("skip_cert_verify"):
            tls["insecure"] = True
        base["tls"] = tls
    if n.get("username"):
        base["username"] = n["username"]
        base["password"] = n.get("password") or ""
    if t == "http":
        pass  # http 代理：username/password 已平铺
    elif t in ("socks5", "socks"):
        base["type"] = "socks"
    elif t == "vmess":
        base["uuid"] = n.get("uuid")
        base["alter_id"] = int(n.get("alterId") or 0)
        if n.get("cipher"):
            base["method"] = n["cipher"]
    elif t in ("vless", "trojan", "ss", "ssr", "hysteria", "hysteria2", "tuic"):
        # 通用字段兜底，复杂协议导出后可能还需人工校对
        for k in ("uuid", "password", "cipher", "flow", "network"):
            if n.get(k):
                base[k] = n[k]
    return base


def to_singbox_json(nodes):
    """导出为 sing-box 可导入的 JSON（只含可用节点，按速度排序）。"""
    outbounds = [_to_singbox_outbound(n) for n in nodes]
    outbounds.insert(0, {"tag": "direct", "type": "direct"})
    outbounds.append({"tag": "block", "type": "block"})
    tags = [o["tag"] for o in outbounds]
    return json.dumps({
        "log": {"level": "warn"},
        "outbounds": outbounds + [
            {"tag": "select", "type": "selector", "outbounds": [n["name"] for n in nodes]},
            {"tag": "auto", "type": "urltest", "outbounds": [n["name"] for n in nodes],
             "url": "https://www.gstatic.com/generate_204", "interval": "5m"},
        ],
        "route": {"final": "select"},
    }, ensure_ascii=False, indent=2)


def to_base64_links(nodes):
    """vmess/ss/trojan 链接的 base64 订阅；http/socks 节点写成 vless 兜底（多数客户端不吃，仅留档）。"""
    links = []
    for n in nodes:
        t = (n.get("type") or "").lower()
        if t in ("vmess", "vless", "trojan", "ss", "ssr"):
            # 具体链接格式由各协议决定，这里只做能导出的最小实现
            links.append(f"# {n['name']}（链接导出需逐协议实现，此处用 YAML 兜底）")
        else:
            links.append(f"# {n['name']} {n.get('server')}:{n.get('port')}")
    raw = "\n".join(links) + "\n"
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def to_csv(nodes, results):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "type", "server", "port", "tls", "alive", "latency_ms", "dl_mbps", "error"])
    for n in nodes:
        r = results.get(n["name"], {})
        w.writerow([n.get("name"), n.get("type"), n.get("server"), n.get("port"),
                    "yes" if n.get("tls") else "no",
                    r.get("alive", ""), r.get("latency", ""), r.get("dl_mbps", ""),
                    r.get("err", "")])
    return buf.getvalue()


def to_markdown(nodes, results):
    lines = ["| 节点 | 类型 | 服务器 | 端口 | TLS | 可用 | 延时(ms) | 下载(MB/s) | 错误 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for n in nodes:
        r = results.get(n["name"], {})
        alive = r.get("alive")
        mark = "✅" if alive else ("❌" if alive is False else "—")
        lines.append(
            f"| {n.get('name','')} | {n.get('type','')} | {n.get('server','')} | {n.get('port','')} "
            f"| {'是' if n.get('tls') else ''} | {mark} "
            f"| {r.get('latency', '—') if r.get('latency') is not None else '—'} "
            f"| {r.get('dl_mbps', '—') if r.get('dl_mbps') is not None else '—'} "
            f"| {r.get('err', '') or ''} |")
    return "\n".join(lines) + "\n"
