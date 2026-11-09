"""Export a full-resolution review image outside the disposable image cache."""
from pathlib import Path
import re

from PySide6.QtCore import QBuffer, QIODevice, QStandardPaths, Qt
from PySide6.QtGui import QTransform


def _component(value, fallback):
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value or ''))
    return text.strip(' .')[:40] or fallback


def save_review_image(image, project, day, source_key, *, rotation=0, directory=None):
    if image is None or image.isNull():
        raise ValueError('没有可保存的图片')
    if directory is None:
        location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        directory = Path(location) if location else Path.home() / 'Downloads'
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if rotation % 4:
        image = image.transformed(QTransform().rotate((rotation % 4) * 90), Qt.TransformationMode.SmoothTransformation)
    buffer = QBuffer()
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(buffer, 'PNG'):
        raise OSError('图片编码失败')
    content = bytes(buffer.data())
    stem = '_'.join((_component(project, '审核图片'), _component(day, '未知日期'), _component(source_key, '样本')))
    index = 1
    while True:
        destination = directory / (stem + (f' ({index})' if index > 1 else '') + '.png')
        try:
            output = destination.open('xb')
        except FileExistsError:
            index += 1
            continue
        try:
            with output:
                output.write(content)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return destination
