"""Shared visual language for the desktop workspace, startup, and dialogs."""
from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication


PATHS = {
    "grid": '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
    "image": '<rect x="3" y="3" width="18" height="18" rx="4"/><circle cx="9" cy="9" r="2"/><path d="m4 18 5-5 4 4 3-3 5 5"/>',
    "download": '<path d="M12 3v12m-5-5 5 5 5-5M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>',
    "cloud": '<path d="M7 18H6a4 4 0 0 1-1-8 7 7 0 0 1 13-2 5 5 0 0 1 0 10h-1M12 21V11m-4 4 4-4 4 4"/>',
    "shield": '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z"/><path d="m8 12 3 3 5-6"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "check": '<path d="m5 12 4 4L19 6"/>',
    "close": '<path d="m6 6 12 12M18 6 6 18"/>',
    "skip": '<circle cx="12" cy="12" r="9"/><path d="m6 6 12 12"/>',
    "left": '<path d="m14 6-6 6 6 6"/>',
    "right": '<path d="m10 6 6 6-6 6"/>',
    "rotate": '<path d="M20 11a8 8 0 1 0-2 7M20 4v7h-7"/>',
    "fit": '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v1"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "spark": '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z"/>',
}


def icon(name, color="#787E90", size=24):
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{PATHS[name]}</svg>'
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode())).render(painter)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


def app_icon():
    pixmap = QPixmap(128, 128)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#6658E8"))
    painter.drawRoundedRect(0, 0, 128, 128, 30, 30)
    painter.setBrush(QColor("#FFFFFF"))
    for x, y in ((28, 68), (52, 48), (76, 28)):
        painter.drawRoundedRect(x, y, 15, 100 - y, 4, 4)
    painter.end()
    return QIcon(pixmap)


def apply_theme(app: QApplication):
    from pathlib import Path
    app.setStyle("Fusion")
    # Qt's offscreen platform has no font discovery on Windows. Use installed
    # Windows fonts for deterministic UI rendering in the packaged self-test.
    if not QFontDatabase.families():
        import os
        from pathlib import Path
        fonts = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts"
        for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "seguisb.ttf"):
            QFontDatabase.addApplicationFont(str(fonts / name))
    font = QFont("Microsoft YaHei UI", 10)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)
    app.setWindowIcon(app_icon())
    assets = (Path(__file__).parent / "assets").as_posix()
    app.setStyleSheet(STYLE.replace("ASSET_ROOT", assets))


STYLE = """
QWidget { color: #272A39; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
QMainWindow, QDialog { background: #F5F6FA; }
QLabel { background: transparent; border: none; }
QLabel[muted="true"] { color: #868C9E; font-size: 12px; }
QLabel[heading="true"] { color: #222638; font-size: 24px; font-weight: 700; }
QLabel[section="true"] { color: #8C91A2; font-size: 11px; font-weight: 600; }
QFrame#sidebar { background: #FFFFFF; border-right: 1px solid #E9EBF2; }
QWidget#sidebarPanel { background: #FFFFFF; }
QWidget#batchPanel { background: #F5F6FA; }
QFrame#card, QFrame#stat, QFrame#dialogCard { background: #FFFFFF; border: 1px solid #E9EBF2; border-radius: 14px; }
QFrame#dialogCard { border-radius: 20px; }
QFrame#line { background: #ECEEF4; min-height: 1px; max-height: 1px; }
QFrame#viewer { background: #181C27; border: 1px solid #262C3C; border-radius: 14px; }
QFrame#viewerToolbar { background: #202532; border-bottom: 1px solid #303647; border-top-left-radius: 14px; border-top-right-radius: 14px; }
QFrame#imageStage { background: #181C27; }
QGraphicsView { background: #181C27; border: 0; }
QPushButton { background: #FFFFFF; border: 1px solid #E3E6EF; border-radius: 9px; padding: 9px 14px; font-weight: 500; }
QPushButton:hover { background: #F4F3FF; border-color: #CFC9FA; }
QPushButton:pressed { background: #E9E6FC; }
QPushButton:focus { border-color: #9E92F5; }
QPushButton:disabled { color: #AFB4C4; background: #F2F3F7; border-color: #ECEEF4; }
QPushButton[variant="primary"] { color: white; background: #6658E8; border-color: #6658E8; font-weight: 600; padding: 11px 16px; }
QPushButton[variant="primary"]:hover { background: #7668F1; border-color: #7668F1; }
QPushButton[variant="primary"]:pressed { background: #5446D5; }
QPushButton[variant="primary"]:disabled { color: #E5E1FF; background: #B8B0F0; border-color: #B8B0F0; }
QPushButton[variant="ghost"] { border: none; background: transparent; color: #6D7386; text-align: left; padding: 9px 10px; }
QPushButton[variant="ghost"]:hover { background: #F2F1FE; color: #6658E8; }
QPushButton[variant="tool"] { background: #2A3040; border: 1px solid #363D50; color: #D5DBE8; padding: 6px 9px; border-radius: 7px; }
QPushButton[variant="tool"]:hover { background: #3A4256; }
QPushButton[variant="tool"]:disabled { color: #59647B; background: #202532; border-color: #2A3040; }
QPushButton[decision="correct"] { background: #EAF8F0; border-color: #C9ECD9; color: #27845B; }
QPushButton[decision="correct"]:hover { background: #D5F1E2; border-color: #8DD4AE; }
QPushButton[decision="incorrect"] { background: #FFF0F0; border-color: #F5D6D9; color: #D05C68; }
QPushButton[decision="incorrect"]:hover { background: #FFE0E3; border-color: #EBACB4; }
QPushButton[decision="invalid"] { background: #F5F6FA; border-color: #E1E4EF; color: #71798D; }
QPushButton[decision="invalid"]:hover { background: #EBEDF5; }
QPushButton[decision]:disabled { background: #F5F6F8; border-color: #EBEDF2; color: #B9BDCA; }
QComboBox, QDateEdit, QSpinBox, QLineEdit { background: #F8F9FC; border: 1px solid #E5E8F0; border-radius: 9px; padding: 10px; min-height: 20px; selection-background-color: #6658E8; }
QComboBox:hover, QDateEdit:hover, QSpinBox:hover { border-color: #C5BEF4; }
QComboBox:focus, QDateEdit:focus, QSpinBox:focus { border-color: #887BEC; background: #FFFFFF; }
QComboBox::drop-down, QDateEdit::drop-down { width: 24px; border: 0; }
QComboBox::down-arrow, QDateEdit::down-arrow { image: url(ASSET_ROOT/down.svg); width: 12px; height: 12px; }
QComboBox QAbstractItemView { background: white; border: 1px solid #E1DDF9; selection-background-color: #F0EDFF; selection-color: #6658E8; padding: 6px; outline: 0; }
QAbstractItemView::item { padding: 9px; min-height: 22px; }
QSpinBox::up-button, QSpinBox::down-button { width: 20px; border: 0; background: #ECEEF6; border-radius: 3px; }
QSpinBox::up-arrow { image: url(ASSET_ROOT/up.svg); width: 12px; height: 12px; }
QSpinBox::down-arrow { image: url(ASSET_ROOT/down.svg); width: 12px; height: 12px; }
QProgressBar { border: none; border-radius: 3px; background: #ECECF6; height: 6px; max-height: 6px; text-align: center; }
QProgressBar::chunk { background: #7A6CEE; border-radius: 3px; }
QCheckBox { spacing: 8px; color: #646C80; }
QRadioButton { spacing: 9px; color: #555E74; padding: 3px 0; }
QRadioButton::indicator { width: 16px; height: 16px; border: 1px solid #C4C9D7; border-radius: 9px; background: white; }
QRadioButton::indicator:checked { border: 5px solid #6658E8; width: 8px; height: 8px; background: white; }
QRadioButton::indicator:disabled { border-color: #D7DAE3; background: #F0F1F5; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 5px; border: 1px solid #CDD1DF; background: white; }
QCheckBox::indicator:checked { background: #6658E8; border: 1px solid #6658E8; image: url(ASSET_ROOT/check.svg); }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 7px; margin: 2px; }
QScrollBar::handle:vertical { background: #D1D5E1; border-radius: 2px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: #202532; height: 7px; }
QScrollBar::handle:horizontal { background: #555F75; border-radius: 3px; min-width: 28px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QToolTip { color: #F5F6FB; background: #303649; border: none; padding: 8px; border-radius: 5px; }
QCalendarWidget QToolButton { color: #6658E8; background: white; padding: 6px; }
QCalendarWidget QAbstractItemView { background: white; selection-background-color: #6658E8; selection-color: white; }
QTableWidget { background: #FFFFFF; alternate-background-color: #F8F9FC; border: 1px solid #E5E8F0; border-radius: 10px; selection-background-color: #F0EDFE; selection-color: #403851; outline: 0; }
QTableWidget::item { padding: 8px; border-bottom: 1px solid #F0F1F6; }
QHeaderView::section { background: #F5F6FA; border: none; border-bottom: 1px solid #E5E8F0; color: #868C9E; padding: 10px 8px; font-weight: 500; }
"""
