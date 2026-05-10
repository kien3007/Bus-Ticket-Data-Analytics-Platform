# -*- coding: utf-8 -*-
import os
import time
from datetime import datetime, timedelta
import pandas as pd

from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


DEFAULT_WIN_BASE = r"C:\KLTN2\data\raw"
LOCAL_RAW_DIR = os.getenv("LOCAL_RAW_DIR", DEFAULT_WIN_BASE)
FACILITY_BASE_DIR = os.path.join(LOCAL_RAW_DIR, "facility")
MASTER_PATH = os.path.join(FACILITY_BASE_DIR, "bus_facilities_master.jsonl")


# ======================
# Selenium Driver
# ======================
def initialize_driver():
    options = webdriver.ChromeOptions()
    flags = [
        "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
        "--disable-gpu", "--disable-extensions",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1920,1080", "--disable-notifications",
        "--disable-popup-blocking", "--lang=vi-VN"
    ]
    for f in flags:
        options.add_argument(f)
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(45)
    driver.set_script_timeout(45)
    return driver


def scroll_and_click_see_more(driver):
    previous_count = 0
    while True:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1)
        buses = driver.find_elements(By.CLASS_NAME, "bus-name")
        current_count = len(buses)
        if current_count == previous_count:
            try:
                btn = WebDriverWait(driver, 2).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(@class,'load-more') or contains(.,'Xem thêm') or contains(.,'See more')]"))
                )
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(1)
            except Exception:
                break
        previous_count = current_count


def get_bus_names_and_buttons(driver):
    wait = WebDriverWait(driver, 10)
    bus_data = []
    try:
        wait.until(EC.presence_of_all_elements_located((By.CLASS_NAME, "bus-name")))
        tickets = driver.find_elements(By.XPATH, "//div[contains(@class, 'ticket')]")
        for t in tickets:
            try:
                name_el = t.find_element(By.CLASS_NAME, "bus-name")
                name = name_el.text.strip()
                btn = t.find_element(By.XPATH, ".//button[contains(@class,'btn-detail')]")
                if name and btn:
                    bus_data.append({"name": name, "button": btn})
            except NoSuchElementException:
                continue
        print(f"Found {len(bus_data)} bus entries")
        return bus_data
    except Exception as e:
        print(f"Error get_bus_names_and_buttons: {e}")
        return []


# ======================
# Utils: chuẩn hóa và hợp nhất Facilities
# ======================
def _norm_fac_list(x):
    """Đưa về list duy nhất (không trùng), giữ ổn định thứ tự."""
    if isinstance(x, (list, tuple)):
        seen, out = set(), []
        for it in x:
            s = (str(it) or "").strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out
    return []


def _union_lists(list_of_lists):
    """Union của nhiều list, có thứ tự alpha ổn định."""
    s = set()
    for li in list_of_lists:
        for it in (li or []):
            s.add(it)
    # sắp xếp để ổn định
    return sorted(s)


# ======================
# Extract Facilities
# ======================
def extract_facilities_for_bus(driver, bus_entry):
    facilities = []
    wait = WebDriverWait(driver, 10)
    try:
        # mở panel
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", bus_entry["button"])
        time.sleep(0.1)
        driver.execute_script("arguments[0].click();", bus_entry["button"])
        time.sleep(1)

        # next tab (mũi tên next trên tab) - một số layout cần bấm
        try:
            next_tab_button = wait.until(EC.element_to_be_clickable((
                By.XPATH,
                "//div[contains(@class,'DetailInfo__TabsStyled')]//span[contains(@class,'ant-tabs-tab-next')]"
            )))
            next_tab_button.click()
            time.sleep(0.5)
        except Exception:
            pass

        # chọn tab FACILITY
        facility_tab = wait.until(EC.element_to_be_clickable((By.XPATH, "//div[@role='tab' and @id='FACILITY']")))
        facility_tab.click()
        time.sleep(0.5)

        # lấy danh sách tiện ích (thử nhiều selector để tránh miss)
        # hàng tiện ích
        try:
            facility_container = wait.until(EC.presence_of_element_located((
                By.XPATH, "//div[contains(@class,'Facilities__FacilityRow')]"
            )))
            items = facility_container.find_elements(By.CLASS_NAME, "name")
        except Exception:
            # fallback: bất kỳ item có class name trong drawer
            items = driver.find_elements(By.XPATH, "//div[contains(@class,'ant-drawer-open')]//div[contains(@class,'name')]")

        for it in items:
            txt = (it.text or "").strip()
            if txt:
                facilities.append(txt)

        facilities = _norm_fac_list(facilities)

        # đóng panel chi tiết (click lại nút)
        driver.execute_script("arguments[0].click();", bus_entry["button"])
        time.sleep(0.5)

        wait.until(EC.presence_of_all_elements_located((By.CLASS_NAME, "bus-name")))
        return facilities
    except Exception as e:
        print(f"  - Error extracting facilities for {bus_entry.get('name','?')}: {e}")
        try:
            time.sleep(0.5)
            wait.until(EC.presence_of_all_elements_located((By.CLASS_NAME, "bus-name")))
        except Exception:
            pass
        return []


def get_company_id(province, key, driver, date_str):
    url = f"https://vexere.com/vi-VN/ve-xe-khach-tu-sai-gon-di-{province}-{key}.html?date={date_str}"
    driver.get(url)
    try:
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CLASS_NAME, "bus-name")))
        scroll_and_click_see_more(driver)
    except Exception:
        print(f"Không thể tải dữ liệu từ {url}")
        return []

    ids = []
    # Vexere dùng class 'ticket-item' hoặc 'ticket' tùy version, thường chứa data-company-id
    # Chúng ta tìm các container có data-company-id
    containers = driver.find_elements(By.XPATH, "//*[@data-company-id]")
    
    for container in containers:
        try:
            # Tìm tên nhà xe bên trong container này
            name_el = container.find_element(By.CLASS_NAME, "bus-name")
            name = (name_el.text or "").strip()
            comp_id = container.get_attribute("data-company-id") or "Unknown"
            
            if name:
                ids.append([name, comp_id])
        except Exception:
            # Bỏ qua nếu không tìm thấy tên (có thể là container rác)
            continue
            
    print(f"  - Tìm thấy {len(ids)} nhà xe từ container.")
    return ids


# ======================
# LƯU LIKE TICKET + MERGE MASTER (JSONL)
# ======================
def _ensure_facility_dirs():
    os.makedirs(FACILITY_BASE_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    daily_dir = os.path.join(FACILITY_BASE_DIR, f"date={today}")
    os.makedirs(daily_dir, exist_ok=True)
    return daily_dir, today


def _normalize_facilities(df: pd.DataFrame) -> pd.DataFrame:
    """
    Đảm bảo cột Facilities là list (mảng) để ghi JSONL đúng chuẩn.
    """
    out = df.copy()
    if "Facilities" not in out.columns:
        out["Facilities"] = [[]]
    out["Facilities"] = out["Facilities"].apply(_norm_fac_list)
    return out


def _save_daily_jsonl(df: pd.DataFrame):
    daily_dir, today = _ensure_facility_dirs()
    out = _normalize_facilities(df)
    daily_path = os.path.join(daily_dir, f"bus_facility_{today}.jsonl")
    out.to_json(daily_path, orient="records", lines=True, force_ascii=False)
    print(f"💾 Đã lưu file NGÀY (JSONL): {daily_path}")


def _read_master_jsonl() -> pd.DataFrame:
    if not os.path.exists(MASTER_PATH):
        return pd.DataFrame(columns=["Id", "Bus_Name", "Facilities"])
    try:
        return pd.read_json(MASTER_PATH, orient="records", lines=True, dtype={"Id": "Int64"})
    except ValueError:
        # file rỗng
        return pd.DataFrame(columns=["Id", "Bus_Name", "Facilities"])


def _merge_to_master_jsonl(df_new: pd.DataFrame):
    """
    Merge theo Bus_Name:
    - Nếu đã có trong master: GIỮ nguyên Id cũ, Facilities = UNION(master, new)
    - Nếu chưa có: thêm dòng mới, cấp Id tăng dần.
    """
    master = _read_master_jsonl()
    master = _normalize_facilities(master)

    # Chuẩn hóa df_new
    df_new = df_new.copy()
    for c in ["Id", "Bus_Name", "Facilities"]:
        if c not in df_new.columns:
            df_new[c] = None
    df_new = _normalize_facilities(df_new)

    # Hợp nhất (union) các tiện ích trong batch theo Bus_Name (tránh thiếu do nhiều bản ghi)
    if not df_new.empty:
        df_new = (
            df_new.groupby("Bus_Name", as_index=False)["Facilities"]
            .apply(lambda s: _union_lists(s.tolist()))
            .reset_index()
        )

    # Chuẩn bị master map
    master_map = {}  # Bus_Name -> (Id, Facilities)
    if not master.empty:
        for _, r in master.iterrows():
            bus = str(r.get("Bus_Name") or "").strip()
            if not bus:
                continue
            mid = int(r.get("Id")) if pd.notna(r.get("Id")) else None
            facs = _norm_fac_list(r.get("Facilities"))
            if bus not in master_map:
                master_map[bus] = [mid, facs]
            else:
                # nếu trùng Bus_Name nhiều dòng trong master -> union
                master_map[bus][1] = sorted(set(master_map[bus][1]).union(set(facs)))

    max_id = 0
    if master_map:
        # lấy max từ master hiện có
        max_id = max([v[0] or 0 for v in master_map.values()])

    # Merge
    for _, r in df_new.iterrows():
        bus = str(r.get("Bus_Name") or "").strip()
        if not bus:
            continue
        facs_new = _norm_fac_list(r.get("Facilities"))
        if bus in master_map and master_map[bus][0] is not None:
            # cập nhật union tiện ích
            facs_old = master_map[bus][1]
            master_map[bus][1] = sorted(set(facs_old).union(set(facs_new)))
        else:
            # thêm mới
            max_id += 1
            master_map[bus] = [max_id, facs_new]

    # Đưa về DataFrame
    merged_rows = []
    for bus, (mid, facs) in master_map.items():
        merged_rows.append({"Id": int(mid) if mid is not None else None,
                            "Bus_Name": bus,
                            "Facilities": _norm_fac_list(facs)})
    updated = pd.DataFrame(merged_rows, columns=["Id", "Bus_Name", "Facilities"]).sort_values("Id")

    # Ghi đè master
    updated.to_json(MASTER_PATH, orient="records", lines=True, force_ascii=False)
    print(f"✅ Đã MERGE vào MASTER (JSONL): {MASTER_PATH} — tổng {len(updated)} dòng")


# ======================
# HÀM CHÍNH
# ======================
def crwl_facility():
    provinces_keys = {
        "binh-thuan": "129t1111",
    }

    # Vexere hay trả về data tốt hơn nếu query ngày ngày mai
    date_query = (datetime.now() + timedelta(days=1)).strftime("%d-%m-%Y")

    df = pd.DataFrame(columns=["Id", "Bus_Name", "Facilities"])
    driver = initialize_driver()

    try:
        for province, key in provinces_keys.items():
            comp = get_company_id(province, key, driver, date_query)
            uniq_ids = sorted(set([i[1] for i in comp if len(i) == 2]))
            for company_id in uniq_ids:
                url = f"https://vexere.com/vi-VN/ve-xe-khach-tu-sai-gon-di-{province}-{key}.html?date={date_query}&companies={company_id}&sort=time%3Aasc"
                try:
                    driver.get(url)
                    time.sleep(1)
                    entries = get_bus_names_and_buttons(driver)
                    print(f"\nProcessing {len(entries)} bus entries for company {company_id} / {province}")
                    batch = []
                    seen_button = set()
                    seen_name = set()
                    for e in entries:
                        name = e["name"]
                        bid = id(e["button"])
                        if bid in seen_button or name in seen_name:
                            continue
                        seen_button.add(bid)
                        seen_name.add(name)
                        facs = extract_facilities_for_bus(driver, e)
                        batch.append({"Id": None, "Bus_Name": name, "Facilities": facs})
                    if batch:
                        temp = pd.DataFrame(batch, columns=["Id", "Bus_Name", "Facilities"])
                        df = pd.concat([df, temp], ignore_index=True)
                except Exception as ex:
                    print(f"Error processing {province}/{company_id}: {ex}")
    finally:
        driver.quit()

    # ✅ Hợp nhất (union) tất cả Facilities theo Bus_Name để tránh thiếu sót
    if not df.empty:
        df = _normalize_facilities(df)
        df = (
            df.groupby("Bus_Name", as_index=False)["Facilities"]
            .apply(lambda s: _union_lists(s.tolist()))
            .reset_index()
        )

    # 1) LƯU FILE NGÀY (JSONL) — đã là bản union đầy đủ
    _save_daily_jsonl(df)

    # 2) MERGE vào file tổng hợp (JSONL) — cập nhật union với master
    _merge_to_master_jsonl(df)

    print("🎉 Facility crawl done (saved daily JSONL + merged master JSONL).")


if __name__ == "__main__":
    crwl_facility()