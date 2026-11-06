from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import time
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse, urlsplit, urlunsplit

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.core.config import settings
from app.core.models import SourceItem
from app.sources.base import DataSource


class Binxin77HistorySource(DataSource):
    """EasyConnect -> 镔鑫 history -> 当天记录元数据 -> 延迟缓存图片。"""

    def __init__(self, project_config: dict):
        super().__init__(project_config)
        self.source = project_config["source"]
        self.data_root = Path(settings.storage.data_root).resolve()

    # ------------------------------------------------------------------
    # EasyConnect / intranet readiness
    # ------------------------------------------------------------------
    def _probe(self, url: str) -> bool:
        try:
            response = requests.get(url, timeout=3)
            return response.status_code < 500
        except Exception:
            return False

    def _easyconnect_command(self) -> list[str]:
        ec = self.source.get("easyconnect", {})
        command = ec.get("launch_command") or []
        if command:
            return [str(x) for x in command]

        if not ec.get("auto_detect", True) or os.name != "nt":
            return []

        candidates = ec.get("candidate_executables") or [
            r"C:\Program Files (x86)\Sangfor\SSL\SangforCSClient\SangforCSClient.exe",
            r"C:\Program Files\Sangfor\SSL\SangforCSClient\SangforCSClient.exe",
            r"C:\Program Files (x86)\Sangfor\AF\SSL\SangforCSClient\SangforCSClient.exe",
            r"C:\Program Files\Sangfor\AF\SSL\SangforCSClient\SangforCSClient.exe",
        ]
        for candidate in candidates:
            if Path(candidate).exists():
                return [candidate]
        return []

    def _ensure_easyconnect(self) -> None:
        ec = self.source.get("easyconnect", {})
        if not ec.get("enabled", True):
            return

        url = ec.get("healthcheck_url") or self.source["history_url"]
        if self._probe(url):
            return

        command = self._easyconnect_command()
        if command:
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        timeout = int(ec.get("connect_timeout_seconds", 90))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._probe(url):
                return
            time.sleep(2)

        command_hint = command[0] if command else "未找到 EasyConnect 可执行文件，请在 config 中填写 launch_command"
        raise RuntimeError(
            f"EasyConnect/内网未就绪：{url}；等待 {timeout}s 后仍无法访问。EasyConnect: {command_hint}"
        )

    def check_available(self) -> bool:
        try:
            self._ensure_easyconnect()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Binxin web login
    # ------------------------------------------------------------------
    def _credential(self, key: str) -> str:
        auth = self.source.get("auth", {})
        return str(auth.get(key, "")).strip()

    def _is_login_page(self, page) -> bool:
        selectors = self.source.get("auth", {}).get("selectors", {})
        username_selector = selectors.get("username", 'input[placeholder="请输入用户名"]')
        try:
            username = page.locator(username_selector).first
            if username.count() and username.is_visible(timeout=800):
                return True
        except Exception:
            pass
        return "#/login" in page.url

    def _ensure_site_login(self, page) -> None:
        """Login to the Binxin web app when the persistent profile has no valid session."""
        auth = self.source.get("auth", {})
        settle_timeout_ms = int(auth.get("page_settle_timeout_ms", 10000))
        try:
            page.wait_for_function(
                "() => location.hash.includes('/login') || document.querySelector('.page-login') || document.querySelector('.page-history') || document.querySelector('.history')",
                timeout=settle_timeout_ms,
            )
        except PlaywrightTimeoutError:
            pass

        if not self._is_login_page(page):
            return

        username = self._credential("username")
        password = self._credential("password")
        if not username or not password:
            raise RuntimeError(
                "镔鑫网站需要登录，但 source.auth.username/password 未配置。"
                "请直接填写 config.json。"
            )

        selectors = auth.get("selectors", {})
        username_selector = selectors.get("username", 'input[placeholder="请输入用户名"]')
        password_selector = selectors.get("password", 'input[placeholder="请输入密码"]')
        login_button_selector = selectors.get("login_button", 'button:has-text("登录")')
        timeout_ms = int(auth.get("login_timeout_ms", 30000))

        username_input = page.locator(username_selector).first
        password_input = page.locator(password_selector).first
        login_button = page.locator(login_button_selector).first

        username_input.wait_for(state="visible", timeout=timeout_ms)
        password_input.wait_for(state="visible", timeout=timeout_ms)
        login_button.wait_for(state="visible", timeout=timeout_ms)
        username_input.fill(username)
        password_input.fill(password)
        login_button.click()

        try:
            page.wait_for_function(
                "() => !location.hash.includes('/login') && !document.querySelector('.page-login')",
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            # Keep credentials out of the exception message/logs.
            raise RuntimeError("镔鑫网站登录失败或等待登录结果超时，请检查用户名/密码。") from exc

        # Login usually lands on the index page. Always navigate back to the requested history page.
        page.goto(self.source["history_url"], wait_until="domcontentloaded", timeout=60000)

    def _open_history_page(self, page) -> None:
        """Open the OCR history page, using the edge-system login when required."""
        page.goto(self.source["history_url"], wait_until="domcontentloaded", timeout=60000)
        self._ensure_site_login(page)

        ready_selector = self.source.get("selectors", {}).get(
            "history_ready", ".page-history"
        )
        try:
            page.locator(ready_selector).first.wait_for(state="visible", timeout=30000)
            return
        except PlaywrightTimeoutError:
            login_url = str(self.source.get("auth", {}).get("login_url", "")).strip()
            if not login_url or self._is_login_page(page):
                raise

        page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
        self._ensure_site_login(page)
        page.goto(self.source["history_url"], wait_until="domcontentloaded", timeout=60000)
        self._ensure_site_login(page)
        page.locator(ready_selector).first.wait_for(state="visible", timeout=30000)

    # ------------------------------------------------------------------
    # Browser helpers
    # ------------------------------------------------------------------
    def _launch_context(self, playwright):
        browser_cfg = self.source.get("browser", {})
        user_data_dir = Path(browser_cfg.get("user_data_dir", "./runtime/browser/binxin")).resolve()
        user_data_dir.mkdir(parents=True, exist_ok=True)

        kwargs = {
            "headless": bool(browser_cfg.get("headless", False)),
            "accept_downloads": True,
            "viewport": None,
        }
        executable_path = browser_cfg.get("executable_path")
        if executable_path:
            kwargs["executable_path"] = executable_path
        else:
            kwargs["channel"] = browser_cfg.get("channel", "chrome")
        if browser_cfg.get("ignore_no_sandbox_arg", False):
            kwargs["ignore_default_args"] = ["--no-sandbox"]

        return playwright.chromium.launch_persistent_context(str(user_data_dir), **kwargs)

    def _set_day_filter(self, page, business_date: date) -> tuple[str, str]:
        selectors = self.source.get("selectors", {})
        start_selector = selectors.get(
            "start_date", '.page-history input.el-range-input[placeholder="开始时间"]'
        )
        end_selector = selectors.get(
            "end_date", '.page-history input.el-range-input[placeholder="结束时间"]'
        )

        date_format = str(self.source.get("date_input_format", "%Y/%m/%d"))
        date_text = business_date.strftime(date_format)
        start_text = f"{date_text} 00:00:00"
        end_text = f"{date_text} 23:59:59"

        start = page.locator(start_selector).first
        end = page.locator(end_selector).first
        start.wait_for(state="visible", timeout=20000)
        end.wait_for(state="visible", timeout=20000)

        start.fill(start_text)
        end.fill(end_text)
        end.press("Enter")

        # This page has no standalone query button in the supplied DOM snapshot.
        # Keep an optional selector for future versions that may add one.
        search_button = selectors.get("search_button")
        if search_button:
            locator = page.locator(search_button)
            if locator.count() and locator.first.is_visible():
                locator.first.click()

        try:
            page.wait_for_function(
                """
                ([startSelector, endSelector, startText, endText]) => {
                  const start = document.querySelector(startSelector);
                  const end = document.querySelector(endSelector);
                  const normalize = value => (value || '').replaceAll('/', '-');
                  return normalize(start?.value) === normalize(startText)
                    && normalize(end?.value) === normalize(endText);
                }
                """,
                arg=[start_selector, end_selector, start_text, end_text],
                timeout=int(self.source.get("export", {}).get("filter_timeout_ms", 30000)),
            )
        except PlaywrightTimeoutError:
            # Some builds do not print the whole range text while still applying the Vue model.
            page.wait_for_timeout(2500)

        return start_text, end_text

    @staticmethod
    def _day_bounds_ms(business_date: date) -> tuple[int, int]:
        """Return local-browser day bounds, inclusive through the final millisecond."""
        start = datetime.combine(business_date, datetime_time.min)
        next_day = datetime.combine(business_date + timedelta(days=1), datetime_time.min)
        return int(start.timestamp() * 1000), int(next_day.timestamp() * 1000) - 1

    @staticmethod
    def _request_json(request) -> dict:
        try:
            value = json.loads(request.post_data or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _capture_filtered_request(self, page, business_date: date):
        """Apply the UI filter and wait for the matching API response, not just input text."""
        lower_time, upper_time = self._day_bounds_ms(business_date)
        # The Element Plus input has second precision; its emitted upper bound ends in .000.
        ui_upper_time = upper_time - 999
        timeout_ms = int(
            self.source.get("api", {}).get(
                "request_timeout_ms",
                self.source.get("export", {}).get("filter_timeout_ms", 30000) + 30000,
            )
        )

        def matches(response) -> bool:
            if "/v1/history/condition-query" not in response.url:
                return False
            body = self._request_json(response.request)
            return (
                body.get("lowerTime") == lower_time
                and body.get("upperTime") == ui_upper_time
            )

        # The page fires an unfiltered query while it is opening.  Applying the
        # date range before that request settles lets its late response overwrite
        # the filtered table.  Wait for that initial query before changing dates.
        self._wait_history_query_idle(page, timeout_ms)
        try:
            with page.expect_response(matches, timeout=timeout_ms) as response_info:
                start_text, end_text = self._set_day_filter(page, business_date)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                f"{business_date.isoformat()} 时间筛选请求未生效或接口响应超时"
            ) from exc

        response = response_info.value
        if not response.ok:
            raise RuntimeError(
                f"时间筛选接口失败: HTTP {response.status} {response.status_text}"
            )
        self._wait_history_query_idle(page, timeout_ms)
        return start_text, end_text, response

    @staticmethod
    def _wait_history_query_idle(page, timeout_ms: int) -> None:
        """Wait until the history table has no visible Element Plus loading mask."""
        try:
            page.wait_for_function(
                """
                () => {
                  const root = document.querySelector('.page-history');
                  if (!root) return false;
                  return [...root.querySelectorAll('.el-loading-mask')].every(mask => {
                    const style = getComputedStyle(mask);
                    const rect = mask.getBoundingClientRect();
                    return style.display === 'none'
                      || style.visibility === 'hidden'
                      || style.opacity === '0'
                      || rect.width === 0
                      || rect.height === 0;
                  });
                }
                """,
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("历史数据查询未完成，无法安全应用时间窗口") from exc

    @staticmethod
    def _api_headers(request) -> dict[str, str]:
        """Copy only non-secret headers; the browser context supplies its own cookies."""
        available = request.all_headers()
        names = ("accept", "content-type", "origin", "referer", "x-server-origin")
        return {name: available[name] for name in names if available.get(name)}

    def _api_record_to_item(
        self,
        record: dict,
        business_date: date,
        page_no: int,
        row_no: int,
    ) -> SourceItem | None:
        try:
            acc_time = int(record.get("accTime"))
        except (TypeError, ValueError):
            return None

        lower_time, upper_time = self._day_bounds_ms(business_date)
        if not lower_time <= acc_time <= upper_time:
            raise RuntimeError(
                f"接口返回了目标日期之外的记录: id={record.get('id')}, accTime={acc_time}"
            )

        image_url = str(
            record.get("identifyFullUrl") or record.get("identifyPictureUrl") or ""
        ).strip()
        if not image_url:
            return None

        timestamp = datetime.fromtimestamp(acc_time / 1000).strftime("%Y/%m/%d %H:%M:%S")
        recognition = str(record.get("identifyBilletNumber") or "").strip()
        basename = Path(unquote(urlparse(image_url).path)).name
        record_id = record.get("id")
        if record_id is not None:
            source_key = f"{self.source.get('station', 'edge')}_{record_id}"
        else:
            key_material = f"{timestamp}|{basename}|{recognition}|{image_url}"
            digest = hashlib.sha1(key_material.encode("utf-8")).hexdigest()[:16]
            source_key = f"{timestamp.replace('/', '').replace(':', '').replace(' ', '_')}_{digest}"

        return SourceItem(
            source_key=source_key,
            recognition_text=recognition,
            image_url=image_url,
            metadata={
                "timestamp": timestamp,
                "station": self.source.get("station"),
                "sequence": (page_no - 1)
                * int(self.source.get("api", {}).get("page_size", 500))
                + row_no,
                "recognition_type": record.get("identifyTypeEnum") or "",
                "recognition": recognition,
                "recognition_method": record.get("identifyMethodEnum") or "",
                "manual_review_value": record.get("artificialCheckNumber") or "",
                "review_type": record.get("needCheck") or "",
                "recognition_status": record.get("identifyStatusEnum") or "",
                "image_url": image_url,
                "image_name": basename,
                "record_id": record_id,
                "camera_id": record.get("cameraId") or "",
                "source_page": page_no,
                "source_row": row_no,
            },
        )

    def _fetch_filtered_api_items(self, page, business_date: date):
        """Fetch every filtered record through the authenticated API, bypassing DOM paging."""
        start_text, end_text, filtered_response = self._capture_filtered_request(
            page, business_date
        )
        request = filtered_response.request
        parts = urlsplit(request.url)
        endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        headers = self._api_headers(request)
        body = self._request_json(request)
        lower_time, upper_time = self._day_bounds_ms(business_date)
        now_ms = int(time.time() * 1000)
        if lower_time <= now_ms <= upper_time:
            # Freeze an in-progress business day so new rows cannot shift later pages.
            upper_time = now_ms
        body["lowerTime"] = lower_time
        body["upperTime"] = upper_time

        api_cfg = self.source.get("api", {})
        page_size = int(api_cfg.get("page_size", 500))
        request_timeout_ms = int(api_cfg.get("request_timeout_ms", 60000))
        max_pages = int(self.source.get("pagination", {}).get("max_pages", 200))
        if page_size < 1:
            raise RuntimeError("source.api.page_size 必须大于 0")

        all_items: list[SourceItem] = []
        seen_keys: set[str] = set()
        page_no = 1
        total_pages = 1
        expected_count: int | None = None

        while page_no <= total_pages:
            if page_no > max_pages:
                raise RuntimeError(f"API 分页超过 max_pages={max_pages}，为避免死循环已停止")
            response = page.context.request.post(
                endpoint,
                params={
                    "currPage": page_no,
                    "pageSize": page_size,
                    "sort": "accTime,descending",
                },
                data=body,
                headers=headers,
                timeout=request_timeout_ms,
            )
            if not response.ok:
                raise RuntimeError(
                    f"历史数据第 {page_no} 页拉取失败: "
                    f"HTTP {response.status} {response.status_text}"
                )

            envelope = response.json()
            payload = envelope.get("data") if isinstance(envelope, dict) else None
            if not isinstance(payload, dict):
                raise RuntimeError(f"历史数据第 {page_no} 页返回格式错误")
            records = payload.get("data") or []
            if not isinstance(records, list):
                raise RuntimeError(f"历史数据第 {page_no} 页 records 格式错误")

            if expected_count is None:
                expected_count = int(payload.get("rowCount") or 0)
                total_pages = max(1, (expected_count + page_size - 1) // page_size)
                if total_pages > max_pages:
                    raise RuntimeError(
                        f"API 需要 {total_pages} 页，超过 max_pages={max_pages}"
                    )

            before_count = len(all_items)
            for row_no, record in enumerate(records, start=1):
                if not isinstance(record, dict):
                    continue
                item = self._api_record_to_item(record, business_date, page_no, row_no)
                if item and item.source_key not in seen_keys:
                    seen_keys.add(item.source_key)
                    all_items.append(item)

            if records and len(all_items) == before_count:
                raise RuntimeError(f"历史数据第 {page_no} 页与前页完全重复")
            page_no += 1

        if expected_count is not None and len(all_items) != expected_count:
            raise RuntimeError(
                f"历史数据拉取不完整: 接口报告 {expected_count} 条，实际有效 {len(all_items)} 条"
            )
        return start_text, end_text, all_items

    def _download_data_export(self, page, business_date: date) -> Path | None:
        export_cfg = self.source.get("export", {})
        if not export_cfg.get("archive_data_export", True):
            return None

        selector = self.source.get("selectors", {}).get("export_button", 'button:has-text("数据导出")')
        button = page.locator(selector).first
        if not button.count() or not button.is_visible():
            if export_cfg.get("archive_required", False):
                raise RuntimeError("找不到“数据导出”按钮")
            return None

        download_root = Path(
            self.source.get("browser", {}).get("download_dir", "./runtime/downloads/binxin")
        ).resolve()
        download_dir = download_root / business_date.isoformat()
        download_dir.mkdir(parents=True, exist_ok=True)

        try:
            with page.expect_download(timeout=int(export_cfg.get("download_timeout_ms", 60000))) as info:
                button.click()
            download = info.value
            suggested = download.suggested_filename or f"binxin_history_{business_date.isoformat()}.dat"
            destination = download_dir / suggested
            download.save_as(destination)
            return destination
        except PlaywrightTimeoutError:
            if export_cfg.get("archive_required", False):
                raise
            return None

    # ------------------------------------------------------------------
    # DOM scraping: collect metadata for all pages, not all image bytes.
    # ------------------------------------------------------------------
    @staticmethod
    def _text(cell) -> str:
        try:
            return cell.inner_text().strip()
        except Exception:
            return ""

    def _row_to_item(self, page, row, page_no: int, row_no: int) -> SourceItem | None:
        cells = row.locator("td")
        columns = self.source.get("selectors", {}).get("columns", {})

        def column(name: str, default: int) -> int:
            return int(columns.get(name, default))

        sequence_index = column("sequence", 1)
        image_index = column("image", 2)
        timestamp_index = column("timestamp", 3)
        recognition_type_index = column("recognition_type", 4)
        recognition_index = column("recognition", 5)
        recognition_method_index = column("recognition_method", 6)
        recognition_status_index = column("recognition_status", 7)
        review_type_index = column("review_type", 8)
        manual_value_index = column("manual_review_value", 9)
        required_index = max(image_index, timestamp_index, recognition_index)
        if cells.count() <= required_index:
            return None

        sequence = self._text(cells.nth(sequence_index))
        timestamp = self._text(cells.nth(timestamp_index))
        recognition_type = self._text(cells.nth(recognition_type_index))
        image = cells.nth(image_index).locator("img").first
        image_src = image.get_attribute("src") if image.count() else None
        recognition = self._text(cells.nth(recognition_index))
        recognition_method = self._text(cells.nth(recognition_method_index))
        recognition_status = self._text(cells.nth(recognition_status_index))
        review_type = self._text(cells.nth(review_type_index))
        manual_value = self._text(cells.nth(manual_value_index))

        if not image_src:
            return None
        if image_src.startswith("data:"):
            image_url = image_src
            basename = "inline-image"
        else:
            image_url = urljoin(page.url, image_src)
            basename = Path(unquote(urlparse(image_url).path)).name

        key_material = f"{timestamp}|{basename}|{recognition}|{image_url}"
        digest = hashlib.sha1(key_material.encode("utf-8")).hexdigest()[:16]
        source_key = f"{timestamp.replace('/', '').replace(':', '').replace(' ', '_')}_{digest}"

        return SourceItem(
            source_key=source_key,
            recognition_text=recognition,
            image_url=image_url,
            metadata={
                "timestamp": timestamp,
                "station": self.source.get("station"),
                "sequence": sequence,
                "recognition_type": recognition_type,
                "recognition": recognition,
                "recognition_method": recognition_method,
                "manual_review_value": manual_value,
                "review_type": review_type,
                "recognition_status": recognition_status,
                "image_url": image_url,
                "image_name": basename,
                "source_page": page_no,
                "source_row": row_no,
            },
        )

    def _scrape_current_page(self, page, page_no: int) -> list[SourceItem]:
        row_selector = self.source.get("selectors", {}).get(
            "table_rows", ".table-wrapper .el-table__body tbody tr.el-table__row"
        )
        rows = page.locator(row_selector)
        rows.first.wait_for(state="visible", timeout=30000)
        result: list[SourceItem] = []
        for i in range(rows.count()):
            item = self._row_to_item(page, rows.nth(i), page_no, i + 1)
            if item:
                result.append(item)
        return result

    @staticmethod
    def _table_signature(page, row_selector: str) -> str:
        """Return a stable-enough marker used only to observe table replacement."""
        row = page.locator(row_selector).first
        if not row.count():
            return ""
        text = row.inner_text().strip()
        image = row.locator("img").first
        image_src = image.get_attribute("src") if image.count() else ""
        return f"{text}\n{image_src or ''}"

    @staticmethod
    def _exact_page_button(page, page_number: int):
        """Element Plus may render 1, 10 and 11 together; match exact text."""
        buttons = page.locator(".el-pagination .el-pager li.number")
        for index in range(buttons.count()):
            button = buttons.nth(index)
            if button.inner_text().strip() == str(page_number):
                return button
        return None

    def _scrape_all_pages(self, page) -> list[SourceItem]:
        pagination_cfg = self.source.get("pagination", {})
        next_selector = pagination_cfg.get("next_button", ".el-pagination button.btn-next")
        active_selector = pagination_cfg.get("active_page", ".el-pagination .el-pager li.is-active.number")
        row_selector = self.source.get("selectors", {}).get(
            "table_rows", ".table-wrapper .el-table__body tbody tr.el-table__row"
        )
        max_pages = int(pagination_cfg.get("max_pages", 200))
        page_change_timeout_ms = int(pagination_cfg.get("page_change_timeout_ms", 15000))

        # Ensure we are on page 1 after a new date filter.
        first_page = self._exact_page_button(page, 1)
        if first_page is not None and first_page.is_visible():
            active = page.locator(active_selector).first
            active_text = active.inner_text().strip() if active.count() else "1"
            if active_text != "1":
                before_signature = self._table_signature(page, row_selector)
                first_page.click()
                try:
                    page.wait_for_function(
                        """
                        ([activeSelector, rowSelector, beforeSignature]) => {
                          const active = document.querySelector(activeSelector);
                          const row = document.querySelector(rowSelector);
                          if (!active || active.textContent.trim() !== '1' || !row) return false;
                          const image = row.querySelector('img');
                          const signature = `${row.innerText.trim()}\n${image?.getAttribute('src') || ''}`;
                          return signature && signature !== beforeSignature;
                        }
                        """,
                        arg=[active_selector, row_selector, before_signature],
                        timeout=page_change_timeout_ms,
                    )
                except PlaywrightTimeoutError as exc:
                    raise RuntimeError("分页返回第 1 页后，表格数据未刷新") from exc

        all_items: list[SourceItem] = []
        seen_keys: set[str] = set()

        for _ in range(max_pages):
            active = page.locator(active_selector).first
            page_no = int(active.inner_text().strip()) if active.count() else 1
            page_items = self._scrape_current_page(page, page_no)
            for item in page_items:
                if item.source_key not in seen_keys:
                    seen_keys.add(item.source_key)
                    all_items.append(item)

            next_button = page.locator(next_selector).first
            if not next_button.count() or next_button.is_disabled():
                break

            before_page = page_no
            before_signature = self._table_signature(page, row_selector)
            next_button.click()
            try:
                page.wait_for_function(
                    """
                    ([activeSelector, rowSelector, beforePage, beforeSignature]) => {
                      const active = document.querySelector(activeSelector);
                      const row = document.querySelector(rowSelector);
                      if (!active || active.textContent.trim() === String(beforePage) || !row) return false;
                      const image = row.querySelector('img');
                      const signature = `${row.innerText.trim()}\n${image?.getAttribute('src') || ''}`;
                      return signature && signature !== beforeSignature;
                    }
                    """,
                    arg=[active_selector, row_selector, before_page, before_signature],
                    timeout=page_change_timeout_ms,
                )
            except PlaywrightTimeoutError as exc:
                active = page.locator(active_selector).first
                active_text = active.inner_text().strip() if active.count() else "未知"
                raise RuntimeError(
                    f"分页从第 {before_page} 页切换后表格数据未刷新（当前页码: {active_text}）"
                ) from exc
        else:
            raise RuntimeError(f"分页超过 max_pages={max_pages}，为避免死循环已停止")

        return all_items

    def fetch_day(self, business_date: date) -> list[SourceItem]:
        self._ensure_easyconnect()
        with sync_playwright() as p:
            context = self._launch_context(p)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                self._open_history_page(page)

                start_text, end_text, items = self._fetch_filtered_api_items(
                    page, business_date
                )
                archive = self._download_data_export(page, business_date)

                for item in items:
                    item.metadata["filter_start"] = start_text
                    item.metadata["filter_end"] = end_text
                    item.metadata["data_export_archive"] = str(archive) if archive else ""

                if not items:
                    raise RuntimeError(f"{business_date.isoformat()} 没有抓到可审核记录")
                return items
            finally:
                context.close()

    @staticmethod
    def _detect_image_type(data: bytes) -> str | None:
        """
        根据文件 magic bytes 判断图片类型。

        镔鑫 19000 文件服务会把 JPG 返回为：
            Content-Type: application/octet-stream

        因此这里不能依赖 Content-Type，而是检查文件真实字节。
        """
        if not data:
            return None

        # JPEG: FF D8 FF
        if data.startswith(b"\xff\xd8\xff"):
            return "jpg"

        # PNG
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"

        # GIF
        if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
            return "gif"

        # BMP
        if data.startswith(b"BM"):
            return "bmp"

        # WEBP
        if (
            len(data) >= 12
            and data[:4] == b"RIFF"
            and data[8:12] == b"WEBP"
        ):
            return "webp"

        return None

    def _save_image_bytes(
        self,
        data: bytes,
        content_type: str,
        destination: Path,
        tmp: Path,
    ) -> Path:
        """校验真实图片字节后原子写入本地缓存。"""
        image_type = self._detect_image_type(data)
        if image_type is None:
            head = data[:32]
            raise RuntimeError(
                "response is not a valid image: "
                f"content-type={content_type or '<empty>'}, "
                f"size={len(data)} bytes, "
                f"head={head!r}"
            )

        tmp.write_bytes(data)
        tmp.replace(destination)
        return destination

    # ------------------------------------------------------------------
    # Lazy image cache: only sampled/reviewed images are downloaded.
    # ------------------------------------------------------------------
    def materialize_image(self, image_url: str, destination: Path) -> Path:
        """
        将远端图片缓存到本地。

        下载优先级：
        1. 普通 requests（镔鑫 19000 图片服务当前可直接访问）
        2. 如果失败，再使用已登录的 Chrome persistent context 请求

        注意：镔鑫图片服务把 JPEG 以 application/octet-stream 返回，
        所以两条链路都通过 magic bytes 判断图片，而不再要求 image/* MIME。
        """
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_suffix(destination.suffix + ".part")

        # 清理上次异常退出遗留的临时文件。
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass

        # data:image/...;base64,...
        if image_url.startswith("data:"):
            try:
                header, encoded = image_url.split(",", 1)
                if ";base64" in header:
                    data = base64.b64decode(encoded)
                else:
                    data = unquote(encoded).encode("utf-8")

                content_type = (
                    header[5:].split(";", 1)[0].strip().lower()
                    if header.startswith("data:")
                    else ""
                )
                return self._save_image_bytes(
                    data=data,
                    content_type=content_type,
                    destination=destination,
                    tmp=tmp,
                )
            except Exception as exc:
                raise RuntimeError(f"内嵌图片解析失败: {exc}") from exc

        headers = {
            "Referer": self.source["history_url"],
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

        request_error: Exception | None = None

        # --------------------------------------------------------------
        # 1. Direct requests
        # --------------------------------------------------------------
        try:
            response = requests.get(
                image_url,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()

            content_type = (response.headers.get("content-type") or "").lower()
            data = response.content

            return self._save_image_bytes(
                data=data,
                content_type=content_type,
                destination=destination,
                tmp=tmp,
            )
        except Exception as exc:
            request_error = exc
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

        # --------------------------------------------------------------
        # 2. Browser fallback
        # --------------------------------------------------------------
        with sync_playwright() as p:
            context = self._launch_context(p)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                self._open_history_page(page)

                response = context.request.get(
                    image_url,
                    headers=headers,
                    timeout=30000,
                )
                if not response.ok:
                    raise RuntimeError(
                        f"HTTP {response.status}: {response.status_text}"
                    )

                content_type = (response.headers.get("content-type") or "").lower()
                data = response.body()

                return self._save_image_bytes(
                    data=data,
                    content_type=content_type,
                    destination=destination,
                    tmp=tmp,
                )
            except Exception as browser_error:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

                raise RuntimeError(
                    f"图片缓存失败: {image_url}; "
                    f"requests={request_error}; "
                    f"chrome={browser_error}"
                ) from browser_error
            finally:
                context.close()


