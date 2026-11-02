import bpy
import os
import gc
import re

# ==============================================================================
# --- CRASH-PROOF LOW-RAM CONFIGURATION (v3.1 - 63-Char & Linear Roughness Fix) ---
# ==============================================================================
SAVE_PATH = r"C:\Users\hp\OneDrive\Desktop\Portfolio_2.O\Baked_Images"

IMAGE_SIZE = 2048          # 2048 for fast, low-RAM baking (use 4096 for hero assets)
UV_ISLAND_MARGIN = 0.006   # Safe spacing between UV islands
CLEANUP_NODES = True       # Replaces complex nodes with clean Principled BSDF
UV_STRATEGY = 'AUTO'       # Smart 0-1 UV check + non-overlapping pack
MAX_CPU_THREADS = 8        # Leaves threads free so Windows & Blender UI never freeze

# When True, bakes all channels; when False, keeps unlinked solid sliders as exact sliders
FORCE_BAKE_ALL_MAPS = True 

CHANNELS_TO_BAKE = [
    'BASE_COLOR', 
    'METALLIC', 
    'ROUGHNESS', 
    'TRANSMISSION',
    'NORMAL', 
    'ALPHA', 
    'SPECULAR', 
    'EMISSION'
]
# ==============================================================================

def sanitize_filename(text, max_len=45):
    """Cleans illegal path characters and caps length so filenames never truncate."""
    clean = re.sub(r'[\\/*?:"<>|]', "_", text).strip()
    return clean[:max_len].strip()

def get_3d_view_context():
    """Finds a valid Window, Area, and WINDOW Region for VIEW_3D."""
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'WINDOW':
                        return {'window': window, 'screen': window.screen, 'area': area, 'region': region}
    return None

def set_viewports_to_solid():
    """Switches all 3D Viewports to SOLID mode during baking to prevent GPU driver crashes."""
    saved_shading = []
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        saved_shading.append((space, space.shading.type))
                        if space.shading.type in {'MATERIAL', 'RENDERED'}:
                            space.shading.type = 'SOLID'
    return saved_shading

def restore_viewports(saved_shading):
    for space, shading_type in saved_shading:
        try:
            space.shading.type = shading_type
        except Exception:
            pass

def get_active_output(nodes):
    for n in nodes:
        if n.type == 'OUTPUT_MATERIAL' and getattr(n, 'is_active_output', True):
            return n
    return next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)

def get_active_group_output(nodes):
    for n in nodes:
        if n.type == 'GROUP_OUTPUT' and getattr(n, 'is_active_output', True):
            return n
    return next((n for n in nodes if n.type == 'GROUP_OUTPUT'), None)

def get_socket(node, names):
    for name in names:
        if name in node.inputs:
            return node.inputs[name]
    return None

def make_node_groups_single_user(nt, visited=None):
    if visited is None:
        visited = set()
    if nt in visited:
        return
    visited.add(nt)

    for node in nt.nodes:
        if node.type == 'GROUP' and node.node_tree:
            if node.node_tree.users > 1:
                node.node_tree = node.node_tree.copy()
            make_node_groups_single_user(node.node_tree, visited)

def optimize_raytraced_nodes(node_tree, visited=None):
    """Lowers AO/Bevel samples to 4 and enables 'Only Local' for 10x faster raytracing."""
    if visited is None:
        visited = set()
    if node_tree in visited:
        return False
    visited.add(node_tree)

    has_ray_nodes = False
    for node in node_tree.nodes:
        if node.type == 'AMBIENT_OCCLUSION':
            has_ray_nodes = True
            node.samples = 4
            node.only_local = True
            if not node.inputs['Distance'].is_linked and node.inputs['Distance'].default_value > 0.25:
                node.inputs['Distance'].default_value = 0.2
        elif node.type == 'BEVEL':
            has_ray_nodes = True
            node.samples = 4
        elif node.type == 'GROUP' and node.node_tree:
            if optimize_raytraced_nodes(node.node_tree, visited):
                has_ray_nodes = True
    return has_ray_nodes

def lock_original_uv_references(node_tree, orig_uv_name, visited=None):
    """Explicitly anchors all UV-dependent shader nodes to orig_uv_name."""
    if visited is None:
        visited = set()
    if node_tree in visited:
        return
    visited.add(node_tree)

    nodes = node_tree.nodes
    links = node_tree.links

    for node in list(nodes):
        if node.type == 'UVMAP' and not node.uv_map:
            node.uv_map = orig_uv_name
        elif node.type == 'NORMAL_MAP' and not node.uv_map:
            node.uv_map = orig_uv_name
        elif node.type == 'TEX_COORD' or node.bl_idname == 'ShaderNodeTexCoord':
            uv_out = node.outputs.get('UV')
            if uv_out and uv_out.is_linked:
                explicit_uv = nodes.new('ShaderNodeUVMap')
                explicit_uv.uv_map = orig_uv_name
                explicit_uv.location = (node.location.x, node.location.y - 150)
                for link in list(uv_out.links):
                    links.new(explicit_uv.outputs['UV'], link.to_socket)
        elif node.type == 'GROUP' and node.node_tree:
            lock_original_uv_references(node.node_tree, orig_uv_name, visited)

def is_uv_strictly_in_0_to_1(mesh, uv_name):
    uv_layer = mesh.uv_layers.get(uv_name)
    if not uv_layer or len(uv_layer.data) == 0:
        return False
    for loop_uv in uv_layer.data:
        u, v = loop_uv.uv
        if u < -0.001 or u > 1.001 or v < -0.001 or v > 1.001:
            return False
    return True

def force_normalize_uv_bounds_python(mesh, uv_name, margin=0.01):
    uv_layer = mesh.uv_layers.get(uv_name)
    if not uv_layer or len(uv_layer.data) == 0:
        return

    min_u = min(d.uv.x for d in uv_layer.data)
    max_u = max(d.uv.x for d in uv_layer.data)
    min_v = min(d.uv.y for d in uv_layer.data)
    max_v = max(d.uv.y for d in uv_layer.data)

    if min_u >= 0.0 and max_u <= 1.0 and min_v >= 0.0 and max_v <= 1.0:
        return

    span_u = max(max_u - min_u, 1e-5)
    span_v = max(max_v - min_v, 1e-5)
    max_span = max(span_u, span_v)
    usable = 1.0 - (2.0 * margin)

    for d in uv_layer.data:
        d.uv.x = margin + ((d.uv.x - min_u) / max_span) * usable
        d.uv.y = margin + ((d.uv.y - min_v) / max_span) * usable

def setup_bake_uvs_guaranteed(obj, orig_uv_name):
    mesh = obj.data
    has_orig = orig_uv_name is not None

    # Make single-user mesh if shared with other objects (like Left/Right doors or handles)
    if mesh.users > 1:
        obj.data = mesh.copy()
        mesh = obj.data

    if has_orig and orig_uv_name in mesh.uv_layers:
        mesh.uv_layers.active = mesh.uv_layers[orig_uv_name]

    if "Bake_UV" in mesh.uv_layers:
        mesh.uv_layers.remove(mesh.uv_layers["Bake_UV"])

    bake_uv = mesh.uv_layers.new(name="Bake_UV")
    bake_uv.active = True

    if has_orig and orig_uv_name in mesh.uv_layers:
        mesh.uv_layers[orig_uv_name].active_render = True

    ctx = get_3d_view_context()
    if ctx:
        poly_count = len(mesh.polygons)
        orig_in_0_1 = is_uv_strictly_in_0_to_1(mesh, orig_uv_name) if has_orig else False

        use_repack_only = (
            UV_STRATEGY == 'REPACK_ONLY' or 
            (UV_STRATEGY == 'AUTO' and poly_count > 35000 and orig_in_0_1)
        )

        with bpy.context.temp_override(**ctx):
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.uv.select_all(action='SELECT')

            if use_repack_only:
                try:
                    bpy.ops.uv.average_islands_scale()
                except Exception:
                    pass
                try:
                    bpy.ops.uv.pack_islands(rotate=False, scale=True, margin=UV_ISLAND_MARGIN, shape_method='AABB')
                except TypeError:
                    bpy.ops.uv.pack_islands(rotate=False, margin=UV_ISLAND_MARGIN)
            else:
                bpy.ops.uv.smart_project(
                    angle_limit=1.15192,
                    island_margin=UV_ISLAND_MARGIN,
                    area_weight=1.0,
                    correct_aspect=True,
                    scale_to_bounds=True
                )
                try:
                    bpy.ops.uv.pack_islands(rotate=False, scale=True, margin=UV_ISLAND_MARGIN, shape_method='AABB')
                except TypeError:
                    bpy.ops.uv.pack_islands(rotate=False, margin=UV_ISLAND_MARGIN)

            bpy.ops.object.mode_set(mode='OBJECT')

    force_normalize_uv_bounds_python(mesh, "Bake_UV", margin=UV_ISLAND_MARGIN)

def is_channel_linked_recursive(nt, surface_node, channel):
    if not surface_node:
        return False

    if surface_node.type == 'GROUP' and surface_node.node_tree:
        g_out = get_active_group_output(surface_node.node_tree.nodes)
        if g_out:
            shader_in = next((s for s in g_out.inputs if s.type == 'SHADER' and s.is_linked), None)
            if shader_in:
                return is_channel_linked_recursive(surface_node.node_tree, shader_in.links[0].from_node, channel)
        return False

    if surface_node.type in {'MIX_SHADER', 'ADD_SHADER'}:
        return True

    socket_names = {
        'BASE_COLOR':   ['Base Color', 'Color'],
        'METALLIC':     ['Metallic'],
        'ROUGHNESS':    ['Roughness'],
        'TRANSMISSION': ['Transmission Weight', 'Transmission'],
        'NORMAL':       ['Normal'],
        'ALPHA':        ['Alpha'],
        'SPECULAR':     ['Specular IOR Level', 'Specular'],
        'EMISSION':     ['Emission Color', 'Emission'],
    }.get(channel, [])

    sock = get_socket(surface_node, socket_names)
    if sock and sock.is_linked:
        return True
    if channel == 'EMISSION':
        str_sock = get_socket(surface_node, ['Emission Strength'])
        if str_sock and str_sock.is_linked:
            return True
    return False

def build_channel_emission_shader(nt, surface_node, channel, temp_cleanup_records):
    """
    Constructs a temporary Emission Shader outputting the exact linear value of `channel`.
    """
    if not surface_node:
        return None

    nodes = nt.nodes
    links = nt.links

    if surface_node.type == 'GROUP' and surface_node.node_tree:
        inner_nt = surface_node.node_tree
        g_out = get_active_group_output(inner_nt.nodes)
        if g_out:
            shader_in = next((s for s in g_out.inputs if s.type == 'SHADER' and s.is_linked), None)
            if shader_in:
                orig_inner_from = shader_in.links[0].from_socket
                inner_surf_node = orig_inner_from.node

                inner_emit_out = build_channel_emission_shader(inner_nt, inner_surf_node, channel, temp_cleanup_records)
                if inner_emit_out:
                    inner_nt.links.new(inner_emit_out, shader_in)
                    temp_cleanup_records.append(('RESTORE_LINK', inner_nt, orig_inner_from, shader_in))
                    out_idx = list(g_out.inputs).index(shader_in)
                    if out_idx < len(surface_node.outputs):
                        return surface_node.outputs[out_idx]
        return None

    def make_temp_node(node_type):
        n = nodes.new(node_type)
        temp_cleanup_records.append(('REMOVE_NODE', nt, n, None))
        return n

    socket_map = {
        'BASE_COLOR':   (['Base Color', 'Color'], 'RGBA', (0.8, 0.8, 0.8, 1.0)),
        'METALLIC':     (['Metallic'], 'VALUE', 0.0),
        'ROUGHNESS':    (['Roughness'], 'VALUE', 0.5),
        'TRANSMISSION': (['Transmission Weight', 'Transmission'], 'VALUE', 0.0),
        'ALPHA':        (['Alpha'], 'VALUE', 1.0),
        'SPECULAR':     (['Specular IOR Level', 'Specular'], 'VALUE', 0.5),
        'EMISSION':     (['Emission Color', 'Emission'], 'RGBA', (0.0, 0.0, 0.0, 1.0)),
    }

    val_socket = None
    emit_strength_socket = None
    emit_strength_val = 1.0

    if surface_node.type == 'BSDF_PRINCIPLED':
        sock_names, sock_type, default_val = socket_map[channel]
        sock = get_socket(surface_node, sock_names)
        if sock:
            if channel == 'EMISSION':
                strength_sock = get_socket(surface_node, ['Emission Strength'])
                if strength_sock:
                    if strength_sock.is_linked:
                        emit_strength_socket = strength_sock.links[0].from_socket
                    else:
                        emit_strength_val = strength_sock.default_value

            if sock.is_linked:
                val_socket = sock.links[0].from_socket
            elif sock_type == 'RGBA':
                rgb_node = make_temp_node('ShaderNodeRGB')
                rgb_node.outputs[0].default_value = sock.default_value
                val_socket = rgb_node.outputs[0]
            else:
                val_node = make_temp_node('ShaderNodeValue')
                val_node.outputs[0].default_value = sock.default_value
                val_socket = val_node.outputs[0]

    elif surface_node.type in {'BSDF_GLASS', 'BSDF_REFRACTION'}:
        if channel == 'BASE_COLOR':
            sock = get_socket(surface_node, ['Color'])
            if sock and sock.is_linked:
                val_socket = sock.links[0].from_socket
            else:
                rgb_node = make_temp_node('ShaderNodeRGB')
                rgb_node.outputs[0].default_value = sock.default_value if sock else (1.0, 1.0, 1.0, 1.0)
                val_socket = rgb_node.outputs[0]
        elif channel == 'ROUGHNESS':
            sock = get_socket(surface_node, ['Roughness'])
            if sock and sock.is_linked:
                val_socket = sock.links[0].from_socket
            else:
                val_node = make_temp_node('ShaderNodeValue')
                val_node.outputs[0].default_value = sock.default_value if sock else 0.0
                val_socket = val_node.outputs[0]
        elif channel in {'TRANSMISSION', 'ALPHA'}:
            val_node = make_temp_node('ShaderNodeValue')
            val_node.outputs[0].default_value = 1.0
            val_socket = val_node.outputs[0]
        else:
            val_node = make_temp_node('ShaderNodeValue')
            val_node.outputs[0].default_value = 0.0
            val_socket = val_node.outputs[0]

    elif surface_node.type in {'BSDF_GLOSSY', 'BSDF_DIFFUSE', 'BSDF_TRANSPARENT'}:
        if channel == 'BASE_COLOR':
            sock = get_socket(surface_node, ['Color'])
            if sock and sock.is_linked:
                val_socket = sock.links[0].from_socket
            else:
                rgb_node = make_temp_node('ShaderNodeRGB')
                rgb_node.outputs[0].default_value = sock.default_value if sock else (0.8, 0.8, 0.8, 1.0)
                val_socket = rgb_node.outputs[0]
        elif channel == 'ROUGHNESS':
            sock = get_socket(surface_node, ['Roughness'])
            if sock and sock.is_linked:
                val_socket = sock.links[0].from_socket
            else:
                val_node = make_temp_node('ShaderNodeValue')
                val_node.outputs[0].default_value = sock.default_value if sock else 0.2
                val_socket = val_node.outputs[0]
        else:
            val_node = make_temp_node('ShaderNodeValue')
            val_node.outputs[0].default_value = 1.0 if channel == 'ALPHA' else 0.0
            val_socket = val_node.outputs[0]

    elif surface_node.type in {'MIX_SHADER', 'ADD_SHADER'}:
        s1_sock = surface_node.inputs[1] if surface_node.type == 'MIX_SHADER' else surface_node.inputs[0]
        s2_sock = surface_node.inputs[2] if surface_node.type == 'MIX_SHADER' else surface_node.inputs[1]

        s1 = s1_sock.links[0].from_node if s1_sock.is_linked else None
        s2 = s2_sock.links[0].from_node if s2_sock.is_linked else None

        emit1 = build_channel_emission_shader(nt, s1, channel, temp_cleanup_records) if s1 else None
        emit2 = build_channel_emission_shader(nt, s2, channel, temp_cleanup_records) if s2 else None

        if emit1 and emit2:
            mix_sh = make_temp_node('ShaderNodeMixShader')
            if surface_node.type == 'MIX_SHADER':
                fac_sock = surface_node.inputs[0]
                if fac_sock.is_linked:
                    links.new(fac_sock.links[0].from_socket, mix_sh.inputs[0])
                else:
                    mix_sh.inputs[0].default_value = fac_sock.default_value
            else:
                mix_sh.inputs[0].default_value = 0.5
            links.new(emit1, mix_sh.inputs[1])
            links.new(emit2, mix_sh.inputs[2])
            return mix_sh.outputs[0]
        return emit1 or emit2

    if val_socket is not None:
        emit_node = make_temp_node('ShaderNodeEmission')
        links.new(val_socket, emit_node.inputs['Color'])
        if channel == 'EMISSION':
            if emit_strength_socket:
                links.new(emit_strength_socket, emit_node.inputs['Strength'])
            else:
                emit_node.inputs['Strength'].default_value = emit_strength_val
        else:
            emit_node.inputs['Strength'].default_value = 1.0
        return emit_node.outputs['Emission']

    return None

def capture_all_principled_scalars(nt, visited=None):
    """Captures all unlinked scalar and color settings from the original Principled BSDF."""
    if visited is None:
        visited = set()
    if nt in visited:
        return {}
    visited.add(nt)

    props = {
        'IOR': 1.45,
        'Transmission': 0.0,
        'Emission Strength': 0.0,
        'Base Color': (0.8, 0.8, 0.8, 1.0),
        'Metallic': 0.0,
        'Roughness': 0.5,
        'Specular': 0.5,
        'Alpha': 1.0,
        'Emission Color': (0.0, 0.0, 0.0, 1.0),
        'Coat Weight': 0.0,
        'Coat Roughness': 0.03,
        'Sheen Weight': 0.0,
        'Sheen Roughness': 0.5,
    }

    for n in nt.nodes:
        if n.type == 'GROUP' and n.node_tree:
            sub_props = capture_all_principled_scalars(n.node_tree, visited)
            props.update(sub_props)
            for inp in n.inputs:
                uname = inp.name.upper()
                if not inp.is_linked:
                    if uname == 'IOR':
                        props['IOR'] = inp.default_value
                    elif 'TRANSMISSION' in uname:
                        props['Transmission'] = max(props['Transmission'], inp.default_value)
                    elif uname in {'BASE COLOR', 'COLOR'} and inp.type == 'RGBA':
                        props['Base Color'] = tuple(inp.default_value)
                    elif uname == 'ROUGHNESS' and inp.type == 'VALUE':
                        props['Roughness'] = inp.default_value

        elif n.type == 'BSDF_PRINCIPLED':
            if 'IOR' in n.inputs:
                props['IOR'] = n.inputs['IOR'].default_value
            bc = get_socket(n, ['Base Color'])
            if bc:
                props['Base Color'] = tuple(bc.default_value)
            met = get_socket(n, ['Metallic'])
            if met:
                props['Metallic'] = met.default_value
            rough = get_socket(n, ['Roughness'])
            if rough:
                props['Roughness'] = rough.default_value
            spec = get_socket(n, ['Specular IOR Level', 'Specular'])
            if spec:
                props['Specular'] = spec.default_value
            alpha = get_socket(n, ['Alpha'])
            if alpha:
                props['Alpha'] = alpha.default_value
            t_sock = get_socket(n, ['Transmission Weight', 'Transmission'])
            if t_sock and not t_sock.is_linked:
                props['Transmission'] = max(props['Transmission'], t_sock.default_value)
            ec = get_socket(n, ['Emission Color', 'Emission'])
            if ec:
                props['Emission Color'] = tuple(ec.default_value)
            es = get_socket(n, ['Emission Strength'])
            if es:
                props['Emission Strength'] = es.default_value
            cw = get_socket(n, ['Coat Weight', 'Clearcoat'])
            if cw:
                props['Coat Weight'] = cw.default_value
            cr = get_socket(n, ['Coat Roughness', 'Clearcoat Roughness'])
            if cr:
                props['Coat Roughness'] = cr.default_value
            sw = get_socket(n, ['Sheen Weight', 'Sheen'])
            if sw:
                props['Sheen Weight'] = sw.default_value
            sr = get_socket(n, ['Sheen Roughness'])
            if sr:
                props['Sheen Roughness'] = sr.default_value

        elif n.type in {'BSDF_GLASS', 'BSDF_REFRACTION'}:
            if 'IOR' in n.inputs:
                props['IOR'] = n.inputs['IOR'].default_value
            props['Transmission'] = 1.0

    return props

def get_background_fill_color(channel, scalar_props):
    if channel == 'BASE_COLOR':
        c = scalar_props.get('Base Color', (0.8, 0.8, 0.8, 1.0))
        return (c[0], c[1], c[2], 1.0)
    elif channel == 'ROUGHNESS':
        r = scalar_props.get('Roughness', 0.5)
        return (r, r, r, 1.0)
    elif channel == 'METALLIC':
        m = scalar_props.get('Metallic', 0.0)
        return (m, m, m, 1.0)
    elif channel == 'NORMAL':
        return (0.5, 0.5, 1.0, 1.0)
    elif channel == 'EMISSION':
        ec = scalar_props.get('Emission Color', (0.0, 0.0, 0.0, 1.0))
        es = scalar_props.get('Emission Strength', 0.0)
        return (ec[0] * es, ec[1] * es, ec[2] * es, 1.0)
    elif channel == 'ALPHA':
        a = scalar_props.get('Alpha', 1.0)
        return (a, a, a, 1.0)
    elif channel == 'SPECULAR':
        s = scalar_props.get('Specular', 0.5)
        return (s, s, s, 1.0)
    return (0.0, 0.0, 0.0, 1.0)

def should_bake_channel_recursive(nt, surface_node, channel):
    if not surface_node:
        return False
    if channel in ['BASE_COLOR', 'ROUGHNESS', 'NORMAL']:
        return True

    if surface_node.type == 'GROUP' and surface_node.node_tree:
        g_out = get_active_group_output(surface_node.node_tree.nodes)
        if g_out:
            shader_in = next((s for s in g_out.inputs if s.type == 'SHADER' and s.is_linked), None)
            if shader_in:
                return should_bake_channel_recursive(surface_node.node_tree, shader_in.links[0].from_node, channel)
        return False

    if surface_node.type in {'MIX_SHADER', 'ADD_SHADER'}:
        return channel in ['METALLIC', 'ROUGHNESS', 'BASE_COLOR', 'NORMAL']

    if surface_node.type == 'BSDF_PRINCIPLED':
        if channel == 'METALLIC':
            s = get_socket(surface_node, ['Metallic'])
            return s and (s.is_linked or s.default_value > 0.001)
        if channel == 'TRANSMISSION':
            s = get_socket(surface_node, ['Transmission Weight', 'Transmission'])
            if not s or not s.is_linked:
                return False
            if s.links[0].from_node.type == 'GROUP_INPUT':
                return False
            return True
        if channel == 'ALPHA':
            s = get_socket(surface_node, ['Alpha'])
            if not s:
                return False
            if s.is_linked and s.links[0].from_node.type != 'GROUP_INPUT':
                return True
            return not s.is_linked and s.default_value < 0.999
        if channel == 'SPECULAR':
            s = get_socket(surface_node, ['Specular IOR Level', 'Specular'])
            return s and s.is_linked and s.links[0].from_node.type != 'GROUP_INPUT'
        if channel == 'EMISSION':
            s_col = get_socket(surface_node, ['Emission Color', 'Emission'])
            s_str = get_socket(surface_node, ['Emission Strength'])
            has_str = (s_str is None) or s_str.is_linked or (s_str.default_value > 0.001)
            has_col = s_col and (s_col.is_linked or any(c > 0.001 for c in list(s_col.default_value)[:3]))
            return has_str and has_col
    return False

def handle_displacement_safe(nt, mat_output, surface_node):
    if not mat_output or 'Displacement' not in mat_output.inputs:
        return
    disp_sock = mat_output.inputs['Displacement']
    if not disp_sock.is_linked:
        return

    disp_link = disp_sock.links[0]
    disp_node = disp_link.from_node
    
    height_source = None
    scale_val = 0.05
    if disp_node.type == 'DISPLACEMENT':
        if disp_node.inputs['Height'].is_linked:
            height_source = disp_node.inputs['Height'].links[0].from_socket
        scale_val = disp_node.inputs['Scale'].default_value
    else:
        height_source = disp_link.from_socket

    nt.links.remove(disp_link)

    if height_source and surface_node.type == 'BSDF_PRINCIPLED':
        norm_sock = surface_node.inputs.get('Normal')
        if norm_sock and not norm_sock.is_linked:
            bump_node = nt.nodes.new('ShaderNodeBump')
            bump_node.inputs['Strength'].default_value = min(max(scale_val * 5.0, 0.15), 0.8)
            bump_node.inputs['Distance'].default_value = 0.02
            nt.links.new(height_source, bump_node.inputs['Height'])
            nt.links.new(bump_node.outputs['Normal'], norm_sock)

    elif surface_node.type == 'GROUP' and surface_node.node_tree and disp_node == surface_node:
        inner_nt = surface_node.node_tree
        g_out = get_active_group_output(inner_nt.nodes)
        if g_out:
            for g_in_sock in g_out.inputs:
                if 'DISPLACEMENT' in g_in_sock.name.upper() and g_in_sock.is_linked:
                    inner_disp_src = g_in_sock.links[0].from_socket
                    if inner_disp_src.node.type == 'DISPLACEMENT' and inner_disp_src.node.inputs['Height'].is_linked:
                        inner_disp_src = inner_disp_src.node.inputs['Height'].links[0].from_socket
                    for in_n in inner_nt.nodes:
                        if in_n.type in {'BSDF_PRINCIPLED', 'BSDF_GLASS'} and not in_n.inputs['Normal'].is_linked:
                            bump_node = inner_nt.nodes.new('ShaderNodeBump')
                            bump_node.inputs['Strength'].default_value = 0.25
                            bump_node.inputs['Distance'].default_value = 0.02
                            inner_nt.links.new(inner_disp_src, bump_node.inputs['Height'])
                            inner_nt.links.new(bump_node.outputs['Normal'], in_n.inputs['Normal'])

def bake_selected_objects():
    if not os.path.exists(SAVE_PATH):
        os.makedirs(SAVE_PATH)

    scene = bpy.context.scene
    selected_meshes = [obj for obj in bpy.context.selected_objects if obj.type == 'MESH']
    if not selected_meshes:
        print("No mesh objects selected!")
        return

    # --- 1. SWITCH VIEWPORTS TO SOLID TO PREVENT EEVEE / GPU DRIVER CRASHES ---
    saved_viewport_shading = set_viewports_to_solid()

    # --- 2. SAVE ORIGINAL SCENE SETTINGS ---
    orig_engine = scene.render.engine
    orig_samples = scene.cycles.samples
    orig_view_transform = scene.view_settings.view_transform
    orig_look = scene.view_settings.look
    orig_exposure = scene.view_settings.exposure
    orig_gamma = scene.view_settings.gamma
    orig_use_denoising = scene.cycles.use_denoising
    orig_use_auto_tile = scene.cycles.use_auto_tile
    orig_tile_size = scene.cycles.tile_size
    orig_max_bounces = scene.cycles.max_bounces
    orig_threads_mode = scene.render.threads_mode
    orig_threads = scene.render.threads
    orig_margin_type = getattr(scene.render.bake, 'margin_type', None)
    orig_color_depth = scene.render.image_settings.color_depth

    # --- 3. APPLY EXACT LINEAR / STANDARD COLOR MANAGEMENT & LOW-RAM CYCLES SETTINGS ---
    scene.render.engine = 'CYCLES'
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.look = 'None'
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0
    scene.cycles.device = 'CPU'
    scene.cycles.use_denoising = False
    scene.cycles.use_auto_tile = True
    scene.cycles.tile_size = 512
    scene.cycles.max_bounces = 0
    scene.render.threads_mode = 'FIXED'
    scene.render.threads = MAX_CPU_THREADS
    
    if hasattr(scene.render.bake, 'margin_type'):
        scene.render.bake.margin_type = 'EXTEND'

    safe_pixel_margin = max(4, min(12, int((UV_ISLAND_MARGIN * IMAGE_SIZE) * 0.45)))

    # --- 4. ISOLATE SCENE & MAKE MATERIALS SINGLE-USER UPFRONT ---
    # CRITICAL FIX FOR BUG #3 (Almirah Body vs Doors sharing material):
    # Make every material single-user BEFORE any object modifies or cleans up nodes!
    for obj in selected_meshes:
        for slot in obj.material_slots:
            if slot.material and slot.material.users > 1:
                slot.material = slot.material.copy()
            if slot.material and slot.material.use_nodes:
                make_node_groups_single_user(slot.material.node_tree)

    orig_hide_render = {o: o.hide_render for o in scene.objects}
    for o in scene.objects:
        o.hide_render = True

    disabled_subsurf_mods = []
    for obj in selected_meshes:
        for mod in obj.modifiers:
            if mod.type == 'SUBSURF' and mod.show_render:
                mod.show_render = False
                disabled_subsurf_mods.append(mod)

    try:
        for obj in selected_meshes:
            obj.hide_render = False

            for o in bpy.context.selected_objects:
                o.select_set(False)
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj

            original_uvs = [uv.name for uv in obj.data.uv_layers if uv.name != "Bake_UV"]
            orig_uv_name = original_uvs[0] if original_uvs else None

            # --- STEP 1: LOCK ORIGINAL UV REFERENCES ---
            if orig_uv_name:
                for slot in obj.material_slots:
                    if slot.material and slot.material.use_nodes:
                        lock_original_uv_references(slot.material.node_tree, orig_uv_name)

            # --- STEP 2: GUARANTEED 0-TO-1 NON-OVERLAPPING UV SETUP ---
            setup_bake_uvs_guaranteed(obj, orig_uv_name)

            if "Bake_UV" in obj.data.uv_layers:
                obj.data.uv_layers["Bake_UV"].active = True
            if orig_uv_name and orig_uv_name in obj.data.uv_layers:
                obj.data.uv_layers[orig_uv_name].active_render = True

            # --- STEP 3: OPTIMIZE NODES & CAPTURE ALL SCALAR PROPERTIES ---
            has_ao_or_bevel = False
            active_materials = []
            mat_scalar_props = {}
            mat_linked_channels = {}

            for slot in obj.material_slots:
                mat = slot.material
                if mat and mat.use_nodes and mat not in active_materials:
                    active_materials.append(mat)
                    if optimize_raytraced_nodes(mat.node_tree):
                        has_ao_or_bevel = True
                    
                    mat_scalar_props[mat] = capture_all_principled_scalars(mat.node_tree)
                    mat_linked_channels[mat] = set()

                    out_node = get_active_output(mat.node_tree.nodes)
                    if out_node and out_node.inputs['Surface'].is_linked:
                        surf_node = out_node.inputs['Surface'].links[0].from_node
                        handle_displacement_safe(mat.node_tree, out_node, surf_node)
                        for ch in CHANNELS_TO_BAKE:
                            if is_channel_linked_recursive(mat.node_tree, surf_node, ch):
                                mat_linked_channels[mat].add(ch)

            saved_filepaths = {mat: {} for mat in active_materials}

            # --- STEP 4: BAKE CHANNELS NON-DESTRUCTIVELY ---
            for channel in CHANNELS_TO_BAKE:
                mats_needing_channel = []
                for mat in active_materials:
                    out_node = get_active_output(mat.node_tree.nodes)
                    if out_node and out_node.inputs['Surface'].is_linked:
                        surf_node = out_node.inputs['Surface'].links[0].from_node
                        if should_bake_channel_recursive(mat.node_tree, surf_node, channel):
                            if (channel in mat_linked_channels[mat]) or FORCE_BAKE_ALL_MAPS:
                                mats_needing_channel.append(mat)

                if not mats_needing_channel:
                    continue

                if channel == 'NORMAL':
                    scene.cycles.samples = 8
                elif channel == 'BASE_COLOR' and has_ao_or_bevel:
                    scene.cycles.samples = 4
                else:
                    scene.cycles.samples = 1

                print(f"[{obj.name}] Baking {channel} ({scene.cycles.samples} samples)...")
                pass_records = []

                for mat_idx, mat in enumerate(active_materials):
                    nt = mat.node_tree
                    nodes = nt.nodes
                    links = nt.links
                    out_node = get_active_output(nodes)
                    if not out_node or not out_node.inputs['Surface'].is_linked:
                        continue

                    orig_surf_socket = out_node.inputs['Surface'].links[0].from_socket
                    surf_node = orig_surf_socket.node
                    temp_cleanup_records = []

                    # CRITICAL FIX FOR BUG #2 & #3 (Blender 63-character ID name truncation!):
                    # Keep internal Blender image name under 50 chars so `_NORMAL` / `_ROUGHNESS` is NEVER cut off!
                    short_obj = sanitize_filename(obj.name, 22)
                    short_mat = sanitize_filename(mat.name, 18)
                    internal_img_name = f"BK_{short_obj}_{mat_idx}_{channel}"
                    
                    # Full descriptive filename on disk (OS supports 255+ chars)
                    full_disk_filename = f"{sanitize_filename(obj.name, 60)}_{sanitize_filename(mat.name, 50)}_{channel}.png"

                    if internal_img_name in bpy.data.images:
                        bpy.data.images.remove(bpy.data.images[internal_img_name])

                    is_data = channel not in ['BASE_COLOR', 'EMISSION']
                    # CRITICAL FIX FOR BUG #1 (Roughness & Metallic value accuracy):
                    # Always use float_buffer=True for Non-Color data passes (Roughness, Metallic, Normal)
                    # so Cycles writes linear floating-point values without sRGB gamma distortion!
                    use_float = is_data
                    bg_col = get_background_fill_color(channel, mat_scalar_props[mat])

                    img = bpy.data.images.new(
                        internal_img_name, 
                        width=IMAGE_SIZE, 
                        height=IMAGE_SIZE, 
                        alpha=False, 
                        float_buffer=use_float
                    )
                    img.colorspace_settings.name = 'Non-Color' if is_data else 'sRGB'
                    img.generated_color = bg_col

                    bake_node = nodes.new(type='ShaderNodeTexImage')
                    bake_node.image = img
                    bake_node.name = f"TEMP_BAKE_{channel}"
                    temp_cleanup_records.append(('REMOVE_NODE', nt, bake_node, None))

                    for n in nodes:
                        n.select = False
                    bake_node.select = True
                    nodes.active = bake_node

                    if channel != 'NORMAL':
                        emit_shader_out = build_channel_emission_shader(nt, surf_node, channel, temp_cleanup_records)
                        if emit_shader_out:
                            links.new(emit_shader_out, out_node.inputs['Surface'])
                            temp_cleanup_records.append(('RESTORE_LINK', nt, orig_surf_socket, out_node.inputs['Surface']))

                    pass_records.append((mat, img, is_data, full_disk_filename, temp_cleanup_records))

                # Execute Cycles Bake
                try:
                    bake_type = 'NORMAL' if channel == 'NORMAL' else 'EMIT'
                    bpy.ops.object.bake(type=bake_type, margin=safe_pixel_margin, use_clear=False)
                except RuntimeError as e:
                    print(f"Bake failed on {obj.name} ({channel}): {e}")

                # Save to disk AND safely restore nodes before removing image datablocks
                for mat, img, is_data, full_disk_filename, temp_cleanup_records in pass_records:
                    if mat in mats_needing_channel and img.has_data:
                        filepath = os.path.join(SAVE_PATH, full_disk_filename)
                        img.filepath_raw = filepath
                        img.file_format = 'PNG'
                        scene.render.image_settings.color_depth = '16' if channel == 'NORMAL' else '8'
                        img.save()
                        saved_filepaths[mat][channel] = (filepath, is_data)

                    for action, target_nt, item1, item2 in reversed(temp_cleanup_records):
                        if action == 'RESTORE_LINK':
                            target_nt.links.new(item1, item2)
                        elif action == 'REMOVE_NODE':
                            try:
                                target_nt.nodes.remove(item1)
                            except Exception:
                                pass

                    try:
                        img.buffers_free()
                        bpy.data.images.remove(img)
                    except Exception:
                        pass

                gc.collect()

            # --- STEP 5: REBUILD CLEAN PBR MATERIAL & LOAD SAVED PNGS ---
            if CLEANUP_NODES:
                for uv_name in original_uvs:
                    if uv_name in obj.data.uv_layers:
                        obj.data.uv_layers.remove(obj.data.uv_layers[uv_name])

                if "Bake_UV" in obj.data.uv_layers:
                    obj.data.uv_layers["Bake_UV"].active = True
                    obj.data.uv_layers["Bake_UV"].active_render = True

                for mat in active_materials:
                    nt = mat.node_tree
                    nodes = nt.nodes
                    links = nt.links

                    s_props = mat_scalar_props.get(mat, {})

                    nodes.clear()

                    out_node = nodes.new('ShaderNodeOutputMaterial')
                    out_node.location = (400, 0)

                    bsdf = nodes.new('ShaderNodeBsdfPrincipled')
                    bsdf.location = (0, 0)

                    # Restore ALL original scalar properties onto the new Principled BSDF
                    if 'IOR' in bsdf.inputs:
                        bsdf.inputs['IOR'].default_value = s_props.get('IOR', 1.45)
                    
                    bc_sock = get_socket(bsdf, ['Base Color'])
                    if bc_sock:
                        bc_sock.default_value = s_props.get('Base Color', (0.8, 0.8, 0.8, 1.0))

                    met_sock = get_socket(bsdf, ['Metallic'])
                    if met_sock:
                        met_sock.default_value = s_props.get('Metallic', 0.0)

                    rough_sock = get_socket(bsdf, ['Roughness'])
                    if rough_sock:
                        rough_sock.default_value = s_props.get('Roughness', 0.5)

                    spec_sock = get_socket(bsdf, ['Specular IOR Level', 'Specular'])
                    if spec_sock:
                        spec_sock.default_value = s_props.get('Specular', 0.5)

                    trans_sock = get_socket(bsdf, ['Transmission Weight', 'Transmission'])
                    if trans_sock:
                        trans_sock.default_value = s_props.get('Transmission', 0.0)

                    cw_sock = get_socket(bsdf, ['Coat Weight', 'Clearcoat'])
                    if cw_sock:
                        cw_sock.default_value = s_props.get('Coat Weight', 0.0)
                    cr_sock = get_socket(bsdf, ['Coat Roughness', 'Clearcoat Roughness'])
                    if cr_sock:
                        cr_sock.default_value = s_props.get('Coat Roughness', 0.03)

                    sw_sock = get_socket(bsdf, ['Sheen Weight', 'Sheen'])
                    if sw_sock:
                        sw_sock.default_value = s_props.get('Sheen Weight', 0.0)
                    sr_sock = get_socket(bsdf, ['Sheen Roughness'])
                    if sr_sock:
                        sr_sock.default_value = s_props.get('Sheen Roughness', 0.5)

                    emit_sock = get_socket(bsdf, ['Emission Color', 'Emission'])
                    if emit_sock:
                        emit_sock.default_value = s_props.get('Emission Color', (0.0, 0.0, 0.0, 1.0))
                    str_sock = get_socket(bsdf, ['Emission Strength'])
                    if str_sock:
                        str_sock.default_value = s_props.get('Emission Strength', 0.0)

                    links.new(bsdf.outputs['BSDF'], out_node.inputs['Surface'])

                    uv_node = nodes.new('ShaderNodeUVMap')
                    uv_node.uv_map = "Bake_UV"
                    uv_node.location = (-800, 0)

                    y_pos = 300
                    for channel, (filepath, is_data) in saved_filepaths[mat].items():
                        # Remove any stale cached image with the same filepath so Blender loads fresh from disk
                        for existing_img in list(bpy.data.images):
                            if existing_img.filepath_raw == filepath or existing_img.filepath == filepath:
                                if existing_img.users == 0:
                                    bpy.data.images.remove(existing_img)

                        loaded_img = bpy.data.images.load(filepath, check_existing=False)
                        loaded_img.colorspace_settings.name = 'Non-Color' if is_data else 'sRGB'

                        tex_node = nodes.new('ShaderNodeTexImage')
                        tex_node.image = loaded_img
                        tex_node.extension = 'EXTEND'
                        tex_node.label = f"Baked {channel}"
                        tex_node.location = (-500, y_pos)
                        links.new(uv_node.outputs['UV'], tex_node.inputs['Vector'])

                        if channel == 'BASE_COLOR':
                            links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
                        elif channel == 'METALLIC':
                            links.new(tex_node.outputs['Color'], bsdf.inputs['Metallic'])
                        elif channel == 'ROUGHNESS':
                            links.new(tex_node.outputs['Color'], bsdf.inputs['Roughness'])
                        elif channel == 'TRANSMISSION':
                            if trans_sock:
                                links.new(tex_node.outputs['Color'], trans_sock)
                        elif channel == 'ALPHA':
                            links.new(tex_node.outputs['Color'], bsdf.inputs['Alpha'])
                        elif channel == 'SPECULAR':
                            if spec_sock:
                                links.new(tex_node.outputs['Color'], spec_sock)
                        elif channel == 'EMISSION':
                            if emit_sock:
                                links.new(tex_node.outputs['Color'], emit_sock)
                            if str_sock and s_props.get('Emission Strength', 0.0) > 0.001:
                                str_sock.default_value = 1.0
                        elif channel == 'NORMAL':
                            norm_node = nodes.new('ShaderNodeNormalMap')
                            norm_node.uv_map = "Bake_UV"
                            norm_node.location = (-250, y_pos)
                            links.new(tex_node.outputs['Color'], norm_node.inputs['Color'])
                            links.new(norm_node.outputs['Normal'], bsdf.inputs['Normal'])

                        y_pos -= 280

            obj.hide_render = True
            gc.collect()

    finally:
        for o, was_hidden in orig_hide_render.items():
            if o.name in bpy.data.objects:
                o.hide_render = was_hidden

        for mod in disabled_subsurf_mods:
            mod.show_render = True

        for obj in selected_meshes:
            obj.select_set(True)

        scene.render.engine = orig_engine
        scene.cycles.samples = orig_samples
        scene.view_settings.view_transform = orig_view_transform
        scene.view_settings.look = orig_look
        scene.view_settings.exposure = orig_exposure
        scene.view_settings.gamma = orig_gamma
        scene.cycles.use_denoising = orig_use_denoising
        scene.cycles.use_auto_tile = orig_use_auto_tile
        scene.cycles.tile_size = orig_tile_size
        scene.cycles.max_bounces = orig_max_bounces
        scene.render.threads_mode = orig_threads_mode
        scene.render.threads = orig_threads
        scene.render.image_settings.color_depth = orig_color_depth
        if orig_margin_type and hasattr(scene.render.bake, 'margin_type'):
            scene.render.bake.margin_type = orig_margin_type

        restore_viewports(saved_viewport_shading)

    print(f"--- Fast Low-RAM PBR Bake Complete! Saved to: {SAVE_PATH} ---")

bake_selected_objects()
