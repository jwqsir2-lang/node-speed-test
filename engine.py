# -*- coding: utf-8 -*-
"""核心测速引擎：复用本地已存在的 mihomo.exe，不安装任何东西。

方案：mihomo 多 listener（一端口一节点）+ 线程池并发下载测速。
mihomo 由调用方负责启停（SpeedTester 的 start/stop）。
"""
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

MIHOMO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mihomo.exe")
LAT_URL = "https://www.google.com/generate_204"
DL_URL = "https://speed.cloudflare.com/__down?bytes={bytes}"


def build_cfg(nodes, base_port, ctrl_port, mixed_port):
    return {
        "mixed-port": mixed_port,
        "external-controller": f"127.0.0.1:{ctrl_port}",
        "mode": "global",
        "log-level": "warning",
        "ipv6": False,
        "proxies": nodes,
        "listeners": [
            {"name": f"in{i}", "type": "http", "port": base_port + i, "proxy": n["name"]}
            for i, n in enumerate(nodes)
        ],
        "rules": ["MATCH,DIRECT"],
    }


class SpeedTester:
    """管理 mihomo 子进程，并发测速，支持中途停止。"""

    def __init__(self, mihomo=MIHOMO, base_port=21101, ctrl_port=9099, mixed_port=7899,
                 concurrency=16, timeout_lat=8, timeout_dl=20, dl_bytes=1_000_000,
                 on_result=None):
        self.mihomo = mihomo
        self.base_port = base_port
        self.ctrl_port = ctrl_port
        self.mixed_port = mixed_port
        self.concurrency = concurrency
        self.timeout_lat = timeout_lat
        self.timeout_dl = timeout_dl
        self.dl_bytes = dl_bytes
        self.on_result = on_result  # callback(result_dict)
        self.proc = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ---------- mihomo 生命周期 ----------
    def start(self, nodes, cfg_path=None):
        self._stop.clear()
        cfg = build_cfg(nodes, self.base_port, self.ctrl_port, self.mixed_port)
        cfg_path = cfg_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "_run.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True)
        self.proc = subprocess.Popen([self.mihomo, "-f", cfg_path],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL,
                                     creationflags=0x08000000)  # CREATE_NO_WINDOW：不弹黑窗
        # 等所有 listener 起来
        for i in range(len(nodes)):
            if not self._wait_port(self.base_port + i, timeout=10):
                raise RuntimeError(f"listener {self.base_port + i} 未启动")
        return cfg_path

    def stop(self):
        self._stop.set()
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    def is_stopped(self):
        return self._stop.is_set()

    def _wait_port(self, port, timeout=8):
        end = time.time() + timeout
        while time.time() < end and not self._stop.is_set():
            s = socket.socket(); s.settimeout(0.3)
            up = s.connect_ex(("127.0.0.1", port)) == 0
            s.close()
            if up:
                return True
            time.sleep(0.15)
        return False

    # ---------- 测速 ----------
    def _opener(self, port):
        # 本地 listener 是明文 http 代理；显式指定，避免 urllib 读系统代理
        h = urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{port}",
                                         "https": f"http://127.0.0.1:{port}"})
        return urllib.request.build_opener(h)

    def _fetch(self, op, url, timeout, maxbytes=None):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Chrome/130"})
        t0 = time.time()
        r = op.open(req, timeout=timeout)
        n = 0
        while True:
            c = r.read(65536)
            if not c:
                break
            n += len(c)
            if maxbytes and n >= maxbytes:
                break
        el = time.time() - t0
        r.close()
        return n, el

    def _test_latency(self, node):
        """只测延时。alive=False 的节点不进入第二阶段。"""
        idx = node["_idx"]
        port = self.base_port + idx
        res = {"name": node["name"], "type": node.get("type"), "server": node.get("server"),
               "port": node.get("port"), "tls": bool(node.get("tls")),
               "alive": False, "latency": None, "dl_mbps": None, "err": None}
        op = self._opener(port)
        try:
            _, el = self._fetch(op, LAT_URL, self.timeout_lat)
            res["latency"] = round(el * 1000)
            res["alive"] = True
        except urllib.error.HTTPError:
            res["alive"] = True
            res["err"] = "lat-http"
        except Exception as e:
            res["err"] = f"lat:{type(e).__name__}"
        return res

    def _test_download(self, res, node):
        """对已存活的节点补测下载速度，结果就地写入 res。"""
        if not res.get("alive"):
            return res
        if self._stop.is_set():
            res["err"] = "stopped"
            return res
        op = self._opener(self.base_port + node["_idx"])
        try:
            n, el = self._fetch(op, DL_URL.format(bytes=self.dl_bytes), self.timeout_dl,
                                maxbytes=self.dl_bytes)
            res["dl_mbps"] = round(n / el / 1_000_000, 2) if el > 0 else None
        except urllib.error.HTTPError:
            res["err"] = "dl-http"
        except Exception as e:
            res["err"] = f"dl:{type(e).__name__}"
        return res

    def _test_one(self, node):
        """单节点全流程（兼容旧调用与 __main__ 自测）。"""
        res = self._test_latency(node)
        return self._test_download(res, node)

    def run(self, nodes, on_result=None):
        """两阶段测速：先并发测延时 -> 清掉不通的 -> 只对存活节点测下载。返回全部结果列表。"""
        cb = on_result or self.on_result
        tasks = [dict(n, _idx=i) for i, n in enumerate(nodes)]
        results = []

        def _emit(r):
            results.append(r)
            if cb:
                cb(r)

        # ---- 阶段 1：延时（全部节点） ----
        ex = ThreadPoolExecutor(max_workers=self.concurrency)
        futs = [ex.submit(self._test_latency, t) for t in tasks]
        try:
            for fut in as_completed(futs):
                if self._stop.is_set():
                    break
                try:
                    _emit(fut.result())
                except Exception as e:
                    _emit({"name": "?", "alive": False, "err": f"ex:{type(e).__name__}"})
        finally:
            for f in futs:
                f.cancel()
            ex.shutdown(wait=False)

        # ---- 阶段 2：下载（仅存活节点，省时间） ----
        alive = [(t, r) for t, r in zip(tasks, results) if r.get("alive")]
        if alive and not self._stop.is_set():
            ex2 = ThreadPoolExecutor(max_workers=self.concurrency)
            futs2 = [ex2.submit(self._test_download, r, t) for t, r in alive]
            try:
                for fut in as_completed(futs2):
                    if self._stop.is_set():
                        break
                    try:
                        fut.result()
                    except Exception as e:
                        pass  # 错误已在 _test_download 内写入 res
            finally:
                for f in futs2:
                    f.cancel()
                ex2.shutdown(wait=False)
            # 存活节点的下载结果已就地更新，重新回调一次让表格刷新
            if cb:
                for _, r in alive:
                    cb(r)
        return results


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from nodes import load_proxies

    import sys as _s
    cfg = _s.argv[1] if len(_s.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.yaml")
    ns = load_proxies(cfg)[:6]
    st = SpeedTester(dl_bytes=500_000)
    st.start(ns)
    try:
        for r in st.run(ns):
            print(f"{r['name'][:30]:32} alive={r['alive']!s:5} lat={str(r['latency']):>6}ms "
                  f"dl={str(r['dl_mbps']):>8}MB/s {r['err'] or ''}")
    finally:
        st.stop()
