# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixed, versioned 14-key face-light interpolation and its native GN builder.

The convex hull was baked at author time. Runtime uses only the standard
library and native Blender nodes; it never imports SciPy or an audit script.
Weights are ray/planar barycentrics: C0, piecewise differentiable, not C1.
The six upper/back keys share one authored correction in this profile.
"""
from __future__ import annotations

import hashlib
import json
import math
import numbers

SCHEMA = 'toon-spherical-key-cones.v1'
UPPER_KEY_INDICES = (8, 9, 10, 11, 12, 13)
TOP_KEY_INDEX = 4
PAIR_KEY_INDICES = (8, 11)
NATIVE_TOLERANCE_RULE = '8*float32_eps*max(1,infinity_norm(inverse))'
_FLOAT32_EPSILON = 2.**-23
_FLOAT64_EPSILON = 2.**-52
_DOCUMENT_JSON = '{"convention":"head=(sin(yaw)*cos(elev),cos(yaw)*cos(elev),sin(elev))","directions":[[0.0,1.0,0.0],[1.0,0.0,0.0],[0.0,-1.0,0.0],[-1.0,0.0,0.0],[0.0,0.0,1.0],[0.0,0.0,-1.0],[0.7094064799162225,0.49673176489215404,0.49999999999999994],[-0.7094064799162225,0.49673176489215404,0.49999999999999994],[0.43301270189221946,-0.24999999999999992,0.8660254037844385],[6.123233995736767e-17,-0.5000000000000001,0.8660254037844385],[-0.43301270189221935,-0.2500000000000002,0.8660254037844385],[0.22414386804201342,-0.12940952255126034,0.9659258262890684],[3.169619151431765e-17,-0.2588190451025208,0.9659258262890684],[-0.22414386804201336,-0.1294095225512605,0.9659258262890684]],"exact_key_policy":"Only exact equality to a stored vector returns an exact one-hot; no near-key snapping","interpolation":"nonnegative ray/planar barycentrics, normalize coefficients to sum1","inverse_matrices":[[[0.0,1.0,0.0],[1.0,0.0,0.0],[-0.0,-0.0,-1.0]],[[0.0,1.0,-0.9934635297843083],[1.0,0.0,-1.4188129598324453],[0.0,0.0,2.0000000000000004]],[[0.0,1.0,0.0],[-1.0,-0.0,-0.0],[-0.0,-0.0,-1.0]],[[0.0,1.0,-0.9934635297843083],[-1.0,-0.0,-1.4188129598324453],[0.0,0.0,2.0000000000000004]],[[-0.7002075382097097,1.0,0.0],[-0.7048145374412812,0.0,1.0],[1.4096290748825626,0.0,0.0]],[[0.7002075382097097,1.0,0.0],[0.7048145374412812,0.0,1.0],[-1.4096290748825626,-0.0,-0.0]],[[1.0,0.0,0.0],[-0.0,-1.0,-0.0],[-0.0,-0.0,-1.0]],[[1.0,0.0,-0.5000000000000002],[-0.0,-1.0,-0.2886751345948128],[0.0,0.0,1.1547005383792517]],[[1.0,-0.7166252647699247,-0.7068718947615016],[0.0,1.559893680430038,0.45030251815173916],[-0.0,-0.9006050363034784,0.8947182583075788]],[[-0.0,-1.0,-0.0],[-1.0,-0.0,-0.0],[-0.0,-0.0,-1.0]],[[-0.0,-1.0,-0.2886751345948132],[-1.0,-0.0,-0.5000000000000001],[0.0,0.0,1.1547005383792517]],[[0.5773502691896262,-1.0,-0.577350269189626],[2.309401076758503,0.0,-2.563950248511419e-16],[-2.309401076758503,-0.0,1.154700538379252]],[[-0.5773502691896253,-1.0,-0.5773502691896258],[2.3094010767585025,0.0,1.1547005383792515],[-2.3094010767585025,-0.0,1.6328623988631378e-16]],[[-1.0,-0.7166252647699245,-0.7068718947615018],[0.0,1.5598936804300376,0.4503025181517396],[-0.0,-0.9006050363034782,0.8947182583075786]],[[-2.6804266407378616,2.821466487418608,1.0],[0.6370355385308836,1.103377918962492,0.0],[2.4452279949346565,-3.4921474869959472,-0.0]],[[2.680426640737861,2.8214664874186073,1.0],[-0.637035538530884,1.103377918962491,-0.0],[-2.4452279949346556,-3.492147486995946,-0.0]],[[-2.1547005383792524,3.732050807568877,1.0],[4.4614202866016415,5.463664073637767e-16,0.0],[-2.23071014330082,-3.863703305156273,-0.0]],[[2.1547005383792497,3.7320508075688776,1.0],[2.2307101433008234,-3.8637033051562724,-0.0],[-4.461420286601642,-5.463664073637768e-16,-0.0]],[[0.6370355385308832,1.1033779189624922,-7.8250132755784e-17],[2.6804266407378607,-2.821466487418607,-0.9999999999999991],[-2.732958640589242,1.9585072094175184,1.9318516525781353]],[[-0.6370355385308839,1.1033779189624915,1.56500265511568e-16],[-2.6804266407378603,-2.8214664874186064,-0.9999999999999996],[2.732958640589243,1.9585072094175189,1.9318516525781357]],[[3.309401076758502,-1.7320508075688752,-0.9999999999999996],[-1.1547005383792501,-2.0,-2.5639502485114184e-16],[-1.9318516525781366,3.3460652149512287,1.931851652578136]],[[1.154700538379252,-2.0000000000000004,-5.127900497022838e-16],[-3.3094010767585016,-1.7320508075688763,-0.9999999999999996],[1.9318516525781344,3.34606521495123,1.9318516525781362]],[[2.1547005383792515,-3.7320508075688754,-0.9999999999999998],[4.4614202866016415,5.184711396669231e-16,0.0],[-6.393271939179777,3.3460652149512287,1.931851652578136]],[[-2.1547005383792492,-3.7320508075688763,-0.9999999999999996],[6.393271939179776,3.34606521495123,1.9318516525781357],[-4.461420286601642,-5.463664073637768e-16,-5.645133381647913e-33]]],"keys":[[0.0,0.0],[90.0,0.0],[180.0,0.0],[270.0,0.0],[0.0,90.0],[0.0,-90.0],[55.0,30.0],[305.0,30.0],[120.0,60.0],[180.0,60.0],[240.0,60.0],[120.0,75.0],[180.0,75.0],[240.0,75.0]],"numerical":{"dtype":"float64","machine_epsilon":2.220446049250313e-16,"maximum_condition_number":15.653943804555484,"maximum_inverse_residual":3.8857805861880474e-16,"negative_coefficient_tolerance":[1.4210854715202004e-14,3.437339955542662e-14,1.4210854715202004e-14,3.437339955542662e-14,2.4226871707942352e-14,2.4226871707942352e-14,1.4210854715202004e-14,2.131628207280301e-14,3.4439966036805835e-14,1.4210854715202004e-14,2.1316282072803006e-14,4.922784477141924e-14,4.922784477141923e-14,3.4439966036805835e-14,9.239745861800187e-14,9.239745861800186e-14,9.786662283699072e-14,9.786662283699069e-14,9.412300276188854e-14,9.412300276188855e-14,1.0245697296948465e-13,1.0245697296948465e-13,1.6585756848583545e-13,1.6585756848583545e-13],"tolerance_rule":"64*eps*max(1,infinity_norm(inverse))"},"schema":"toon-spherical-key-cones.v1","scipy_runtime_required":false,"triangles":[[0,1,5],[0,1,6],[0,3,5],[0,3,7],[0,4,6],[0,4,7],[1,2,5],[1,2,8],[1,6,8],[2,3,5],[2,3,10],[2,8,9],[2,9,10],[3,7,10],[4,6,11],[4,7,13],[4,11,12],[4,12,13],[6,8,11],[7,10,13],[8,9,11],[9,10,13],[9,11,12],[9,12,13]]}'


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def document():
    """Return an independent JSON-compatible copy of this profile's bake."""
    return json.loads(_DOCUMENT_JSON)


def identity():
    """Exact fixed identity shared by carrier, reader and freeze witnesses."""
    value = document()
    return dict(schema=SCHEMA,
        document_sha256=hashlib.sha256(_canonical(value).encode('utf8')).hexdigest(),
        key_count=len(value['directions']), cone_count=len(value['triangles']),
        upper_key_indices=list(UPPER_KEY_INDICES), top_key_index=TOP_KEY_INDEX,
        interpolation=value['interpolation'], native_tolerance_rule=NATIVE_TOLERANCE_RULE)


def continuity_identity():
    """Versioned packed-pair identity; the original key identity is unchanged."""
    layout=dict(role='direction_pair',domain='POINT',data_type='FLOAT_VECTOR',
        components=[dict(component='X',key_index=PAIR_KEY_INDICES[0]),
                    dict(component='Y',key_index=PAIR_KEY_INDICES[1]),
                    dict(component='Z',constant=0.0)])
    return dict(schema='toon-face-direction-continuity.v1',base_key_identity=identity(),
        layout=layout,layout_sha256=hashlib.sha256(_canonical(layout).encode('utf8')).hexdigest())


def _unit(value):
    try:
        raw = tuple(value)
    except TypeError as error:
        raise ValueError('Direction must be a finite real three-vector') from error
    if len(raw) != 3 or any(isinstance(v,bool) or not isinstance(v,numbers.Real) or not math.isfinite(v) for v in raw):
        raise ValueError('Direction must be a finite real three-vector, never bool')
    scale = max(abs(v) for v in raw)
    if scale == 0.:
        raise ValueError('Direction must be nonzero')
    scaled = tuple(v/scale for v in raw)
    length = math.hypot(*scaled)
    return tuple(v/length for v in scaled)


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def weights(value):
    """Double-precision oracle for the fixed bake; no near-key snapping."""
    try:raw=tuple(value)
    except TypeError as error:raise ValueError('Direction must be a finite real three-vector') from error
    unit = _unit(raw)
    data = document()
    result = [0.]*len(data['directions'])
    # Only the exact stored vector is a one-hot. An incoming float32 vector
    # near a key remains a genuinely different light direction.
    for index, direction in enumerate(data['directions']):
        if tuple(direction) == raw:
            result[index] = 1.
            return result
    candidates = []
    for index, inverse in enumerate(data['inverse_matrices']):
        coefficients = [_dot(row, unit) for row in inverse]
        norm = max(sum(abs(v) for v in row) for row in inverse)
        allowance = 64.*_FLOAT64_EPSILON*max(1., norm)
        if min(coefficients) >= -allowance:
            candidates.append((min(coefficients), index, coefficients))
    if not candidates:
        raise ValueError('No positive fixed key cone contains this direction')
    # The numerical reference chooses the least boundary-sensitive candidate;
    # native float32 uses a stable first hit with its stated f32 allowance.
    _, selected, coefficients = max(candidates, key=lambda row: row[0])
    positive = [max(v, 0.) for v in coefficients]
    total = sum(positive)
    if not math.isfinite(total) or total <= 0.:
        raise ValueError('Invalid fixed key coefficient normalization')
    for index, value in zip(data['triangles'][selected], positive):
        result[index] = value/total
    total = sum(result)
    return [v/total for v in result]


def contributions(value):
    """Return the two correction contributions and full oracle weights."""
    result = weights(value)
    return dict(direction_upper=sum(result[i] for i in UPPER_KEY_INDICES),
                direction_top=result[TOP_KEY_INDEX], weights=result)


def continuity_contributions(value):
    """Same exact oracle weights, with key8/key11 packed into XY and Z=0."""
    result=contributions(value)
    result['direction_pair']=[result['weights'][i] for i in PAIR_KEY_INDICES]+[0.0]
    return result


def build(group, head_direction, *, node_key='toon_face_threshold_node',continuity=False):
    """Build uniform native weight sockets from a live normalized head vector.

    The caller owns the group, admission predicates, output stores and cleanup.
    This function adds only Shader Math/Vector Math nodes, tagged by semantic
    role. ``found`` must join the caller's validity chain. No Python callback,
    Object ID default, geometry position or new modifier is introduced here.
    """
    if type(continuity) is not bool:
        raise ValueError('continuity must be an explicit boolean')
    data = document()

    def socket(values, identifier):
        matches = [v for v in values if v.identifier == identifier]
        if len(matches) != 1:
            raise ValueError('Expected exactly one native socket: '+identifier)
        return matches[0]

    def node(key, kind, **properties):
        result = group.nodes.new(kind)
        result[node_key] = 'direction_'+key
        result.label = 'direction_'+key
        for name, value in properties.items():
            setattr(result, name, value)
        return result

    def assign(value, target):
        if isinstance(value, (int, float)):
            target.default_value = float(value)
        elif isinstance(value, (tuple, list)):
            target.default_value = tuple(float(v) for v in value)
        else:
            group.links.new(value, target)

    def scalar(key, operation, a, b=0.):
        result = node(key, 'ShaderNodeMath', operation=operation)
        assign(a, result.inputs[0]); assign(b, result.inputs[1])
        return socket(result.outputs, 'Value')

    def dot(key, row):
        result = node(key, 'ShaderNodeVectorMath', operation='DOT_PRODUCT')
        assign(row, result.inputs[0]); assign(head_direction, result.inputs[1])
        return socket(result.outputs, 'Value')

    found = upper_total = top_total = 0.
    pair_totals=[0.,0.]
    for index, (triangle, inverse) in enumerate(zip(data['triangles'], data['inverse_matrices'])):
        key = 'cone_'+str(index)
        coefficients = [dot(key+'_coefficient_'+str(i), row) for i, row in enumerate(inverse)]
        minimum = scalar(key+'_minimum01', 'MINIMUM', coefficients[0], coefficients[1])
        minimum = scalar(key+'_minimum012', 'MINIMUM', minimum, coefficients[2])
        norm = max(sum(abs(v) for v in row) for row in inverse)
        allowance = 8.*_FLOAT32_EPSILON*max(1., norm)
        inside = scalar(key+'_inside', 'SUBTRACT', 1.,
            scalar(key+'_outside', 'LESS_THAN', minimum, -allowance))
        selected = scalar(key+'_first_hit', 'MULTIPLY', inside,
            scalar(key+'_not_previous', 'SUBTRACT', 1., found))
        found = scalar(key+'_any_hit', 'MAXIMUM', found, inside)
        positive = [scalar(key+'_positive_'+str(i), 'MAXIMUM', v, 0.)
                    for i, v in enumerate(coefficients)]
        denominator = scalar(key+'_sum01', 'ADD', positive[0], positive[1])
        denominator = scalar(key+'_sum012', 'ADD', denominator, positive[2])
        safe_denominator = scalar(key+'_safe_denominator', 'MAXIMUM', denominator, 1.e-20)
        upper_indices = [i for i, v in enumerate(triangle) if v in UPPER_KEY_INDICES]
        if not upper_indices:
            upper_weight = 0.
        elif len(upper_indices) == 3:
            upper_weight = 1.
        else:
            numerator = 0.
            for i in upper_indices:
                numerator = scalar(key+'_upper_numerator_'+str(i), 'ADD', numerator, positive[i])
            upper_weight = scalar(key+'_normalized_upper', 'DIVIDE', numerator, safe_denominator)
        upper_total = scalar(key+'_accumulated_upper', 'ADD', upper_total,
            scalar(key+'_selected_upper', 'MULTIPLY', selected, upper_weight))
        if TOP_KEY_INDEX in triangle:
            top_weight = scalar(key+'_normalized_top', 'DIVIDE', positive[triangle.index(TOP_KEY_INDEX)], safe_denominator)
            top_total = scalar(key+'_accumulated_top', 'ADD', top_total,
                scalar(key+'_selected_top', 'MULTIPLY', selected, top_weight))
        if continuity:
            for pair_index,key_index in enumerate(PAIR_KEY_INDICES):
                if key_index not in triangle:continue
                pair_weight=scalar(key+'_normalized_pair_'+str(key_index),'DIVIDE',
                    positive[triangle.index(key_index)],safe_denominator)
                pair_totals[pair_index]=scalar(key+'_accumulated_pair_'+str(key_index),'ADD',
                    pair_totals[pair_index],scalar(key+'_selected_pair_'+str(key_index),
                    'MULTIPLY',selected,pair_weight))
    upper_total = scalar('upper_low', 'MAXIMUM', upper_total, 0.)
    upper_total = scalar('upper_high', 'MINIMUM', upper_total, 1.)
    # Same f32 arithmetic as the qualified probe; leave joint renormalization
    # of U+V to the reader, so this carrier exposes the actual key weights.
    result=dict(direction_upper=upper_total, direction_top=top_total, found=found)
    if continuity:
        packed=node('pair_components','ShaderNodeCombineXYZ')
        for axis,value in zip(('X','Y'),pair_totals):assign(value,socket(packed.inputs,axis))
        socket(packed.inputs,'Z').default_value=0.
        result['direction_pair']=socket(packed.outputs,'Vector')
    return result
