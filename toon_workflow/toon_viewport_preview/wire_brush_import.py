# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure import of newly introduced native SIMPLE actuals; never overwrite edits.

Full captured native evidence stays in the caller's original full_preset. This
module returns only the existing eight-field editor brush. Native chain knobs
are recognized, kept in full_preset and seeded separately by migration, never
converted to editor brush fields. Native color maps are never alpha maps.
"""
import copy
import math
from . import source_bindings as bindings
from . import style
from ._support import paint_editor_state as codec

EDITOR_FIELDS = {'uid','native_size','blend_amount','color','width_map','alpha_map','size_curve','alpha_curve'}
CURVE_KEYS = ('size_curve','alpha_curve')
UI_FIELDS = set(["color","color_tag","dimensions","height","hide","label","location","location_absolute","mute","name","select","show_options","show_preview","show_texture","type","use_custom_color","warning_propagation","width"])
BRUSH_FIELDS = set(["blend_amount","brush_color","brush_detail_node","color","color_map","color_map_on","color_map_on_gui","color_map_opacity","color_tag","dimensions","height","hide","label","location","location_absolute","mute","name","select","show_options","show_preview","show_texture","size","size_map","size_map_amount","size_map_on","size_map_on_gui","type","use_custom_color","warning_propagation","width"])
DETAIL_FIELDS = set(["alpha_reduction_curve","alpha_reduction_curve_data_str","alpha_reduction_enabled","angle","angle_random","antialiasing","brush_map","brush_map_on","brush_map_on_gui","brush_map_opacity","brush_type","color","color_space_blue","color_space_green","color_space_hue","color_space_red","color_space_saturation","color_space_type","color_space_value","color_tag","dimensions","distortion_amount","distortion_cycles","distortion_cycles_random","distortion_enabled","distortion_map","distortion_map_amount","distortion_map_on","distortion_map_on_gui","distortion_phase","distortion_phase_random","distortion_random","extend","extend_random","groove","groove_number","height","hide","horizontal_space","horizontal_space_random","label","length","length_random","line_copy","line_copy_random","line_direction","line_link_length","line_split_angle","line_type","location","location_absolute","loop_direction_type","min_line_length","mute","name","normal_offset","normal_offset_random","reduction_end","reduction_start","select","show_options","show_preview","show_texture","size","size_random","size_reduction_curve","size_reduction_curve_data_str","size_reduction_enabled","space","space_random","stretch","stretch_random","stroke_size_random","stroke_type","type","use_custom_color","vertical_space","vertical_space_random","warning_propagation","width","x_offset","x_offset_random","y_offset","y_offset_random"])
CHAIN_KEYS = set(["line_link_length","line_split_angle","line_direction","min_line_length","loop_direction_type"])
# Conservative captured SIMPLE profile; unsupported active/inactive native
# detail variants are refused rather than silently assigned autonomous semantics.
DETAIL_PROFILE = {
  "angle": 0,
  "angle_random": 0,
  "antialiasing": 0.5,
  "brush_map": "",
  "brush_map_on": False,
  "brush_map_on_gui": False,
  "brush_map_opacity": 1,
  "brush_type": "SIMPLE",
  "color_space_blue": 0,
  "color_space_green": 0,
  "color_space_hue": 0,
  "color_space_red": 0,
  "color_space_saturation": 0,
  "color_space_type": "RGB",
  "color_space_value": 0,
  "distortion_amount": 5,
  "distortion_cycles": 100,
  "distortion_cycles_random": 0,
  "distortion_enabled": False,
  "distortion_map": "",
  "distortion_map_amount": 5,
  "distortion_map_on": False,
  "distortion_map_on_gui": False,
  "distortion_phase": 0,
  "distortion_phase_random": 0,
  "distortion_random": 0,
  "extend": 0,
  "extend_random": 0,
  "groove": 0,
  "groove_number": 5,
  "horizontal_space": 0.10000000149011612,
  "horizontal_space_random": 100,
  "length": 5,
  "length_random": 0,
  "line_copy": 1,
  "line_copy_random": 0,
  "line_type": "FULL",
  "normal_offset": 0,
  "normal_offset_random": 0,
  "reduction_end": 1,
  "reduction_start": 1,
  "size": 16,
  "size_random": 80,
  "space": 5,
  "space_random": 0,
  "stretch": 0,
  "stretch_random": 0,
  "stroke_size_random": 0,
  "stroke_type": "NORMAL",
  "vertical_space": 0.10000000149011612,
  "vertical_space_random": 100,
  "x_offset": 0,
  "x_offset_random": 0,
  "y_offset": 0,
  "y_offset_random": 0
}


def _need(value,message):
    if not value:raise ValueError(message)


def _exact(value,keys,label):
    _need(type(value) is dict and set(value)==set(keys),label+' fields differ or are unknown')


def _text(value,label):
    _need(type(value) is str and 0<len(value)<=256 and '\x00' not in value,label+' must be a bounded identity')
    try:value.encode('utf8')
    except UnicodeError as exc:raise ValueError(label+' must be UTF-8') from exc
    return value


def _scalar(value,label,lo=None,hi=None):
    _need(type(value) in (int,float),label+' must be a finite real scalar')
    try:finite=math.isfinite(value)
    except OverflowError:finite=False
    _need(finite and (lo is None or value>=lo) and (hi is None or value<=hi),label+' is out of range')


def _source(full,name):
    bindings._preflight(full)
    _need(type(full) is dict and full.get('schema')=='toon-c4-original-preset.v1','supported captured full_preset required')
    _text(name,'brush name')
    source=bindings._object(full.get('source'),'full.source',nonempty=True)
    bindings._canonical(source)  # Canonical actual identity must be UTF-8 encodable.
    brushes=bindings._object(full.get('brushes'),'full.brushes',nonempty=True)
    _need(len(brushes)<=bindings.MAX_BRUSHES and name in brushes,'exact captured brush name missing')
    return source,brushes[name]


def canonical_actual(full,name):
    """The existing source_bindings / curve_editor identity, no UID substitution."""
    source,_=_source(full,name)
    return bindings._identity(source,name)


def _disabled_map():
    # Inert autonomous editor defaults. No raw color-map -> alpha-map inference.
    return dict(enabled=False,selection_kind='index',layer_name='',layer_index=0,
                channel='mean_rgb',encoding='linear_rgba',missing='error')


def _import_curve(detail,key):
    raw=detail[key];props=detail['properties'];stem='size' if key=='size_curve' else 'alpha'
    _exact(raw,{'enabled','evidence','id','mapping','points','samples','source_data_str'},key+' capture')
    _need(raw['evidence']=='CALLER_CAPTURE_POINTS_VERIFIED','verified captured points required for '+key)
    _need(type(raw['enabled']) is bool and type(props[stem+'_reduction_enabled']) is bool and
          raw['enabled']==props[stem+'_reduction_enabled'],key+' enable evidence conflicts')
    _text(raw['id'],key+' id')
    _need(raw['id']==props[stem+'_reduction_curve'] and type(raw['source_data_str']) is str and
          raw['source_data_str']==props[stem+'_reduction_curve_data_str'],key+' captured identity/data differs')
    _exact(raw['mapping'],{'use_clip','clip','extend','tone','black_level','white_level'},key+' mapping')
    result=dict(enabled=raw['enabled'],points=copy.deepcopy(raw['points']),samples=copy.deepcopy(raw['samples']),
                **copy.deepcopy(raw['mapping']))
    codec._curve(result)
    return result


def _validate_editor(editor):
    bindings._preflight(editor)
    _exact(editor,EDITOR_FIELDS,'editor brush')
    codec.uid(editor['uid'])
    _scalar(editor['native_size'],'native_size',0)
    _scalar(editor['blend_amount'],'blend_amount',0,1)
    codec.vector(editor['color'],3,0,1,'Linear brush RGB')
    for key in ('width_map','alpha_map'):codec._map(editor[key])
    for key in CURVE_KEYS:codec._curve(editor[key])
    bindings._canonical(editor)  # Validate UTF-8 even for disabled map names.


def project_editor_brush(editor):
    """Owned seven-field boundary brush using the existing style rules.

    All disabled map/curve author metadata is validated before projection.
    Sample projection is not native CurveMapping reevaluation/authentication.
    """
    _validate_editor(editor)
    def mapping(value):
        if not value['enabled']:return None
        return dict(selection=({'name':value['layer_name']} if value['selection_kind']=='name' else {'index':value['layer_index']}),
                    channel=value['channel'],encoding=value['encoding'],missing=value['missing'])
    projected=dict(native_size=copy.deepcopy(editor['native_size']),blend_amount=copy.deepcopy(editor['blend_amount']),
        color=copy.deepcopy(editor['color']),width_map=mapping(editor['width_map']),alpha_map=mapping(editor['alpha_map']),
        **{key:copy.deepcopy(editor[key]['samples']) if editor[key]['enabled'] else None for key in CURVE_KEYS})
    warnings=[]
    compiled,meta=style._compile_brush(editor,projected,'wire import brush','brush',warnings,set())
    _need(compiled=={k:v for k,v in projected.items() if k!='native_size'} and
          meta['native_size']==editor['native_size'],'style numeric projection differs')
    _need(all(w['code']=='sampled-curve-scope' for w in warnings),'style projection produced an unexpected warning')
    return projected


def import_editor_brush(full,name,uid):
    """Import only a genuinely new actual. Existing editor clones are caller-owned.

    No lookup/replacement of existing editor records, no UID generation, no raw
    evidence mutation, and no IO or Blender imports take place here.
    """
    codec.uid(uid)
    _,brush=_source(full,name)
    _exact(brush,{'detail','maps','properties'},'captured brush')
    props=brush['properties'];_exact(props,BRUSH_FIELDS,'captured brush properties')
    _need(props['name']==name,'captured brush property name differs')
    _need(props['mute'] is False,'muted captured brush nodes are unsupported')
    _exact(brush['maps'],{'size','color'},'captured raw maps')
    for role in ('size','color'):
        mapping=brush['maps'][role];_exact(mapping,{'enabled','node'},'raw '+role+' map')
        _need(mapping['enabled'] is False and mapping['node'] is None and
              props[role+'_map_on'] is False and props[role+'_map_on_gui'] is False,
              'enabled or linked raw '+role+' map is unsupported; no color-to-alpha conversion')
    detail_name=_text(brush['detail'],'detail name')
    details=bindings._object(full.get('details'),'full.details',nonempty=True)
    _need(detail_name in details,'captured detail missing')
    detail=details[detail_name];_exact(detail,{'properties','size_curve','alpha_curve'},'captured detail')
    dp=detail['properties'];_exact(dp,DETAIL_FIELDS,'captured detail properties')
    _need(dp['name']==detail_name,'captured detail property name differs')
    _need(dp['mute'] is False,'muted captured detail nodes are unsupported')
    for key,expected in DETAIL_PROFILE.items():
        value=dp[key]
        if type(expected) is bool:_need(type(value) is bool and value==expected,'unsupported detail '+key)
        elif type(expected) is str:_need(type(value) is str and value==expected,'unsupported detail '+key)
        else:
            _scalar(value,'detail '+key)
            _need(value==expected,'unsupported detail '+key)
    # These recognized fields belong to the independent chain authoring layer.
    for key in CHAIN_KEYS-{'loop_direction_type'}:_scalar(dp[key],'chain evidence '+key)
    _text(dp['loop_direction_type'],'chain evidence loop_direction_type')
    result=dict(uid=uid,native_size=copy.deepcopy(props['size']),blend_amount=copy.deepcopy(props['blend_amount']),
                color=copy.deepcopy(props['brush_color']),width_map=_disabled_map(),alpha_map=_disabled_map(),
                **{key:_import_curve(detail,key) for key in CURVE_KEYS})
    # Validate the complete author and its current executable numeric projection.
    project_editor_brush(result)
    return result
