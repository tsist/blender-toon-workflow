# SPDX-License-Identifier: GPL-3.0-or-later
"""Plain-data validation and immutable frame identities for sequence exports."""
import hashlib
import json
import re
import os
import math

FRAME_SCHEMA='toon.render-sequence-frame.v1'
RESULT_SCHEMA='toon.render-sequence-result.v1'
MIN_FRAME=-1048574
MAX_FRAME=1048574
_RESERVED=re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)',re.I)
_TOKEN=re.compile(r'[0-9a-f]{32}\Z')

def frames(value):
    if not isinstance(value,(list,tuple)) or not value:
        raise ValueError('序列需要至少一个整数帧')
    result=[];seen=set()
    for frame in value:
        if type(frame) is not int or not MIN_FRAME<=frame<=MAX_FRAME:
            raise ValueError('序列帧必须是 Blender 合法范围内的整数')
        if frame in seen:raise ValueError('序列帧不能重复')
        seen.add(frame);result.append(frame)
    return result

def prefix(value):
    if (not isinstance(value,str) or not value or len(value)>120
            or value[-1] in '. ' or value[0].isspace()
            or any(ord(c)<32 or c in '<>:"/\\|?*' for c in value)
            or _RESERVED.match(value)):
        raise ValueError('请选择不含路径分隔符或 Windows 保留名称的序列前缀')
    try:value.encode('utf8')
    except UnicodeEncodeError as error:raise ValueError('序列前缀需要有效的 Unicode 文本') from error
    return value

def extension(value):
    if value not in ('.png','.exr'):raise ValueError('序列仅支持 .png 或 .exr')
    return value

def sequence_id(value):
    if not isinstance(value,str) or not _TOKEN.fullmatch(value):
        raise ValueError('Invalid sequence identity')
    return value

def frame_name(stem,frame,suffix):
    return prefix(stem)+'_'+format(frame,'06d')+extension(suffix)

def marker_name(index):
    if type(index) is not int or index<0:raise ValueError('Invalid sequence index')
    return 'frame-'+format(index,'06d')+'.json'

def pixel_relative(index,suffix):
    marker_name(index)
    return 'frames/'+format(index,'06d')+'/pixels'+extension(suffix)

def _read_stream(path):
    """Permit Windows delete-sharing; host rename failures remain possible."""
    if os.name!='nt':return open(path,'rb')
    import ctypes
    from ctypes import wintypes
    import msvcrt
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    create=kernel.CreateFileW
    create.argtypes=(wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,
                     wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE)
    create.restype=wintypes.HANDLE
    close=kernel.CloseHandle;close.argtypes=(wintypes.HANDLE,);close.restype=wintypes.BOOL
    # GENERIC_READ, FILE_SHARE_READ|WRITE|DELETE, OPEN_EXISTING.
    handle=create(os.fspath(path),0x80000000,0x1|0x2|0x4,None,3,0x80,None)
    if handle==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
    try:descriptor=msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
    except BaseException:
        close(handle);raise
    try:return os.fdopen(descriptor,'rb')
    except BaseException:
        os.close(descriptor);raise

def _json_constant(value):
    raise ValueError('Non-finite JSON number: '+value)

def _json_float(value):
    result=float(value)
    if not math.isfinite(result):raise ValueError('Non-finite JSON number: '+value)
    return result

def read_json(path):
    with _read_stream(path) as stream:return json.load(stream,parse_constant=_json_constant,parse_float=_json_float)

def digest(path):
    with _read_stream(path) as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def checked_frame_marker(value,*,token,index,frame):
    if (not isinstance(value,dict) or value.get('schema')!=FRAME_SCHEMA
            or value.get('ok') is not True or value.get('sequence_id')!=sequence_id(token)
            or type(value.get('index')) is not int or value['index']!=index
            or type(value.get('frame')) is not int or value['frame']!=frame
            or not isinstance(value.get('result'),dict)):
        raise ValueError('Invalid completed sequence-frame marker')
    result=value['result']
    if (type(result.get('frame')) is not int or result['frame']!=frame
            or type(result.get('frame_subframe')) is not float or result['frame_subframe']!=0.0):
        raise ValueError('Sequence marker does not describe its requested integer frame')
    return result

def atomic_json(path,value,*,create_only=False):
    """Replace a private mutable receipt/progress file after its contents close."""
    import os
    import uuid
    from pathlib import Path
    path=Path(path);temporary=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
    try:
        with temporary.open('x',encoding='utf8') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)
            stream.flush();os.fsync(stream.fileno())
        if create_only:
            if os.name=='nt':
                # Windows rename is create-only and leaves no post-publication
                # unlink which a contemporaneous reader could block.
                os.rename(temporary,path)
            else:os.link(temporary,path)
        else:os.replace(temporary,path)
    finally:
        try:temporary.unlink(missing_ok=True)
        except (OSError,ValueError,TypeError):pass  # Cleanup must not replace the primary error.
