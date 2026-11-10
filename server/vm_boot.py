"""What the bridge's wake runs in the Colab VM's Jupyter kernel.

The bridge uploads /content/colab_yoga_server.py and /content/colab_kernel_runners.json,
then sends this file as code. It is plain standalone Python: install what is missing, stop
an older server, start the new one detached, and print WAKE_URL:<url> or WAKE_ERROR:<why>.
"""
import importlib.util
import os
import re
import subprocess
import sys
import time

SERVER = "/content/colab_yoga_server.py"
LOG = "/content/colab_yoga_server.log"
WAIT_SECONDS = 180

# module -> pip package. Colab preinstalls most of these; only the missing ones are installed.
# gradio is here for its share tunnel (frpc), not for any UI.
DEPENDENCIES = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "pydantic": "pydantic",
    "requests": "requests",
    "gradio": "gradio",
}


def log_tail(chars=600):
    try:
        with open(LOG, "r", encoding="utf-8", errors="replace") as f:
            return f.read()[-chars:]
    except OSError:
        return "no log"


def main():
    missing = [pkg for mod, pkg in DEPENDENCIES.items() if importlib.util.find_spec(mod) is None]
    if missing:
        print("正在安装缺失依赖: " + " ".join(missing), flush=True)
        done = subprocess.run([sys.executable, "-m", "pip", "install", "-q"] + missing,
                              capture_output=True, text=True)
        if done.returncode != 0:
            print("WAKE_ERROR: 依赖安装失败: " + (done.stdout + done.stderr)[-600:], flush=True)
            return

    if not os.path.exists(SERVER):
        print("WAKE_ERROR: 缺少 " + SERVER + "，bridge 没有上传服务脚本", flush=True)
        return

    # Only one server may run (one idle watchdog). Never touch llama-server or the MCP
    # forwarder: they must outlive a redeploy. frpc is the old server's gradio tunnel,
    # orphaned when its python is killed.
    # A pre-rename EasyColab app.py (same :7860, its own watchdog) counts as an older server.
    subprocess.run(["pkill", "-f", "python.*/content/colab_yoga_server.py"], check=False)
    subprocess.run(["pkill", "-f", "python.*/content/app.py"], check=False)
    for _ in range(20):
        if subprocess.run(["pgrep", "-f", "python.*/content/(colab_yoga_server|app).py"],
                          capture_output=True).returncode != 0:
            break
        time.sleep(0.5)
    subprocess.run(["pkill", "-f", "frpc_linux"], check=False)
    time.sleep(1)

    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    with open(LOG, "w", encoding="utf-8", newline="\n") as out:
        proc = subprocess.Popen([sys.executable, "-u", SERVER], cwd="/content", env=env,
                                stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)

    url_re = re.compile(r"COLAB_YOGA_URL=(https://\S+)")
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        time.sleep(2)
        try:
            with open(LOG, "r", encoding="utf-8", errors="replace") as f:
                m = url_re.search(f.read())
        except OSError:
            m = None
        if m:
            print("WAKE_URL:" + m.group(1), flush=True)
            return
        if proc.poll() is not None:
            print("WAKE_ERROR: 服务进程已退出 (exit=%s)，日志尾部: %s" % (proc.returncode, log_tail()), flush=True)
            return
    print("WAKE_ERROR: 启动超时，日志尾部: " + log_tail(), flush=True)


main()
