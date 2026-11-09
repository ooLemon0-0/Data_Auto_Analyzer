from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.projects.binxin_phase2.source import BinxinPhase2HistorySource


class Binxin88HistorySource(BinxinPhase2HistorySource):
    """One station, two OCR cameras, using the newer camera/group API."""

    def __init__(self, project_config):
        super().__init__(project_config)
        names = self.source.get('camera_names')
        if (not isinstance(names, list) or not names or
                any(not isinstance(name, str) or not name.strip() for name in names) or
                len(set(names)) != len(names)):
            raise ValueError('88 点位必须配置不重复的 source.camera_names')
        self.camera_names = names

    def _resolve_camera(self, session):
        payload = self._query(session, '/api/v1/camera/condition-query',
            params={'currPage': 1, 'pageSize': 9999, 'sort': 'id,descending'})
        rows = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError('88 相机列表格式错误')
        cameras = []
        for name in self.camera_names:
            matches = [row for row in rows if row.get('cameraName') == name]
            if len(matches) != 1:
                raise RuntimeError(f'88 相机名称未唯一匹配：{name}')
            camera = matches[0]
            if not camera.get('id') or not camera.get('businessGroupId') or camera.get('ocrCamera') is not True:
                raise RuntimeError(f'88 相机缺少 OCR 配置或业务分组：{name}')
            cameras.append(camera)
        groups = {str(camera['businessGroupId']) for camera in cameras}
        if len(groups) != 1 or len({str(camera['id']) for camera in cameras}) != len(cameras):
            raise RuntimeError('88 相机业务分组不一致或相机标识重复，请检查现场配置')
        # The group is fetched once, then each record is mapped to its own camera.
        return {'id': '', 'businessGroupId': cameras[0]['businessGroupId'],
                'cameras': {str(camera['id']): camera for camera in cameras}}

    def _record_to_item(self, record, camera, business_date):
        cameras = camera['cameras']
        camera_id = str(record.get('cameraId') or '')
        image_cameras = set()
        for field in ('identifyFullUrl', 'identifyPictureUrl'):
            prefix = Path(unquote(urlsplit(str(record.get(field) or '')).path)).name.split('_', 1)[0]
            if prefix in cameras:
                image_cameras.add(prefix)
        if len(image_cameras) > 1 or (camera_id and image_cameras and camera_id not in image_cameras):
            raise RuntimeError('88 历史记录的原图与识别图相机来源不一致')
        if not camera_id:
            if len(image_cameras) != 1:
                raise RuntimeError('88 历史记录无法确定相机来源，未接收该批数据')
            camera_id = next(iter(image_cameras))
        normalized = {**record, 'cameraId': camera_id}
        selected = cameras.get(camera_id)
        item = super()._record_to_item(normalized, selected or camera, business_date)
        if item is not None:
            item.metadata['camera_name'] = selected['cameraName']
        return item
