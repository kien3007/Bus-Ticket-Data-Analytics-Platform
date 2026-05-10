# -*- coding: utf-8 -*-
import os
import re
import json
import time
import math
import random
import traceback
import unicodedata
from datetime import datetime, timedelta, date
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict  # ✅ dùng cho smart-dedup

import pandas as pd

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, TimeoutException


# ======================
# ĐƯỜNG DẪN LƯU (giống crawl_ticket / crawl_facility)
# ======================
DEFAULT_WIN_BASE = r"C:\KLTN2\data\raw"
LOCAL_RAW_DIR = os.getenv("LOCAL_RAW_DIR", DEFAULT_WIN_BASE)
REVIEW_BASE_DIR = os.path.join(LOCAL_RAW_DIR, "review")
MASTER_PATH = os.path.join(REVIEW_BASE_DIR, "bus_reviews_master.jsonl")

# Số ngày gần nhất để lấy review (mặc định 7; có thể override bằng env)
REVIEW_LOOKBACK_DAYS = int(os.getenv("REVIEW_LOOKBACK_DAYS", "7"))


# ======================
# Selenium Driver
# ======================
def initialize_driver():
    options = webdriver.ChromeOptions()
    flags = [
        "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
        "--disable-gpu", "--disable-extensions",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1920,1080",
        "--disable-notifications", "--disable-popup-blocking"
    ]
    for f in flags:
        options.add_argument(f)
    # Ổn định hơn khi chạy nhiều tab
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(options=options)
    return driver


def safe_click(driver, elem):
    """Scroll element vào giữa + thử click bình thường, nếu lỗi thì dùng JS click."""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
        time.sleep(0.1)
        elem.click()
    except Exception:
        driver.execute_script("arguments[0].click();", elem)


def scroll_and_click_see_more(driver):
    """Scroll và bấm 'See more' để tải hết kết quả trên trang listing."""
    previous_count = 0
    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.6)
        bus_elements = driver.find_elements(By.CLASS_NAME, "bus-name")
        current_count = len(bus_elements)

        if current_count == previous_count:
            # Thử tìm nút 'load more' lần cuối
            try:
                button_xpath = ("//button[contains(@class, 'load-more') or "
                                "contains(.,'Xem thêm') or contains(.,'See more')]")
                btn = WebDriverWait(driver, 2).until(
                    EC.element_to_be_clickable((By.XPATH, button_xpath))
                )
                safe_click(driver, btn)
                time.sleep(0.6)
            except Exception:
                break
        previous_count = current_count


def get_bus_names_and_buttons(driver):
    """Lấy danh sách bus: tên + nút chi tiết."""
    wait = WebDriverWait(driver, 6)
    bus_data = []
    try:
        wait.until(EC.presence_of_all_elements_located((By.CLASS_NAME, "bus-name")))
        ticket_containers = driver.find_elements(By.XPATH, "//div[contains(@class, 'ticket')]")
        for container in ticket_containers:
            try:
                bus_name_element = container.find_element(By.CLASS_NAME, "bus-name")
                bus_name = (bus_name_element.text or "").strip()
                detail_button = container.find_element(By.XPATH, ".//button[contains(@class, 'btn-detail')]")
                if bus_name and detail_button:
                    bus_data.append({"name": bus_name, "button": detail_button})
            except NoSuchElementException:
                continue
        print(f"Found {len(bus_data)} bus entries")
        return bus_data
    except Exception as e:
        print(f"Error getting bus names: {e}")
        return []


# ======================
# Helpers: cuộn panel review & parse ngày tiếng Việt
# ======================
def _scroll_reviews_panel(driver):
    """
    Cuộn vùng review trong ant-drawer nếu có; nếu không thì cuộn window.
    Trả về True nếu đã cuộn panel; False nếu fallback cuộn window.
    """
    panel_xpaths = [
        "//div[contains(@class,'ant-drawer-open')]//div[contains(@class,'ant-drawer-body')]",
        "//div[contains(@class,'review')]/ancestor::div[contains(@class,'ant-drawer-body')]",
    ]
    for xp in panel_xpaths:
        els = driver.find_elements(By.XPATH, xp)
        if els:
            panel = els[0]
            driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", panel)
            return True
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    return False


def _strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')


def parse_vn_date(raw: str) -> date | None:
    """
    Hỗ trợ các dạng:
    - dd/MM/yyyy | dd-MM-yyyy
    - 'Hôm nay' | 'Hôm qua'
    - 'x ngày/giờ/phút trước'
    - 'dd tháng MM, yyyy' | 'dd thg MM, yyyy'
    """
    if not raw:
        return None
    txt = raw.strip().lower()
    today = date.today()
    txt_norm = _strip_accents(txt)

    # Hôm nay / Hôm qua
    if "hom nay" in txt_norm:
        return today
    if "hom qua" in txt_norm:
        return today - timedelta(days=1)

    # X ngày/giờ/phút trước
    m = re.search(r"(\d+)\s*(ngay|gio|phut)\s*truoc", txt_norm)
    if m:
        val = int(m.group(1))
        unit = m.group(2)
        if unit == "ngay":
            return today - timedelta(days=val)
        if unit == "gio":
            # Quy đổi thô theo ngày để lọc (24h ~ 1 ngày)
            days = max(0, math.floor(val / 24))
            return today - timedelta(days=days)
        if unit == "phut":
            return today

    # dd/MM/yyyy hoặc dd-MM-yyyy
    for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(txt, fmt).date()
        except Exception:
            pass

    # "dd tháng MM, yyyy" | "dd thg MM, yyyy" (không dấu)
    m2 = re.search(r"(\d{1,2})\s*(thang|thg)\s*(\d{1,2})[, ]+\s*(\d{4})", txt_norm)
    if m2:
        d = int(m2.group(1))
        mth = int(m2.group(3))
        y = int(m2.group(4))
        try:
            return date(y, mth, d)
        except Exception:
            return None

    return None


# ======================
# Parse & Extract Reviews
# ======================
def extract_reviews_from_page(driver, start_date, end_date):
    """
    Trích review ở trang hiện tại, chỉ giữ Date trong [start_date, end_date].
    Trả về list[dict]: customer_name, stars, comment, Date (ISO).
    """
    reviews = []
    processed = set()

    try:
        # Mềm dẻo hơn: container có class 'review' (không chỉ 'review-item')
        WebDriverWait(driver, 4).until(
            EC.presence_of_element_located((By.XPATH, "//div[contains(@class,'review')]"))
        )
        review_containers = driver.find_elements(By.XPATH, "//div[contains(@class,'review')]")
        print(f"Found {len(review_containers)} review containers on current page")

        for container in review_containers:
            try:
                # Tên KH
                customer_name = "Unknown"
                try:
                    customer_name_elem = container.find_element(
                        By.XPATH, ".//*[contains(@class,'name')] | .//p[contains(@class,'name')]"
                    )
                    customer_name = (customer_name_elem.text or "").strip()
                except NoSuchElementException:
                    pass

                # Số sao
                stars = 0
                try:
                    stars = len(container.find_elements(By.XPATH, ".//i[contains(@class,'color--critical')]"))
                    if stars == 0:
                        aria = container.get_attribute("aria-label") or ""
                        mstar = re.search(r"(\d+(\.\d+)?)", aria)
                        if mstar:
                            stars = int(float(mstar.group(1)))
                except Exception:
                    pass

                # Comment
                comment = ""
                try:
                    comment_elem = container.find_element(
                        By.XPATH, ".//*[contains(@class,'comment')] | .//p[contains(@class,'comment')]"
                    )
                    comment = (comment_elem.text or "").strip()
                except NoSuchElementException:
                    pass

                # Ngày đánh giá (raw & parsed)
                raw_date = ""
                try:
                    date_elem = container.find_element(
                        By.XPATH, ".//*[contains(@class,'rated-date') or contains(@class,'date')]"
                    )
                    raw_date = (date_elem.text or "").strip()
                except NoSuchElementException:
                    pass

                d = parse_vn_date(raw_date)
                if not d:
                    # Nếu không parse được, đừng loại—gán hôm nay để bạn có data tham khảo
                    d = date.today()
                if not (start_date <= d <= end_date):
                    continue

                key = (customer_name, stars, comment, d.isoformat())
                if key in processed:
                    continue
                processed.add(key)

                # ✅ CHỈNH #1: Lưu Date ISO thay vì raw
                reviews.append({
                    "customer_name": customer_name,
                    "stars": stars,
                    "comment": comment,
                    "Date": d.isoformat()
                })

            except Exception as e:
                print(f"Error extracting single review: {e}")
                traceback.print_exc()

        return reviews

    except Exception as e:
        print(f"Error extracting reviews from page: {e}")
        traceback.print_exc()
        return []


def extract_reviews_for_bus(driver, bus_entry):
    """Vào chi tiết từng bus, chuyển tab REVIEW, phân trang để lấy review."""
    bus_name = bus_entry["name"]
    all_reviews = []
    wait = WebDriverWait(driver, 6)
    page_number = 1

    # Khoảng ngày: từ (today - LOOKBACK) -> today
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=REVIEW_LOOKBACK_DAYS)

    try:
        safe_click(driver, bus_entry["button"])

        # Tab REVIEW
        try:
            review_tab = wait.until(EC.element_to_be_clickable((By.XPATH, "//div[@role='tab' and @id='REVIEW']")))
            safe_click(driver, review_tab)
            time.sleep(0.6)
        except Exception:
            # Thử phương án dự phòng
            tabs = driver.find_elements(By.XPATH, "//div[@role='tab']")
            clicked = False
            for tab in tabs:
                if "REVIEW" in (tab.get_attribute("id") or "") or "ĐÁNH GIÁ" in tab.text:
                    safe_click(driver, tab)
                    time.sleep(0.6)
                    clicked = True
                    break
            if not clicked:
                try:
                    safe_click(driver, bus_entry["button"])  # đóng panel
                except Exception:
                    pass
                return []

        print(f"Extracting reviews for '{bus_name}'")

        try:
            wait.until(EC.presence_of_element_located((By.XPATH, "//div[contains(@class, 'review')]")))
        except TimeoutException:
            print(f"  No reviews found for '{bus_name}'")
            try:
                safe_click(driver, bus_entry["button"])  # đóng panel
            except Exception:
                pass
            time.sleep(0.2)
            return []

        while True:
            _scroll_reviews_panel(driver)
            time.sleep(0.4 + random.random() * 0.3)

            page_reviews = extract_reviews_from_page(driver, start_date, end_date)
            for r in page_reviews:
                r["bus_name"] = bus_name
            all_reviews.extend(page_reviews)
            print(f"  Page {page_number}: Extracted {len(page_reviews)} reviews")

            # Pagination trong drawer
            try:
                next_btn = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH,
                        "//div[contains(@class,'ant-drawer-open')]//li[contains(@class,'ant-pagination-next')]"
                    ))
                )
                if next_btn.get_attribute("aria-disabled") == "true":
                    break
                safe_click(driver, next_btn)
                page_number += 1
                time.sleep(0.6 + random.random() * 0.4)
            except Exception:
                break

        # đóng panel
        try:
            safe_click(driver, bus_entry["button"])
        except Exception:
            pass
        time.sleep(0.15)

        print(f"  Total reviews collected for '{bus_name}': {len(all_reviews)}")
        return all_reviews

    except Exception as e:
        print(f"Error extracting reviews for {bus_name}: {e}")
        traceback.print_exc()
        try:
            safe_click(driver, bus_entry["button"])
        except Exception:
            pass
        time.sleep(0.2)
        return []


# ======================
# Company IDs
# ======================
def get_company_id(province, key, driver, date_str_dd_mm_yyyy):
    """
    Lấy danh sách (bus_name, company_id) cho 1 tỉnh/ngày.
    date_str phải là dd-mm-YYYY (đúng format URL Vexere).
    """
    url = f"https://vexere.com/vi-VN/ve-xe-khach-tu-sai-gon-di-{province}-{key}.html?date={date_str_dd_mm_yyyy}"
    driver.get(url)
    try:
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CLASS_NAME, "bus-name")))
        scroll_and_click_see_more(driver)
    except Exception:
        print(f"Couldn't load data from {url}")
        return []

    ids = []
    containers = driver.find_elements(By.CSS_SELECTOR, "[data-company-id]")
    names = [b.text.strip() for b in driver.find_elements(By.CLASS_NAME, "bus-name")]
    comp_ids = [c.get_attribute("data-company-id") or "Unknown" for c in containers]
    n = min(len(names), len(comp_ids))
    for i in range(n):
        ids.append([names[i], comp_ids[i]])
    return ids


def process_company(province, key, company_id, date_str_dd_mm_yyyy):
    """Xử lý từng company_id (một driver/luồng)."""
    driver = initialize_driver()
    all_reviews = []
    try:
        print(f"\nProcessing company ID: {company_id} in province: {province}")
        company_url = (
            f"https://vexere.com/vi-VN/ve-xe-khach-tu-sai-gon-di-{province}-{key}.html"
            f"?date={date_str_dd_mm_yyyy}&companies={company_id}&sort=time%3Aasc"
        )
        driver.get(company_url)
        time.sleep(1.2)
        entries = get_bus_names_and_buttons(driver)
        for e in entries:
            all_reviews.extend(extract_reviews_for_bus(driver, e))
            time.sleep(0.3 + random.random() * 0.7)  # nhẹ nhàng để tránh bị chặn
    except Exception as e:
        print(f"Error processing company {company_id}: {e}")
        traceback.print_exc()
    finally:
        driver.quit()
    return all_reviews


# ======================
# LƯU & MERGE JSONL (giống crawl_facility)
# ======================
def _ensure_review_dirs():
    os.makedirs(REVIEW_BASE_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    daily_dir = os.path.join(REVIEW_BASE_DIR, f"date={today}")
    os.makedirs(daily_dir, exist_ok=True)
    return daily_dir, today


def _normalize_reviews_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Chuẩn hoá schema & kiểu dữ liệu.
    Output columns: Review_Id (optional), Bus_Name, Customer_Name, Stars, Comment, Date
    """
    out = df.copy()
    for c, default in [
        ("Review_Id", None),
        ("Bus_Name", "Unknown"),
        ("Customer_Name", "Unknown"),
        ("Stars", 0),
        ("Comment", ""),
        ("Date", "Unknown"),
    ]:
        if c not in out.columns:
            out[c] = default

    out["Bus_Name"] = out["Bus_Name"].astype(str)
    out["Customer_Name"] = out["Customer_Name"].astype(str)
    out["Comment"] = out["Comment"].astype(str)
    out["Stars"] = pd.to_numeric(out["Stars"], errors="coerce").fillna(0).astype(int)
    return out[["Review_Id", "Bus_Name", "Customer_Name", "Stars", "Comment", "Date"]]


def _save_daily_jsonl(df: pd.DataFrame):
    daily_dir, today = _ensure_review_dirs()
    out = _normalize_reviews_df(df)
    daily_path = os.path.join(daily_dir, f"bus_reviews_{today}.jsonl")
    out.to_json(daily_path, orient="records", lines=True, force_ascii=False)
    print(f"💾 Đã lưu file NGÀY (JSONL): {daily_path}")


def _read_master_jsonl() -> pd.DataFrame:
    if not os.path.exists(MASTER_PATH):
        return pd.DataFrame(columns=["Review_Id", "Bus_Name", "Customer_Name", "Stars", "Comment", "Date"])
    try:
        return pd.read_json(MASTER_PATH, orient="records", lines=True, dtype={"Review_Id": "Int64"})
    except ValueError:
        # file rỗng/trống
        return pd.DataFrame(columns=["Review_Id", "Bus_Name", "Customer_Name", "Stars", "Comment", "Date"])


def _merge_to_master_jsonl(df_new: pd.DataFrame):
    """
    Merge vào master (JSONL) với Review_Id tăng dần.
    Trùng lặp được loại theo (Bus_Name, Customer_Name, Comment, Date, Stars).
    """
    master = _read_master_jsonl()
    max_id = int(master["Review_Id"].max()) if not master.empty and master["Review_Id"].notna().any() else 0

    df_new = _normalize_reviews_df(df_new)

    # Loại trùng với master theo khóa
    key_cols = ["Bus_Name", "Customer_Name", "Comment", "Date", "Stars"]
    if not master.empty:
        master_keys = set(tuple(row) for row in master[key_cols].itertuples(index=False, name=None))
        mask_keep = ~df_new[key_cols].apply(tuple, axis=1).isin(master_keys)
        df_new = df_new.loc[mask_keep].copy()

    # Cấp Review_Id cho những bản ghi chưa có
    need_id_mask = df_new["Review_Id"].isna() | (df_new["Review_Id"].astype(str).str.strip() == "")
    if need_id_mask.any():
        count_need = int(need_id_mask.sum())
        new_ids = list(range(max_id + 1, max_id + 1 + count_need))
        df_new.loc[need_id_mask, "Review_Id"] = new_ids
        max_id += count_need

    df_new["Review_Id"] = df_new["Review_Id"].astype(int)

    # Gộp và ghi đè master
    updated = pd.concat([master, df_new], ignore_index=True)
    updated.to_json(MASTER_PATH, orient="records", lines=True, force_ascii=False)
    print(f"✅ Đã MERGE vào MASTER (JSONL): {MASTER_PATH} — tổng {len(updated)} dòng")


# ======================
# HÀM CHÍNH
# ======================
def crwl_reviews():
    provinces_keys = {
        # Bạn có thể bật/tắt bớt tỉnh để thử ổn định
        "binh-thuan": "129t1111",
    }

    # Vexere ổn hơn khi query ngày mai -> PHẢI là dd-mm-YYYY
    query_date = (datetime.now() + timedelta(days=1)).strftime("%d-%m-%Y")

    collected = []
    processed_company_ids = set()

    # 2 thread cho Windows ổn định hơn (giảm QUOTA_EXCEEDED)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = []
        for province, key in provinces_keys.items():
            print(f"\n--- Processing province: {province} ---")
            # Lấy danh sách company_id trước
            driver = initialize_driver()
            try:
                pairs = get_company_id(province, key, driver, query_date)
            finally:
                driver.quit()

            uniq_ids = sorted(set([p[1] for p in pairs if len(p) == 2]))
            for cid in uniq_ids:
                if cid in processed_company_ids:
                    print(f"Skipping already processed company ID: {cid}")
                    continue
                processed_company_ids.add(cid)
                futures.append(executor.submit(process_company, province, key, cid, query_date))

        # Thu kết quả
        for f in futures:
            try:
                company_reviews = f.result()
                collected.extend(company_reviews)
            except Exception as e:
                print(f"Thread error: {e}")

    # ✅ SMART-DEDUP: chỉ giữ dòng đầy đủ nhất theo (Bus_Name, Comment, Date)
    grouped = defaultdict(list)
    for r in collected:
        bn = r.get("bus_name", "Unknown")
        cn = r.get("customer_name", "Unknown")
        cm = r.get("comment", "")
        dt = r.get("Date") or date.today().isoformat()   # dùng Date ISO đã lưu
        st = int(r.get("stars", 0) or 0)
        rid = r.get("review_id") or r.get("Review_Id") or 0  # tie-break nếu có

        grouped[(bn, cm, dt)].append({
            "Customer_Name": cn,
            "Stars": st,
            "Review_Id": rid
        })

    deduped = []
    for (bn, cm, dt), rows in grouped.items():
        # Sắp xếp theo mức "đầy đủ": Stars>0 → tên != Unknown → tên dài hơn → Review_Id nhỏ hơn
        rows_sorted = sorted(
            rows,
            key=lambda x: (
                x["Stars"] > 0,
                (str(x["Customer_Name"]).lower() != "unknown"),
                len(str(x["Customer_Name"])),
                -(int(x["Review_Id"]) if str(x["Review_Id"]).isdigit() else 0)  # đảo dấu để Review_Id nhỏ ưu tiên (ở sort reverse=False)
            ),
            reverse=True
        )
        best = rows_sorted[0]
        deduped.append({
            "Bus_Name": bn,
            "Customer_Name": best["Customer_Name"],
            "Stars": int(best["Stars"]),
            "Comment": cm,
            "Date": dt
        })

    df = pd.DataFrame(deduped, columns=["Bus_Name", "Customer_Name", "Stars", "Comment", "Date"])

    # 1) LƯU FILE NGÀY (JSONL)
    _save_daily_jsonl(df)

    # 2) MERGE vào MASTER (JSONL) với Review_Id tăng dần + loại trùng
    _merge_to_master_jsonl(df)

    print("🎉 Reviews crawl done (saved daily JSONL + merged master JSONL).")


if __name__ == "__main__":
    crwl_reviews()