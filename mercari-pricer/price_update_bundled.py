"""メルカリ出品の価格を毎日ちょっとずつ動かす。スプシ依存ナシ版。

ルール:
  現在価格 == 800円         → 1500/1600/1700 円のうちランダムで上げる (底値リバウンド)
  現在価格 >  800円         → 100/200 円のうちランダムで下げる。ただし下限800円でクランプ
  現在価格 <  800円 (異常)  → 1500/1600/1700 円のうちランダムで上げる (底値リバウンド扱い)

使い方 (cron で1日1回叩く想定):
  ACCOUNT_NAME=ちひろ python price_update.py
  ACCOUNT_NAME=ちひろ DRY_RUN=true python price_update.py            # 「変更する」を押さない
  ACCOUNT_NAME=ちひろ MAX_ITEMS=5 DRY_RUN=true python price_update.py # 5件だけ smoke
"""
import os
import random
import sys
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from common import (
    account_name,
    dismiss_popups,
    ensure_login_or_die,
    jiggle_description,
    log,
    make_driver,
    notify_line,
)
from scrape_listings import get_on_sale_items, read_current_price
from inventory import import_from_scrape, load_inventory, active_items, upsert_item, mark_sold, save_inventory, detect_new

TOOL = "price_update"

BUMP_VALUES = [1500, 1600, 1700]
DROP_VALUES = [100, 200]
FLOOR = 800

EDIT_LINK_XPATH = "//a[@data-testid='checkout-link' and contains(., '商品の編集')]"
PRICE_INPUT_CSS = "input[name='price']"
SUBMIT_BUTTON_CSS = "button[type='submit'][data-testid='edit-button']"


def decide_new_price(current: int) -> tuple[int, str]:
    """ルール適用。返り値 (new_price, 理由文字列)。"""
    if current == FLOOR:
        return random.choice(BUMP_VALUES), "底値ヒット → リバウンド"
    if current < FLOOR:
        return random.choice(BUMP_VALUES), f"異常値({current})検出 → リバウンド"
    drop = random.choice(DROP_VALUES)
    new = max(FLOOR, current - drop)
    return new, f"-{drop}円 (下限{FLOOR}クランプ)"


def update_one(driver, item: dict, dry_run: bool) -> dict:
    url = item["url"]
    # まず一覧スクレ時に取得済の価格を使う (詳細ページ訪問より信頼性高い)
    current = item.get("price")
    if not current:
        current = read_current_price(driver, url)
    else:
        # 商品ページ訪問はしておく (編集ボタン押下のため)
        try:
            driver.get(url)
            time.sleep(1.0)
        except Exception:
            pass
    if not current:
        return {"id": item["id"], "ok": False, "reason": "現在価格取得失敗"}
    try:
        current = int(current)
    except (ValueError, TypeError):
        return {"id": item["id"], "ok": False, "reason": f"価格パース失敗: {current!r}"}

    new_price, why = decide_new_price(current)
    if new_price == current:
        return {"id": item["id"], "ok": True, "skipped": True, "reason": f"変更なし ({current}円)"}

    # 1行サマリ: 旧→新 + 商品名 + URL (Cmd+クリックで開ける) + 理由
    title = (item.get("title") or "").strip().replace("\n", " ")[:50]
    log(TOOL, f"[{item.get('_idx','?')}/{item.get('_total','?')}] ✏️ ¥{current} → ¥{new_price}  {title}  {item['url']}  ({why})")

    # 編集画面に遷移 (ポップアップ被ってる場合があるので閉じてから)
    dismiss_popups(driver)
    try:
        edit_link = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, EDIT_LINK_XPATH))
        )
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", edit_link)
        time.sleep(0.4)
        try:
            edit_link.click()
        except Exception:
            driver.execute_script("arguments[0].click();", edit_link)
    except Exception as e:
        return {"id": item["id"], "ok": False, "reason": f"編集ボタン未検出: {e.__class__.__name__}"}

    # 価格入力（編集ページに遷移してから再度ポップアップ消す）
    dismiss_popups(driver)
    try:
        price_input = WebDriverWait(driver, 12).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, PRICE_INPUT_CSS))
        )
        price_input.click()
        # 全選択して上書き
        modifier = Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL
        price_input.send_keys(modifier, "a")
        price_input.send_keys(Keys.DELETE)
        price_input.clear()
        price_input.send_keys(str(new_price))
    except Exception as e:
        return {"id": item["id"], "ok": False, "reason": f"価格入力失敗: {e.__class__.__name__}"}

    # 商品説明文に「見えない揺らぎ」を入れる (タイトルは絶対に触らない)
    # メルカリ側のSEO的に「同じ内容で更新」と見なされないように、毎回ハッシュを変える。
    try:
        desc_textarea = driver.find_element(By.CSS_SELECTOR, "textarea[name='description']")
        current_desc = desc_textarea.get_attribute("value") or ""
        new_desc = jiggle_description(current_desc)
        if new_desc and new_desc != current_desc:
            modifier = Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL
            desc_textarea.click()
            desc_textarea.send_keys(modifier, "a")
            desc_textarea.send_keys(Keys.DELETE)
            desc_textarea.clear()
            # 改行を含むので JS 経由で値を入れて input イベントを発火
            driver.execute_script(
                "arguments[0].value = arguments[1]; "
                "arguments[0].dispatchEvent(new Event('input', {bubbles: true})); "
                "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
                desc_textarea, new_desc,
            )
    except Exception:
        pass  # 説明文の揺らぎは best-effort、失敗しても価格更新は続行

    if dry_run:
        return {"id": item["id"], "ok": True, "dry_run": True, "from": current, "to": new_price}

    # 「変更する」を押す
    try:
        submit_btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, SUBMIT_BUTTON_CSS))
        )
        submit_btn.click()
        time.sleep(3.5)
    except Exception as e:
        return {"id": item["id"], "ok": False, "reason": f"変更ボタン押下失敗: {e.__class__.__name__}"}

    return {"id": item["id"], "ok": True, "from": current, "to": new_price}


def main() -> int:
    dry_run = os.environ.get("DRY_RUN", "").lower() in ("true", "1", "yes")
    max_items = int(os.environ.get("MAX_ITEMS", "0")) or None
    headless = os.environ.get("HEADLESS", "").lower() in ("true", "1", "yes")
    use_inventory = os.environ.get("USE_INVENTORY", "").lower() in ("true", "1", "yes")
    new_check = int(os.environ.get("NEW_CHECK_TOP_N", "50"))  # 新規検知時の先頭取得件数

    mode = "INVENTORY" if use_inventory else "FULL_SCRAPE"
    log(TOOL, f"=== price_update start account={account_name()} mode={mode} dry_run={dry_run} max_items={max_items} ===")

    driver = make_driver(headless=headless)
    try:
        ensure_login_or_die(driver, TOOL)

        if use_inventory:
            existing_inv = load_inventory()
            if len(existing_inv) == 0:
                # 初回: CSV 空 → 全件スクレして初期化
                log(TOOL, "在庫CSV 空 → 初回全件スクレ実施 (約16分)")
                all_items = get_on_sale_items(driver, max_items=None, log_fn=lambda m: log(TOOL, m))
                import_from_scrape(all_items)
                log(TOOL, f"CSV 初期化完了: {len(all_items)}件 active")
            else:
                # 通常: 先頭 NEW_CHECK_TOP_N で新規検知
                log(TOOL, f"在庫モード: 先頭{new_check}件で新規検知 + CSV参照")
                top_items = get_on_sale_items(driver, max_items=new_check, log_fn=lambda m: log(TOOL, m))
                new_items = detect_new(top_items)
                if new_items:
                    log(TOOL, f"新規出品検知: {len(new_items)}件 → 在庫CSV末尾追加")
                    inv = load_inventory()
                    for it in new_items:
                        upsert_item(inv, it["id"], it["url"], price=it.get("price"))
                    save_inventory(inv)
            inv = load_inventory()
            actives = active_items(inv)
            items = []
            for r in actives:
                items.append({
                    "id": r["item_id"],
                    "url": r["url"],
                    "price": int(r.get("last_price") or 0) or None,
                    "title": "",
                })
            log(TOOL, f"在庫から処理対象: {len(items)}件 (active)")
            if max_items:
                items = items[:max_items]
        else:
            items = get_on_sale_items(driver, max_items=max_items, log_fn=lambda m: log(TOOL, m))
            # 全件スクレモードでは結果を在庫CSVに同期 (初回 import + 売却検知)
            if not max_items:
                log(TOOL, "全件スクレ → 在庫CSV同期 (sold検知含む)")
                import_from_scrape(items)

        if not items:
            log(TOOL, "出品中商品が0件。終了")
            return 0

        from selenium.common.exceptions import NoSuchWindowException, WebDriverException
        ok = 0
        ng = 0
        consecutive_window_errors = 0
        # 在庫モード時は処理結果を CSV に随時反映
        inv_for_update = load_inventory() if use_inventory else None
        for i, item in enumerate(items, 1):
            item["_idx"] = i
            item["_total"] = len(items)
            try:
                res = update_one(driver, item, dry_run)
                consecutive_window_errors = 0
                if res.get("ok"):
                    ok += 1
                    # 在庫CSVに新価格反映 (DRY_RUN除く)
                    if inv_for_update is not None and not dry_run and res.get("to"):
                        upsert_item(inv_for_update, item["id"], item["url"], price=res["to"])
                else:
                    ng += 1
                    log(TOOL, f"  NG: {res.get('reason')}")
                    # 編集ボタン無し系 = 売却済の可能性 → sold マーク
                    reason = (res.get("reason") or "").lower()
                    if inv_for_update is not None and any(
                        k in res.get("reason", "") for k in ["編集ボタン", "現在価格取得失敗"]
                    ):
                        mark_sold(inv_for_update, item["id"])
            except (NoSuchWindowException, WebDriverException) as e:
                ng += 1
                consecutive_window_errors += 1
                log(TOOL, f"  ⚠️ window/driver エラー ({consecutive_window_errors}連続): {e.__class__.__name__}")
                if consecutive_window_errors >= 3:
                    log(TOOL, "  🚨 3連続失敗 → 停止 (Selenium セッション持続不可)")
                    notify_line(f"price_update: 3連続window異常で停止 ({i}/{len(items)} 処理)")
                    break
                # driver 再起動
                try:
                    driver.quit()
                except Exception:
                    pass
                time.sleep(5)
                log(TOOL, "  driver 再起動中...")
                driver = make_driver(headless=headless)
                ensure_login_or_die(driver, TOOL)
                log(TOOL, "  ✅ driver 復帰完了、次の件から続行")
            except Exception as e:
                ng += 1
                log(TOOL, f"  EXC: {e.__class__.__name__}: {e}")
            # メルカリ側のレート抑制 + AI検知対策で広い乱数 + 周期的長休憩
            # ペース目標: 約4秒/件 → 1024件で約70分
            time.sleep(random.uniform(0.3, 1.5))
            if i % random.randint(15, 25) == 0:
                rest = random.uniform(20, 60)
                log(TOOL, f"  (周期休憩 {rest:.0f}秒)")
                time.sleep(rest)

        # 在庫モードの最終結果保存
        if inv_for_update is not None:
            save_inventory(inv_for_update)
            log(TOOL, "在庫CSV保存完了")

        log(TOOL, f"=== done ok={ok} ng={ng} total={len(items)} dry_run={dry_run} ===")
        if ng > 0:
            notify_line(f"price_update: {ng}件失敗 ({ok}/{len(items)} 成功)")
        return 0 if ng == 0 else 2
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
