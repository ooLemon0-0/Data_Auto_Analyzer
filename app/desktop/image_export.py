"""Copy a verified source original without rendering, resizing or re-encoding."""
from pathlib import Path
import re
import shutil

from PySide6.QtCore import QStandardPaths


def _component(value, fallback):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value or ''))
    return text.strip(' .')[:40] or fallback


def downloads_directory():
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    return Path(location) if location else Path.home() / 'Downloads'


def save_original_image(source, project, day, source_key, *, directory=None):
    from PIL import Image
    source = Path(source)
    with Image.open(source) as image:
        suffix = { 'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp', 'BMP': '.bmp', 'TIFF': '.tif', 'GIF': '.gif' }.get(image.format)
        image.verify()
    if not suffix:
        raise ValueError('原图格式暂不支持保存')
    if directory is None:
        directory = downloads_directory()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stem = '_'.join((_component(project, '审核图片'), _component(day, '未知日期'), _component(source_key, '样本')))
    index = 1
    while True:
        destination = directory / (stem + (f' ({index})' if index > 1 else '') + suffix)
        try:
            output = destination.open('xb')
        except FileExistsError:
            index += 1
            continue
        try:
            with output:
                with source.open('rb') as original:
                    shutil.copyfileobj(original, output)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return destination
