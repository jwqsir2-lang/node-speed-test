# -*- coding: utf-8 -*-
"""GUI：节点测速工具。导入(文件/订阅/文本) -> 测速 -> 查看/复制/导出。"""
import base64
import json
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from nodes import load_proxies
from engine import SpeedTester
from exporters import to_clash_yaml, to_singbox_json, to_base64_links, to_csv, to_markdown

APP_TITLE = "节点测速工具"
GEOMETRY = "1100x640"
SUBS_FILE = os.path.join(HERE, "subs.json")


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry(GEOMETRY)
        self.nodes = []          # 已导入的节点（原始 proxy dict）
        self.results = {}        # name -> result dict
        self.tester = None
        self.running = False
        self.sort_col = None     # 当前排序列；None = 保持导入顺序
        self.sort_rev = False    # 该列是否降序
        self._build_ui()

    # ---------------- UI ----------------
    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}
        top = ttk.Frame(self.root)
        top.pack(side="top", fill="x", **pad)

        # ---- 导入区 ----
        imp = ttk.LabelFrame(top, text="导入节点")
        imp.pack(side="top", fill="x", padx=6, pady=(4, 2))
        self.src_var = tk.StringVar()
        row = ttk.Frame(imp); row.pack(fill="x", padx=6, pady=4)
        ttk.Button(row, text="打开文件…", width=12, command=self.load_file).pack(side="left")
        ttk.Button(row, text="从订阅 URL 导入", width=14, command=self.load_url).pack(side="left", padx=(4, 0))
        ttk.Button(row, text="订阅管理…", width=12, command=self.manage_subs).pack(side="left", padx=(4, 0))
        ttk.Button(row, text="从文本粘贴导入", width=14, command=self.load_text).pack(side="left", padx=(4, 0))
        ttk.Button(row, text="从剪贴板导入", width=12, command=self.load_clipboard).pack(side="left", padx=(4, 0))
        self.src_label = ttk.Label(row, textvariable=self.src_var, foreground="#666")
        self.src_label.pack(side="left", padx=(10, 0))

        # 参数区
        par = ttk.Frame(imp); par.pack(fill="x", padx=6, pady=(0, 6))
        self.conc_var = tk.IntVar(value=16)
        self.dlkb_var = tk.IntVar(value=1000)
        self.latto_var = tk.IntVar(value=8)
        self.dlto_var = tk.IntVar(value=20)
        for label, var, lo, hi, tip in (
            ("并发", self.conc_var, 1, 64, "并发线程数"),
            ("下载KB", self.dlkb_var, 50, 20000, "每个节点下载的数据量(KB)"),
            ("延时超时(s)", self.latto_var, 2, 60, "延时测试超时"),
            ("下载超时(s)", self.dlto_var, 5, 120, "下载测试超时"),
        ):
            f = ttk.Frame(par); f.pack(side="left", padx=(0, 12))
            ttk.Label(f, text=label).pack(side="left")
            ttk.Spinbox(f, from_=lo, to=hi, width=6, textvariable=var).pack(side="left", padx=(4, 0))

        # ---- 操作区 ----
        act = ttk.Frame(top)
        act.pack(side="top", fill="x", padx=6, pady=2)
        self.btn_start = ttk.Button(act, text="开始测速", command=self.start_test)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(act, text="停止测速", command=self.stop_test, state="disabled")
        self.btn_stop.pack(side="left", padx=(4, 0))
        self.btn_clear = ttk.Button(act, text="清空结果", command=self.clear_results)
        self.btn_clear.pack(side="left", padx=(4, 0))
        self.status = ttk.Label(act, text="就绪")
        self.status.pack(side="left", padx=(12, 0))
        self.prog = ttk.Progressbar(act, mode="determinate", length=200)
        self.prog.pack(side="right")

        # ---- 结果表 ----
        mid = ttk.Frame(self.root)
        mid.pack(side="top", fill="both", expand=True, padx=6, pady=4)
        cols = ("idx", "name", "type", "server", "port", "tls", "alive", "latency", "dl", "err")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        widths = {"idx": 44, "name": 300, "type": 60, "server": 150, "port": 60, "tls": 40,
                  "alive": 50, "latency": 90, "dl": 100, "err": 120}
        for c in cols:
            self.tree.heading(c, text={"idx": "#", "name": "节点名", "type": "类型", "server": "服务器",
                                       "port": "端口", "tls": "TLS", "alive": "可用",
                                       "latency": "延时(ms)", "dl": "下载(MB/s)", "err": "错误"}[c],
                             command=(lambda col=c: self.sort_by(col)))
            self.tree.column(c, width=widths[c], stretch=(c == "name"))
        self.tree.column("idx", anchor="center")
        self.tree.column("latency", anchor="e")
        self.tree.column("dl", anchor="e")
        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.show_detail)
        # 右键菜单
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="复制节点名", command=self.copy_names)
        self.menu.add_command(label="复制服务器:端口", command=self.copy_server_port)
        self.menu.add_command(label="复制选中行(JSON)", command=self.copy_rows_json)
        self.menu.add_separator()
        self.menu.add_command(label="查看节点详情", command=self.show_detail)
        self.tree.bind("<Button-3>", self.popup_menu)

        # ---- 导出区 ----
        bot = ttk.Frame(self.root)
        bot.pack(side="top", fill="x", padx=6, pady=(2, 6))
        ttk.Label(bot, text="导出:").pack(side="left")
        for label, fn in (
            ("Clash YAML", self.exp_clash),
            ("sing-box JSON", self.exp_singbox),
            ("Base64 订阅", self.exp_base64),
            ("CSV", self.exp_csv),
            ("Markdown", self.exp_md),
        ):
            ttk.Button(bot, text=label, command=fn).pack(side="left", padx=(4, 0))
        ttk.Button(bot, text="复制结果表格(Markdown)", command=self.copy_md).pack(side="left", padx=(10, 0))
        self.lbl_sel = ttk.Label(bot, text="", foreground="#666")
        self.lbl_sel.pack(side="right")
        self.tree.bind("<<TreeviewSelect>>", self._update_sel_label)

    def _update_sel_label(self, _ev=None):
        n = len(self.tree.selection())
        self.lbl_sel.config(text=f"已选 {n} 行" if n else "未选中行时导出全部可用节点")

    # ---------------- 订阅管理 ----------------
    def _load_subs(self):
        """从 subs.json 读取已保存的订阅。格式: [{"name": ..., "url": ...}]"""
        try:
            with open(SUBS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return [s for s in data if isinstance(s, dict) and s.get("url")]
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return []

    def _save_subs(self, subs):
        with open(SUBS_FILE, "w", encoding="utf-8") as f:
            json.dump(subs, f, ensure_ascii=False, indent=2)

    def manage_subs(self):
        """订阅管理窗口：列表 + 增删改 + 直接测速。"""
        subs = self._load_subs()
        d = tk.Toplevel(self.root)
        d.title("订阅管理")
        d.geometry("640x420")
        d.transient(self.root)
        d.focus_set()
        d.grab_set()
        self._center_over(d, 640, 420)

        frm = ttk.Frame(d)
        frm.pack(fill="both", expand=True, padx=8, pady=6)
        cols = ("name", "url")
        tree = ttk.Treeview(frm, columns=cols, show="headings", height=10)
        tree.heading("name", text="名称")
        tree.heading("url", text="订阅地址")
        tree.column("name", width=140)
        tree.column("url", width=440)
        vsb = ttk.Scrollbar(frm, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        def _fill():
            for iid in tree.get_children():
                tree.delete(iid)
            for i, s in enumerate(subs):
                tree.insert("", "end", iid=str(i), values=(s.get("name", ""), s.get("url", "")))

        _fill()

        btns = ttk.Frame(d)
        btns.pack(fill="x", padx=8, pady=6)
        en_name = ttk.Entry(btns, width=14)
        en_url = ttk.Entry(btns)
        en_name.pack(side="left")
        en_url.pack(side="left", fill="x", expand=True, padx=(4, 4))

        def _add():
            name = en_name.get().strip() or "未命名订阅"
            url = en_url.get().strip()
            if not url:
                messagebox.showwarning("缺少地址", "请输入订阅地址。", parent=d)
                return
            if any(s["url"] == url for s in subs):
                messagebox.showinfo("已存在", "这个地址已经在列表里了。", parent=d)
                return
            subs.append({"name": name, "url": url})
            self._save_subs(subs)
            en_name.delete(0, "end"); en_url.delete(0, "end")
            _fill()

        def _del():
            sel = tree.selection()
            if not sel:
                return
            keep = [s for i, s in enumerate(subs) if str(i) not in sel]
            subs[:] = keep
            self._save_subs(subs)
            _fill()

        def _rename():
            sel = tree.selection()
            if not sel:
                return
            i = int(sel[0])
            new = messagebox.askstring("重命名", "新的订阅名称:", parent=d,
                                       initialvalue=subs[i].get("name", ""))
            if new and new.strip():
                subs[i]["name"] = new.strip()
                self._save_subs(subs)
                _fill()

        def _test():
            sel = tree.selection()
            if not sel:
                messagebox.showwarning("未选择", "先在列表里选中一个订阅。", parent=d)
                return
            s = subs[int(sel[0])]
            d.destroy()
            self.status.config(text="正在下载订阅…")
            self.root.update()
            self._import(s["url"], s.get("name", "订阅"))

        ttk.Button(btns, text="加入", command=_add).pack(side="left")
        ttk.Button(btns, text="重命名", command=_rename).pack(side="left", padx=(4, 0))
        ttk.Button(btns, text="删除选中", command=_del).pack(side="left", padx=(4, 0))
        ttk.Button(btns, text="测这个订阅", command=_test).pack(side="right")
        d.bind("<Return>", lambda _ev: _add())

    # ---------------- 导入 ----------------
    def _import(self, src, label):
        try:
            ns = load_proxies(src)
        except Exception as e:
            messagebox.showerror("导入失败", f"{type(e).__name__}: {e}")
            return
        if not ns:
            messagebox.showwarning("没有节点", "未能从该来源解析出任何节点。")
            return
        self.nodes = ns
        self.results = {}
        self.src_var.set(f"{label}: {len(ns)} 个节点")
        self.refresh_table()
        self.status.config(text=f"已导入 {len(ns)} 个节点")

    def load_file(self):
        p = filedialog.askopenfilename(filetypes=[
            ("配置文件", "*.yaml *.yml *.json *.txt *.conf"),
            ("所有文件", "*.*")])
        if p:
            self._import(p, os.path.basename(p))

    def load_url(self):
        d = tk.Toplevel(self.root)
        d.title("从订阅 URL 导入")
        d.geometry("460x120")
        d.transient(self.root)          # 随主窗口最小化/置顶
        d.focus_set()
        d.grab_set()                    # 模态：必须先关掉它才能点主窗口
        self._center_over(d, 460, 120)
        ttk.Label(d, text="订阅地址:").pack(pady=8)
        e = ttk.Entry(d, width=58)
        e.pack(padx=10)
        e.focus()
        def ok():
            url = e.get().strip()
            if not url:
                return
            d.destroy()
            self.status.config(text="正在下载订阅…")
            self.root.update()
            self._import(url, url[:48])
        ttk.Button(d, text="导入", command=ok).pack(pady=10)
        d.bind("<Return>", lambda _ev: ok())

    def _center_over(self, d, w, h):
        """把子窗口 d 放在主窗口正中央（不超出屏幕）。"""
        self.root.update_idletasks()
        rx = self.root.winfo_x(); ry = self.root.winfo_y()
        rw = self.root.winfo_width(); rh = self.root.winfo_height()
        x = rx + (rw - w) // 2
        y = ry + (rh - h) // 2
        sw = d.winfo_screenwidth(); sh = d.winfo_screenheight()
        x = max(0, min(x, sw - w)); y = max(0, min(y, sh - h))
        d.geometry(f"{w}x{h}+{x}+{y}")

    def load_text(self):
        d = tk.Toplevel(self.root)
        d.title("粘贴节点数据")
        d.geometry("620x360")
        d.transient(self.root)
        d.focus_set()
        d.grab_set()
        self._center_over(d, 620, 360)
        ttk.Label(d, text="粘贴 Clash YAML / sing-box JSON / 节点文本:").pack(pady=6)
        t = tk.Text(d, width=80, height=18)
        t.pack(fill="both", expand=True, padx=8)
        def ok():
            txt = t.get("1.0", "end")
            d.destroy()
            self._import(txt, "粘贴的文本")
        ttk.Button(d, text="导入", command=ok).pack(pady=8)
        d.bind("<Return>", lambda _ev: ok())

    def load_clipboard(self):
        try:
            txt = self.root.clipboard_get()
        except Exception:
            messagebox.showwarning("剪贴板为空", "剪贴板里没有内容。")
            return
        self._import(txt, "剪贴板")

    # ---------------- 测速 ----------------
    def start_test(self):
        if not self.nodes:
            messagebox.showwarning("没有节点", "请先导入节点。")
            return
        if self.running:
            return
        self.running = True
        self.results = {}
        # 主线程里把参数快照下来，工作线程不准碰 Tkinter 变量
        self._params = {"concurrency": self.conc_var.get(),
                        "timeout_lat": self.latto_var.get(),
                        "timeout_dl": self.dlto_var.get(),
                        "dl_bytes": self.dlkb_var.get() * 1000}
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.prog["maximum"] = len(self.nodes)
        self.prog["value"] = 0
        self.refresh_table()
        self._set_status("正在启动测速内核…")
        threading.Thread(target=self._worker, daemon=True).start()

    def _set_status(self, text):
        """工作线程通过这个改状态栏（转发到主线程）。"""
        try:
            self.root.after(0, lambda: self.status.config(text=text))
        except RuntimeError:
            pass  # 窗口已关

    def stop_test(self):
        if self.tester:
            self.tester.stop()
            self._set_status("已请求停止…")
        self.btn_stop.config(state="disabled")

    def _worker(self):
        """工作线程：启 mihomo -> 并发测速 -> 回收。"""
        try:
            p = self._params
            self.tester = SpeedTester(concurrency=p["concurrency"],
                                     timeout_lat=p["timeout_lat"],
                                     timeout_dl=p["timeout_dl"],
                                     dl_bytes=p["dl_bytes"])
            self.tester.start(self.nodes)
            self._set_status("测速中…")
            self.tester.run(self.nodes, on_result=self._on_result)
            self._set_status(f"完成：{len(self.results)}/{len(self.nodes)} 已测")
        except Exception as e:
            self._set_status(f"出错: {type(e).__name__} {str(e)[:60]}")
        finally:
            if self.tester:
                self.tester.stop()
                self.tester = None
            self.running = False
            self.root.after(0, lambda: self.btn_start.config(state="normal"))
            self.root.after(0, lambda: self.btn_stop.config(state="disabled"))

    def _on_result(self, r):
        self.results[r["name"]] = r
        self.root.after(0, self.refresh_table)

    def clear_results(self):
        self.results = {}
        self.refresh_table()
        self.status.config(text="已清空")

    # ---------------- 表格 ----------------
    def _row_order(self):
        """返回 (索引, 节点) 的当前显示顺序（按排序列排）。"""
        items = list(range(len(self.nodes)))
        col, rev = self.sort_col, self.sort_rev
        if not col:
            return list(enumerate(self.nodes))

        def key(i):
            n, r = self.nodes[i], self.results.get(self.nodes[i]["name"], {})
            if col == "latency":
                return r.get("latency") if r.get("latency") is not None else 10**9
            if col == "dl":
                return r.get("dl_mbps") if r.get("dl_mbps") is not None else -1.0
            if col == "alive":
                return 0 if r.get("alive") else 1
            if col == "idx":
                return i
            if col == "name":
                return self.nodes[i]["name"]
            return str(n.get(col, "") or "")

        return sorted(((i, self.nodes[i]) for i in items), key=lambda p: key(p[0]), reverse=rev)

    def sort_by(self, col):
        if self.sort_col == col:
            self.sort_rev = not self.sort_rev
        else:
            self.sort_col, self.sort_rev = col, False
        for c in ("idx", "name", "type", "server", "port", "tls", "alive", "latency", "dl", "err"):
            arrow = ""
            if c == col:
                arrow = " ▼" if self.sort_rev else " ▲"
            base = {"idx": "#", "name": "节点名", "type": "类型", "server": "服务器",
                    "port": "端口", "tls": "TLS", "alive": "可用",
                    "latency": "延时(ms)", "dl": "下载(MB/s)", "err": "错误"}[c]
            self.tree.heading(c, text=base + arrow)
        self.refresh_table()

    def refresh_table(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for display_pos, (i, n) in enumerate(self._row_order(), start=1):
            name = n["name"]
            r = self.results.get(name, {})
            alive = r.get("alive")
            vals = (display_pos, name, n.get("type", ""), n.get("server", ""), n.get("port", ""),
                    "是" if n.get("tls") else "",
                    "✓" if alive else ("✗" if alive is False else "…"),
                    r.get("latency") if r.get("latency") is not None else "",
                    r.get("dl_mbps") if r.get("dl_mbps") is not None else "",
                    r.get("err") or "")
            iid = self.tree.insert("", "end", iid=str(i), values=vals)
            if alive is True:
                self.tree.item(iid, tags=("ok",))
            elif alive is False:
                self.tree.item(iid, tags=("bad",))
        self.tree.tag_configure("ok", foreground="#1a7f37")
        self.tree.tag_configure("bad", foreground="#b3261e")
        self.prog["value"] = len(self.results)
        self.prog["maximum"] = max(len(self.nodes), 1)

    def _selected_nodes(self):
        sel = self.tree.selection()
        out = []
        for iid in sel:
            idx = int(iid)
            if 0 <= idx < len(self.nodes):
                out.append(self.nodes[idx])
        return out

    def popup_menu(self, event):
        if self.tree.identify_row(event.y):
            self.menu.post(event.x_root, event.y_root)

    def copy_names(self):
        ns = self._selected_nodes() or self.nodes
        self._copy("\n".join(n["name"] for n in ns))

    def copy_server_port(self):
        ns = self._selected_nodes() or self.nodes
        self._copy("\n".join(f"{n.get('server','')}:{n.get('port','')}" for n in ns))

    def copy_rows_json(self):
        ns = self._selected_nodes() or self.nodes
        rows = []
        for n in ns:
            r = dict(self.results.get(n["name"], {}))
            r["node"] = {k: v for k, v in n.items() if k != "_idx"}
            rows.append(r)
        self._copy(json.dumps(rows, ensure_ascii=False, indent=2))

    def show_detail(self, event=None):
        ns = self._selected_nodes()
        if not ns:
            return
        n = ns[0]
        r = self.results.get(n["name"], {})
        d = tk.Toplevel(self.root)
        d.title("节点详情")
        d.geometry("560x420")
        t = tk.Text(d, width=70, height=22, wrap="word")
        t.pack(fill="both", expand=True, padx=8, pady=8)
        payload = {"节点配置": {k: v for k, v in n.items() if k != "_idx"},
                   "测速结果": r}
        t.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2))
        t.config(state="disabled")
        ttk.Button(d, text="复制 JSON", command=lambda: self._copy(json.dumps(payload, ensure_ascii=False, indent=2))).pack(pady=6)

    # ---------------- 导出 ----------------
    def _export_nodes(self):
        """选中了行就只导出选中的（按当前排序顺序）；否则导出全部可用节点（按速度排序）。"""
        sel = self.tree.selection()
        if sel:
            idxs = sorted((int(iid) for iid in sel), key=lambda i: [p[0] for p in self._row_order()].index(i))
            return [self.nodes[i] for i in idxs]
        return [n for n, _ in self._alive_sorted()]

    def _alive_sorted(self):
        rows = []
        for n in self.nodes:
            r = self.results.get(n["name"])
            if r and r.get("alive"):
                rows.append((n, r))
        rows.sort(key=lambda x: (x[1].get("dl_mbps") is None,
                                 -(x[1].get("dl_mbps") or 0),
                                 x[1].get("latency") or 10**9))
        return rows

    def _save(self, content, default_name):
        p = filedialog.asksaveasfilename(defaultextension=os.path.splitext(default_name)[1],
                                         initialfile=default_name,
                                         filetypes=[("全部文件", "*.*")],
                                         parent=self.root)
        if not p:
            return None
        mode = "wb" if isinstance(content, bytes) else "w"
        kw = {"encoding": "utf-8"} if mode == "w" else {}
        with open(p, mode, **kw) as f:
            f.write(content)
        self.status.config(text=f"已导出: {p}")
        return p

    def exp_clash(self):
        ns = self._export_nodes()
        if not ns:
            messagebox.showwarning("无可用节点", "没有可导出的存活节点。请先选中行或完成测速。")
            return
        self._save(to_clash_yaml(ns), "speedtest.yaml")

    def exp_singbox(self):
        ns = self._export_nodes()
        if not ns:
            messagebox.showwarning("无可用节点", "没有可导出的存活节点。请先选中行或完成测速。")
            return
        self._save(to_singbox_json(ns), "speedtest.json")

    def exp_base64(self):
        ns = self._export_nodes()
        if not ns:
            messagebox.showwarning("无可用节点", "没有可导出的存活节点。请先选中行或完成测速。")
            return
        self._save(to_base64_links(ns), "speedtest.txt")

    def exp_csv(self):
        self._save(to_csv(self.nodes, self.results), "speedtest.csv")

    def exp_md(self):
        self._save(to_markdown(self.nodes, self.results), "speedtest.md")

    def copy_md(self):
        self._copy(to_markdown(self.nodes, self.results))

    def _copy(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status.config(text="已复制到剪贴板")


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        style.theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
