from __future__ import annotations

import base64
import hashlib
import re
import socket
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import unquote_to_bytes, urljoin, urlparse

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.core.models import SourceItem
from app.sources.base import DataSource


class RuifengHistorySource(DataSource):
    """Collect Ruifeng history records through the SocksOverRDP proxy."""

    DEFAULT_COLUMN_ALIASES = {
        "timestamp": ("识别时间", "采集时间", "拍摄时间", "创建时间", "时间"),
        "camera_group": ("相机分组", "相机组", "相机名称", "点位名称", "分组"),
        "recognition": (
            "识别结果",
            "识别内容",
            "OCR结果",
            "OCR识别结果",
            "车号",
            "钢包号",
            "炉号",
        ),
        "image": ("识别图片", "原始图片", "采集图片", "现场图片", "图片", "原图"),
    }

    def __init__(self, project_config: dict):
        super().__init__(project_config)
        self.source = project_config["source"]
        self.camera_groups = tuple(
            str(value).strip()
            for value in self.source.get("camera_groups", [])
            if str(value).strip()
        )
        if not self.camera_groups:
            raise ValueError("瑞丰项目必须配置 source.camera_groups")

    # ------------------------------------------------------------------
    # SocksOverRDP route
    # ------------------------------------------------------------------
    def _proxy_config(self) -> dict:
        return self.source.get("proxy", {})

    def _proxy_enabled(self) -> bool:
        return bool(self._proxy_config().get("enabled", True))

    def _browser_proxy_server(self) -> str:
        return str(
            self._proxy_config().get("server", "socks5://127.0.0.1:1080")
        ).strip()

    def _requests_proxy_url(self) -> str:
        configured = str(self._proxy_config().get("requests_url", "")).strip()
        if configured:
            return configured
        server = self._browser_proxy_server()
        if server.startswith("socks5://"):
            return "socks5h://" + server.removeprefix("socks5://")
        return server

    def _requests_proxies(self) -> dict[str, str] | None:
        if not self._proxy_enabled():
            return None
        proxy_url = self._requests_proxy_url()
        return {"http": proxy_url, "https": proxy_url}

    def _proxy_endpoint(self) -> tuple[str, int]:
        parsed = urlparse(self._browser_proxy_server())
        if not parsed.hostname or not parsed.port:
            raise RuntimeError("瑞丰 source.proxy.server 必须包含主机和端口")
        return parsed.hostname, parsed.port

    def _ensure_route(self) -> None:
        timeout = float(self._proxy_config().get("connect_timeout_seconds", 5))
        if self._proxy_enabled():
            host, port = self._proxy_endpoint()
            try:
                with socket.create_connection((host, port), timeout=min(timeout, 3)):
                    pass
            except OSError as exc:
                raise RuntimeError(
                    f"瑞丰 SOCKS5 代理未就绪：{host}:{port}。"
                    "请先保持目标服务器的 SocksOverRDP-Server 和远程桌面连接正常。"
                ) from exc

        healthcheck_url = str(
            self._proxy_config().get("healthcheck_url") or self.source["history_url"]
        )
        try:
            response = requests.get(
                healthcheck_url,
                proxies=self._requests_proxies(),
                timeout=timeout,
            )
            if response.status_code >= 500:
                raise RuntimeError(f"HTTP {response.status_code}")
        except Exception as exc:
            raise RuntimeError(f"瑞丰数据页面无法访问：{healthcheck_url}；{exc}") from exc

    def check_available(self) -> bool:
        try:
            self._ensure_route()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Browser and login
    # ------------------------------------------------------------------
    def _launch_context(self, playwright):
        browser_cfg = self.source.get("browser", {})
        profile = Path(
            browser_cfg.get("user_data_dir", "./runtime/browser/ruifeng")
        ).resolve()
        profile.mkdir(parents=True, exist_ok=True)

        kwargs = {
            "headless": bool(browser_cfg.get("headless", False)),
            "accept_downloads": False,
            "viewport": None,
        }
        if self._proxy_enabled():
            kwargs["proxy"] = {"server": self._browser_proxy_server()}
        executable_path = str(browser_cfg.get("executable_path", "")).strip()
        if executable_path:
            kwargs["executable_path"] = executable_path
        else:
            kwargs["channel"] = browser_cfg.get("channel", "chrome")
        if browser_cfg.get("ignore_no_sandbox_arg", False):
            kwargs["ignore_default_args"] = ["--no-sandbox"]
        return playwright.chromium.launch_persistent_context(str(profile), **kwargs)

    @staticmethod
    def _first_visible(page, selector: str):
        locator = page.locator(selector)
        for index in range(locator.count()):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible():
                    return candidate
            except Exception:
                continue
        return None

    def _auth_selector(self, key: str, default: str) -> str:
        return str(
            self.source.get("auth", {}).get("selectors", {}).get(key, default)
        ).strip()

    def _is_login_page(self, page) -> bool:
        password_selector = self._auth_selector(
            "password", 'input[type="password"], input[placeholder*="密码"]'
        )
        try:
            if self._first_visible(page, password_selector) is not None:
                return True
        except Exception:
            pass
        return "/login" in page.url.lower()

    def _ensure_site_login(self, page) -> None:
        auth = self.source.get("auth", {})
        settle_timeout_ms = int(auth.get("page_settle_timeout_ms", 10000))
        try:
            page.wait_for_load_state("networkidle", timeout=settle_timeout_ms)
        except PlaywrightTimeoutError:
            pass
        if not self._is_login_page(page):
            return

        username = str(auth.get("username", "")).strip()
        password = str(auth.get("password", ""))
        if not username or not password:
            if auth.get("allow_interactive_login", True):
                timeout_ms = int(
                    auth.get("interactive_login_timeout_seconds", 300)
                ) * 1000
                page.bring_to_front()
                try:
                    page.wait_for_function(
                        "() => !location.href.toLowerCase().includes('/login') "
                        "&& ![...document.querySelectorAll('input[type=password]')]"
                        ".some(element => element.offsetParent !== null)",
                        timeout=timeout_ms,
                    )
                except PlaywrightTimeoutError as exc:
                    raise RuntimeError(
                        "等待瑞丰网站手动登录超时，请重新拉取后在打开的 Chrome 中完成登录。"
                    ) from exc
                page.goto(
                    self.source["history_url"],
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                return
            raise RuntimeError(
                "瑞丰网站需要登录，但 source.auth.username/password 尚未配置。"
            )

        username_selector = self._auth_selector(
            "username",
            'input[placeholder*="用户名"], input[placeholder*="账号"], input[type="text"]',
        )
        password_selector = self._auth_selector(
            "password", 'input[type="password"], input[placeholder*="密码"]'
        )
        button_selector = self._auth_selector(
            "login_button", 'button:has-text("登录"), input[type="submit"]'
        )
        timeout_ms = int(auth.get("login_timeout_ms", 30000))

        username_input = self._first_visible(page, username_selector)
        password_input = self._first_visible(page, password_selector)
        login_button = self._first_visible(page, button_selector)
        if username_input is None or password_input is None or login_button is None:
            raise RuntimeError("无法在瑞丰登录页找到用户名、密码或登录按钮")

        username_input.fill(username)
        password_input.fill(password)
        login_button.click()
        try:
            page.wait_for_function(
                "() => !location.href.toLowerCase().includes('/login') "
                "&& ![...document.querySelectorAll('input[type=password]')]"
                ".some(element => element.offsetParent !== null)",
                timeout=timeout_ms,
            )
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("瑞丰网站登录失败或等待登录结果超时，请检查用户名/密码。") from exc
        page.goto(self.source["history_url"], wait_until="domcontentloaded", timeout=60000)

    # ------------------------------------------------------------------
    # Filters
    # ------------------------------------------------------------------
    def _selector(self, key: str, default: str) -> str:
        return str(self.source.get("selectors", {}).get(key, default)).strip()

    @staticmethod
    def _fill_input(locator, value: str) -> None:
        try:
            locator.evaluate("element => element.removeAttribute('readonly')")
        except Exception:
            pass
        locator.click()
        locator.fill(value)

    def _set_day_filter(self, page, business_date: date) -> tuple[str, str]:
        start_selector = self._selector(
            "start_date",
            'input[placeholder*="开始时间"], input[placeholder*="开始日期"]',
        )
        end_selector = self._selector(
            "end_date",
            'input[placeholder*="结束时间"], input[placeholder*="结束日期"]',
        )
        start = self._first_visible(page, start_selector)
        end = self._first_visible(page, end_selector)
        if start is None or end is None:
            raise RuntimeError("无法在瑞丰历史页找到开始、结束时间输入框")

        date_format = str(self.source.get("filters", {}).get("date_format", "%Y-%m-%d %H:%M:%S"))
        start_text = datetime.combine(business_date, datetime.min.time()).strftime(date_format)
        end_text = datetime.combine(business_date, datetime.max.time()).replace(
            microsecond=0
        ).strftime(date_format)
        self._fill_input(start, start_text)
        self._fill_input(end, end_text)
        try:
            end.press("Enter")
        except Exception:
            pass
        return start_text, end_text

    def _select_camera_group(self, page, camera_group: str) -> None:
        trigger_selector = self._selector(
            "camera_group",
            'input[placeholder*="相机分组"], input[placeholder*="相机组"], '
            '.el-form-item:has-text("相机分组") .el-select, '
            '.ant-form-item:has-text("相机分组") .ant-select-selector',
        )
        trigger = self._first_visible(page, trigger_selector)
        if trigger is None:
            raise RuntimeError("无法在瑞丰历史页找到“相机分组”筛选控件")

        try:
            tag_name = trigger.evaluate("element => element.tagName.toLowerCase()")
        except Exception:
            tag_name = ""
        if tag_name == "select":
            trigger.select_option(label=camera_group)
            return

        trigger.click()
        option_selector = self._selector(
            "camera_group_options",
            '[role="option"]:visible, .el-select-dropdown__item:visible, '
            '.ant-select-item-option:visible',
        )
        options = page.locator(option_selector)
        selected = None
        for index in range(options.count()):
            option = options.nth(index)
            try:
                if option.inner_text().strip() == camera_group and option.is_visible():
                    selected = option
                    break
            except Exception:
                continue
        if selected is None:
            raise RuntimeError(f"相机分组下拉中不存在：{camera_group}")
        selected.click()

    def _submit_filter(self, page) -> None:
        selector = self._selector(
            "search_button", 'button:has-text("查询"), button:has-text("搜索")'
        )
        if selector:
            button = self._first_visible(page, selector)
            if button is not None:
                button.click()
        wait_ms = int(self.source.get("filters", {}).get("wait_after_filter_ms", 1500))
        page.wait_for_timeout(wait_ms)

    # ------------------------------------------------------------------
    # Table parsing and pagination
    # ------------------------------------------------------------------
    @staticmethod
    def _text(locator) -> str:
        try:
            return locator.inner_text().strip()
        except Exception:
            return ""

    @staticmethod
    def _normalise_header(value: str) -> str:
        return re.sub(r"[\s:：/（）()_-]+", "", value).lower()

    def _column_aliases(self, field: str) -> tuple[str, ...]:
        configured = self.source.get("columns", {}).get("aliases", {}).get(field)
        values = configured or self.DEFAULT_COLUMN_ALIASES[field]
        return tuple(self._normalise_header(str(value)) for value in values)

    def _column_index(self, headers: list[str], field: str) -> int | None:
        configured = self.source.get("columns", {}).get("indexes", {}).get(field)
        if configured is not None:
            return int(configured)
        aliases = self._column_aliases(field)
        for index, header in enumerate(headers):
            normalised = self._normalise_header(header)
            if any(alias == normalised or alias in normalised for alias in aliases):
                return index
        return None

    def _headers(self, page) -> list[str]:
        selector = self._selector(
            "table_headers",
            '.el-table__header thead th, .ant-table-thead > tr > th, table thead th',
        )
        headers = page.locator(selector)
        return [self._text(headers.nth(index)) for index in range(headers.count())]

    @staticmethod
    def _cell(cells, index: int | None):
        if index is None or index < 0 or index >= cells.count():
            return None
        return cells.nth(index)

    @staticmethod
    def _image_src(cell) -> str:
        if cell is None:
            return ""
        image = cell.locator("img").first
        if image.count():
            for attribute in ("src", "data-src", "data-original"):
                value = image.get_attribute(attribute)
                if value:
                    return value.strip()
        link = cell.locator("a[href]").first
        return (link.get_attribute("href") or "").strip() if link.count() else ""

    def _row_to_item(
        self,
        page,
        row,
        headers: list[str],
        camera_group: str,
        page_no: int,
        row_no: int,
    ) -> SourceItem | None:
        cells = row.locator("td")
        if not cells.count():
            return None

        timestamp_cell = self._cell(cells, self._column_index(headers, "timestamp"))
        recognition_cell = self._cell(cells, self._column_index(headers, "recognition"))
        group_cell = self._cell(cells, self._column_index(headers, "camera_group"))
        image_cell = self._cell(cells, self._column_index(headers, "image"))

        image_src = self._image_src(image_cell)
        if not image_src:
            for index in range(cells.count()):
                image_src = self._image_src(cells.nth(index))
                if image_src:
                    break
        if not image_src:
            return None

        timestamp = self._text(timestamp_cell) if timestamp_cell is not None else ""
        recognition = self._text(recognition_cell) if recognition_cell is not None else ""
        row_group = self._text(group_cell) if group_cell is not None else camera_group
        if row_group and row_group != camera_group:
            return None

        if image_src.startswith("data:"):
            image_url = image_src
            image_name = "inline-image"
        else:
            image_url = urljoin(page.url, image_src)
            image_name = Path(urlparse(image_url).path).name or "remote-image"

        key_material = f"{camera_group}|{timestamp}|{image_name}|{recognition}|{image_url}"
        digest = hashlib.sha1(key_material.encode("utf-8")).hexdigest()[:16]
        prefix = re.sub(r"[^0-9A-Za-z]+", "", timestamp) or "record"
        return SourceItem(
            source_key=f"{prefix}_{digest}",
            recognition_text=recognition,
            image_url=image_url,
            metadata={
                "timestamp": timestamp,
                "camera_group": camera_group,
                "recognition": recognition,
                "image_url": image_url,
                "image_name": image_name,
                "source_page": page_no,
                "source_row": row_no,
            },
        )

    def _row_selector(self) -> str:
        return self._selector(
            "table_rows",
            '.el-table__body-wrapper tbody tr, .ant-table-tbody > tr, table tbody tr',
        )

    def _scrape_current_page(
        self, page, camera_group: str, page_no: int
    ) -> list[SourceItem]:
        headers = self._headers(page)
        rows = page.locator(self._row_selector())
        result: list[SourceItem] = []
        for index in range(rows.count()):
            row = rows.nth(index)
            try:
                if not row.is_visible():
                    continue
            except Exception:
                pass
            item = self._row_to_item(
                page, row, headers, camera_group, page_no, index + 1
            )
            if item:
                result.append(item)
        return result

    def _table_signature(self, page) -> str:
        row = page.locator(self._row_selector()).first
        if not row.count():
            return ""
        return self._text(row)

    @staticmethod
    def _disabled(locator) -> bool:
        try:
            if locator.is_disabled():
                return True
        except Exception:
            pass
        classes = (locator.get_attribute("class") or "").lower()
        aria = (locator.get_attribute("aria-disabled") or "").lower()
        return "disabled" in classes or aria == "true" or locator.get_attribute("disabled") is not None

    def _active_page(self, page) -> int:
        selector = self._selector(
            "active_page",
            '.el-pagination .el-pager li.is-active, .ant-pagination-item-active',
        )
        active = self._first_visible(page, selector)
        if active is None:
            return 1
        match = re.search(r"\d+", self._text(active))
        return int(match.group()) if match else 1

    def _return_to_first_page(self, page) -> None:
        if self._active_page(page) == 1:
            return
        selector = self._selector(
            "first_page",
            '.el-pagination .el-pager li.number, .ant-pagination-item',
        )
        buttons = page.locator(selector)
        for index in range(buttons.count()):
            button = buttons.nth(index)
            if self._text(button) == "1":
                button.click()
                page.wait_for_timeout(500)
                return

    def _scrape_all_pages(self, page, camera_group: str) -> list[SourceItem]:
        pagination = self.source.get("pagination", {})
        max_pages = int(pagination.get("max_pages", 200))
        next_selector = str(
            pagination.get(
                "next_button",
                '.el-pagination button.btn-next, .ant-pagination-next button, '
                '.ant-pagination-next a',
            )
        )
        change_timeout_ms = int(pagination.get("page_change_timeout_ms", 15000))
        self._return_to_first_page(page)

        result: list[SourceItem] = []
        seen: set[str] = set()
        for _ in range(max_pages):
            page_no = self._active_page(page)
            for item in self._scrape_current_page(page, camera_group, page_no):
                if item.source_key not in seen:
                    seen.add(item.source_key)
                    result.append(item)

            next_button = self._first_visible(page, next_selector)
            if next_button is None or self._disabled(next_button):
                break
            before_page = page_no
            before_signature = self._table_signature(page)
            next_button.click()
            deadline = time.monotonic() + change_timeout_ms / 1000
            while time.monotonic() < deadline:
                page.wait_for_timeout(250)
                if (
                    self._active_page(page) != before_page
                    or self._table_signature(page) != before_signature
                ):
                    break
            else:
                raise RuntimeError(
                    f"瑞丰相机分组“{camera_group}”从第 {before_page} 页翻页后数据未刷新"
                )
        else:
            raise RuntimeError(f"瑞丰分页超过 max_pages={max_pages}，已停止")
        return result

    def fetch_day(self, business_date: date) -> list[SourceItem]:
        self._ensure_route()
        with sync_playwright() as playwright:
            context = self._launch_context(playwright)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(self.source["history_url"], wait_until="domcontentloaded", timeout=60000)
                self._ensure_site_login(page)
                ready_selector = self._selector("history_ready", "body")
                page.locator(ready_selector).first.wait_for(state="visible", timeout=30000)
                start_text, end_text = self._set_day_filter(page, business_date)

                result: list[SourceItem] = []
                seen: set[str] = set()
                for camera_group in self.camera_groups:
                    self._select_camera_group(page, camera_group)
                    self._submit_filter(page)
                    for item in self._scrape_all_pages(page, camera_group):
                        if item.source_key in seen:
                            continue
                        seen.add(item.source_key)
                        item.metadata["filter_start"] = start_text
                        item.metadata["filter_end"] = end_text
                        result.append(item)

                if not result:
                    groups = "、".join(self.camera_groups)
                    raise RuntimeError(
                        f"{business_date.isoformat()} 在相机分组（{groups}）中没有抓到可审核记录"
                    )
                return result
            finally:
                context.close()

    # ------------------------------------------------------------------
    # Lazy image cache
    # ------------------------------------------------------------------
    @staticmethod
    def _detect_image_type(data: bytes) -> str | None:
        if data.startswith(b"\xff\xd8\xff"):
            return "jpg"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "gif"
        if data.startswith(b"BM"):
            return "bmp"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "webp"
        return None

    def _save_image(self, data: bytes, content_type: str, destination: Path) -> Path:
        if self._detect_image_type(data) is None:
            raise RuntimeError(
                "响应不是有效图片："
                f"content-type={content_type or '<empty>'}, size={len(data)}"
            )
        temporary = destination.with_suffix(destination.suffix + ".part")
        temporary.write_bytes(data)
        temporary.replace(destination)
        return destination

    def materialize_image(self, image_url: str, destination: Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        if temporary.exists():
            temporary.unlink()

        if image_url.startswith("data:"):
            header, encoded = image_url.split(",", 1)
            data = (
                base64.b64decode(encoded)
                if ";base64" in header
                else unquote_to_bytes(encoded)
            )
            content_type = header[5:].split(";", 1)[0]
            return self._save_image(data, content_type, destination)

        headers = {
            "Referer": self.source["history_url"],
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
            ),
        }
        request_error: Exception | None = None
        try:
            response = requests.get(
                image_url,
                headers=headers,
                proxies=self._requests_proxies(),
                timeout=30,
            )
            response.raise_for_status()
            return self._save_image(
                response.content,
                (response.headers.get("content-type") or "").lower(),
                destination,
            )
        except Exception as exc:
            request_error = exc

        with sync_playwright() as playwright:
            context = self._launch_context(playwright)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(self.source["history_url"], wait_until="domcontentloaded", timeout=60000)
                self._ensure_site_login(page)
                response = context.request.get(image_url, headers=headers, timeout=30000)
                if not response.ok:
                    raise RuntimeError(f"HTTP {response.status}: {response.status_text}")
                return self._save_image(
                    response.body(),
                    (response.headers.get("content-type") or "").lower(),
                    destination,
                )
            except Exception as browser_error:
                if temporary.exists():
                    temporary.unlink()
                raise RuntimeError(
                    f"瑞丰图片缓存失败：{image_url}; "
                    f"requests={request_error}; chrome={browser_error}"
                ) from browser_error
            finally:
                context.close()
