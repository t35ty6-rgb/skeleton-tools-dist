"""共通ユーティリティ: WebDriver セットアップ、ログイン判定、LINE通知、状態ファイル。

各PCで以下の env を設定する想定:
  ACCOUNT_NAME       例: "ちひろ" / "とくさ" / "ひかる" / "かつま"
  USER_DATA_DIR      Chrome プロファイル保存先 (例: ~/Desktop/selenium_chrome_profile)
  LINE_TOKEN         (任意) LINE Notify トークン。設定するとエラー時に通知。
  STATE_DIR          (任意) 状態ファイル置き場。デフォルト ~/.skeleton-mercari-relist
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.by import By

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def env_required(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.stderr.write(f"FATAL: env {name} が未設定\n")
        sys.exit(1)
    return v


def account_name() -> str:
    return env_required("ACCOUNT_NAME")


def user_data_dir() -> str:
    """Chrome プロファイルのディレクトリパスを返す。

    Windowsでは Desktop が OneDrive 同期されている環境が多く、OneDrive が
    プロファイルファイルを掴むと Chrome が起動直後に exit する既知バグがある。
    そのため Windows では LOCALAPPDATA (絶対に同期されない場所) を強制的に使う。
    Mac/Linux はこれまで通り ~/Desktop/selenium_chrome_profile。
    """
    if sys.platform == "win32":
        # Windows: LOCALAPPDATA配下に固定。env変数が Desktop/OneDrive を指してても無視
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        default = os.path.join(base, "skeleton-mercari", "chrome_profile")
        override = os.environ.get("USER_DATA_DIR", "")
        if override:
            low = override.lower()
            # OneDrive 同期される可能性のあるパスは却下してデフォルトを使う
            if "desktop" in low or "onedrive" in low or "ドキュメント" in low:
                return default
            return os.path.expanduser(override)
        return default
    # Mac/Linux
    return os.path.expanduser(os.environ.get("USER_DATA_DIR", "~/Desktop/selenium_chrome_profile"))


def state_dir() -> Path:
    p = Path(os.path.expanduser(os.environ.get("STATE_DIR", "~/.skeleton-mercari-relist")))
    p.mkdir(parents=True, exist_ok=True)
    (p / "logs").mkdir(exist_ok=True)
    (p / "photos").mkdir(exist_ok=True)
    (p / "snapshots").mkdir(exist_ok=True)
    return p


def log_path(tool: str) -> Path:
    today = datetime.now().strftime("%Y%m%d")
    return state_dir() / "logs" / f"{tool}_{account_name()}_{today}.log"


def log(tool: str, line: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = f"[{ts}] {line}"
    print(msg, flush=True)
    try:
        with open(log_path(tool), "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def notify_line(message: str) -> None:
    """LINE Notify でメッセージ送信。LINE_TOKEN 未設定なら何もしない。"""
    token = os.environ.get("LINE_TOKEN")
    if not token:
        return
    try:
        requests.post(
            "https://notify-api.line.me/api/notify",
            headers={"Authorization": f"Bearer {token}"},
            data={"message": f"\n[{account_name()}] {message}"},
            timeout=10,
        )
    except Exception:
        pass


def _get_chrome_version(chrome_path: str) -> str:
    """インストールされてる Chrome のバージョン文字列を返す (例: '148.0.7778.97')。"""
    if sys.platform == "win32":
        # Windowsでは --version が動かないので registry/wmic/PowerShell
        try:
            r = subprocess.run(
                ["powershell", "-Command", f"(Get-Item '{chrome_path}').VersionInfo.ProductVersion"],
                capture_output=True, text=True, timeout=10,
            )
            return r.stdout.strip()
        except Exception:
            return ""
    else:
        try:
            r = subprocess.run([chrome_path, "--version"], capture_output=True, text=True, timeout=10)
            # 出力例: "Google Chrome 148.0.7778.97"
            return r.stdout.strip().split()[-1]
        except Exception:
            return ""


def _download_matching_chromedriver(chrome_version: str) -> str:
    """chrome-for-testing から chrome_version に対応する chromedriver を取得してパスを返す。

    キャッシュ: ~/.skeleton-mercari/chromedriver/<version>/chromedriver(.exe)
    """
    cache_root = Path(os.path.expanduser("~/.skeleton-mercari/chromedriver")) / chrome_version
    cache_root.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        exe_name = "chromedriver.exe"
        platform_key = "win64"
    elif sys.platform == "darwin":
        exe_name = "chromedriver"
        import platform
        platform_key = "mac-arm64" if platform.machine() == "arm64" else "mac-x64"
    else:
        exe_name = "chromedriver"
        platform_key = "linux64"

    cached = cache_root / exe_name
    if cached.exists() and cached.stat().st_size > 0:
        return str(cached)

    # 完全一致が無い場合は同 major のメジャー版を探す
    # API: https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json
    print(f"[chromedriver] {chrome_version} に対応するドライバをDL中...", flush=True)
    try:
        with urllib.request.urlopen(
            "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json",
            timeout=30,
        ) as r:
            data = json.loads(r.read())
    except Exception as e:
        raise RuntimeError(f"chrome-for-testing バージョン一覧の取得失敗: {e}")

    versions = data.get("versions", [])
    # 完全一致 → 同マイナー版 → 同メジャー版の最新 の順でフォールバック
    major = chrome_version.split(".")[0]
    candidates = []
    exact = [v for v in versions if v.get("version") == chrome_version]
    same_major = [v for v in versions if v.get("version", "").split(".")[0] == major]
    candidates = exact + sorted(same_major, key=lambda v: v.get("version", ""), reverse=True)

    url = None
    chosen_ver = None
    for v in candidates:
        downloads = (v.get("downloads") or {}).get("chromedriver", [])
        for d in downloads:
            if d.get("platform") == platform_key:
                url = d.get("url")
                chosen_ver = v.get("version")
                break
        if url:
            break

    if not url:
        raise RuntimeError(f"chrome-for-testing に Chrome {chrome_version} (major={major}) 用 {platform_key} chromedriver が無い")

    print(f"[chromedriver] DL: {url} (chrome {chrome_version} → driver {chosen_ver})", flush=True)
    import zipfile
    import io
    with urllib.request.urlopen(url, timeout=120) as r:
        zip_bytes = r.read()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            if name.endswith(exe_name):
                with z.open(name) as src, open(cached, "wb") as dst:
                    dst.write(src.read())
                if not sys.platform == "win32":
                    os.chmod(cached, 0o755)
                break
    if not cached.exists():
        raise RuntimeError(f"chromedriver.exe が zip から抽出できない: {url}")
    print(f"[chromedriver] ✅ 保存: {cached}", flush=True)
    return str(cached)


def _find_chrome_binary() -> str:
    """Chrome 本体のフルパスを返す。見つからなければ FileNotFoundError。"""
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        candidates = ["/usr/bin/google-chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium"]
    for c in candidates:
        if os.path.exists(c):
            return c
    raise FileNotFoundError(f"Chrome本体が見つからない (試行: {candidates})")


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_devtools_ready(port: int, timeout_sec: float = 20.0) -> None:
    """Chrome の DevTools エンドポイントが listening になるまで待つ。"""
    url = f"http://127.0.0.1:{port}/json/version"
    deadline = time.time() + timeout_sec
    last_err = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception as e:
            last_err = e
            time.sleep(0.3)
    raise RuntimeError(f"Chrome DevTools port {port} が {timeout_sec}秒以内に開かなかった (最後のエラー: {last_err})")


def make_driver(headless: bool = False) -> webdriver.Chrome:
    """Chrome を起動して WebDriver を返す。

    起動方式は2段構え:
      ① 通常: chromedriver に Chrome 起動を任せる (Selenium Manager 経由)
      ② フォールバック: 自分で Chrome を subprocess 起動 → debuggerAddress でアタッチ

    ② は Windows + Python 3.14 等で発生する "session not created: Chrome instance exited"
    バグ (chromedriver-Chrome 間のハンドシェイク失敗) を回避するために使う。
    Chrome の生存管理を chromedriver から剥がすので、Selenium API は通常通り使えるが
    driver.quit() しても Chrome 本体は残る点だけ注意。
    """
    profile_dir = os.path.normpath(user_data_dir())
    os.makedirs(profile_dir, exist_ok=True)

    common_args = [
        f"--user-data-dir={profile_dir}",
        f"--user-agent={DEFAULT_USER_AGENT}",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=ChromeWhatsNewUI",
    ]
    if headless:
        common_args += ["--headless=new", "--window-size=1280,900"]

    # --- ① 通常起動 ---
    options = webdriver.ChromeOptions()
    for a in common_args:
        options.add_argument(a)
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    def _kill_orphans():
        """フォールバック前に残ってる Chrome / chromedriver を掃除する。"""
        if sys.platform == "win32":
            for img in ("chrome.exe", "chromedriver.exe"):
                try:
                    subprocess.run(["taskkill", "/F", "/IM", img, "/T"],
                                   capture_output=True, timeout=10)
                except Exception:
                    pass

    primary_err = None
    # Windows では Selenium Manager の chromedriver が Chrome版に追従しない既知バグが
    # 多発するため、最初から chrome-for-testing 経由で確実な版を取りに行く
    if sys.platform == "win32":
        try:
            chrome_bin_for_ver = _find_chrome_binary()
            chrome_ver = _get_chrome_version(chrome_bin_for_ver)
            if chrome_ver:
                driver_path = _download_matching_chromedriver(chrome_ver)
                service2 = Service(executable_path=driver_path)
                driver = webdriver.Chrome(service=service2, options=options)
                driver.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"},
                )
                print(f"[make_driver] ✅ chrome-for-testing chromedriver で起動成功", flush=True)
                return driver
        except Exception as e:
            primary_err = e
            print(f"[make_driver] chrome-for-testing 経由失敗 → アタッチ方式に切替: {e}", flush=True)
            _kill_orphans()
    else:
        try:
            driver = webdriver.Chrome(options=options)
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"},
            )
            return driver
        except Exception as e:
            primary_err = e
            print(f"[make_driver] 通常起動失敗 → chrome-for-testing経由で再試行: {e}", flush=True)
        try:
            chrome_bin_for_ver = _find_chrome_binary()
            chrome_ver = _get_chrome_version(chrome_bin_for_ver)
            if chrome_ver:
                driver_path = _download_matching_chromedriver(chrome_ver)
                service2 = Service(executable_path=driver_path)
                driver = webdriver.Chrome(service=service2, options=options)
                driver.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"},
                )
                return driver
        except Exception as e:
            print(f"[make_driver] chrome-for-testing 経由も失敗 → アタッチ方式: {e}", flush=True)

    # --- ② フォールバック: subprocess起動 + debuggerAddress アタッチ ---
    chrome_bin = _find_chrome_binary()
    port = _find_free_port()
    args = [chrome_bin] + common_args + [f"--remote-debugging-port={port}"]

    # Windowsで chrome.exe を新規プロセスとして切り離す
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
    subprocess.Popen(args, creationflags=creationflags)

    try:
        _wait_devtools_ready(port)
    except Exception as wait_err:
        raise RuntimeError(
            f"Chrome起動失敗:\n  通常側: {primary_err}\n  アタッチ側(待機): {wait_err}"
        ) from primary_err

    options2 = webdriver.ChromeOptions()
    options2.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    # attach 方式でも chromedriver は必要 → version一致した driver を使う
    attach_service = None
    try:
        chrome_ver = _get_chrome_version(chrome_bin)
        if chrome_ver:
            driver_path = _download_matching_chromedriver(chrome_ver)
            attach_service = Service(executable_path=driver_path)
    except Exception:
        pass
    try:
        if attach_service:
            driver = webdriver.Chrome(service=attach_service, options=options2)
        else:
            driver = webdriver.Chrome(options=options2)
    except Exception as attach_err:
        raise RuntimeError(
            f"Chrome起動失敗:\n  通常側: {primary_err}\n  アタッチ側(接続): {attach_err}"
        ) from primary_err

    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"},
        )
    except Exception:
        pass  # アタッチ方式では失敗しても致命的じゃない
    print(f"[make_driver] ✅ アタッチ方式でChrome接続成功 (port={port})", flush=True)
    return driver


def dismiss_popups(driver: webdriver.Chrome, max_rounds: int = 3) -> int:
    """ページ被せのポップアップを閉じる。

    メルカリは機能告知・キャンペーン・オンボ再表示などのモーダルを予告なく被せてくる。
    検知できるパターンを順に試して閉じる。閉じられた数を返す（デバッグ用）。
    無ければ何もしない。

    対応パターン:
      - aria-label="閉じる" / "close" のボタン
      - テキストが「閉じる」「あとで」「スキップ」「キャンセル」「OK」のボタン
      - 「×」「✕」「✗」だけの button
    """
    from selenium.webdriver.common.by import By  # 関数内 import（循環回避）
    closed = 0
    close_texts = ["閉じる", "あとで", "後で", "スキップ", "キャンセル", "OK", "×", "✕", "✗", "とじる"]
    aria_labels = ["閉じる", "close", "Close", "Dismiss", "dismiss"]
    for _ in range(max_rounds):
        any_clicked = False
        # aria-label / aria-labelledby
        for label in aria_labels:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, f"[aria-label='{label}']")
                for e in els:
                    if e.is_displayed() and e.is_enabled():
                        try:
                            e.click()
                            any_clicked = True
                            closed += 1
                            time.sleep(0.5)
                            break
                        except Exception:
                            try:
                                driver.execute_script("arguments[0].click();", e)
                                any_clicked = True
                                closed += 1
                                time.sleep(0.5)
                                break
                            except Exception:
                                continue
                if any_clicked:
                    break
            except Exception:
                continue
        if any_clicked:
            continue
        # テキスト一致 (button or div[role='button'])
        for txt in close_texts:
            try:
                xp = (
                    f"//button[normalize-space(text())='{txt}'] | "
                    f"//div[@role='button' and normalize-space(text())='{txt}'] | "
                    f"//a[normalize-space(text())='{txt}']"
                )
                els = driver.find_elements(By.XPATH, xp)
                for e in els:
                    if e.is_displayed() and e.is_enabled():
                        try:
                            e.click()
                            any_clicked = True
                            closed += 1
                            time.sleep(0.5)
                            break
                        except Exception:
                            try:
                                driver.execute_script("arguments[0].click();", e)
                                any_clicked = True
                                closed += 1
                                time.sleep(0.5)
                                break
                            except Exception:
                                continue
                if any_clicked:
                    break
            except Exception:
                continue
        if not any_clicked:
            break  # 閉じるべきものが見つからなくなったら終わり
    return closed


def is_logged_in(driver: webdriver.Chrome) -> bool:
    """ログイン状態の簡易判定: マイページに飛んでログインフォームが出ないかどうか。"""
    driver.get("https://jp.mercari.com/mypage")
    time.sleep(2.0)
    cur = driver.current_url
    if "/login" in cur or "/onboarding" in cur:
        return False
    try:
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'マイページ') or contains(text(), '出品した商品')]"))
        )
        return True
    except Exception:
        return False


def ensure_login_or_die(driver: webdriver.Chrome, tool: str) -> None:
    """ログイン切れを検知したら LINE通知して exit。cron 無人実行用。"""
    if not is_logged_in(driver):
        log(tool, "ERR: ログインCookie切れ。手動で再ログインが必要")
        notify_line(f"⚠️ {tool}: ログインCookie切れ。リモデスで手動再ログインしてください")
        try:
            driver.quit()
        except Exception:
            pass
        sys.exit(2)
    log(tool, "login OK")


def cleanup_old_snapshots(retention_days: int = 30) -> int:
    """snapshots/ と photos/ で retention_days 日より古いファイルを削除。"""
    import time as _t
    cutoff = _t.time() - retention_days * 86400
    removed = 0
    for sub in ("snapshots", "photos"):
        base = state_dir() / sub
        if not base.exists():
            continue
        for root, dirs, files in os.walk(base):
            for f in files:
                p = os.path.join(root, f)
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                        removed += 1
                except OSError:
                    continue
    return removed


def save_snapshot(name: str, data: dict) -> Path:
    """商品キャプチャJSONを snapshots/ に保存。再出品失敗時のロールバック用。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = name.replace("/", "_").replace(" ", "_")[:80]
    path = state_dir() / "snapshots" / f"{safe_name}_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load_quota(tool: str) -> dict:
    """月次/日次クォータの読み出し。tool ごとに分ける。"""
    path = state_dir() / f"quota_{tool}_{account_name()}.json"
    if not path.exists():
        return {"month": "", "month_count": 0, "day": "", "day_count": 0, "history": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"month": "", "month_count": 0, "day": "", "day_count": 0, "history": []}


def save_quota(tool: str, q: dict) -> None:
    path = state_dir() / f"quota_{tool}_{account_name()}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(q, f, ensure_ascii=False, indent=2)


def quota_can_use(tool: str, monthly_max: int, daily_max: int) -> tuple[bool, str]:
    """今クォータが使えるか判定。使えるなら (True, '...')、使えないなら (False, 理由)。"""
    q = load_quota(tool)
    today = datetime.now().strftime("%Y-%m-%d")
    this_month = datetime.now().strftime("%Y-%m")
    if q.get("month") != this_month:
        q["month"] = this_month
        q["month_count"] = 0
    if q.get("day") != today:
        q["day"] = today
        q["day_count"] = 0
    save_quota(tool, q)
    if q["month_count"] >= monthly_max:
        return False, f"月次上限到達 ({q['month_count']}/{monthly_max})"
    if q["day_count"] >= daily_max:
        return False, f"日次上限到達 ({q['day_count']}/{daily_max})"
    return True, f"残り 月{monthly_max - q['month_count']}件 / 日{daily_max - q['day_count']}件"


def jiggle_description(text: str) -> str:
    """商品説明文に「見えない揺らぎ」を入れて、メルカリのSEO/AI検知に対し
    "毎回少し違う" 状態にする。

    タイトルは絶対に触らない (オーナーの別ツール「タイトル一致照合」が壊れる)。
    商品説明文だけを次の方法で揺らがす:

    - 末尾の改行・空白・ゼロ幅スペースを一旦剥がす
    - 改行 0-2 個 + ゼロ幅スペース U+200B を 0-5 個 + 全角スペース 0-2 個
    - たまに見える絵文字 (♡ ✨ ★ など) を末尾に追加 (確率 30%)

    結果: 購入者には見た目ほぼ一緒、ただしハッシュ/バイト列は毎回違う。
    """
    import random as _r

    if not text:
        return ""

    # 既存末尾の不可視文字 + jiggle 痕跡を剥がす
    cleaned = text.rstrip("​‌‍ \n\t　")
    # たまに既存末尾の単発装飾絵文字も整理 (蓄積防止)
    cleaned = cleaned.rstrip(" \n\t　")

    # ランダムサフィックス組み立て
    parts = []
    parts.append("\n" * _r.randint(0, 2))
    parts.append("　" * _r.randint(0, 2))      # 全角スペース
    parts.append("​" * _r.randint(0, 5))      # ゼロ幅スペース
    if _r.random() < 0.30:
        parts.append(_r.choice(["♡", "✨", "★", "♪", "✿", "◎", "○", "❀"]))
    parts.append("​" * _r.randint(0, 3))

    return cleaned + "".join(parts)


def quota_consume(tool: str, item_id: str, note: str = "") -> None:
    q = load_quota(tool)
    q["month_count"] = q.get("month_count", 0) + 1
    q["day_count"] = q.get("day_count", 0) + 1
    q.setdefault("history", []).append({
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "item_id": item_id,
        "note": note,
    })
    # 履歴は直近500件で打ち止め
    q["history"] = q["history"][-500:]
    save_quota(tool, q)
