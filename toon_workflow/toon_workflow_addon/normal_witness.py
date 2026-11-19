# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit, bounded shading-data witness for artistic-normal frame freezing.

The existing geometry SHA retains its topology/position meaning. This feature
only certifies the listed artistic-normal targets and their named fields.
"""
import hashlib
import math
import re

SCHEMA = 'toon-artistic-normal-freeze.v1'


def snapshot(mesh, names):
    import numpy as np
    normal = np.empty(len(mesh.corner_normals)*3, np.float32)
    mesh.corner_normals.foreach_get('vector', normal)
    if not np.isfinite(normal).all():
        raise ValueError('Nonfinite corner normal in artistic-normal target')
    result = dict(vertices=len(mesh.vertices), corners=len(mesh.loops),
                  custom_normals=bool(mesh.has_custom_normals),
                  corner_normals_sha256=hashlib.sha256(normal.tobytes()).hexdigest(), fields={})
    for name in names:
        attribute = mesh.attributes.get(name)
        if attribute is None or attribute.domain != 'POINT':
            raise ValueError('Artistic-normal POINT field is missing: '+name)
        if attribute.data_type == 'FLOAT_VECTOR':
            key, count = 'vector', 3
        elif attribute.data_type in {'FLOAT', 'BOOLEAN'}:
            key, count = 'value', 1
        else:
            raise ValueError('Unsupported artistic-normal field type: '+name)
        values = np.empty(len(attribute.data)*count, np.float32)
        attribute.data.foreach_get(key, values)
        if not np.isfinite(values).all():
            raise ValueError('Nonfinite artistic-normal field: '+name)
        result['fields'][name] = dict(domain='POINT', data_type=attribute.data_type,
            count=len(attribute.data), sha256=hashlib.sha256(values.tobytes()).hexdigest())
    return result


def validate_receipt(feature):
    """Cold structural/integrity check; never claims artistic acceptance."""
    if (type(feature) is not dict or feature.get('schema') != SCHEMA
            or type(feature.get('targets')) is not list or len(feature['targets']) > 256):
        raise ValueError('Unsupported artistic-normal freeze witness')
    targets=set()
    for row in feature['targets']:
        if (row.get('target') in targets or not isinstance(row.get('target'), str)
                or row.get('profile') not in {'toon-artistic-normal.v1','toon-artistic-normal.v2'}
                or row.get('status') != 'valid' or row.get('space') != 'WORLD_PRE_CAMERA_CORRECTION'
                or row.get('data_exact') is not True
                or not isinstance(row.get('strength'), (int,float))
                or isinstance(row['strength'],bool) or not math.isfinite(row['strength'])
                or not 0 <= row['strength'] <= 1):
            raise ValueError('Invalid artistic-normal target receipt')
        if row['profile']=='toon-artistic-normal.v2':
            if (row.get('mapping_space')!='REFERENCE_COVECTOR'
                    or not isinstance(row.get('mapping_attribute'),str)
                    or not 1<=len(row['mapping_attribute'])<=63):
                raise ValueError('Invalid reference-space artistic-normal mapping receipt')
        targets.add(row['target'])
        attributes = row.get('attributes')
        if (type(attributes) is not dict or set(attributes) != {'normal','mix','valid'}
                or any(not isinstance(name,str) or not name for name in attributes.values())
                or len(set(attributes.values())) != 3):
            raise ValueError('Invalid artistic-normal attribute bindings')
        before=row['before']
        if before != row['copied'] or before != row['frozen']:
            raise ValueError('Artistic-normal shading data changed during frame freezing')
        if (type(before['vertices']) is not int or before['vertices'] < 0
                or type(before['corners']) is not int or before['corners'] < 0
                or not re.fullmatch('[0-9a-f]{64}',str(before['corner_normals_sha256']))
                or type(before['fields']) is not dict or set(before['fields']) != set(attributes.values())):
            raise ValueError('Invalid shading-data witness extent')
        kinds=[]
        for field in before['fields'].values():
            kinds.append(field.get('data_type'))
            if (field.get('domain') != 'POINT' or field.get('count') != before['vertices']
                    or not re.fullmatch('[0-9a-f]{64}',str(field.get('sha256')))):
                raise ValueError('Invalid shading-data field witness')
        if sorted(kinds) != ['BOOLEAN','FLOAT','FLOAT_VECTOR']:
            raise ValueError('Artistic-normal witness fields differ')
        for role,kind in [('normal','FLOAT_VECTOR'),('mix','FLOAT'),('valid','BOOLEAN')]:
            if before['fields'][attributes[role]]['data_type'] != kind:
                raise ValueError('Artistic-normal field role differs from its witnessed type')
    return len(targets)


def validate_head_receipt(config, feature):
    """Bind an explicit whole-head author configuration to render witnesses."""
    if config is None:
        return None
    if (type(config) is not dict or config.get('profile')!='toon-head-normals.v1'
            or config.get('status')!='applied' or not re.fullmatch('[0-9a-f]{32}',str(config.get('owner')))
            or type(config.get('bindings')) is not list or not 1<=len(config['bindings'])<=64
            or config.get('binding_count')!=len(config['bindings'])):
        raise ValueError('Invalid whole-head normal configuration receipt')
    actual={row['owner']:row for row in feature['targets']}
    owners=set();roles={}
    for binding in config['bindings']:
        owner=binding.get('owner');role=binding.get('role')
        if (owner in owners or not isinstance(owner,str)
                or role not in {'FACE','LASH','BROW','HAIR','EYE','MOUTH','ACCESSORY'}
                or type(binding.get('render_visible')) is not bool):
            raise ValueError('Invalid whole-head normal member identity')
        owners.add(owner);roles[role]=roles.get(role,0)+1
        if binding['render_visible']:
            row=actual.get(owner)
            if row is None or any(row.get(key)!=binding.get(key) for key in
                ('target','modifier_uid','weight_attribute','attributes','strength')) or row['reference']!=config['reference']:
                raise ValueError('Whole-head normal member differs from actual RENDER witness')
            author=binding.get('gn')
            if isinstance(author,dict) and author.get('profile')=='toon-artistic-normal.v2':
                if row.get('profile')!=author['profile'] or any(row.get(key)!=author.get(key)
                        for key in ('mapping_attribute','mapping_space')):
                    raise ValueError('Whole-head authored mapping differs from actual RENDER witness')
            elif row.get('profile')=='toon-artistic-normal.v2':
                raise ValueError('Whole-head mapped normal has no matching author mapping')
        elif owner in actual:
            raise ValueError('Whole-head visible member was marked unwitnessed')
    if config.get('roles')!=roles:
        raise ValueError('Whole-head role inventory differs')
    return len(owners)
