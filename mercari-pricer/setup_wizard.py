"""メルカリ自動値下げシステム — セットアップウィザード (PyInstaller .exe entry)

お客様が .exe をダブルクリックすると、この main() が走る。
PowerShell やコマンドラインは一切表示せず、Chrome を起動してメルカリ
ログイン画面を表示。ログイン完了を検知したら、Windows Task Scheduler に
日次タスクを登録し、完了ダイアログを出して終了する。

ビルド方法 (Windows + PyInstaller):
    pyinstaller --noconfirm --onefile --windowed --name "メルカリ自動値下げ_セットアップ" setup_wizard.py
"""
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import messagebox
except Exception:
    tk = None
    messagebox = None

# --- Selenium は ImportError 防止のため遅延 import ---

APP_NAME = "メルカリ自動値下げシステム"
INSTALL_DIR = Path(os.path.expandvars(r"%LOCALAPPDATA%\skeleton-mercari"))
PROFILE_DIR = INSTALL_DIR / "chrome_profile"
CHROMEDRIVER_DIR = INSTALL_DIR / "chromedriver"
SCRIPTS_DIR = INSTALL_DIR / "scripts"
LOGIN_URL = "https://jp.mercari.com/login"


def show_error(title: str, message: str):
    if messagebox:
        messagebox.showerror(title, message)
    else:
        print(f"[ERROR] {title}: {message}", file=sys.stderr)


def show_info(title: str, message: str):
    if messagebox:
        messagebox.showinfo(title, message)
    else:
        print(f"[INFO] {title}: {message}")


def find_chrome() -> str:
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return ""


def chrome_version(chrome_path: str) -> str:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Item '{chrome_path}').VersionInfo.ProductVersion"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def download_chromedriver(version: str) -> str:
    """chrome-for-testing から chromedriver-win64.zip を取得し展開、exe パスを返す。"""
    cache = CHROMEDRIVER_DIR / version
    cache.mkdir(parents=True, exist_ok=True)
    exe = cache / "chromedriver.exe"
    if exe.exists() and exe.stat().st_size > 0:
        return str(exe)

    with urllib.request.urlopen(
        "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json",
        timeout=30,
    ) as r:
        data = json.loads(r.read())

    major = version.split(".")[0]
    exact = [v for v in data.get("versions", []) if v.get("version") == version]
    same_major = [v for v in data.get("versions", []) if v.get("version", "").split(".")[0] == major]
    candidates = exact + sorted(same_major, key=lambda v: v.get("version", ""), reverse=True)

    url = None
    for v in candidates:
        for d in (v.get("downloads") or {}).get("chromedriver", []):
            if d.get("platform") == "win64":
                url = d.get("url")
                break
        if url:
            break
    if not url:
        raise RuntimeError(f"chrome-for-testing に Chrome {version} 対応の chromedriver が見つからない")

    with urllib.request.urlopen(url, timeout=120) as r:
        zb = r.read()
    with zipfile.ZipFile(io.BytesIO(zb)) as z:
        for name in z.namelist():
            if name.endswith("chromedriver.exe"):
                with z.open(name) as src, open(exe, "wb") as dst:
                    dst.write(src.read())
                break
    if not exe.exists():
        raise RuntimeError("chromedriver.exe を zip から抽出できなかった")
    return str(exe)


def find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_devtools(port: int, timeout: float = 20.0):
    url = f"http://127.0.0.1:{port}/json/version"
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"Chrome DevTools port {port} が応答しない")


def register_scheduled_task(exe_path: str):
    """Windows Task Scheduler に日次タスクを登録 (毎朝5時)。"""
    task_name = "SkeletonMercariPricer-Daily"
    try:
        subprocess.run(
            ["schtasks", "/Delete", "/TN", task_name, "/F"],
            capture_output=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception:
        pass
    cmd = [
        "schtasks", "/Create", "/TN", task_name,
        "/TR", f'"{exe_path}" --daily',
        "/SC", "DAILY", "/ST", "05:00",
        "/RL", "HIGHEST", "/F",
    ]
    subprocess.run(
        cmd, check=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


def disable_sleep():
    """powercfg でスリープと画面OFFを無効化。"""
    cmds = [
        ["powercfg", "/change", "standby-timeout-ac", "0"],
        ["powercfg", "/change", "standby-timeout-dc", "0"],
        ["powercfg", "/change", "monitor-timeout-ac", "0"],
        ["powercfg", "/change", "monitor-timeout-dc", "0"],
        ["powercfg", "/change", "hibernate-timeout-ac", "0"],
        ["powercfg", "/change", "hibernate-timeout-dc", "0"],
    ]
    for c in cmds:
        try:
            subprocess.run(
                c, capture_output=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except Exception:
            pass


def setup_mode():
    """セットアップ初回モード: Chrome起動 → メルカリログイン待機 → タスク登録。"""
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    chrome = find_chrome()
    if not chrome:
        show_error(
            APP_NAME,
            "Google Chrome がインストールされていません。\n\n"
            "https://www.google.com/intl/ja/chrome/ から先にダウンロードして"
            "インストールしてください。",
        )
        return 1

    cver = chrome_version(chrome)
    if not cver:
        show_error(APP_NAME, "Chrome のバージョン検出に失敗しました")
        return 1

    try:
        driver_path = download_chromedriver(cver)
    except Exception as e:
        show_error(APP_NAME, f"ChromeDriver のダウンロードに失敗しました:\n{e}")
        return 1

    # 既存 chrome 殺す (プロファイルロック回避)
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "chrome.exe"],
            capture_output=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception:
        pass
    time.sleep(0.5)

    port = find_free_port()
    chrome_args = [
        chrome,
        f"--user-data-dir={PROFILE_DIR}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=ChromeWhatsNewUI",
        LOGIN_URL,
    ]
    chrome_proc = subprocess.Popen(
        chrome_args,
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008) if sys.platform == "win32" else 0,
    )

    try:
        wait_devtools(port, 20.0)
    except Exception as e:
        show_error(APP_NAME, f"Chrome の起動に失敗しました: {e}")
        return 1

    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    options = webdriver.ChromeOptions()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    driver = webdriver.Chrome(service=Service(executable_path=driver_path), options=options)

    # ログイン待ち案内ダイアログ (非ブロッキングっぽくするため tkinter で別ウィンドウ)
    if tk:
        root = tk.Tk()
        root.title(APP_NAME)
        root.geometry("520x280")
        root.configure(bg="#0d1a2e")
        root.attributes("-topmost", True)
        tk.Label(root, text="メルカリにログインしてください", bg="#0d1a2e", fg="#b89968",
                 font=("Yu Mincho", 16, "bold")).pack(pady=24)
        tk.Label(root,
                 text="Chrome ウィンドウで:\n\n"
                      "1. メールアドレス入力\n"
                      "2. パスワード入力\n"
                      "3. SMS 認証コードを入力\n\n"
                      "ログイン完了 (マイページ表示) を検知すると\n"
                      "自動でこの画面が閉じます。",
                 bg="#0d1a2e", fg="#ffffff", font=("Yu Gothic", 11), justify="left").pack(pady=8)
        root.update()
    else:
        root = None

    # 10 分間ログインを待つ (10秒ごとに URL チェック)
    logged_in = False
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            cur = driver.current_url
            if "/mypage" in cur or "/jp.mercari.com/?" in cur or (
                "mercari.com" in cur and "/login" not in cur and "/account/" not in cur and cur != LOGIN_URL
            ):
                logged_in = True
                break
        except Exception:
            break
        if root:
            try:
                root.update()
            except Exception:
                root = None
        time.sleep(3)

    if root:
        try:
            root.destroy()
        except Exception:
            pass

    if not logged_in:
        show_error(APP_NAME,
                   "ログインが検知されませんでした。\n\n"
                   "もう一度このプログラムを起動してログインし直してください。\n"
                   "問題が続く場合はサポート (support@skeleton.example) までご連絡ください。")
        try:
            driver.quit()
        except Exception:
            pass
        return 1

    # ログイン状態確認できたので、Cookie をプロファイルに保存させてから Chrome を閉じる
    time.sleep(1)
    try:
        driver.quit()
    except Exception:
        pass
    try:
        chrome_proc.terminate()
    except Exception:
        pass

    # 自身の .exe パスを取得 (PyInstaller --onefile では sys.executable)
    if getattr(sys, "frozen", False):
        exe_path = sys.executable
    else:
        exe_path = sys.argv[0]

    # SCRIPTS_DIR に .exe をコピー (再配置されても安定動作させるため)
    installed_exe = SCRIPTS_DIR / "mercari-pricer.exe"
    try:
        import shutil
        if Path(exe_path).resolve() != installed_exe.resolve():
            shutil.copy2(exe_path, installed_exe)
    except Exception:
        installed_exe = Path(exe_path)

    try:
        register_scheduled_task(str(installed_exe))
    except Exception as e:
        show_error(APP_NAME, f"自動実行スケジュールの登録に失敗:\n{e}\n手動で再実行してください。")
        return 1

    disable_sleep()

    show_info(
        APP_NAME,
        "✓ セットアップが完了しました\n\n"
        "明朝 5:00 から自動でメルカリの価格更新が開始されます。\n\n"
        "今後はこのプログラムを起動する必要はありません。",
    )
    return 0


def daily_mode():
    """日次実行モード: 価格更新を実行 (Task Scheduler から起動される)。"""
    # ここで bundled price_update のロジックを実行する。
    # PyInstaller でバンドルする際 price_update.py 相当を import する。
    try:
        import price_update_bundled  # ビルド時に同梱
        price_update_bundled.main()
    except Exception as e:
        # daily 実行は UI 出さずログだけ
        logd = INSTALL_DIR / "logs"
        logd.mkdir(parents=True, exist_ok=True)
        with open(logd / "daily_error.log", "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {e}\n")
    return 0


def main():
    if "--daily" in sys.argv:
        return daily_mode()
    return setup_mode()


if __name__ == "__main__":
    sys.exit(main())
