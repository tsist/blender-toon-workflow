# SPDX-License-Identifier: GPL-3.0-or-later
"""Leaf contract helpers for explicit 0.39 line-ink dependencies.

This module deliberately does not import line_template or any backend/native
module.  It owns structural validation, target rebinding and JSON-safe witness
assembly shared by the mother template module and its protocol tests.
"""
import copy
import hashlib
import json
import math

SOURCE_SCHEMA = 'toon-line-ink-dependencies.v1'
OBSERVATION_SCHEMA = 'toon-line-ink-observation.v1'
KIND_AUTHOR = 'AUTHOR_RGB'
KIND_NODE = 'NODE_RGBA'
KIND_UV = 'UV_IMAGE'
KINDS = (KIND_AUTHOR, KIND_NODE, KIND_UV)
BINDING_FIELDS = ('nodes', 'images', 'uv_names', 'uv_transforms')
MAX_POLICIES = 256
MAX_CONSUMERS = 4096
MAX_SIDE = 2048

_IMAGE_DESCRIPTOR_FIELDS = {
    'kind', 'image', 'width', 'height', 'colorspace', 'is_float',
    'alpha_mode', 'alpha_policy', 'raw_rgba_sha256', 'linear_rgba_sha256',
    'pixel_space', 'source',
}
_INPUT_NODE_TYPES = ('ShaderNodeBsdfPrincipled', 'ShaderNodeEmission', 'ShaderNodeGroup')
_OUTPUT_NODE_TYPES = ('ShaderNodeRGB',)


def need(value, message):
    if not value:
        raise ValueError(message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def json_sha(value):
    return hashlib.sha256(_json(value).encode('utf8')).hexdigest()


def sealed(value):
    """Return a copy carrying the canonical hash of every other field."""
    result = copy.deepcopy(value)
    result.pop('sha256', None)
    result['sha256'] = json_sha(result)
    return result


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _rgb(value):
    return (type(value) is list and len(value) == 3
            and all(_finite(v) and 0 <= v <= 1 for v in value))


def _affine(value):
    return (type(value) is list and len(value) == 6
            and all(_finite(v) and abs(v) <= 65536 for v in value))


def _text(value, label, maximum=1024):
    need(type(value) is str and 0 < len(value) <= maximum, 'Invalid ' + label)


def _sha_text(value, label):
    need(type(value) is str and len(value) == 64
         and all(c in '0123456789abcdef' for c in value), 'Invalid ' + label)


def normalize_ref(ref, label):
    need(type(ref) is dict and set(ref) == {'name', 'library'}, 'Invalid ' + label)
    _text(ref['name'], label + ' name', 1024)
    need(ref['library'] is None, label + ' must be local')
    return dict(name=ref['name'], library=None)


def ref_key(ref):
    return ref['name'], ref['library'] or ''


def normalize_source_descriptor(value, *, direction=None):
    need(direction in (None, 'INPUT', 'OUTPUT'), 'Invalid NODE_RGBA source direction')
    need(type(value) is dict
         and set(value) == {'kind', 'node_type', 'socket_type'}
         and value['kind'] == KIND_NODE,
         'Invalid NODE_RGBA source descriptor')
    allowed = _INPUT_NODE_TYPES if direction == 'INPUT' else (
        _OUTPUT_NODE_TYPES if direction == 'OUTPUT' else _INPUT_NODE_TYPES + _OUTPUT_NODE_TYPES)
    need(value['node_type'] in allowed and value['socket_type'] == 'RGBA',
         'NODE_RGBA source descriptor differs from its source direction')
    return dict(value)


def normalize_image_descriptor(value):
    need(type(value) is dict and set(value) == _IMAGE_DESCRIPTOR_FIELDS
         and value['kind'] == KIND_UV,
         'Invalid UV_IMAGE source descriptor')
    _text(value['image'], 'image name')
    need(type(value['width']) is int and type(value['height']) is int
         and 1 <= value['width'] <= MAX_SIDE and 1 <= value['height'] <= MAX_SIDE,
         'Invalid UV image extent')
    need(value['colorspace'] in ('sRGB', 'Linear Rec.709', 'Non-Color')
         and type(value['is_float']) is bool
         and not (value['is_float'] and value['colorspace'] == 'sRGB'),
         'Invalid UV image color interpretation')
    need(value['source'] == 'FILE'
         and value['alpha_mode'] in ('STRAIGHT', 'CHANNEL_PACKED', 'NONE')
         and value['alpha_policy'] == 'ignored; line coverage unchanged'
         and value['pixel_space'] == 'scene_linear_Rec709',
         'Invalid UV image alpha/profile')
    _sha_text(value['raw_rgba_sha256'], 'UV raw pixel hash')
    _sha_text(value['linear_rgba_sha256'], 'UV linear pixel hash')
    return copy.deepcopy(value)


def image_descriptor_matches(source, target):
    """True when two descriptors differ only by image datablock name."""
    source = normalize_image_descriptor(source)
    target = normalize_image_descriptor(target)
    return {k: v for k, v in source.items() if k != 'image'} == {
        k: v for k, v in target.items() if k != 'image'
    }


def _consumers(value):
    need(type(value) is list and 1 <= len(value) <= MAX_CONSUMERS,
         'UV source consumers must be a non-empty local Scene Mesh reference list')
    result = []
    seen = set()
    for index, ref in enumerate(value):
        normalized = normalize_ref(ref, 'UV consumer')
        key = ref_key(normalized)
        need(key not in seen, 'Duplicate UV consumer reference')
        seen.add(key)
        result.append(normalized)
    return result


def _policy_identity(rows):
    need(type(rows) is list and len(rows) <= MAX_POLICIES,
         'Invalid material ink policy list')
    result = []
    seen = set()
    for row in rows:
        need(type(row) is dict and type(row.get('id')) is str
             and row['id'] and type(row.get('material')) is dict
             and type(row.get('source')) is dict,
             'Invalid material ink policy')
        need(row['id'] not in seen, 'Duplicate material ink policy ID')
        seen.add(row['id'])
        source = row['source']
        need(source.get('kind') in KINDS, 'Unsupported material ink source kind')
        result.append(row)
    return result


def normalize_sources(payload, policies):
    """Validate ink_sources against the ordered native material policies."""
    need(type(payload) is dict and set(payload) == {'schema', 'policies'}
         and payload['schema'] == SOURCE_SCHEMA,
         'Invalid ink source schema')
    rows = payload['policies']
    need(type(rows) is list and len(rows) == len(policies),
         'ink_sources must correspond one-for-one with material ink policies')
    material_policies = _policy_identity(policies)
    result = []
    for row, policy in zip(rows, material_policies):
        need(type(row) is dict, 'Invalid ink source row')
        kind = policy['source']['kind']
        expected = {'policy_id', 'kind'}
        if kind == KIND_NODE:
            expected |= {'source_descriptor', 'source_rgb'}
        elif kind == KIND_UV:
            expected |= {'image', 'consumers'}
        need(set(row) == expected and row['policy_id'] == policy['id']
             and row['kind'] == kind,
             'Ink source fields, policy ID or kind differ from the native policy order')
        target = dict(policy_id=row['policy_id'], kind=kind)
        if kind == KIND_NODE:
            target['source_descriptor'] = normalize_source_descriptor(
                row['source_descriptor'], direction=policy['source']['direction'])
            need(_rgb(row['source_rgb']), 'Invalid captured NODE_RGBA source RGB')
            target['source_rgb'] = list(row['source_rgb'])
        elif kind == KIND_UV:
            image = normalize_image_descriptor(row['image'])
            need(image['image'] == policy['source']['image'],
                 'UV source descriptor image differs from the native source name')
            target['image'] = image
            target['consumers'] = _consumers(row['consumers'])
        result.append(target)
    return dict(schema=SOURCE_SCHEMA, policies=result)


def _images(bindings, policies):
    maps = bindings['images']
    result = {}
    for policy in policies:
        if policy['source']['kind'] != KIND_UV:
            continue
        value = maps[policy['id']]
        need(value is not None and type(getattr(value, 'name', None)) is str
             and value.name, 'Bind a local Image for UV policy ' + policy['id'])
        result[policy['id']] = value.name
    return result


def normalize_bindings(bindings, policies):
    """Validate the exact public ink_bindings shape; Image objects stay live."""
    need(type(bindings) is dict and set(bindings) == set(BINDING_FIELDS),
         'ink_bindings must contain exactly nodes, images, uv_names and uv_transforms')
    for field in BINDING_FIELDS:
        need(type(bindings[field]) is dict, 'Invalid ink binding map: ' + field)
    policies = _policy_identity(policies)
    known = {row['id']: row for row in policies}
    for field in BINDING_FIELDS:
        unknown = set(bindings[field]) - set(known)
        if unknown:
            raise ValueError('Unknown ink binding policy ID: ' + sorted(unknown)[0])
    for policy in policies:
        pid = policy['id']
        kind = policy['source']['kind']
        if kind == KIND_AUTHOR:
            need(all(pid not in bindings[field] for field in BINDING_FIELDS),
                 'AUTHOR_RGB policy must not carry a target binding: ' + pid)
        elif kind == KIND_NODE:
            need(pid in bindings['nodes']
                 and not any(pid in bindings[field]
                             for field in ('images', 'uv_names', 'uv_transforms')),
                 'NODE_RGBA policy needs exactly one explicit node binding: ' + pid)
            node = bindings['nodes'][pid]
            need(type(node) is dict and set(node) == {'node', 'direction', 'socket'},
                 'Invalid NODE_RGBA binding fields: ' + pid)
            _text(node['node'], 'target node name')
            _text(node['socket'], 'target socket identifier')
            need(node['direction'] in ('INPUT', 'OUTPUT'),
                 'Invalid NODE_RGBA binding direction')
        else:
            need(all(pid in bindings[field]
                     for field in ('images', 'uv_names', 'uv_transforms'))
                 and pid not in bindings['nodes'],
                 'UV_IMAGE policy needs exactly one image, UV name and transform binding: ' + pid)
            _text(bindings['uv_names'][pid], 'target UV name')
            need(_affine(bindings['uv_transforms'][pid]),
                 'Target UV transform must be six bounded finite affine coefficients')
    _images(bindings, policies)
    return bindings


def target_image_names(bindings, policies):
    normalize_bindings(bindings, policies)
    return _images(bindings, policies)


def _target_source(policy, bindings, image_names):
    source = copy.deepcopy(policy['source'])
    kind = source['kind']
    if kind == KIND_NODE:
        target = bindings['nodes'][policy['id']]
        source.update(node=target['node'], direction=target['direction'],
                      socket=target['socket'])
    elif kind == KIND_UV:
        source.update(image=image_names[policy['id']],
                      uv_name=bindings['uv_names'][policy['id']],
                      uv_transform=list(bindings['uv_transforms'][policy['id']]))
    elif kind != KIND_AUTHOR:
        need(False, 'Unsupported ink source kind')
    return source


def remap_style(style, material_map, bindings, policies):
    """Remap material refs and install explicit target NODE/UV bindings.

    The native repair_names helper deliberately does not know this extension.
    Callers must pass the exact source-name -> target-ref map obtained from the
    explicit role bindings; no name lookup or fallback is performed here.
    """
    result = copy.deepcopy(style)
    payload = result['document']['request'].get('toon_line_material_colors')
    if payload is None:
        need(not policies, 'Material ink policies disappeared before remapping')
        return result
    rows = payload.get('policies')
    need(type(rows) is list and len(rows) == len(policies),
         'Material ink policy order changed before remapping')
    image_names = target_image_names(bindings, policies) if policies else {}
    for target, source_policy in zip(rows, policies):
        need(type(target) is dict and target.get('id') == source_policy['id'],
             'Material ink policy identity changed before remapping')
        source_ref = target.get('material')
        need(type(source_ref) is dict and type(source_ref.get('name')) is str,
             'Invalid material ink source reference')
        mapped = material_map.get(source_ref['name'])
        need(type(mapped) is dict and set(mapped) == {'name', 'library'}
             and mapped['library'] is None,
             'Material ink ref has no explicit local target binding: ' + source_ref['name'])
        target['material'] = dict(mapped)
        target['source'] = _target_source(source_policy, bindings, image_names)
    return result


def map_consumers(source_row, object_map):
    """Map every recorded source consumer through explicit object role bindings."""
    need(source_row['kind'] == KIND_UV, 'Only UV_IMAGE policies have consumers')
    result = []
    seen = set()
    for ref in source_row['consumers']:
        mapped = object_map.get(ref['name'])
        need(type(mapped) is dict and set(mapped) == {'name', 'library'}
             and mapped['library'] is None,
             'UV consumer has no explicit local object binding: ' + ref['name'])
        key = ref_key(mapped)
        need(key not in seen, 'Two UV consumers map to the same target object')
        seen.add(key)
        result.append(dict(mapped))
    return result


def object_ref(obj):
    return dict(name=obj.name,
                library=obj.library.filepath if obj.library else None)


def consumer_objects(scene, material):
    """All Scene Mesh objects that actually use the material on a polygon."""
    result = []
    for obj in scene.objects:
        if obj.type != 'MESH':
            continue
        slots = {index for index, slot in enumerate(obj.material_slots)
                 if slot.material == material}
        if slots and any(polygon.material_index in slots
                         for polygon in obj.data.polygons):
            result.append(obj)
    result.sort(key=lambda obj: (obj.name, obj.library.filepath if obj.library else ''))
    return result


def uv_witness(obj, uv_name):
    """Hash the current UV layer without copying or changing it."""
    import array
    mesh = obj.data
    layer = mesh.uv_layers.get(uv_name)
    need(layer is not None, 'Target UV is missing on ' + obj.name + ': ' + uv_name)
    count = len(layer.data)
    values = array.array('f', [0.0]) * (count * 2)
    if count:
        layer.data.foreach_get('uv', values)
    finite = all(math.isfinite(value) for value in values)
    digest = hashlib.sha256()
    digest.update(b'toon-line-uv-witness.v1\0')
    digest.update(layer.name.encode('utf8'))
    digest.update(b'\0')
    digest.update(str(count).encode('ascii'))
    digest.update(b'\0')
    digest.update(values.tobytes())
    return dict(uv_name=uv_name, loops=count, finite=finite, sha256=digest.hexdigest())
