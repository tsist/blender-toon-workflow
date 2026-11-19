# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent implementation of the documented/public bridge unit transform.

Design reference only, no upstream source copied. The returned ScaleEx matches
the public v4.1.5 bridge formula, not a physical raster diameter or Alpha model.
"""
import math
import struct

SCHEMA='toon.line-size-resolution.v1'

def _float32(value):
    try:result=struct.unpack('<f',struct.pack('<f',value))[0]
    except (OverflowError,struct.error) as error:raise ValueError('Scale exceeds float32') from error
    if not math.isfinite(result):raise ValueError('Finite float32 scale required')
    return result

def _dimension(value,name,optional=False):
    if optional and value is None:return None
    if type(value) is not int or not (0 if optional else 1)<=value<=2147483647:
        raise ValueError(name+' must be an integer dimension')
    return value

def _scale(value,name):
    if type(value) not in (int,float) or not math.isfinite(value) or value<0:
        raise ValueError(name+' must be finite and nonnegative')
    return _float32(value)

def resolve_size_scale(*,mode,width,height,sensor_fit='AUTO',initial_scale_ex=1.0,
                       absolute_scale=1.0,relative_target_width=None,relative_target_height=None):
    """Return the float32 ScaleEx for a new Line evaluation, without mutation.

    width/height are the actual output dimensions. Optional positive target
    dimensions override each axis independently; zero/None means absent.
    AUTO chooses using the resulting target aspect, including square W>=H.
    ABSOLUTE ignores relative targets and uses absolute_scale; RELATIVE ignores
    absolute_scale. Negative overrides are rejected by this new input contract.
    The caller passes an unmodified initial ScaleEx on every update, so a repeat
    request cannot compound a prior resolution factor.
    """
    if type(mode) is not str or mode not in ('RELATIVE','ABSOLUTE'):raise ValueError('Explicit size mode required')
    if type(sensor_fit) is not str or sensor_fit not in ('AUTO','HORIZONTAL','VERTICAL'):raise ValueError('Explicit sensor fit required')
    width=_dimension(width,'width');height=_dimension(height,'height')
    tw=_dimension(relative_target_width,'relative_target_width',True)
    th=_dimension(relative_target_height,'relative_target_height',True)
    target_width=tw if tw else width;target_height=th if th else height
    # DrawOptions targets are float; the ternary also promotes output integer
    # dimensions to float before AUTO compares them. Preserve the rounded tie.
    effective_width=_float32(target_width);effective_height=_float32(target_height)
    initial=_scale(initial_scale_ex,'initial_scale_ex');absolute=_scale(absolute_scale,'absolute_scale')
    if mode=='ABSOLUTE':factor=absolute;axis=None
    else:
        axis='width' if sensor_fit=='HORIZONTAL' or (sensor_fit=='AUTO' and effective_width>=effective_height) else 'height'
        target=effective_width if axis=='width' else effective_height
        factor=_float32(target/(640.0 if axis=='width' else 480.0))
    result=_float32(initial*factor)
    return dict(schema=SCHEMA,source_mode=mode,resolved_mode='ABSOLUTE',sensor_fit=sensor_fit,
        output_size=[width,height],relative_target_size=[target_width,target_height],axis=axis,
        effective_float32_target_size=[effective_width,effective_height],
        resolution_factor=factor,scale_ex=result,arithmetic='IEEE754 binary32 public bridge ScaleEx',
        coverage_diameter_resolved=False)
