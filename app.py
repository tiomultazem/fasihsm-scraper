import requests
import os, time, json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, flash, url_for, jsonify, session, Response
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from urllib.parse import unquote
import csv
import io
import base64
import tempfile, uuid
_csv_temp_store = {}  # token → filepath

# app = Flask(__name__)
app = Flask(__name__, static_url_path='/fasihsm-fetcher/static')
app.secret_key = 'bebas_aja_yang_penting_aman'
app.config['APPLICATION_ROOT'] = '/fasihsm-fetcher'
app.config['PREFERRED_URL_SCHEME'] = 'http'

chrome_driver = None

# ── Session State Persistence ─────────────────────────────────────────────────

STATE_FILE = '.session_state.json'
SESSION_CACHE = '.session_cache.json'

def save_state(is_running: bool):
    with open(STATE_FILE, 'w') as f:
        json.dump({'is_running': is_running}, f)

def load_state() -> bool:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f).get('is_running', False)
        except: return False
    return False

def check_session() -> bool:
    return load_state()

def save_session_cache(cookies: list, bearer: str, csrf: str, user_agent: str):
    with open(SESSION_CACHE, 'w') as f:
        json.dump({'cookies': cookies, 'bearer': bearer, 'csrf': csrf, 'user_agent': user_agent}, f)

def load_session_cache() -> dict:
    if os.path.exists(SESSION_CACHE):
        try:
            with open(SESSION_CACHE) as f:
                return json.load(f)
        except: pass
    return {}

def clear_session_cache():
    for f in [STATE_FILE, SESSION_CACHE]:
        if os.path.exists(f):
            os.remove(f)

# ── Helpers ───────────────────────────────────────────────────────────────────

def build_driver(headless=False):
    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    if headless:
        options.add_argument("--headless=new")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    driver.execute_cdp_cmd("Network.enable", {})
    return driver

def format_fasih_date(date_str, timezone_label="WITA"):
    if not date_str or date_str == "-":
        return "-"
    tz_offset = {"WIB": 7, "WITA": 8, "WIT": 9}
    offset = tz_offset.get(timezone_label, 8)
    try:
        clean_date = date_str.split(".")[0]
        if "T" not in clean_date:
            return date_str
        dt = datetime.strptime(clean_date, "%Y-%m-%dT%H:%M:%S")
        dt_local = dt + timedelta(hours=offset)
        return dt_local.strftime(f"%d %b %Y at %H.%M {timezone_label}")
    except:
        try: return f"{date_str[:10]} (Raw)"
        except: return date_str

def get_req_session():
    global chrome_driver
    if chrome_driver is not None:
        try:
            cookies = chrome_driver.get_cookies()
            bearer = chrome_driver.execute_script("return window.localStorage.getItem('token');") or ""
            user_agent = chrome_driver.execute_script("return navigator.userAgent;") or ""
            csrf = ""
            for c in cookies:
                if c['name'] == 'XSRF-TOKEN':
                    csrf = unquote(c['value'])
                    break
            save_session_cache(cookies, bearer.replace('"', ''), csrf, user_agent)
        except Exception as e:
            print(f"[get_req_session] Gagal refresh dari driver: {e}")

    cache = load_session_cache()
    req_session = requests.Session()
    if not cache:
        return req_session
    for cookie in cache.get('cookies', []):
        req_session.cookies.set(cookie['name'], cookie['value'])
    req_session.headers.update({
        "User-Agent": cache.get('user_agent', ''),
        "Accept": "application/json, text/plain, */*",
        "X-XSRF-TOKEN": cache.get('csrf', ''),
        "Authorization": f"Bearer {cache.get('bearer', '')}" if cache.get('bearer') else ""
    })
    return req_session

def fetch_list_surveys(session, survey_type="Pencacahan", page_size=100):
    url = f"https://fasih-sm.bps.go.id/survey/api/v1/surveys/datatable?surveyType={survey_type}"
    payload = {"pageNumber": 0, "pageSize": page_size, "sortBy": "CREATED_AT", "sortDirection": "DESC", "keywordSearch": ""}
    try:
        response = session.post(url, json=payload, timeout=15)
        if response.status_code == 200:
            data = response.json()
            return data.get('data', {}).get('content', [])
    except Exception as e:
        print(f"[fetch_list_surveys] Error: {e}")
    return []

def fetch_json(req_session, url):
    try:
        r = req_session.get(url, timeout=30)
        r.raise_for_status()
        res = r.json()
        return res.get("data") if res.get("data") is not None else {}
    except Exception as e:
        print(f"[fetch_json] Error {url}: {e}")
        return {}

# ── Metadata Survei ────────────────────────────────────────────────────────────────────

def fetch_full_survey_settings_flat(req_session, survey_id):
    with ThreadPoolExecutor(max_workers=3) as executor:
        f_det = executor.submit(fetch_json, req_session, f"https://fasih-sm.bps.go.id/survey/api/v1/surveys/{survey_id}")
        f_per = executor.submit(fetch_json, req_session, f"https://fasih-sm.bps.go.id/survey/api/v1/survey-periods?surveyId={survey_id}")
        det = f_det.result()
        per = f_per.result()

    per_list = per if isinstance(per, list) else []
    region_id = det.get("regionGroupId")

    reg = fetch_json(req_session, f"https://fasih-sm.bps.go.id/region/api/v1/region-metadata?id={region_id}") if region_id else {}

    act_per = next((p for p in per_list if p.get("isActive")), per_list[0] if per_list else {})
    return {
        "judul": det.get("name", "-"),
        "tipe": det.get("surveyType", "-"),
        "mode": ", ".join([m.get("mode", "") for m in det.get("surveyModeList", [])]) if det.get("surveyModeList") else "-",
        "wilayah_ver": reg.get("groupName", "-"),
        "level_wilayah": " > ".join([l.get("name", "") for l in reg.get("level", [])]) if reg.get("level") else "-",
        "jenis_panel": "Panel" if det.get("panelType") else "Non-Panel",
        "jenis_pencacah": "Banyak" if det.get("isMultiPencacah") else "Satu",
        "periode_aktif": act_per.get("name", "-"),
        "tgl_mulai": format_fasih_date(act_per.get("startDate"), timezone_label="WITA"),
        "tgl_selesai": format_fasih_date(act_per.get("endDate"), timezone_label="WITA"),
        "id_periode": act_per.get("id", "-"),
    }

# ── Petugas ────────────────────────────────────────────────────────────────────
def fetch_petugas_all_roles(req_session, survey_id, period_id):
    role_url = f"https://fasih-sm.bps.go.id/survey/api/v1/survey-roles?surveyId={survey_id}"
    try:
        role_res = req_session.get(role_url, timeout=15)
        roles_data = role_res.json().get("data", []) if role_res.status_code == 200 else []
    except:
        return {"roles": [], "data": {}}

    def fetch_by_role(role):
        role_id = role.get("id")
        group_id = role.get("surveyRoleGroupId")
        api_url = (
            f"https://fasih-sm.bps.go.id/analytic/api/v2/survey-period-role-user/datatable"
            f"?surveyPeriodId={period_id}&surveyRoleGroupId={group_id}&surveyRoleId={role_id}"
        )
        payload = {"pageNumber": 0, "pageSize": 100, "sortBy": "ID", "sortDirection": "ASC", "keywordSearch": ""}
        try:
            res = req_session.post(api_url, json=payload, timeout=15)
            if res.status_code != 200:
                return []
            rows = []
            for i, item in enumerate(res.json().get("data", {}).get("searchData", []), start=1):
                user = item.get("user", {})
                regions = [r.get("smallestRegionCode") for r in item.get("smallestRegionCodes", [])]
                rows.append({
                    "no":      i,
                    "nama":    user.get("fullname") or "-",
                    "email":   user.get("email") or "-",
                    "wilayah": ", ".join([str(r) for r in regions]) if regions else "-",
                })
            return rows
        except:
            return []

    # key = slug dari description, label = description asli
    roles_meta = []
    data = {}
    with ThreadPoolExecutor(max_workers=len(roles_data) or 1) as executor:
        futures = {}
        for role in roles_data:
            desc = role.get("description", "")
            key = desc.lower().replace(" ", "_")
            roles_meta.append({"key": key, "label": desc})
            futures[key] = executor.submit(fetch_by_role, role)
        for key, future in futures.items():
            data[key] = future.result()

    return {"roles": roles_meta, "data": data}

# ── Ringkasan Sampel ────────────────────────────────────────────────────────────────────

def fetch_sampel_aggregation(req_session, period_id):
    url = "https://fasih-sm.bps.go.id/analytic/api/v2/assignment/datatable-all-user-survey-periode"
    payload = {
        "draw": 1,
        "columns": [
            {"data": "id", "name": "", "searchable": True, "orderable": False, "search": {"value": "", "regex": False}},
            {"data": "codeIdentity", "name": "", "searchable": True, "orderable": False, "search": {"value": "", "regex": False}},
            {"data": "data1", "name": "", "searchable": True, "orderable": True, "search": {"value": "", "regex": False}},
            {"data": "data2", "name": "", "searchable": True, "orderable": True, "search": {"value": "", "regex": False}},
            {"data": "data3", "name": "", "searchable": True, "orderable": True, "search": {"value": "", "regex": False}},
            {"data": "data4", "name": "", "searchable": True, "orderable": True, "search": {"value": "", "regex": False}},
        ],
        "order": [{"column": 0, "dir": "asc"}],
        "start": 0, "length": 1,
        "search": {"value": "", "regex": False},
        "assignmentExtraParam": {
            "region1Id": None, "region2Id": None, "region3Id": None, "region4Id": None,
            "region5Id": None, "region6Id": None, "region7Id": None, "region8Id": None,
            "region9Id": None, "region10Id": None,
            "surveyPeriodId": period_id,
            "assignmentErrorStatusType": -1,
            "assignmentStatusAlias": None,
            "data1": None, "data2": None, "data3": None, "data4": None,
            "data5": None, "data6": None, "data7": None, "data8": None,
            "data9": None, "data10": None,
            "userIdResponsibility": None, "currentUserId": None, "regionId": None,
            "filterTargetType": "TARGET_ONLY"
        }
    }
    try:
        res = req_session.post(url, json=payload, timeout=15)
        if res.status_code == 200:
            data = res.json()
            return {"total": data.get("totalHit", 0), "statuses": data.get("searchAggregation", [])}
    except Exception as e:
        print(f"[fetch_sampel_aggregation] {e}")
    return {"total": 0, "statuses": []}


def fetch_sampel_by_status(req_session, period_id, n_target, batch_size, status_alias, tz="WITA"):
    url = "https://fasih-sm.bps.go.id/analytic/api/v2/assignment/datatable-all-user-survey-periode"
    all_rows = []
    start_idx = 0
    draw_count = 1

    while start_idx < n_target:
        payload = {
            "draw": draw_count,
            "columns": [
                {"data": "id", "name": "", "searchable": True, "orderable": False, "search": {"value": "", "regex": False}},
                {"data": "codeIdentity", "name": "", "searchable": True, "orderable": False, "search": {"value": "", "regex": False}},
                {"data": "data1", "name": "", "searchable": True, "orderable": True, "search": {"value": "", "regex": False}},
                {"data": "data2", "name": "", "searchable": True, "orderable": True, "search": {"value": "", "regex": False}},
                {"data": "data3", "name": "", "searchable": True, "orderable": True, "search": {"value": "", "regex": False}},
                {"data": "data4", "name": "", "searchable": True, "orderable": True, "search": {"value": "", "regex": False}},
            ],
            "order": [{"column": 0, "dir": "asc"}],
            "start": start_idx,
            "length": batch_size,
            "search": {"value": "", "regex": False},
            "assignmentExtraParam": {
                "region1Id": None, "region2Id": None, "region3Id": None, "region4Id": None,
                "region5Id": None, "region6Id": None, "region7Id": None, "region8Id": None,
                "region9Id": None, "region10Id": None,
                "surveyPeriodId": period_id,
                "assignmentErrorStatusType": -1,
                "assignmentStatusAlias": None if status_alias == "SEMUA" else status_alias,
                "data1": None, "data2": None, "data3": None, "data4": None,
                "data5": None, "data6": None, "data7": None, "data8": None,
                "data9": None, "data10": None,
                "userIdResponsibility": None, "currentUserId": None, "regionId": None,
                "filterTargetType": "TARGET_ONLY"
            }
        }
        try:
            res = req_session.post(url, json=payload, timeout=30)
            if res.status_code != 200:
                break
            raw = res.json()
            n_target = min(n_target, raw.get("totalHit", n_target))
            for item in raw.get("searchData", []):
                reg = item.get("region", {})
                lvl3 = reg.get("level1", {}).get("level2", {}).get("level3", {}) or {}
                lvl4 = lvl3.get("level4", {}) or {}
                lvl5 = lvl4.get("level5", {}) or {}
                lvl6 = lvl5.get("level6", {}) or {}   # ← tambah ini
                all_rows.append({
                    "no":         len(all_rows) + 1,
                    "id_sls":     item.get("codeIdentity", "-"),
                    "kk":         item.get("data1") or "-",
                    "anggota":    item.get("data2") or "-",      # ← tambah ini
                    "alamat":     item.get("data3") or "-",
                    "status_kb":  item.get("data4") or "-",
                    "status_dok": item.get("assignmentStatusAlias", "-"),
                    "pencacah":   item.get("currentUserFullname") or "-",
                    "email_pcj":  item.get("currentUserUsername") or "-",
                    "kec":        f"{lvl3.get('code','-')}. {lvl3.get('name','-')}" if lvl3 else "-",
                    "des":        f"{lvl4.get('code','-')}. {lvl4.get('name','-')}" if lvl4 else "-",
                    "sls":        lvl5.get("name", "-") if lvl5 else "-",
                    "sub_sls":    lvl6.get("code", "-") if lvl6 else "-",   # ← tambah ini (perlu lvl6)
                    "modified":   format_fasih_date(item.get("dateModified"), timezone_label=tz),
                    "sample_id":  item.get("id", "-"),          # ← tambah ini
                    "lat":        item.get("latitude", 0),       # ← tambah ini
                    "lon":        item.get("longitude", 0),      # ← tambah ini
                    "created":    format_fasih_date(item.get("dateCreated"), timezone_label=tz),   # ← tambah ini
                })
            start_idx += batch_size
            draw_count += 1
            time.sleep(0.1)
        except Exception as e:
            print(f"[fetch_sampel_by_status] {e}")
            break
    return all_rows

# ── Download Sampel ────────────────────────────────────────────────────────────────────

import base64

@app.route('/api/sampel-detail-csv', methods=['POST'])
def api_sampel_detail_csv():
    if not check_session():
        return jsonify({"error": "Sesi tidak aktif"}), 401

    body        = request.get_json()
    sample_ids  = body.get("sample_ids", [])
    survey_name = body.get("survey_name", "rincian_sampel")
    if not sample_ids:
        return jsonify({"error": "sample_ids kosong"}), 400

    req_session = get_req_session()
    total       = len(sample_ids)

    def generate():
        all_rows = []
        for i, s_id in enumerate(sample_ids, 1):
            url = f"https://fasih-sm.bps.go.id/assignment-general/api/assignment/get-by-id-with-data-for-scm?id={s_id}"
            try:
                res = req_session.get(url, timeout=15)
                if res.status_code == 200:
                    row = parse_detail_sample(res.json())
                    if row:
                        all_rows.append(row)
            except Exception as e:
                print(f"[detail-csv] ERROR {s_id}: {e}")
            time.sleep(0.1)
            yield f"data: {{\"progress\": {i}, \"total\": {total}}}\n\n"

        if all_rows:
            all_keys = []
            seen = set()
            for row in all_rows:
                for k in row.keys():
                    if k not in seen:
                        all_keys.append(k)
                        seen.add(k)

            token = str(uuid.uuid4())
            tmp   = tempfile.NamedTemporaryFile(delete=False, suffix='.csv', mode='w',
                                                encoding='utf-8-sig', newline='')
            writer = csv.DictWriter(tmp, fieldnames=all_keys, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(all_rows)
            tmp.close()

            safe_name = survey_name.replace('"', '').replace('/', '-')
            _csv_temp_store[token] = {"path": tmp.name, "filename": f"{safe_name}.csv"}
            yield f"data: {{\"done\": true, \"token\": \"{token}\", \"filename\": \"{safe_name}.csv\"}}\n\n"
        else:
            yield f"data: {{\"done\": true, \"error\": \"Tidak ada data berhasil diambil\"}}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route('/api/sampel-detail-download/<token>')
def api_sampel_detail_download(token):
    entry = _csv_temp_store.pop(token, None)
    if not entry or not os.path.exists(entry["path"]):
        return "File tidak ditemukan atau sudah diunduh.", 404
    def stream_and_delete():
        try:
            with open(entry["path"], 'rb') as f:
                yield from f
        finally:
            os.remove(entry["path"])
    return Response(stream_and_delete(), mimetype='text/csv',
                    headers={"Content-Disposition": f"attachment; filename=\"{entry['filename']}\""})

def parse_detail_sample(json_response):
    if not json_response or not json_response.get("success"):
        return None
    raw_data = json_response.get("data", {})
    result = {
        "Sample ID":       raw_data.get("_id"),
        "ID SLS":          raw_data.get("code_identity"),
        "Status Dokumen":  raw_data.get("assignment_status_alias"),
        "Latitude":        raw_data.get("latitude"),
        "Longitude":       raw_data.get("longitude"),
        "Petugas Terakhir": raw_data.get("current_user_fullname"),
    }
    pre_defined_str = raw_data.get("pre_defined_data", "{}")
    try:
        pre_data = json.loads(pre_defined_str)
        for item in pre_data.get("predata", []):
            val = item.get("answer")
            result[f"Prelist_{item.get('dataKey')}"] = str(val) if not isinstance(val, (list, dict)) else json.dumps(val, ensure_ascii=False)
    except:
        pass
    data_content_str = raw_data.get("data", "{}")
    try:
        content_data = json.loads(data_content_str)
        result["Waktu Submit"] = content_data.get("updatedAt")
        for ans in content_data.get("answers", []):
            val = ans.get("answer")
            if isinstance(val, list):
                result[f"Ans_{ans.get('dataKey')}"] = ", ".join(
                    [str(v.get('label', v)) if isinstance(v, dict) else str(v) for v in val]
                )
            else:
                result[f"Ans_{ans.get('dataKey')}"] = val
    except:
        pass
    return result

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def home():
    user = session.get('fasih_user') or os.getenv("FASIH_USER")
    return render_template('index.html', is_running=load_state(), user=user)

@app.route('/import-env', methods=['POST'])
def import_env():
    load_dotenv(override=True)
    user = os.getenv("FASIH_USER")
    pwd = os.getenv("FASIH_PASS")
    if user is None or pwd is None:
        flash('Warning: File .env tidak ditemukan!', 'danger')
    elif not user.strip() or not pwd.strip():
        flash('Isian .env nya salah (kosong)!', 'warning')
    else:
        session['fasih_user'] = user  # simpan ke session
        flash('Berhasil impor.', 'success')
    return redirect(url_for('home'))

@app.route('/interrupt')
def interrupt():
    global chrome_driver
    if not load_state() and chrome_driver is None:
        return redirect(url_for('home'))
    try:
        os.system("taskkill /f /im chromedriver.exe /t")
        os.system("taskkill /f /im chrome.exe /t")
    except: pass
    chrome_driver = None
    save_state(False)
    clear_session_cache()
    flash("Sistem diinterupsi! Semua proses Chrome dipaksa berhenti.", "warning")
    return redirect(url_for('home'))

@app.route('/open-chrome')
def open_chrome():
    global chrome_driver
    if load_state():
        flash("Sesi masih aktif!", "danger")
        return redirect(url_for('home'))
    user = os.getenv("FASIH_USER")
    pwd  = os.getenv("FASIH_PASS")
    if not user or not pwd:
        flash("Gagal: Variabel belum diimpor atau sudah dihapus! Klik 'Import .env' dulu.", "danger")
        return redirect(url_for('home'))
    try:
        chrome_driver = build_driver(headless=False)
        chrome_driver.get("https://fasih-sm.bps.go.id/oauth_login.html")
        wait = WebDriverWait(chrome_driver, 20)
        sso_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.login-button")))
        chrome_driver.execute_script("arguments[0].click();", sso_btn)
        wait.until(EC.presence_of_element_located((By.ID, "username"))).send_keys(user)
        chrome_driver.find_element(By.ID, "password").send_keys(pwd + Keys.ENTER)
        wait.until(lambda d: "fasih-sm.bps.go.id" in d.current_url)
        time.sleep(3)
        get_req_session()  # simpan cache segera setelah login
        save_state(True)
        flash("Sesi Windowed aktif! Login sukses.", "success")
    except Exception as e:
        if chrome_driver:
            try: chrome_driver.quit()
            except: pass
        chrome_driver = None
        save_state(False)
        clear_session_cache()
        flash(f"Gagal: {str(e)}", "danger")
    return redirect(url_for('home'))

@app.route('/close-chrome')
def close_chrome():
    global chrome_driver
    if chrome_driver:
        try: chrome_driver.quit()
        except: pass
        chrome_driver = None
    try:
        os.system("taskkill /f /im chromedriver.exe /t")
        os.system("taskkill /f /im chrome.exe /t")
    except: pass
    save_state(False)
    clear_session_cache()
    flash("Sesi Chrome dan seluruh proses terkait telah dipaksa berhenti.", "info")
    return redirect(url_for('home'))

# ── List Survei ───────────────────────────────────────────────────────────────

@app.route('/listsurvei')
@app.route('/listsurvei/<category>')
@app.route('/listsurvei/<category>/<survey_id>')
def listsurvei(category="Pencacahan", survey_id=None):
    if not check_session():
        flash("Buka sesi chrome terlebih dahulu.", "danger")
        return redirect(url_for('home'))

    req_session = get_req_session()
    raw = fetch_list_surveys(req_session, survey_type=category)

    surveys = []
    for i, item in enumerate(raw, start=1):
        surveys.append({
            "no":           i,
            "judul_survei": item.get("name", "-"),
            "id":           item.get("id", "-"),
            "unit":         item.get("unit", "-"),
            "dibuat_pada":  format_fasih_date(item.get("createdAt"), timezone_label="WITA")
        })

    meta = None
    petugas = []
    if survey_id:
        meta = fetch_full_survey_settings_flat(req_session, survey_id)
        if meta and meta.get("id_periode") and meta["id_periode"] != "-":
            # baru
            petugas = fetch_petugas_all_roles(req_session, survey_id, meta["id_periode"])

    return render_template('listsurvei.html', surveys=surveys, active_cat=category,
                           meta=meta, selected_id=survey_id, petugas=petugas)

# ── Proteksi & Error Handlers ─────────────────────────────────────────────────

@app.route('/import-env', methods=['GET'])
def import_env_get():
    flash(f"Path {request.path} tidak bisa diakses langsung.", "danger")
    return redirect(url_for('home'))

@app.errorhandler(404)
def page_not_found(e):
    flash(f"Path {request.path} tidak ada.", "danger")
    return redirect(url_for('home'))

@app.errorhandler(405)
def method_not_allowed(e):
    flash(f"Path {request.path} tidak bisa diakses dengan method ini.", "danger")
    return redirect(url_for('home'))

@app.route('/secret-wipe')
def secret_wipe():
    for key in ["FASIH_USER", "FASIH_PASS"]:
        os.environ.pop(key, None)
    flash("Variabel dihapus!", "warning")
    return redirect(url_for('home'))

# ── Sampel ────────────────────────────────────────────────────────────────────

@app.route('/listsurvei/<category>/<survey_id>/sampel', methods=['GET', 'POST'])
def sampel(category, survey_id):
    if not check_session():
        flash("Buka sesi chrome terlebih dahulu.", "danger")
        return redirect(url_for('home'))

    req_session = get_req_session()

    # ambil meta untuk dapat period_id
    meta = fetch_full_survey_settings_flat(req_session, survey_id)
    period_id = meta.get("id_periode") if meta else None

    if not period_id or period_id == "-":
        flash("Periode aktif tidak ditemukan.", "danger")
        return redirect(url_for('listsurvei', category=category, survey_id=survey_id))

    tz = request.form.get("tz", "WITA")

    # selalu fetch aggregation dulu
    agg = fetch_sampel_aggregation(req_session, period_id)

    sampel_rows = None
    active_status = None

    if request.method == "POST" and "fetch_sampel" in request.form:
        n_target   = int(request.form.get("n_target", 50))
        batch_size = int(request.form.get("batch_size", 25))
        status_alias = request.form.get("status_alias", "SEMUA")
        active_status = status_alias
        sampel_rows = fetch_sampel_by_status(req_session, period_id, n_target, batch_size, status_alias, tz=tz)

    return render_template(
        'sampel.html',
        category=category,
        survey_id=survey_id,
        meta=meta,
        agg=agg,
        sampel_rows=sampel_rows,
        active_status=active_status,
        tz=tz,
    )

@app.route('/api/sampel-status')
def api_sampel_status():
    if not check_session():
        return jsonify({"error": "Sesi tidak aktif"}), 401
    period_id = request.args.get("period_id", "")
    if not period_id:
        return jsonify({"error": "period_id diperlukan"}), 400
    req_session = get_req_session()
    result = fetch_sampel_aggregation(req_session, period_id)
    return jsonify(result)


@app.route('/api/sampel-fetch', methods=['POST'])
def api_sampel_fetch():
    if not check_session():
        return jsonify({"error": "Sesi tidak aktif"}), 401
    body = request.get_json()
    period_id    = body.get("period_id", "")
    n_target     = int(body.get("n_target", 50))
    batch_size   = int(body.get("batch_size", 25))
    status_alias = body.get("status_alias", "SEMUA")
    tz           = body.get("tz", "WITA")
    if not period_id:
        return jsonify({"error": "period_id diperlukan"}), 400
    req_session = get_req_session()
    rows = fetch_sampel_by_status(req_session, period_id, n_target, batch_size, status_alias, tz=tz)
    return jsonify({"rows": rows})

# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    from werkzeug.middleware.dispatcher import DispatcherMiddleware
    from werkzeug.serving import run_simple

    def dummy_app(environ, start_response):
        start_response('200 OK', [('Content-Type', 'text/plain')])
        return [b'']

    application = DispatcherMiddleware(dummy_app, {
        '/fasihsm-fetcher': app
    })

    run_simple('0.0.0.0', 5000, application, use_reloader=True, use_debugger=True)
