# SPDX-License-Identifier: GPL-3.0-or-later
"""Single install entry for the embedded Toon domains and Pencil backend."""
bl_info = {
    'name': '三渲二工作台', 'author': 'Blender AI',
    'version': (1, 0, 1), 'blender': (5, 3, 0), 'category': '3D View',
    'description': '头部法向、二维描边、镜头修形与联合渲染的统一工作台',
}


# Blender removes legacy bl_info when enabling an extension.
_RELEASE_VERSION = '1.0.1'


def _validate_payload():
    import hashlib,json
    from pathlib import Path,PurePosixPath
    root=Path(__file__).resolve().parent
    manifest=json.loads((root/'bundle.json').read_text(encoding='utf8'))
    if manifest.get('schema')!='toon-unified-bundle.v1' or manifest.get('version')!=_RELEASE_VERSION:
        raise RuntimeError('三渲二安装包身份不匹配')
    files=manifest.get('files')
    if type(files) is not dict or not 1<=len(files)<=512:
        raise RuntimeError('三渲二安装包文件清单无效')
    for name,expected in files.items():
        relative=PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name:
            raise RuntimeError('三渲二安装包包含非法路径')
        path=root/name
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise RuntimeError('三渲二安装包缺少文件：'+name)
        if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise RuntimeError('三渲二安装包内容已改变：'+name)


def register():
    _validate_payload()
    from . import toon_workflow_addon
    toon_workflow_addon.register()


def unregister():
    from . import toon_workflow_addon
    toon_workflow_addon.unregister()
