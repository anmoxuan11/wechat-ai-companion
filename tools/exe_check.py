# -*- coding: utf-8 -*-
"""打包产物端到端验证：跑 dist 里的 exe，确认它起来的是「项目目录」而不是 _internal。

用法：python tools/exe_check.py
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(BASE, "dist", "微信AI陪聊控制面板", "微信AI陪聊控制面板.exe")
PORT = 8765
URL = "http://127.0.0.1:%d" % PORT

passed, failed = [], []


def check(name, cond, extra=""):
    (passed if cond else failed).append(name)
    print("  %s  %s  %s" % ("PASS" if cond else "FAIL", name, extra if not cond else ""))


def http(method, path, headers=None, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


def wait_port(timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = socket.socket()
        s.settimeout(0.5)
        ok = s.connect_ex(("127.0.0.1", PORT)) == 0
        s.close()
        if ok:
            return True
        time.sleep(0.4)
    return False


def main():
    if not os.path.isfile(EXE):
        print("找不到 exe：%s" % EXE)
        return 1
    print("exe: %s (%.1f MB)" % (EXE, os.path.getsize(EXE) / 1048576.0))

    env = dict(os.environ)
    env["BROWSER"] = os.path.join(BASE, "scripts", "tools_noop_browser.bat")
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
        env["DEEPSEEK_API_KEY"] = winreg.QueryValueEx(k, "DEEPSEEK_API_KEY")[0]
    except Exception:
        pass

    print("启动 exe ...")
    subprocess.Popen([EXE], cwd=os.path.dirname(EXE), env=env)

    if not wait_port():
        print("面板未能在 40 秒内就绪")
        return 1

    try:
        st, body = http("GET", "/api/identity")
        ident = json.loads(body) if st == 200 else {}
        check("面板已起来", st == 200, st)
        bd = (ident.get("base_dir") or "").rstrip("\\/")
        check("项目目录解析正确（不是 _internal）",
              bd.lower() == BASE.lower() and "_internal" not in bd.lower(), bd)
        check("版本 3.0", str(ident.get("version", "")).startswith("3"), ident)

        st, html = http("GET", "/")
        check("首页 200", st == 200, st)
        check("HTML 注入令牌", 'name="panel-token"' in html)
        token = ""
        m = 'name="panel-token" content="'
        if m in html:
            token = html.split(m, 1)[1].split('"', 1)[0]

        st, css = http("GET", "/assets/style.css")
        check("打包内能取到 style.css", st == 200, st)
        check("style.css 内容完整（含 :root）", ":root" in css, len(css))

        st, js = http("GET", "/assets/app.js")
        check("打包内能取到 app.js", st == 200, st)
        check("app.js 含令牌逻辑", "X-Panel-Token" in js)

        # ---- 3.0：打包体里必须带得完成新模块，否则面板在 exe 模式下缺功能 ----
        st, extra = http("GET", "/assets/extra.js")
        check("打包内能取到 extra.js", st == 200, st)
        check("extra.js 含三个新标签页逻辑",
              "memory_status" in extra and "tone_dials" in extra)

        for path, key in (("/api/insights", "msgs_stored"),
                          ("/api/cost", "today"),
                          ("/api/health", "score"),
                          ("/api/events", "events"),
                          ("/api/memory_status", "pending_count"),
                          ("/api/facts", "facts"),
                          ("/api/guard", "guard"),
                          ("/api/tone_dials", "dials")):
            st, b2 = http("GET", path)
            j2 = json.loads(b2) if st == 200 else {}
            check("exe 下 GET %s 可用" % path, st == 200 and key in j2,
                  "%s %s" % (st, b2[:100]))

        st, _ = http("POST", "/api/pause", body={})
        check("无令牌被拒 403", st == 403, st)

        st, s = http("GET", "/api/status")
        stt = json.loads(s) if st == 200 else {}
        check("状态接口可用", st == 200, st)
        check("密钥走环境变量", "环境变量" in str(stt.get("api_key_source", "")),
              stt.get("api_key_source"))

        if token:
            st, _ = http("POST", "/api/shutdown",
                         headers={"X-Panel-Token": token}, body={"confirm": True})
            check("shutdown 200", st == 200, st)
            for _ in range(30):
                s2 = socket.socket()
                s2.settimeout(0.5)
                if s2.connect_ex(("127.0.0.1", PORT)) != 0:
                    s2.close()
                    break
                s2.close()
                time.sleep(0.2)
            s3 = socket.socket()
            s3.settimeout(0.5)
            down = s3.connect_ex(("127.0.0.1", PORT)) != 0
            s3.close()
            check("面板已退出（端口释放）", down)
    finally:
        pass

    print("\n通过 %d，失败 %d" % (len(passed), len(failed)))
    for f in failed:
        print("  - " + f)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
