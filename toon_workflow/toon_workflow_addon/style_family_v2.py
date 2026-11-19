# SPDX-License-Identifier: GPL-3.0-or-later
"""Portable CEL/GAME/MANGA mechanisms composed with the unchanged v1 owner.

Documents contain roles and settings only. Native objects and rollback state
remain in runtime tokens; a family name never establishes artistic acceptance.
"""
import copy
import json
import math
import uuid

from . import style_family as BASE, manga_look

SCHEMA = 'toon-style-family.v2'
_FIELDS = {'schema', 'family_id', 'name', 'family', 'recipe', 'line', 'look'}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _base(document):
    value = {key: copy.deepcopy(document[key]) for key in _FIELDS - {'look'}}
    value.update(schema=BASE.SCHEMA, family='CEL')
    return value


def _look(value, roles):
    if type(value) is not dict or set(value) != {'foreground', 'background', 'background_roles'}:
        raise ValueError('MANGA look 须精确携带前景、背景设置和显式背景角色')
    foreground, background = value['foreground'], value['background']
    fields = set(manga_look.DEFAULT_SETTINGS)
    if type(foreground) is not dict or set(foreground) != fields | {'line_ink_strength'}:
        raise ValueError('MANGA 前景须携带完整设置及原描边墨色')
    if type(background) is not dict or set(background) != fields:
        raise ValueError('MANGA 背景须携带完整设置')
    strength = foreground['line_ink_strength']
    if type(strength) not in (int, float) or not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError('原描边墨色须为 [0,1] 的有限非布尔数')
    fg = manga_look.normalize_settings({key: foreground[key] for key in fields})
    bg = manga_look.normalize_settings(background)
    if fg['mode'] != 'MANGA':
        raise ValueError('MANGA 家族前景模式必须为 MANGA')
    fg['line_ink_strength'] = float(strength)
    names = value['background_roles']
    if (type(names) is not list or any(type(name) is not str or name not in roles for name in names)
            or len(names) != len(set(names))):
        raise ValueError('背景角色须为配方中的明确角色列表，不重复')
    # Normalize tuples emitted by the pure image module into JSON-native arrays.
    return json.loads(encode(dict(foreground=fg, background=bg, background_roles=list(names))))


def normalize(value):
    if type(value) is not dict or set(value) != _FIELDS:
        raise ValueError('v2 家族字段不完整或包含未知字段')
    if value['schema'] != SCHEMA or value['family'] not in ('CEL', 'GAME', 'MANGA'):
        raise ValueError('需要 toon-style-family.v2 及 CEL/GAME/MANGA 机制')
    foundation = BASE.normalize(_base(value))
    if uuid.UUID(foundation['family_id']).int == 0:
        raise ValueError('家族需要非零 UUID')
    doc = dict(foundation, schema=SCHEMA, family=value['family'], look=None)
    surfaces = [spec for spec in doc['recipe']['roles'].values() if spec['action'] == 'SURFACE']
    if doc['family'] == 'MANGA':
        if surfaces:
            raise ValueError('MANGA 通过整镜头图像机制工作，所有原生材质角色必须 PRESERVE')
        doc['look'] = _look(value['look'], doc['recipe']['roles'])
    else:
        if value['look'] is not None:
            raise ValueError('CEL/GAME 的 look 必须为 None')
        widths = [spec.get('boundary', {}).get(key, 0.) for spec in surfaces
                  for key in ('width_low', 'width_high')]
        if doc['family'] == 'CEL' and any(width != 0 for width in widths):
            raise ValueError('CEL 家族必须使用零宽度硬边')
        if doc['family'] == 'CEL':
            # Absence means preserve an existing boundary to the lower owner.
            # Canonical CEL must explicitly disable it, including imported
            # documents which originally omitted the optional recipe field.
            for spec in surfaces:
                spec['boundary'] = dict(width_low=0., width_high=0.)
        if doc['family'] == 'GAME' and not any(width > 0 for width in widths):
            raise ValueError('GAME 家族需要普通 CEL3/reflect SURFACE 的非零合法边界')
    encode(doc)
    return copy.deepcopy(doc)


def new_document(recipe, family='CEL', name=None, line_document=None, *, background_roles=None):
    if family not in ('CEL', 'GAME', 'MANGA'):
        raise ValueError('未知风格家族机制')
    recipe = copy.deepcopy(recipe)
    recipe['lines'] = []
    look = None
    if family == 'MANGA':
        if background_roles is None:
            raise ValueError('MANGA 须显式声明 background_roles；没有背景时明确传 []')
        recipe['roles'] = {role: dict(action='PRESERVE') for role in recipe['roles']}
        foreground = manga_look.normalize_settings(dict(mode='MANGA'))
        foreground['line_ink_strength'] = 1.
        background = manga_look.normalize_settings(dict(mode='MANGA', brightness=.12,
            tone=-.15, paper_level=.80, ink_level=.10, dot_spacing_px=10., hatch_spacing_px=10.))
        look = dict(foreground=foreground, background=background,
                    background_roles=copy.deepcopy(background_roles))
    else:
        if background_roles is not None:
            raise ValueError('只有 MANGA 携带背景角色')
        for spec in recipe['roles'].values():
            if spec['action'] != 'SURFACE':
                continue
            boundary = dict(width_low=0., width_high=0.)
            if family == 'GAME':
                settings = spec['settings']
                if settings.get('mode') not in ('eevee_cel3', 'eevee_cel3_reflect'):
                    raise ValueError('GAME 边界只支持普通 CEL3/reflect SURFACE')
                low, high = settings['threshold_low'], settings['threshold_high']
                # Each full window remains within [0,1]; their half-widths
                # sum to at most one third of the actual threshold interval.
                gap = (high - low) / 3.
                boundary = dict(width_low=min(.08, low, gap),
                                width_high=min(.08, 1. - high, gap))
            spec['boundary'] = boundary
    line = dict(mode='PRESERVE') if line_document is None else dict(mode='TEMPLATE', document=copy.deepcopy(line_document))
    return normalize(dict(schema=SCHEMA, family_id=str(uuid.uuid4()),
        name=name if name is not None else family + ' 风格家族', family=family,
        recipe=recipe, line=line, look=look))


def _background_objects(context, doc, bindings):
    if doc['family'] != 'MANGA':
        return []
    # BASE proves scene membership, exact used-slot coverage, real material
    # identity and role existence before this partition is consumed.
    BASE._coverage(context, bindings)
    background = set(doc['look']['background_roles'])
    seen_roles, objects, zones = set(), {}, {}
    for row in bindings:
        obj, role = row['object'], row['role']
        if role not in doc['recipe']['roles']:
            raise ValueError('绑定包含未知配方角色')
        seen_roles.add(role)
        objects[obj.name] = obj
        zones.setdefault(obj.name, set()).add(role in background)
    if background - seen_roles:
        raise ValueError('背景角色缺少真实材质槽绑定：' + str(sorted(background - seen_roles)))
    if any(len(value) != 1 for value in zones.values()):
        raise ValueError('同一对象的全部已用槽必须同属前景或背景；请先显式拆分对象')
    return [objects[name] for name in sorted(objects) if zones[name] == {True}]


def _shot():
    from . import shot_look_ui
    return shot_look_ui


def observe(context, document, bindings, *, template_bindings=None, ink_bindings=None):
    doc = normalize(document)
    state = BASE.observe(context, _base(doc), bindings,
                         template_bindings=template_bindings, ink_bindings=ink_bindings)
    state['document'] = doc
    state['shot_look'] = _shot().document(context.scene)
    state['background_targets'] = [obj.name for obj in _background_objects(context, doc, bindings)]
    encode(state)
    return state


def preflight(context, document, bindings, *, template_bindings=None, ink_bindings=None):
    doc = normalize(document)
    result = BASE.preflight(context, _base(doc), bindings,
        template_bindings=template_bindings, ink_bindings=ink_bindings)
    targets = _background_objects(context, doc, bindings)
    result.update(schema=SCHEMA, family=doc['family'],
        look=dict(settings=copy.deepcopy(doc['look']), background_targets=[obj.name for obj in targets]),
        art_status='not_evaluated')
    return result


def apply(context, document, bindings, *, template_bindings=None, ink_bindings=None):
    doc = normalize(document)
    preflight(context, doc, bindings, template_bindings=template_bindings, ink_bindings=ink_bindings)
    targets = _background_objects(context, doc, bindings)
    base_token = look_token = None
    try:
        base_token = BASE.apply(context, _base(doc), bindings,
            template_bindings=template_bindings, ink_bindings=ink_bindings)
        # apply_document owns rollback if it raises before returning its token.
        look_token = _shot().apply_document(context.scene, doc['look'], targets)
        current = base_token['bindings']
        observation = observe(context, doc, current,
            template_bindings=template_bindings, ink_bindings=ink_bindings)
        result = dict(BASE.receipt(base_token), schema=SCHEMA, family=doc['family'],
            look=copy.deepcopy(observation['shot_look']), art_status='not_evaluated')
        encode(result)
        return dict(status='applied', scene=context.scene, document=doc,
            result=result, observation=observation, bindings=current,
            base_token=base_token, look_token=look_token,
            template_bindings=template_bindings, ink_bindings=ink_bindings)
    except BaseException as error:
        issues = []
        if look_token is not None:
            try:
                _shot().rollback_document(context.scene, look_token)
            except Exception as rollback_error:
                issues.append('look: ' + str(rollback_error))
        if base_token is not None:
            try:
                BASE.rollback(context, base_token)
            except Exception as rollback_error:
                issues.append('surface/line: ' + str(rollback_error))
        if issues:
            raise RuntimeError('v2 家族应用失败且回退不完整：' + '; '.join(issues)) from error
        raise


def receipt(token):
    if token.get('status') != 'applied':
        raise ValueError('v2 风格家族事务未应用')
    return copy.deepcopy(token['result'])


def rollback(context, token):
    if token.get('status') != 'applied' or token['scene'] != context.scene:
        raise ValueError('本场景没有可回退的 v2 家族事务')
    current = observe(context, token['document'], token['bindings'],
        template_bindings=token['template_bindings'], ink_bindings=token['ink_bindings'])
    if encode(current) != encode(token['observation']):
        raise ValueError('家族或整镜头作者已有后续修改，保留当前状态')
    _shot().rollback_document(context.scene, token['look_token'])
    BASE.rollback(context, token['base_token'])
    token['status'] = 'rolled_back'
    return dict(status='rolled_back', family_id=token['document']['family_id'])
