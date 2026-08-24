from __future__ import annotations

import csv
import json
import os
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pyperclip
import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from app.sinks.base import DataSink


class QingTuiDocumentSink(DataSink):
    """本地结果 -> 轻推共享文档 -> WPS WebOffice -> 追加统计行。"""

    # ============================================================
    # Local result
    # ============================================================
    def _local_result(
        self,
        business_date: date,
        summary: dict[str, Any],
    ) -> tuple[Path, dict[str, Any]]:

        sink_cfg = self.config.get(
            "sink",
            {},
        )

        export_dir = Path(
            self.config.get(
                "cache",
                {},
            ).get(
                "result_export_dir",
                "./runtime/results",
            )
        ).resolve()

        export_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        specs = self._column_specs(
            sink_cfg
        )

        fields = [
            spec["field"]
            for spec in specs
        ]

        row = {
            "date": business_date.isoformat(),
            **summary,
        }

        csv_path = (
            export_dir
            / f"{business_date.isoformat()}_summary.csv"
        )

        with csv_path.open(
            "w",
            encoding="utf-8-sig",
            newline="",
        ) as f:

            writer = csv.DictWriter(
                f,
                fieldnames=fields,
                extrasaction="ignore",
            )

            writer.writeheader()
            writer.writerow(row)

        json_path = (
            export_dir
            / f"{business_date.isoformat()}_summary.json"
        )

        json_path.write_text(
            json.dumps(
                row,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        return csv_path, row

    
    # ============================================================
    # Persistent Windows Chrome via CDP
    # ============================================================
    @staticmethod
    def _profile_dir(
        browser_cfg: dict,
    ) -> Path:

        path = Path(
            browser_cfg.get(
                "user_data_dir",
                "./runtime/browser/qingtui",
            )
        ).resolve()

        path.mkdir(
            parents=True,
            exist_ok=True,
        )

        return path

    @staticmethod
    def _cdp_port(
        browser_cfg: dict,
    ) -> int:

        return int(
            browser_cfg.get(
                "remote_debugging_port",
                9223,
            )
        )

    def _cdp_url(
        self,
        browser_cfg: dict,
    ) -> str:

        configured = str(
            browser_cfg.get(
                "cdp_url",
                "",
            )
        ).strip()

        if configured:
            return configured.rstrip("/")

        return (
            f"http://127.0.0.1:"
            f"{self._cdp_port(browser_cfg)}"
        )

    @staticmethod
    def _chrome_candidates() -> list[Path]:

        result: list[Path] = []

        for env_name in (
            "PROGRAMFILES",
            "PROGRAMFILES(X86)",
        ):
            root = os.environ.get(env_name)

            if root:
                result.append(
                    Path(root)
                    / "Google"
                    / "Chrome"
                    / "Application"
                    / "chrome.exe"
                )

        local = os.environ.get(
            "LOCALAPPDATA"
        )

        if local:
            result.append(
                Path(local)
                / "Google"
                / "Chrome"
                / "Application"
                / "chrome.exe"
            )

        result += [
            Path(
                r"C:\Program Files\Google\Chrome\Application\chrome.exe"
            ),
            Path(
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
            ),
        ]

        return result
    def _parse_date_cell(
        self,
        value: Any,
        sink_cfg: dict,
    ) -> date | None:

        text = str(
            value
            if value is not None
            else ""
        ).strip()

        if not text:
            return None

        fmt_cfg = sink_cfg.get(
            "format",
            {},
        )

        configured_format = fmt_cfg.get(
            "date_output_format",
            "%Y-%m-%d",
        )

        formats = [
            configured_format,
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%Y.%m.%d",
            "%Y年%m月%d日",
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
        ]

        seen = set()

        for fmt in formats:

            if fmt in seen:
                continue

            seen.add(
                fmt
            )

            try:

                return datetime.strptime(
                    text,
                    fmt,
                ).date()

            except ValueError:
                pass

        return None
    def _row_to_values(
        self,
        row: dict[str, Any],
        sink_cfg: dict,
    ) -> list[str]:

        specs = self._column_specs(
            sink_cfg
        )

        fmt = sink_cfg.get(
            "format",
            {},
        )

        date_field = fmt.get(
            "date_field",
            "date",
        )

        date_output_format = fmt.get(
            "date_output_format",
            "%Y-%m-%d",
        )

        accuracy_suffix = fmt.get(
            "accuracy_suffix",
            "%",
        )

        values: list[str] = []

        for spec in specs:

            field = spec["field"]

            value = row.get(
                field,
                "",
            )

            # --------------------------
            # Date
            # --------------------------

            if field == date_field:

                parsed = self._parse_date_cell(
                    value,
                    sink_cfg,
                )

                if parsed is not None:

                    value = parsed.strftime(
                        date_output_format
                    )

            # --------------------------
            # Accuracy
            # --------------------------

            if (
                field == "accuracy"
                and
                value not in (
                    "",
                    None,
                )
                and
                accuracy_suffix
            ):

                text = str(
                    value
                )

                if not text.endswith(
                    accuracy_suffix
                ):

                    value = (
                        text
                        +
                        accuracy_suffix
                    )

            values.append(
                self._cell_value(
                    value
                )
            )

        return values
    def _column_specs(
        self,
        sink_cfg: dict,
    ) -> list[dict[str, str]]:
        """
        将 config 中的列配置统一成：

            [
                {
                    "field": "date",
                    "header": "日期",
                },
                ...
            ]

        同时兼容旧配置：

            "columns": [
                "date",
                "sample_count"
            ]
        """

        fmt = sink_cfg.get(
            "format",
            {},
        )

        raw_columns = fmt.get(
            "columns",
            [
                "date",
                "sample_count",
                "correct",
                "incorrect",
                "invalid",
                "accuracy",
            ],
        )

        specs: list[dict[str, str]] = []

        for item in raw_columns:

            if isinstance(
                item,
                str,
            ):

                field = item.strip()

                if not field:
                    continue

                specs.append(
                    {
                        "field": field,
                        "header": field,
                    }
                )

                continue

            if isinstance(
                item,
                dict,
            ):

                field = str(
                    item.get(
                        "field",
                        item.get(
                            "key",
                            "",
                        ),
                    )
                ).strip()

                if not field:

                    raise RuntimeError(
                        f"非法列配置，没有 field: {item}"
                    )

                header = str(
                    item.get(
                        "header",
                        item.get(
                            "label",
                            field,
                        ),
                    )
                ).strip()

                specs.append(
                    {
                        "field": field,
                        "header": header,
                    }
                )

                continue

            raise RuntimeError(
                f"不支持的 column 配置: {item!r}"
            )

        if not specs:

            raise RuntimeError(
                "sink.format.columns 不能为空"
            )

        fields = [
            x["field"]
            for x in specs
        ]

        if len(
            fields
        ) != len(
            set(fields)
        ):

            raise RuntimeError(
                "sink.format.columns 中存在重复 field"
            )

        date_field = fmt.get(
            "date_field",
            "date",
        )

        if date_field not in fields:

            raise RuntimeError(
                f"date_field={date_field!r} "
                "不在 columns 中"
            )

        return specs
    @staticmethod
    def _parse_tsv_table(
        text: str,
    ) -> list[list[str]]:

        if not text:
            return []

        text = (
            text
            .replace(
                "\r\n",
                "\n",
            )
            .replace(
                "\r",
                "\n",
            )
        )

        rows: list[list[str]] = []

        for line in text.split(
            "\n"
        ):

            values = [
                cell.strip()
                for cell
                in line.split("\t")
            ]

            rows.append(
                values
            )

        # 删除最后面的全空行。
        while (
            rows
            and
            not any(
                cell.strip()
                for cell
                in rows[-1]
            )
        ):
            rows.pop()

        return rows
    
    def _copy_used_range(
        self,
        page,
        selectors: dict,
    ) -> str:
        """
        从当前 WPS Sheet 中复制：

            A1
            ↓
            最后使用单元格

        返回 TSV 文本。
        """

        # 再确保一次焦点在真正 Spreadsheet。
        self._focus_editor(
            page,
            selectors,
        )

        keyboard = page.keyboard

        # 回 A1。
        keyboard.press(
            "Control+Home"
        )

        page.wait_for_timeout(
            120
        )

        # 选择 A1 -> 最后一个有效单元格。
        keyboard.press(
            "Control+Shift+End"
        )

        page.wait_for_timeout(
            150
        )

        # 避免读到之前剪贴板里的旧内容。
        marker = (
            "__DATA_REVIEW_CLIPBOARD__"
            f"{time.time_ns()}"
        )

        pyperclip.copy(
            marker
        )

        keyboard.press(
            "Control+C"
        )

        deadline = (
            time.time()
            +
            2.0
        )

        while time.time() < deadline:

            page.wait_for_timeout(
                100
            )

            text = pyperclip.paste()

            if text != marker:
                return text

        # 空白表格时 WPS 有可能根本不修改剪贴板。
        return ""
        
    def _chrome_executable(
        self,
        browser_cfg: dict,
    ) -> Path:

        configured = str(
            browser_cfg.get(
                "executable_path",
                "",
            )
        ).strip()

        if configured:

            path = Path(configured)

            if path.exists():
                return path.resolve()

            raise RuntimeError(
                f"配置的 Chrome 不存在: {configured}"
            )

        for path in self._chrome_candidates():

            if path.exists():
                return path.resolve()

        raise RuntimeError(
            "未找到 Windows Chrome，"
            "请在 sink.browser.executable_path "
            "中配置 chrome.exe。"
        )

    def _cdp_ready(
        self,
        browser_cfg: dict,
    ) -> bool:

        try:

            response = requests.get(
                f"{self._cdp_url(browser_cfg)}/json/version",
                timeout=0.8,
            )

            return (
                response.ok
                and
                "webSocketDebuggerUrl"
                in response.text
            )

        except Exception:
            return False

    def _ensure_cdp_chrome(
        self,
        browser_cfg: dict,
    ) -> str:

        cdp_url = self._cdp_url(
            browser_cfg
        )

        if self._cdp_ready(
            browser_cfg
        ):
            return cdp_url

        chrome = self._chrome_executable(
            browser_cfg
        )

        profile = self._profile_dir(
            browser_cfg
        )

        port = self._cdp_port(
            browser_cfg
        )

        command = [
            str(chrome),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "about:blank",
        ]

        creationflags = 0

        if os.name == "nt":

            creationflags |= getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0,
            )

            creationflags |= getattr(
                subprocess,
                "DETACHED_PROCESS",
                0,
            )

        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )

        timeout_s = int(
            browser_cfg.get(
                "browser_start_timeout_seconds",
                20,
            )
        )

        deadline = (
            time.time()
            +
            timeout_s
        )

        while time.time() < deadline:

            if self._cdp_ready(
                browser_cfg
            ):
                return cdp_url

            time.sleep(0.5)

        raise RuntimeError(
            "轻推专用 Chrome 已启动，"
            f"但 {timeout_s}s 内 CDP 未就绪: "
            f"{cdp_url}。"
            "如果 runtime/browser/qingtui "
            "正被其他 Chrome 占用，请先关闭那个 Chrome。"
        )

    def _connect_context(
        self,
        playwright,
        browser_cfg: dict,
    ):

        browser = (
            playwright.chromium
            .connect_over_cdp(
                self._ensure_cdp_chrome(
                    browser_cfg
                )
            )
        )

        if not browser.contexts:

            raise RuntimeError(
                "已连接轻推 Chrome，"
                "但没有 BrowserContext。"
            )

        return (
            browser,
            browser.contexts[0],
        )

    @staticmethod
    def _table_to_tsv(
        table: list[list[str]],
    ) -> str:

        return "\r\n".join(
            "\t".join(
                str(cell)
                for cell
                in row
            )
            for row
            in table
        )
    
    @staticmethod
    def _normalize_table_width(
        rows: list[list[str]],
        width: int,
    ) -> list[list[str]]:

        result: list[list[str]] = []

        for row in rows:

            values = list(
                row[:width]
            )

            if len(
                values
            ) < width:

                values += [
                    ""
                ] * (
                    width
                    -
                    len(values)
                )

            if any(
                str(x).strip()
                for x in values
            ):

                result.append(
                    values
                )

        return result
    
    def _build_expected_table(
        self,
        current_table: list[list[str]],
        new_row: dict[str, Any],
        business_date: date,
        sink_cfg: dict,
    ) -> tuple[
        list[list[str]],
        int,
        bool,
    ]:
        """
        返回：

            expected_table
            replaced_count
            had_header
        """

        specs = self._column_specs(
            sink_cfg
        )

        headers = [
            x["header"]
            for x in specs
        ]

        fields = [
            x["field"]
            for x in specs
        ]

        width = len(
            specs
        )

        fmt = sink_cfg.get(
            "format",
            {},
        )

        date_field = fmt.get(
            "date_field",
            "date",
        )

        date_output_format = fmt.get(
            "date_output_format",
            "%Y-%m-%d",
        )

        date_index = fields.index(
            date_field
        )

        data_rows, had_header = (
            self._extract_existing_data_rows(
                current_table,
                sink_cfg,
            )
        )

        data_rows = (
            self._normalize_table_width(
                data_rows,
                width,
            )
        )

        kept_rows: list[
            tuple[
                date,
                list[str],
            ]
        ] = []

        replaced_count = 0

        for row in data_rows:

            parsed_date = (
                self._parse_date_cell(
                    row[
                        date_index
                    ],
                    sink_cfg,
                )
            )

            if parsed_date is None:

                raise RuntimeError(
                    "WPS 表格存在无法识别日期的数据行："
                    f"{row}"
                )

            # 当前日期已有：
            # 删除旧值，后面统一加入新的。
            if parsed_date == business_date:

                replaced_count += 1
                continue

            # 顺便统一旧日期的显示格式。
            row[
                date_index
            ] = parsed_date.strftime(
                date_output_format
            )

            kept_rows.append(
                (
                    parsed_date,
                    row,
                )
            )

        new_values = (
            self._row_to_values(
                new_row,
                sink_cfg,
            )
        )

        kept_rows.append(
            (
                business_date,
                new_values,
            )
        )

        # ----------------------------------------
        # 强制日期升序排列
        # ----------------------------------------

        kept_rows.sort(
            key=lambda x: x[0]
        )

        sorted_rows = [
            row
            for _date, row
            in kept_rows
        ]

        expected = [
            headers,
            *sorted_rows,
        ]

        return (
            expected,
            replaced_count,
            had_header,
        )
    
    def _extract_existing_data_rows(
        self,
        table: list[list[str]],
        sink_cfg: dict,
    ) -> tuple[list[list[str]], bool]:

        specs = self._column_specs(
            sink_cfg
        )

        headers = [
            x["header"]
            for x in specs
        ]

        width = len(
            headers
        )

        rows = self._normalize_table_width(
            table,
            width,
        )

        if not rows:

            return [], False

        first = rows[0]

        # 完全匹配 config 表头。
        if first == headers:

            return (
                rows[1:],
                True,
            )

        fmt = sink_cfg.get(
            "format",
            {},
        )

        date_field = fmt.get(
            "date_field",
            "date",
        )

        fields = [
            x["field"]
            for x in specs
        ]

        date_index = fields.index(
            date_field
        )

        # 第一行日期列能解析：
        # 表示第一行就是数据，不存在表头。
        first_date = self._parse_date_cell(
            first[date_index],
            sink_cfg,
        )

        if first_date is not None:

            return (
                rows,
                False,
            )

        # 第一行日期解析不了，而且非空，
        # 大概率就是旧表头。
        #
        # 为避免把未知内容误删，这里要求列数一致，
        # 然后把它作为旧 header 丢弃，
        # 最终统一换成 config header。
        return (
            rows[1:],
            True,
        )

    # ============================================================
    # WPS frame / login / editor state
    # ============================================================
    @staticmethod
    def _host(
        url: str,
    ) -> str:

        try:

            return (
                urlparse(url).hostname
                or ""
            ).lower()

        except Exception:
            return ""

    @staticmethod
    def _visible(
        scope,
        selector: str,
        timeout: int = 300,
    ) -> bool:

        try:

            locator = (
                scope
                .locator(selector)
                .first
            )

            return bool(
                locator.count()
                and
                locator.is_visible(
                    timeout=timeout
                )
            )

        except Exception:
            return False

    # ============================================================
    # 关键修改 1
    #
    # 不再猜 iframe / canvas。
    # 直接找真正的 WPS WebOffice Frame。
    # ============================================================
    def _find_wps_frame(
        self,
        page,
    ):
        """
        找到真正承载 Spreadsheet 的 WPS WebOffice frame。

        注意：
        页面通常有两层：

            https://wps.qingtui.com/wps/...
                ↓
            https://wps.qingtui.com/weboffice/office/s/...

        #et_grid 位于第二层 WebOffice frame 中，
        不能仅根据 hostname == wps.qingtui.com 判断。
        """

        frames = list(page.frames)

        # --------------------------------------------------------
        # 第一优先级：
        # 真正的 WPS WebOffice Spreadsheet frame
        # --------------------------------------------------------
        for frame in frames:

            url = frame.url or ""

            if "/weboffice/office/" in url:
                return frame

        # --------------------------------------------------------
        # 第二优先级：
        # 有些部署 URL 可能略有差异，但仍是 weboffice。
        # --------------------------------------------------------
        for frame in frames:

            url = frame.url or ""

            if (
                "wps.qingtui.com" in url
                and
                "/weboffice/" in url
            ):
                return frame

        return None
    # ============================================================
    # 关键修改 2
    #
    # 真正判断 Spreadsheet 是否加载：
    #
    # #et_grid
    # #et_canvas
    # #edit_proxy_runs
    #
    # 而不是 generic "canvas"。
    # ============================================================
    def _wps_editor_ready(
        self,
        page,
    ) -> bool:

        frame = self._find_wps_frame(
            page
        )

        if frame is None:
            return False

        try:
            grid = frame.locator(
                "#et_grid"
            ).first

            if (
                grid.count()
                and
                grid.is_visible(timeout=500)
            ):
                return True

        except Exception:
            pass

        # #et_grid 最可靠。
        # 以下只是页面正在加载时的辅助判断。
        fallback_selectors = [
            "#et_canvas",
            ".et-grid-view-wrap",
            "#edit_proxy_runs",
        ]

        for selector in fallback_selectors:

            try:
                locator = (
                    frame
                    .locator(selector)
                    .first
                )

                if (
                    locator.count()
                    and
                    locator.is_visible(
                        timeout=300
                    )
                ):
                    return True

            except Exception:
                pass

        return False

    def _has_login_marker(
        self,
        page,
        browser_cfg: dict,
    ) -> bool:

        # 表格已经真正出现，就不要因为页面其他地方
        # 有“登录”两个字而误判。
        if self._wps_editor_ready(
            page
        ):
            return False

        markers = browser_cfg.get(
            "login_markers",
            [
                'input[type="password"]',
                'text="登录"',
                "text=扫码登录",
                "text=扫码登录后查看",
                "text=手机验证",
                "text=轻推登录",
            ],
        )

        # 外层轻推页面。
        for selector in markers:

            if self._visible(
                page,
                selector,
                300,
            ):
                return True

        # iframe 内部。
        for frame in page.frames[1:]:

            for selector in markers:

                if self._visible(
                    frame,
                    selector,
                    250,
                ):
                    return True

        return False

    def _is_auth_gate(
        self,
        page,
        browser_cfg: dict,
        selectors: dict,
    ) -> bool:

        # selectors 参数保留，
        # 兼容现有调用接口。
        del selectors

        # 真正表格已经出来：
        # 一律认为认证完成。
        if self._wps_editor_ready(
            page
        ):
            return False

        if self._has_login_marker(
            page,
            browser_cfg,
        ):
            return True

        urls = [
            page.url or ""
        ]

        urls += [
            frame.url or ""
            for frame
            in page.frames[1:]
        ]

        for url in urls:

            if (
                "qrcodeUrl=" in url
                or
                "/oauth/qrcode"
                in url
            ):
                return True

        return False

    def _wait_for_auth_if_needed(
        self,
        page,
        browser_cfg: dict,
        selectors: dict,
        phase: str,
    ) -> None:

        if not self._is_auth_gate(
            page,
            browser_cfg,
            selectors,
        ):
            return

        if not browser_cfg.get(
            "allow_interactive_login",
            True,
        ):

            raise RuntimeError(
                f"轻推/WPS 在{phase}阶段要求登录，"
                "但 allow_interactive_login=false。"
            )

        timeout_s = int(
            browser_cfg.get(
                "interactive_login_timeout_seconds",
                300,
            )
        )

        deadline = (
            time.time()
            +
            timeout_s
        )

        while time.time() < deadline:

            page.wait_for_timeout(
                1000
            )

            if not self._is_auth_gate(
                page,
                browser_cfg,
                selectors,
            ):

                page.wait_for_timeout(
                    int(
                        browser_cfg.get(
                            "post_login_settle_ms",
                            2500,
                        )
                    )
                )

                return

        raise RuntimeError(
            f"轻推/WPS {phase}验证超时，"
            "请在常驻 Chrome 中完成扫码/手机验证。"
        )

    # ============================================================
    # Open target document
    # ============================================================
    def _relevant_pages(
        self,
        context,
    ) -> list:

        result = []

        for page in context.pages:

            host = self._host(
                page.url
            )

            if (
                host.endswith(
                    "qingtui.com"
                )
                or
                "wps" in host
            ):
                result.append(
                    page
                )

        return result

    def _existing_editor_page(
        self,
        context,
        browser_cfg: dict,
        selectors: dict,
    ):

        for page in reversed(
            self._relevant_pages(
                context
            )
        ):

            if (
                self._wps_editor_ready(
                    page
                )
                and
                not self._is_auth_gate(
                    page,
                    browser_cfg,
                    selectors,
                )
            ):

                page.bring_to_front()

                return page

        return None

    def _enter_edit_mode(
        self,
        context,
        page,
        selectors: dict,
    ):

        # 当前轻推页面已经直接创建：
        #
        # iframe#office-iframe
        #
        # 如果 WPS frame 已存在，
        # 不再乱点“在线编辑”。
        if self._find_wps_frame(
            page
        ) is not None:
            return page

        before_pages = list(
            context.pages
        )

        texts = selectors.get(
            "edit_button_texts",
            [
                "在线编辑",
                "编辑",
            ],
        )

        for text in texts:

            try:

                button = (
                    page
                    .get_by_text(
                        text,
                        exact=True,
                    )
                    .first
                )

                if (
                    button.count()
                    and
                    button.is_visible(
                        timeout=1500
                    )
                ):

                    button.click()

                    page.wait_for_timeout(
                        int(
                            selectors.get(
                                "wait_after_edit_ms",
                                3000,
                            )
                        )
                    )

                    after_pages = list(
                        context.pages
                    )

                    if (
                        len(after_pages)
                        >
                        len(before_pages)
                    ):
                        return (
                            after_pages[-1]
                        )

                    return page

            except Exception:
                pass

        return page

    def _wait_editor_ready(
        self,
        page,
        browser_cfg: dict,
        selectors: dict,
    ) -> None:

        timeout_ms = int(
            selectors.get(
                "editor_surface_timeout_ms",
                30000,
            )
        )

        deadline = (
            time.time()
            +
            timeout_ms / 1000.0
        )

        last_frame_url = ""

        while time.time() < deadline:

            frame = self._find_wps_frame(
                page
            )

            if frame is not None:

                last_frame_url = (
                    frame.url
                    or ""
                )

                try:

                    grid = (
                        frame
                        .locator(
                            "#et_grid"
                        )
                        .first
                    )

                    if (
                        grid.count()
                        and
                        grid.is_visible(
                            timeout=500
                        )
                    ):
                        return

                except Exception:
                    pass

            if self._is_auth_gate(
                page,
                browser_cfg,
                selectors,
            ):
                return

            page.wait_for_timeout(
                500
            )

        frame_urls = [
            frame.url
            for frame
            in page.frames
        ]

        raise RuntimeError(
            "等待 WPS Spreadsheet 超时。"
            "\n"
            f"selected_weboffice_frame={last_frame_url!r}"
            "\n"
            f"all_frames={frame_urls}"
            "\n"
            "真正的 Spreadsheet 应位于包含 "
            "'/weboffice/office/' 的 frame 中，"
            "并包含 #et_grid。"
        )

    def _open_editor(
        self,
        context,
        document_url: str,
        browser_cfg: dict,
        selectors: dict,
        prefer_existing: bool = True,
    ):

        if prefer_existing:

            existing = (
                self._existing_editor_page(
                    context,
                    browser_cfg,
                    selectors,
                )
            )

            if existing is not None:
                return existing

        pages = self._relevant_pages(
            context
        )

        page = (
            pages[-1]
            if pages
            else context.new_page()
        )

        current = (
            page.url
            or ""
        )

        if (
            current
            in {
                "",
                "about:blank",
            }
            or
            (
                "qingtui.com"
                not in current
                and
                "wps"
                not in current
            )
        ):

            page.goto(
                document_url,
                wait_until="domcontentloaded",
                timeout=60000,
            )

        page.bring_to_front()

        # 轻推自身登录。
        self._wait_for_auth_if_needed(
            page,
            browser_cfg,
            selectors,
            "轻推网页",
        )

        if self._wps_editor_ready(
            page
        ):
            return page

        # 某些版本仍需要点编辑按钮。
        page = self._enter_edit_mode(
            context,
            page,
            selectors,
        )

        page.wait_for_timeout(
            int(
                selectors.get(
                    "editor_ready_wait_ms",
                    2500,
                )
            )
        )

        # WPS 登录 / OAuth。
        self._wait_for_auth_if_needed(
            page,
            browser_cfg,
            selectors,
            "WPS 在线编辑",
        )

        page.wait_for_timeout(
            int(
                selectors.get(
                    "post_auth_wait_ms",
                    1800,
                )
            )
        )

        self._wait_editor_ready(
            page,
            browser_cfg,
            selectors,
        )

        if self._is_auth_gate(
            page,
            browser_cfg,
            selectors,
        ):

            self._wait_for_auth_if_needed(
                page,
                browser_cfg,
                selectors,
                "WPS 在线编辑",
            )

            self._wait_editor_ready(
                page,
                browser_cfg,
                selectors,
            )

        return page

    def _save_storage_state(
        self,
        context,
        browser_cfg: dict,
    ) -> str:

        path = (
            self._profile_dir(
                browser_cfg
            )
            /
            "playwright_storage_state.json"
        )

        try:

            context.storage_state(
                path=str(path)
            )

            return str(path)

        except Exception:
            return ""

    # ============================================================
    # Public auth endpoint
    # ============================================================
    def prepare_auth(
        self,
    ) -> dict[str, Any]:

        sink_cfg = self.config.get(
            "sink",
            {},
        )

        document_url = str(
            sink_cfg.get(
                "document_url",
                "",
            )
        ).strip()

        if not document_url:

            raise RuntimeError(
                "sink.document_url is empty"
            )

        browser_cfg = sink_cfg.get(
            "browser",
            {},
        )

        selectors = sink_cfg.get(
            "selectors",
            {},
        )

        with sync_playwright() as p:

            _, context = (
                self._connect_context(
                    p,
                    browser_cfg,
                )
            )

            page = self._open_editor(
                context,
                document_url,
                browser_cfg,
                selectors,
                prefer_existing=True,
            )

            frame = self._find_wps_frame(
                page
            )

            return {
                "authenticated":
                    not self._is_auth_gate(
                        page,
                        browser_cfg,
                        selectors,
                    ),

                "editor_ready":
                    self._wps_editor_ready(
                        page
                    ),

                "current_url":
                    page.url,

                "wps_frame_url":
                    frame.url
                    if frame
                    else "",

                "profile_dir":
                    str(
                        self._profile_dir(
                            browser_cfg
                        )
                    ),

                "storage_state":
                    self._save_storage_state(
                        context,
                        browser_cfg,
                    ),

                "browser_kept_open":
                    True,

                "cdp_url":
                    self._cdp_url(
                        browser_cfg
                    ),
            }

    # ============================================================
    # Spreadsheet operations
    # ============================================================
    def _select_sheet_if_configured(
        self,
        page,
        selectors: dict,
    ) -> None:

        sheet_name = str(
            selectors.get(
                "sheet_name",
                "",
            )
        ).strip()

        if not sheet_name:
            return

        frame = self._find_wps_frame(
            page
        )

        if frame is None:

            raise RuntimeError(
                "选择 Sheet 时找不到 WPS iframe。"
            )

        sheet = (
            frame
            .locator(
                f'.sheet-name[data-name="{sheet_name}"]'
            )
            .first
        )

        if not sheet.count():

            names = (
                frame
                .locator(
                    ".sheet-name"
                )
            )

            available = []

            for i in range(
                names.count()
            ):

                available.append(
                    names
                    .nth(i)
                    .get_attribute(
                        "data-name"
                    )
                    or
                    names
                    .nth(i)
                    .inner_text()
                    .strip()
                )

            raise RuntimeError(
                f"找不到工作表 {sheet_name!r}，"
                f"当前 sheets={available}"
            )

        sheet.click()

        page.wait_for_timeout(
            300
        )

    # ============================================================
    # 关键修改 3
    #
    # 原来这里：
    #
    #   page.locator("canvas")
    #   iframe 内找 canvas
    #   页面中央 click
    #
    # 全部删除。
    #
    # 现在直接 focus 真正的：
    #
    #   WPS Frame
    #       ↓
    #   #et_grid
    # ============================================================
    def _focus_editor(
        self,
        page,
        selectors: dict,
    ):

        frame = self._find_wps_frame(
            page
        )

        if frame is None:

            raise RuntimeError(
                "没有找到真正的 WPS WebOffice frame。"
                "期望 frame URL 包含 "
                "'/weboffice/office/'。"
            )

        grid = (
            frame
            .locator(
                "#et_grid"
            )
            .first
        )

        grid.wait_for(
            state="visible",
            timeout=int(
                selectors.get(
                    "grid_focus_timeout_ms",
                    30000,
                )
            ),
        )

        # #et_grid 本身 tabindex=1，
        # 可以直接成为键盘事件接收目标。
        grid.focus()

        box = grid.bounding_box()

        if box:

            # 避开：
            # 左侧 row header
            # 顶部 column header
            #
            # 点到真正单元格区域。
            x = min(
                max(
                    80,
                    box["width"] * 0.5,
                ),
                box["width"] - 10,
            )

            y = min(
                max(
                    60,
                    box["height"] * 0.35,
                ),
                box["height"] - 10,
            )

            grid.click(
                position={
                    "x": x,
                    "y": y,
                }
            )

        else:
            grid.click()

        page.wait_for_timeout(
            200
        )

        grid.focus()

        return page

    @staticmethod
    def _cell_value(
        value: Any,
    ) -> str:

        if value is None:
            return ""

        return (
            str(value)
            .replace(
                "\t",
                " ",
            )
            .replace(
                "\r",
                " ",
            )
            .replace(
                "\n",
                " ",
            )
        )
    def _rewrite_table(
        self,
        page,
        selectors: dict,
        table: list[list[str]],
    ) -> str:

        if not table:

            raise RuntimeError(
                "禁止向 WPS 写入空表"
            )

        self._focus_editor(
            page,
            selectors,
        )

        keyboard = page.keyboard

        # -----------------------------------------
        # 清掉旧的 used range
        # -----------------------------------------

        keyboard.press(
            "Control+Home"
        )

        page.wait_for_timeout(
            100
        )

        keyboard.press(
            "Control+Shift+End"
        )

        page.wait_for_timeout(
            120
        )

        keyboard.press(
            "Delete"
        )

        page.wait_for_timeout(
            150
        )

        # -----------------------------------------
        # 从 A1 整块写回
        # -----------------------------------------

        keyboard.press(
            "Control+Home"
        )

        page.wait_for_timeout(
            100
        )

        tsv = self._table_to_tsv(
            table
        )

        pyperclip.copy(
            tsv
        )

        keyboard.press(
            "Control+V"
        )

        page.wait_for_timeout(
            int(
                selectors.get(
                    "paste_wait_ms",
                    800,
                )
            )
        )

        return tsv
    def _verify_written_table(
        self,
        page,
        selectors: dict,
        sink_cfg: dict,
        expected_table: list[list[str]],
    ) -> list[list[str]]:

        actual_text = self._copy_used_range(
            page,
            selectors,
        )

        actual_table = self._parse_tsv_table(
            actual_text
        )

        specs = self._column_specs(
            sink_cfg
        )

        width = len(
            specs
        )

        expected = (
            self._normalize_table_width(
                expected_table,
                width,
            )
        )

        actual = (
            self._normalize_table_width(
                actual_table,
                width,
            )
        )

        # ----------------------------------------
        # 1. 整表内容校验
        # ----------------------------------------

        if actual != expected:

            raise RuntimeError(
                "WPS 写入后回读校验失败。\n"
                f"expected={expected}\n"
                f"actual={actual}"
            )

        # ----------------------------------------
        # 2. Header 校验
        # ----------------------------------------

        expected_headers = [
            x["header"]
            for x in specs
        ]

        if (
            not actual
            or
            actual[0] != expected_headers
        ):

            raise RuntimeError(
                "WPS 表头校验失败："
                f"expected={expected_headers}, "
                f"actual="
                f"{actual[0] if actual else None}"
            )

        # ----------------------------------------
        # 3. 日期排序校验
        # ----------------------------------------

        fmt = sink_cfg.get(
            "format",
            {},
        )

        date_field = fmt.get(
            "date_field",
            "date",
        )

        fields = [
            x["field"]
            for x in specs
        ]

        date_index = fields.index(
            date_field
        )

        dates: list[date] = []

        for row in actual[1:]:

            parsed = self._parse_date_cell(
                row[
                    date_index
                ],
                sink_cfg,
            )

            if parsed is None:

                raise RuntimeError(
                    "WPS 写入后的表格中存在非法日期："
                    f"{row}"
                )

            dates.append(
                parsed
            )

        if dates != sorted(
            dates
        ):

            raise RuntimeError(
                "WPS 表格写入成功，"
                "但日期顺序校验失败："
                f"{dates}"
            )

        return actual

    # ============================================================
    # Upload
    # ============================================================
    def upload_day(
        self,
        business_date: date,
        summary: dict[str, Any],
    ) -> dict[str, Any]:

        sink_cfg = self.config.get(
            "sink",
            {},
        )

        csv_path, row = (
            self._local_result(
                business_date,
                summary,
            )
        )

        if not sink_cfg.get(
            "enabled",
            False,
        ):

            return {
                "uploaded": False,
                "reason": "sink.disabled",
                "local_result": str(
                    csv_path
                ),
            }

        document_url = str(
            sink_cfg.get(
                "document_url",
                "",
            )
        ).strip()

        if not document_url:

            raise RuntimeError(
                "sink.document_url is empty"
            )

        browser_cfg = sink_cfg.get(
            "browser",
            {},
        )

        selectors = sink_cfg.get(
            "selectors",
            {},
        )

        screenshot_path = (
            csv_path.parent
            /
            (
                f"{business_date.isoformat()}"
                "_qingtui_after_upload.png"
            )
        )

        state_path = ""
        frame_url = ""
        pasted_tsv = ""
        replaced_count = 0
        had_header = False
        verified_rows = 0

        with sync_playwright() as p:

            _, context = (
                self._connect_context(
                    p,
                    browser_cfg,
                )
            )

            try:

                # ================================================
                # 1. 打开已经能正常工作的 WPS
                # ================================================

                page = self._open_editor(
                    context,
                    document_url,
                    browser_cfg,
                    selectors,
                    prefer_existing=True,
                )

                if self._is_auth_gate(
                    page,
                    browser_cfg,
                    selectors,
                ):

                    raise RuntimeError(
                        "轻推/WPS 当前仍处于"
                        "扫码/手机验证页面"
                    )

                if not self._wps_editor_ready(
                    page
                ):

                    raise RuntimeError(
                        "WPS Spreadsheet "
                        "#et_grid 尚未加载"
                    )

                # ================================================
                # 2. 选择目标 Sheet
                # ================================================

                self._select_sheet_if_configured(
                    page,
                    selectors,
                )

                # ================================================
                # 3. Focus 真正的 #et_grid
                # ================================================

                self._focus_editor(
                    page,
                    selectors,
                )

                # ================================================
                # 4. 上传前先读取整个已有表格
                # ================================================

                old_text = (
                    self._copy_used_range(
                        page,
                        selectors,
                    )
                )

                old_table = (
                    self._parse_tsv_table(
                        old_text
                    )
                )

                # ================================================
                # 5.
                #
                # Header
                # +
                # 同日期覆盖
                # +
                # 日期排序
                # ================================================

                (
                    expected_table,
                    replaced_count,
                    had_header,
                ) = self._build_expected_table(
                    current_table=old_table,
                    new_row=row,
                    business_date=business_date,
                    sink_cfg=sink_cfg,
                )

                # ================================================
                # 6. 整块覆盖写回 WPS
                # ================================================

                pasted_tsv = (
                    self._rewrite_table(
                        page,
                        selectors,
                        expected_table,
                    )
                )

                page.wait_for_timeout(
                    int(
                        selectors.get(
                            "save_wait_ms",
                            2500,
                        )
                    )
                )

                # ================================================
                # 7.
                #
                # 强制重新读表
                # 强制校验内容
                # 强制校验 header
                # 强制校验 date sort
                # ================================================

                verified_table = (
                    self._verify_written_table(
                        page,
                        selectors,
                        sink_cfg,
                        expected_table,
                    )
                )

                verified_rows = max(
                    0,
                    len(
                        verified_table
                    )
                    -
                    1,
                )

                # ================================================
                # 8. 成功后截图
                # ================================================

                page.screenshot(
                    path=str(
                        screenshot_path
                    ),
                    full_page=False,
                )

                state_path = (
                    self._save_storage_state(
                        context,
                        browser_cfg,
                    )
                )

                frame = (
                    self._find_wps_frame(
                        page
                    )
                )

                frame_url = (
                    frame.url
                    if frame
                    else ""
                )

            except PlaywrightTimeoutError as exc:

                raise RuntimeError(
                    "轻推/WPS 页面操作超时："
                    f"{exc}"
                ) from exc

        return {
            "uploaded": True,

            "document_url":
                document_url,

            "wps_frame_url":
                frame_url,

            "local_result":
                str(csv_path),

            "upload_screenshot":
                str(
                    screenshot_path
                ),

            # 本次是否覆盖旧日期。
            "replaced_existing":
                replaced_count > 0,

            "replaced_count":
                replaced_count,

            # 原表是否已经有正确表头。
            "had_header":
                had_header,

            # 写完以后重新读取并验证到的数据行。
            "verified_rows":
                verified_rows,

            "verified_sorted":
                True,

            "verified_header":
                True,

            "pasted_tsv":
                pasted_tsv,

            "storage_state":
                state_path,

            "browser_kept_open":
                True,

            "cdp_url":
                self._cdp_url(
                    browser_cfg
                ),
        }    