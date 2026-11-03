import bpy
import os
import gc
import re

# ==============================================================================
# --- MASTER PBR BAKER v3.3 (Overlap Detection, Multi-Mat UV & Fabric Sheen) ---
# ==============================================================================
SAVE_PATH = r"C:\Users\hp\OneDrive\Desktop\Portfolio_2.O\Baked_Images"

IMAGE_SIZE = 2048                # Default resolution (fast & low-RAM)
HIGH_DETAIL_IMAGE_SIZE = 4096    # Auto-used when tiled Mapping (>=4x) or high-scale procedural is detected
BAKE_MARGIN = 16                 # Pixel padding around UV islands
UV_ISLAND_MARGIN = 0.005         # Safe spacing between UV islands
CLEANUP_NODES = True             # Replaces complex nodes with clean Principled BSDF
MAX_CPU_THREADS = 8              # Leaves threads free so Windows & Blender UI never freeze

CHANNELS_TO_BAKE = [
    'BASE_COLOR', 
    'METALLIC', 
    'ROUGHNESS', 
    'TRANSMISSION',
    'NORMAL', 
    'ALPHA', 
    'SPECULAR', 
    'SHEEN_WEIGHT',
    'EMISSION'
]
# ==============================================================================

def sanitize_filename(text, max_len=45):
    clean = re.sub(r'[\\/*?:"<>|]', "_", text).strip()
    return clean[:max_len].strip()

def get_3d_view_context():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'WINDOW':
                        return {'window': window, 'screen': window.screen, 'area': area, 'region': region}
    return None

def set_viewports_to_solid():
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

def needs_high_res_4k(nt, visited=None):
    """
    Detects ultra-high-frequency procedural nodes (Scale >= 400) OR heavily tiled 
    Mapping nodes (Scale >= 4.0, like Fabric_2 on Bed_blanket in Image 7!) 
    so micro-weaves and Voronoi flakes bake at 4096x4096 without blurring!
    """
    if visited is None:
        visited = set()
    if nt in visited:
        return False
    visited.add(nt)

    for n in nt.nodes:
        if n.type in {'TEX_VORONOI', 'TEX_NOISE', 'TEX_MUSGRAVE', 'TEX_WAVE'}:
            sc = n.inputs.get('Scale')
            if sc and not sc.is_linked and abs(sc.default_value) >= 400.0:
                return True
        elif n.type == 'MAPPING':
            sc = n.inputs.get('Scale')
            if sc and not sc.is_linked and hasattr(sc.default_value, '__len__'):
                if max(abs(sc.default_value[0]), abs(sc.default_value[1]), abs(sc.default_value[2])) >= 4.0:
                    return True
        elif n.type == 'GROUP' and n.node_tree:
            if needs_high_res_4k(n.node_tree, visited):
                return True
    return False

def add_temp_group_output_socket(group_tree, sock_name="__TEMP_BAKE_SH__"):
    if hasattr(group_tree, 'interface'):
        for item in group_tree.interface.items_tree:
            if item.item_type == 'SOCKET' and item.in_out == 'OUTPUT' and item.name == sock_name:
                return sock_name
        group_tree.interface.new_socket(name=sock_name, in_out='OUTPUT', socket_type='NodeSocketShader')
    else:
        if sock_name not in group_tree.outputs:
            group_tree.outputs.new('NodeSocketShader', sock_name)
    return sock_name

def remove_temp_group_output_socket(group_tree, sock_name="__TEMP_BAKE_SH__"):
    if hasattr(group_tree, 'interface'):
        for item in list(group_tree.interface.items_tree):
            if item.item_type == 'SOCKET' and item.in_out == 'OUTPUT' and item.name == sock_name:
                group_tree.interface.remove(item)
    else:
        if sock_name in group_tree.outputs:
            group_tree.outputs.remove(group_tree.outputs[sock_name])

def optimize_raytraced_nodes(node_tree, visited=None):
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
    if visited is None:
        visited = set()
    if node_tree in visited:
        return
    visited.add(node_tree)

    nodes = node_tree.nodes
    links = node_tree.links

    for node in list(nodes):
        if node.type == 'UVMAP' and (not node.uv_map or node.uv_map == "Bake_UV"):
            node.uv_map = orig_uv_name
        elif node.type == 'NORMAL_MAP' and (not node.uv_map or node.uv_map == "Bake_UV"):
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

def has_clean_non_overlapping_uvs(mesh, uv_name):
    """
    CRITICAL FIX FOR IMAGE 1 (Table Clock Black Patches) & IMAGE 3 (Bottle Artifacts):
    Checks if the existing UV map has:
    1. Zero degenerate/collapsed faces (which cause black/stretched polygons).
    2. Consistent UV winding (no flipped/folded-over UV triangles within an island).
    3. No overlapping UV polygons across different parts of the same material!
    If ANY overlap or fold is detected, returns False so we run a fresh Smart UV Project!
    """
    uv_layer = mesh.uv_layers.get(uv_name)
    if not uv_layer or len(uv_layer.data) == 0 or len(mesh.polygons) == 0:
        return False

    poly_count = len(mesh.polygons)
    step = max(1, poly_count // 600)

    occupied_cells = {}
    degenerate_faces = 0
    sampled = 0

    for idx in range(0, poly_count, step):
        poly = mesh.polygons[idx]
        if poly.loop_total < 3:
            continue
        sampled += 1
        mat_idx = poly.material_index

        uvs = [uv_layer.data[li].uv for li in poly.loop_indices]
        for u, v in uvs:
            if u < -0.005 or u > 1.005 or v < -0.005 or v > 1.005:
                return False

        # Signed shoelace area (detects folded/inverted UV faces and zero-area faces!)
        signed_area = 0.0
        n = len(uvs)
        for i in range(n):
            j = (i + 1) % n
            signed_area += uvs[i].x * uvs[j].y - uvs[j].x * uvs[i].y
        signed_area *= 0.5

        if abs(signed_area) < 1e-7:
            degenerate_faces += 1
            continue

        # Check center point in a 100x100 spatial hash grid per material to detect overlapping UV faces
        cx = sum(p.x for p in uvs) / n
        cy = sum(p.y for p in uvs) / n
        cell_key = (mat_idx, int(cx * 100), int(cy * 100))
        if poly_count <= 2000:
            if cell_key in occupied_cells:
                # Two different polygons land on the exact same UV cell!
                prev_poly_idx = occupied_cells[cell_key]
                # Check if their centroids are nearly identical (stacked/overlapping faces)
                if prev_poly_idx != idx:
                    degenerate_faces += 1
            else:
                occupied_cells[cell_key] = idx

    if sampled == 0 or (degenerate_faces / sampled) > 0.01:
        return False
    return True

def normalize_uvs_per_material_slot(mesh, uv_name, margin=0.008):
    """
    CRITICAL FIX FOR IMAGE 9 (`Bed_blanket` Multi-Material UV Starvation):
    In Image 9, `Bed_blanket_Fabric_2` only used 25% of the texture image (the bottom-left corner)
    because `fabric_1` and `Fabric_2` were packed together into a single [0, 1] square!
    Since each material slot gets its own dedicated 2048/4096 PNG texture, each material slot's 
    UV islands can be scaled to fill 98% of the [0, 1] UV square independently—giving 4x the resolution!
    """
    uv_layer = mesh.uv_layers.get(uv_name)
    if not uv_layer or len(uv_layer.data) == 0:
        return

    # Group loop indices by material_index
    mat_loops = {}
    for poly in mesh.polygons:
        m_idx = poly.material_index
        if m_idx not in mat_loops:
            mat_loops[m_idx] = []
        mat_loops[m_idx].extend(poly.loop_indices)

    usable = 1.0 - (2.0 * margin)
    for m_idx, loop_indices in mat_loops.items():
        if not loop_indices:
            continue
        min_u = min(uv_layer.data[li].uv.x for li in loop_indices)
        max_u = max(uv_layer.data[li].uv.x for li in loop_indices)
        min_v = min(uv_layer.data[li].uv.y for li in loop_indices)
        max_v = max(uv_layer.data[li].uv.y for li in loop_indices)

        span_u = max(max_u - min_u, 1e-5)
        span_v = max(max_v - min_v, 1e-5)
        max_span = max(span_u, span_v)

        # Normalize this material's islands to fill [margin, 1 - margin]
        for li in loop_indices:
            d = uv_layer.data[li]
            d.uv.x = margin + ((d.uv.x - min_u) / max_span) * usable
            d.uv.y = margin + ((d.uv.y - min_v) / max_span) * usable

def ensure_valid_source_uv(obj):
    mesh = obj.data
    if mesh.users > 1:
        obj.data = mesh.copy()
        mesh = obj.data

    if len(mesh.uv_layers) == 1 and mesh.uv_layers[0].name == "Bake_UV":
        mesh.uv_layers[0].name = "Source_UV"

    original_uvs = [uv.name for uv in mesh.uv_layers if uv.name != "Bake_UV"]
    return original_uvs[0] if original_uvs else None

def setup_bake_uvs_smart(obj, orig_uv_name):
    """
    Sets up Bake_UV with:
    1. Temporary scale application if the object has non-uniform scale (fixes stretching on Bed_blanket!).
    2. Overlap detection (forces clean Smart UV Project on Table Clock so black patches never happen!).
    3. Per-material UV space maximization (fixes quarter-size textures on Bed_blanket!).
    """
    mesh = obj.data
    has_orig = orig_uv_name is not None and orig_uv_name in mesh.uv_layers

    clean_existing_uv = has_clean_non_overlapping_uvs(mesh, orig_uv_name) if has_orig else False

    if has_orig:
        mesh.uv_layers.active = mesh.uv_layers[orig_uv_name]

    if "Bake_UV" in mesh.uv_layers:
        mesh.uv_layers.remove(mesh.uv_layers["Bake_UV"])

    bake_uv = mesh.uv_layers.new(name="Bake_UV")
    bake_uv.active = True

    if has_orig:
        mesh.uv_layers[orig_uv_name].active_render = True

    ctx = get_3d_view_context()
    if ctx:
        with bpy.context.temp_override(**ctx):
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.uv.select_all(action='SELECT')

            # For high-poly sculpted meshes (>15k polys) that already have valid non-overlapping UVs
            # (like Bed_blanket), keep their clean seams; for low/mid-poly meshes with overlapping 
            # UVs (like Table Clock or Bottle), run Smart UV Project!
            if clean_existing_uv and len(mesh.polygons) > 15000:
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

    # Maximize 0-1 UV space per material slot (fixes Image 9 Bed_blanket quarter-size UVs!)
    normalize_uvs_per_material_slot(mesh, "Bake_UV", margin=UV_ISLAND_MARGIN)

def detect_clearcoat_glossy_mix(surface_node):
    if not surface_node or surface_node.type != 'MIX_SHADER':
        return None

    fac_sock = surface_node.inputs[0]
    s1_sock = surface_node.inputs[1]
    s2_sock = surface_node.inputs[2]

    s1 = s1_sock.links[0].from_node if s1_sock.is_linked else None
    s2 = s2_sock.links[0].from_node if s2_sock.is_linked else None

    if not s1 or not s2:
        return None

    if s1.type == 'BSDF_PRINCIPLED' and s2.type in {'BSDF_GLOSSY', 'BSDF_ANISOTROPIC'}:
        return (s1, s2, fac_sock, False)
    if s2.type == 'BSDF_PRINCIPLED' and s1.type in {'BSDF_GLOSSY', 'BSDF_ANISOTROPIC'}:
        return (s2, s1, fac_sock, True)

    return None

def prepare_normal_bake_override(nt, surface_node, temp_cleanup_records):
    """
    CRITICAL FIX FOR IMAGE 3 & 4 (`Brushed aluminium` Bottle Rectangular Artifacts):
    In Image 4, `Brushed aluminium` uses a `Mix Shader` blending two `Principled BSDF` nodes
    where ONLY the bottom `Principled BSDF` has the `Bump` node connected (`Normal` on the top 
    `Principled BSDF` is unlinked!).
    During a `NORMAL` bake, if Cycles evaluates the `Mix Shader`, the unlinked `Principled BSDF` 
    wipes out the bump map wherever the scratch/fingerprint mask is active—creating rectangular 
    flat patches on the bottle!
    This function routes the normal-mapped `Principled BSDF` directly to the output during the 
    `NORMAL` pass so 100% of the bump map bakes cleanly without mask seams!
    """
    if not surface_node:
        return

    out_node = get_active_output(nt.nodes)
    if not out_node or not out_node.inputs['Surface'].is_linked:
        return
    orig_top_from = out_node.inputs['Surface'].links[0].from_socket

    if surface_node.type == 'GROUP' and surface_node.node_tree:
        inner_nt = surface_node.node_tree
        g_out = get_active_group_output(inner_nt.nodes)
        if g_out:
            shader_in = next((s for s in g_out.inputs if s.type == 'SHADER' and s.is_linked), None)
            if shader_in:
                inner_surf = shader_in.links[0].from_node
                cc_match = detect_clearcoat_glossy_mix(inner_surf)
                if cc_match:
                    base_bsdf, glossy_bsdf, _, _ = cc_match
                    g_norm = glossy_bsdf.inputs.get('Normal')
                    p_norm = base_bsdf.inputs.get('Normal')
                    if g_norm and g_norm.is_linked and p_norm and p_norm.is_linked:
                        p_bump = p_norm.links[0].from_node
                        if p_bump.type == 'BUMP' and not p_bump.inputs['Normal'].is_linked:
                            inner_nt.links.new(g_norm.links[0].from_socket, p_bump.inputs['Normal'])
                            temp_cleanup_records.append(('REMOVE_LINK', inner_nt, g_norm.links[0].from_socket, p_bump.inputs['Normal']))
                    
                    sock_name = add_temp_group_output_socket(inner_nt, "__TEMP_BAKE_SH__")
                    temp_cleanup_records.append(('REMOVE_GROUP_SOCKET', inner_nt, sock_name, None))
                    inner_nt.links.new(base_bsdf.outputs['BSDF'], g_out.inputs[sock_name])
                    nt.links.new(surface_node.outputs[sock_name], out_node.inputs['Surface'])
                    temp_cleanup_records.append(('RESTORE_LINK', nt, orig_top_from, out_node.inputs['Surface']))
        return

    # Case 1: Clearcoat Mix (Principled + Glossy)
    cc_match = detect_clearcoat_glossy_mix(surface_node)
    if cc_match:
        base_bsdf, glossy_bsdf, _, _ = cc_match
        g_norm = glossy_bsdf.inputs.get('Normal')
        p_norm = base_bsdf.inputs.get('Normal')
        if g_norm and g_norm.is_linked and p_norm and p_norm.is_linked:
            p_bump = p_norm.links[0].from_node
            if p_bump.type == 'BUMP' and not p_bump.inputs['Normal'].is_linked:
                nt.links.new(g_norm.links[0].from_socket, p_bump.inputs['Normal'])
                temp_cleanup_records.append(('REMOVE_LINK', nt, g_norm.links[0].from_socket, p_bump.inputs['Normal']))
        nt.links.new(base_bsdf.outputs['BSDF'], out_node.inputs['Surface'])
        temp_cleanup_records.append(('RESTORE_LINK', nt, orig_top_from, out_node.inputs['Surface']))
        return

    # Case 2: Mix Shader blending two Principled BSDFs where only ONE has a Normal/Bump map (Image 3 & 4 Bottle!)
    if surface_node.type in {'MIX_SHADER', 'ADD_SHADER'}:
        s1_sock = surface_node.inputs[1] if surface_node.type == 'MIX_SHADER' else surface_node.inputs[0]
        s2_sock = surface_node.inputs[2] if surface_node.type == 'MIX_SHADER' else surface_node.inputs[1]
        s1 = s1_sock.links[0].from_node if s1_sock.is_linked else None
        s2 = s2_sock.links[0].from_node if s2_sock.is_linked else None

        s1_has_norm = s1 and s1.inputs.get('Normal') and s1.inputs['Normal'].is_linked
        s2_has_norm = s2 and s2.inputs.get('Normal') and s2.inputs['Normal'].is_linked

        if s1_has_norm and not s2_has_norm:
            nt.links.new(s1.outputs[0], out_node.inputs['Surface'])
            temp_cleanup_records.append(('RESTORE_LINK', nt, orig_top_from, out_node.inputs['Surface']))
        elif s2_has_norm and not s1_has_norm:
            nt.links.new(s2.outputs[0], out_node.inputs['Surface'])
            temp_cleanup_records.append(('RESTORE_LINK', nt, orig_top_from, out_node.inputs['Surface']))

def clamp_scalar_socket_0_to_1(nt, val_socket, make_temp_node):
    clamp_min = make_temp_node('ShaderNodeMath')
    clamp_min.operation = 'MINIMUM'
    clamp_min.use_clamp = True
    nt.links.new(val_socket, clamp_min.inputs[0])
    clamp_min.inputs[1].default_value = 1.0
    return clamp_min.outputs[0]

def extract_raw_value_socket(nt, surface_node, channel, temp_cleanup_records):
    if not surface_node:
        return None

    nodes = nt.nodes
    links = nt.links

    def make_temp_node(node_type):
        n = nodes.new(node_type)
        temp_cleanup_records.append(('REMOVE_NODE', nt, n, None))
        return n

    cc_match = detect_clearcoat_glossy_mix(surface_node)
    if cc_match:
        base_bsdf, glossy_bsdf, _, _ = cc_match
        if channel == 'ROUGHNESS':
            p_rough = get_socket(base_bsdf, ['Roughness'])
            if p_rough and p_rough.is_linked:
                clamped_p = clamp_scalar_socket_0_to_1(nt, p_rough.links[0].from_socket, make_temp_node)
                mix_r = make_temp_node('ShaderNodeMath')
                mix_r.operation = 'MULTIPLY'
                mix_r.use_clamp = True
                links.new(clamped_p, mix_r.inputs[0])
                mix_r.inputs[1].default_value = 0.65
                return mix_r.outputs[0]
        return extract_raw_value_socket(nt, base_bsdf, channel, temp_cleanup_records)

    socket_map = {
        'BASE_COLOR':   (['Base Color', 'Color'], 'RGBA', (0.8, 0.8, 0.8, 1.0)),
        'METALLIC':     (['Metallic'], 'VALUE', 0.0),
        'ROUGHNESS':    (['Roughness'], 'VALUE', 0.5),
        'TRANSMISSION': (['Transmission Weight', 'Transmission'], 'VALUE', 0.0),
        'ALPHA':        (['Alpha'], 'VALUE', 1.0),
        'SPECULAR':     (['Specular IOR Level', 'Specular'], 'VALUE', 0.5),
        'SHEEN_WEIGHT': (['Sheen Weight', 'Sheen'], 'VALUE', 0.0),
        'EMISSION':     (['Emission Color', 'Emission'], 'RGBA', (0.0, 0.0, 0.0, 1.0)),
    }

    if surface_node.type == 'BSDF_PRINCIPLED':
        sock_names, sock_type, default_val = socket_map[channel]
        sock = get_socket(surface_node, sock_names)
        if not sock:
            return None

        if channel == 'EMISSION':
            strength_sock = get_socket(surface_node, ['Emission Strength'])
            if sock.is_linked:
                col_sock = sock.links[0].from_socket
            else:
                rgb_node = make_temp_node('ShaderNodeRGB')
                rgb_node.outputs[0].default_value = sock.default_value
                col_sock = rgb_node.outputs[0]

            if strength_sock:
                if strength_sock.is_linked:
                    mul_node = make_temp_node('ShaderNodeVectorMath')
                    mul_node.operation = 'SCALE'
                    links.new(col_sock, mul_node.inputs[0])
                    links.new(strength_sock.links[0].from_socket, mul_node.inputs['Scale'])
                    return mul_node.outputs[0]
                else:
                    s_val = strength_sock.default_value
                    if abs(s_val - 1.0) > 1e-4:
                        mul_node = make_temp_node('ShaderNodeVectorMath')
                        mul_node.operation = 'SCALE'
                        links.new(col_sock, mul_node.inputs[0])
                        mul_node.inputs['Scale'].default_value = s_val
                        return mul_node.outputs[0]
            return col_sock

        if sock.is_linked:
            raw_sock = sock.links[0].from_socket
            if sock_type == 'VALUE':
                return clamp_scalar_socket_0_to_1(nt, raw_sock, make_temp_node)
            return raw_sock
        elif sock_type == 'RGBA':
            rgb_node = make_temp_node('ShaderNodeRGB')
            rgb_node.outputs[0].default_value = sock.default_value
            return rgb_node.outputs[0]
        else:
            val_node = make_temp_node('ShaderNodeValue')
            val_node.outputs[0].default_value = min(max(sock.default_value, 0.0), 1.0)
            return val_node.outputs[0]

    elif surface_node.type in {'BSDF_GLASS', 'BSDF_REFRACTION'}:
        if channel == 'BASE_COLOR':
            sock = get_socket(surface_node, ['Color'])
            if sock and sock.is_linked:
                return sock.links[0].from_socket
            rgb_node = make_temp_node('ShaderNodeRGB')
            rgb_node.outputs[0].default_value = sock.default_value if sock else (1.0, 1.0, 1.0, 1.0)
            return rgb_node.outputs[0]
        elif channel == 'ROUGHNESS':
            sock = get_socket(surface_node, ['Roughness'])
            if sock and sock.is_linked:
                return clamp_scalar_socket_0_to_1(nt, sock.links[0].from_socket, make_temp_node)
            val_node = make_temp_node('ShaderNodeValue')
            val_node.outputs[0].default_value = sock.default_value if sock else 0.0
            return val_node.outputs[0]
        elif channel in {'TRANSMISSION', 'ALPHA'}:
            val_node = make_temp_node('ShaderNodeValue')
            val_node.outputs[0].default_value = 1.0
            return val_node.outputs[0]
        else:
            val_node = make_temp_node('ShaderNodeValue')
            val_node.outputs[0].default_value = 0.0
            return val_node.outputs[0]

    elif surface_node.type in {'BSDF_GLOSSY', 'BSDF_DIFFUSE', 'BSDF_TRANSPARENT', 'BSDF_ANISOTROPIC'}:
        if channel == 'BASE_COLOR':
            sock = get_socket(surface_node, ['Color'])
            if sock and sock.is_linked:
                return sock.links[0].from_socket
            rgb_node = make_temp_node('ShaderNodeRGB')
            rgb_node.outputs[0].default_value = sock.default_value if sock else (0.8, 0.8, 0.8, 1.0)
            return rgb_node.outputs[0]
        elif channel == 'ROUGHNESS':
            sock = get_socket(surface_node, ['Roughness'])
            if sock and sock.is_linked:
                return clamp_scalar_socket_0_to_1(nt, sock.links[0].from_socket, make_temp_node)
            val_node = make_temp_node('ShaderNodeValue')
            val_node.outputs[0].default_value = sock.default_value if sock else 0.2
            return val_node.outputs[0]
        elif channel == 'METALLIC' and surface_node.type in {'BSDF_GLOSSY', 'BSDF_ANISOTROPIC'}:
            val_node = make_temp_node('ShaderNodeValue')
            val_node.outputs[0].default_value = 1.0
            return val_node.outputs[0]
        else:
            val_node = make_temp_node('ShaderNodeValue')
            val_node.outputs[0].default_value = 1.0 if channel == 'ALPHA' else 0.0
            return val_node.outputs[0]

    elif surface_node.type in {'MIX_SHADER', 'ADD_SHADER'}:
        s1_sock = surface_node.inputs[1] if surface_node.type == 'MIX_SHADER' else surface_node.inputs[0]
        s2_sock = surface_node.inputs[2] if surface_node.type == 'MIX_SHADER' else surface_node.inputs[1]

        s1 = s1_sock.links[0].from_node if s1_sock.is_linked else None
        s2 = s2_sock.links[0].from_node if s2_sock.is_linked else None

        val1 = extract_raw_value_socket(nt, s1, channel, temp_cleanup_records) if s1 else None
        val2 = extract_raw_value_socket(nt, s2, channel, temp_cleanup_records) if s2 else None

        if val1 and val2:
            mix_node = make_temp_node('ShaderNodeMix')
            is_rgba = channel in {'BASE_COLOR', 'EMISSION'}
            mix_node.data_type = 'RGBA' if is_rgba else 'FLOAT'
            mix_node.clamp_factor = True
            mix_node.clamp_result = True

            if surface_node.type == 'MIX_SHADER':
                fac_sock = surface_node.inputs[0]
                if fac_sock.is_linked:
                    links.new(fac_sock.links[0].from_socket, mix_node.inputs['Factor'])
                else:
                    mix_node.inputs['Factor'].default_value = fac_sock.default_value
            else:
                mix_node.inputs['Factor'].default_value = 0.5

            if is_rgba:
                links.new(val1, mix_node.inputs[6])
                links.new(val2, mix_node.inputs[7])
                return mix_node.outputs[2]
            else:
                links.new(val1, mix_node.inputs[2])
                links.new(val2, mix_node.inputs[3])
                return mix_node.outputs[0]

        return val1 or val2

    return None

def build_channel_emission_shader(nt, surface_node, channel, temp_cleanup_records):
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
                inner_surf_node = shader_in.links[0].from_node
                inner_emit_out = build_channel_emission_shader(inner_nt, inner_surf_node, channel, temp_cleanup_records)
                if inner_emit_out:
                    sock_name = add_temp_group_output_socket(inner_nt, "__TEMP_BAKE_SH__")
                    temp_cleanup_records.append(('REMOVE_GROUP_SOCKET', inner_nt, sock_name, None))
                    inner_nt.links.new(inner_emit_out, g_out.inputs[sock_name])
                    return surface_node.outputs[sock_name]
        return None

    val_socket = extract_raw_value_socket(nt, surface_node, channel, temp_cleanup_records)
    if val_socket is not None:
        emit_node = nodes.new('ShaderNodeEmission')
        temp_cleanup_records.append(('REMOVE_NODE', nt, emit_node, None))
        links.new(val_socket, emit_node.inputs['Color'])
        emit_node.inputs['Strength'].default_value = 1.0
        return emit_node.outputs['Emission']

    return None

def normalize_hdr_emission_float_buffer(img):
    pixels = list(img.pixels)
    if not pixels:
        return 1.0

    max_val = 0.0
    n_floats = len(pixels)
    for i in range(0, n_floats, 16):
        r, g, b = pixels[i], pixels[i+1], pixels[i+2]
        m = max(r, g, b)
        if m > max_val:
            max_val = m

    if max_val > 1.001:
        inv = 1.0 / max_val
        for i in range(0, n_floats, 4):
            pixels[i]   *= inv
            pixels[i+1] *= inv
            pixels[i+2] *= inv
        img.pixels[:] = pixels
        img.update()
        return float(max_val)

    return 1.0

def capture_all_principled_scalars(nt, visited=None):
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
        'Coat IOR': 1.5,
        'Coat Tint': (1.0, 1.0, 1.0, 1.0),
        'Sheen Weight': 0.0,
        'Sheen Roughness': 0.5,
        'Sheen Tint': (1.0, 1.0, 1.0, 1.0),
    }

    found_first_bsdf = False

    for n in nt.nodes:
        if n.type == 'GROUP' and n.node_tree:
            sub_props = capture_all_principled_scalars(n.node_tree, visited)
            for k, v in sub_props.items():
                if k in {'Coat Weight', 'Sheen Weight', 'Transmission', 'Emission Strength', 'Metallic'}:
                    props[k] = max(props.get(k, 0.0), v)
                else:
                    props[k] = v
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
            if not found_first_bsdf:
                found_first_bsdf = True
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

            met = get_socket(n, ['Metallic'])
            if met and not met.is_linked:
                props['Metallic'] = max(props['Metallic'], met.default_value)

            t_sock = get_socket(n, ['Transmission Weight', 'Transmission'])
            if t_sock and not t_sock.is_linked:
                props['Transmission'] = max(props['Transmission'], t_sock.default_value)

            ec = get_socket(n, ['Emission Color', 'Emission'])
            es = get_socket(n, ['Emission Strength'])
            if es:
                if es.is_linked:
                    props['Emission Strength'] = max(props['Emission Strength'], 1.0)
                elif es.default_value > props['Emission Strength']:
                    props['Emission Strength'] = es.default_value
                    if ec:
                        props['Emission Color'] = tuple(ec.default_value)

            cw = get_socket(n, ['Coat Weight', 'Clearcoat'])
            if cw and cw.default_value > props['Coat Weight']:
                props['Coat Weight'] = cw.default_value
                cr = get_socket(n, ['Coat Roughness', 'Clearcoat Roughness'])
                if cr:
                    props['Coat Roughness'] = cr.default_value
                cior = get_socket(n, ['Coat IOR'])
                if cior:
                    props['Coat IOR'] = cior.default_value

            sw = get_socket(n, ['Sheen Weight', 'Sheen'])
            if sw and (sw.is_linked or sw.default_value > props['Sheen Weight']):
                props['Sheen Weight'] = max(props['Sheen Weight'], sw.default_value)
                sr = get_socket(n, ['Sheen Roughness'])
                if sr:
                    props['Sheen Roughness'] = sr.default_value
                st = get_socket(n, ['Sheen Tint'])
                if st:
                    props['Sheen Tint'] = tuple(st.default_value)

        elif n.type in {'BSDF_GLASS', 'BSDF_REFRACTION'}:
            if 'IOR' in n.inputs:
                props['IOR'] = n.inputs['IOR'].default_value
            props['Transmission'] = 1.0

    return props

def should_bake_channel_recursive(nt, surface_node, channel):
    if not surface_node:
        return False

    if surface_node.type == 'GROUP' and surface_node.node_tree:
        g_out = get_active_group_output(surface_node.node_tree.nodes)
        if g_out:
            shader_in = next((s for s in g_out.inputs if s.type == 'SHADER' and s.is_linked), None)
            if shader_in:
                return should_bake_channel_recursive(surface_node.node_tree, shader_in.links[0].from_node, channel)
        return False

    cc_match = detect_clearcoat_glossy_mix(surface_node)
    if cc_match:
        base_bsdf, glossy_bsdf, _, _ = cc_match
        if channel == 'NORMAL':
            g_norm = glossy_bsdf.inputs.get('Normal')
            p_norm = base_bsdf.inputs.get('Normal')
            return (g_norm and g_norm.is_linked) or (p_norm and p_norm.is_linked)
        return should_bake_channel_recursive(nt, base_bsdf, channel)

    if surface_node.type in {'MIX_SHADER', 'ADD_SHADER'}:
        s1_sock = surface_node.inputs[1] if surface_node.type == 'MIX_SHADER' else surface_node.inputs[0]
        s2_sock = surface_node.inputs[2] if surface_node.type == 'MIX_SHADER' else surface_node.inputs[1]
        s1 = s1_sock.links[0].from_node if s1_sock.is_linked else None
        s2 = s2_sock.links[0].from_node if s2_sock.is_linked else None

        if channel in ['BASE_COLOR', 'ROUGHNESS']:
            return True
        return should_bake_channel_recursive(nt, s1, channel) or should_bake_channel_recursive(nt, s2, channel)

    if surface_node.type == 'BSDF_PRINCIPLED':
        if channel == 'BASE_COLOR':
            s = get_socket(surface_node, ['Base Color'])
            return s and s.is_linked
        if channel == 'ROUGHNESS':
            s = get_socket(surface_node, ['Roughness'])
            return s and s.is_linked
        if channel == 'NORMAL':
            s = get_socket(surface_node, ['Normal'])
            c_norm = get_socket(surface_node, ['Coat Normal', 'Clearcoat Normal'])
            return (s and s.is_linked) or (c_norm and c_norm.is_linked)
        if channel == 'METALLIC':
            s = get_socket(surface_node, ['Metallic'])
            return s and s.is_linked
        if channel == 'TRANSMISSION':
            s = get_socket(surface_node, ['Transmission Weight', 'Transmission'])
            return s and s.is_linked
        if channel == 'ALPHA':
            s = get_socket(surface_node, ['Alpha'])
            return s and s.is_linked
        if channel == 'SPECULAR':
            s = get_socket(surface_node, ['Specular IOR Level', 'Specular'])
            return s and s.is_linked
        if channel == 'SHEEN_WEIGHT':
            # CRITICAL FIX FOR IMAGE 5, 6, 7 (`Bed_blanket` Fabric_2):
            # In Image 7, the Mix node is plugged into BOTH Specular and Sheen Weight!
            s = get_socket(surface_node, ['Sheen Weight', 'Sheen'])
            return s and s.is_linked
        if channel == 'EMISSION':
            s_col = get_socket(surface_node, ['Emission Color', 'Emission'])
            s_str = get_socket(surface_node, ['Emission Strength'])
            has_str = (s_str is None) or s_str.is_linked or (s_str.default_value > 0.001)
            has_linked = (s_col and s_col.is_linked) or (s_str and s_str.is_linked)
            return has_str and has_linked
    return False

def handle_displacement_and_coat_normal(nt, mat_output, surface_node):
    for n in nt.nodes:
        if n.type == 'BSDF_PRINCIPLED':
            norm_sock = get_socket(n, ['Normal'])
            coat_norm_sock = get_socket(n, ['Coat Normal', 'Clearcoat Normal'])
            if coat_norm_sock and coat_norm_sock.is_linked and norm_sock and not norm_sock.is_linked:
                nt.links.new(coat_norm_sock.links[0].from_socket, norm_sock)
        elif n.type == 'GROUP' and n.node_tree:
            handle_displacement_and_coat_normal(n.node_tree, None, None)

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

    if height_source and surface_node and surface_node.type == 'BSDF_PRINCIPLED':
        norm_sock = surface_node.inputs.get('Normal')
        if norm_sock and not norm_sock.is_linked:
            bump_node = nt.nodes.new('ShaderNodeBump')
            bump_node.inputs['Strength'].default_value = min(max(scale_val * 5.0, 0.15), 0.8)
            bump_node.inputs['Distance'].default_value = 0.02
            nt.links.new(height_source, bump_node.inputs['Height'])
            nt.links.new(bump_node.outputs['Normal'], norm_sock)

def find_referenced_coord_objects(nt, ref_objs, visited=None):
    if visited is None:
        visited = set()
    if nt in visited:
        return
    visited.add(nt)

    for n in nt.nodes:
        if (n.type == 'TEX_COORD' or n.bl_idname == 'ShaderNodeTexCoord') and getattr(n, 'object', None):
            ref_objs.add(n.object)
        elif n.type == 'GROUP' and n.node_tree:
            find_referenced_coord_objects(n.node_tree, ref_objs, visited)

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
    orig_filter_width = scene.cycles.filter_width

    # --- 3. APPLY STANDARD COLOR MANAGEMENT & LOW-RAM CYCLES SETTINGS ---
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
    scene.cycles.filter_width = 0.01
    scene.render.threads_mode = 'FIXED'
    scene.render.threads = MAX_CPU_THREADS
    
    if hasattr(scene.render.bake, 'margin_type'):
        scene.render.bake.margin_type = 'EXTEND'

    # --- 4. ISOLATE SCENE & MAKE MATERIALS SINGLE-USER UPFRONT ---
    coord_ref_objects = set()
    for obj in selected_meshes:
        for slot in obj.material_slots:
            if slot.material and slot.material.users > 1:
                slot.material = slot.material.copy()
            if slot.material and slot.material.use_nodes:
                make_node_groups_single_user(slot.material.node_tree)
                find_referenced_coord_objects(slot.material.node_tree, coord_ref_objects)

    orig_hide_render = {o: o.hide_render for o in scene.objects}
    for o in scene.objects:
        if o not in coord_ref_objects:
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

            orig_uv_name = ensure_valid_source_uv(obj)

            # --- STEP 1: LOCK ORIGINAL UV REFERENCES ---
            if orig_uv_name:
                for slot in obj.material_slots:
                    if slot.material and slot.material.use_nodes:
                        lock_original_uv_references(slot.material.node_tree, orig_uv_name)

            # --- STEP 2: OVERLAP-SAFE & PER-MATERIAL NORMALIZED UV SETUP ---
            setup_bake_uvs_smart(obj, orig_uv_name)

            if "Bake_UV" in obj.data.uv_layers:
                obj.data.uv_layers["Bake_UV"].active = True
            if orig_uv_name and orig_uv_name in obj.data.uv_layers:
                obj.data.uv_layers[orig_uv_name].active_render = True

            # --- STEP 3: OPTIMIZE NODES, DETECT 4K NEED & CAPTURE SCALAR PROPERTIES ---
            has_ao_or_bevel = False
            use_4k_for_obj = False
            active_materials = []
            mat_scalar_props = {}

            for slot in obj.material_slots:
                mat = slot.material
                if mat and mat.use_nodes and mat not in active_materials:
                    active_materials.append(mat)
                    if optimize_raytraced_nodes(mat.node_tree):
                        has_ao_or_bevel = True
                    if needs_high_res_4k(mat.node_tree):
                        use_4k_for_obj = True
                    
                    mat_scalar_props[mat] = capture_all_principled_scalars(mat.node_tree)

                    out_node = get_active_output(mat.node_tree.nodes)
                    if out_node and out_node.inputs['Surface'].is_linked:
                        surf_node = out_node.inputs['Surface'].links[0].from_node
                        handle_displacement_and_coat_normal(mat.node_tree, out_node, surf_node)

            current_res = HIGH_DETAIL_IMAGE_SIZE if use_4k_for_obj else IMAGE_SIZE
            saved_filepaths = {mat: {} for mat in active_materials}
            detected_hdr_emission = {mat: 1.0 for mat in active_materials}

            # --- STEP 4: BAKE ACTIVE CHANNELS NON-DESTRUCTIVELY ---
            for channel in CHANNELS_TO_BAKE:
                mats_needing_channel = []
                for mat in active_materials:
                    out_node = get_active_output(mat.node_tree.nodes)
                    if out_node and out_node.inputs['Surface'].is_linked:
                        surf_node = out_node.inputs['Surface'].links[0].from_node
                        if should_bake_channel_recursive(mat.node_tree, surf_node, channel):
                            mats_needing_channel.append(mat)

                if not mats_needing_channel:
                    continue

                if channel == 'BASE_COLOR' and has_ao_or_bevel:
                    scene.cycles.samples = 4
                else:
                    scene.cycles.samples = 1

                is_data = channel not in ['BASE_COLOR', 'EMISSION']
                scene.view_settings.view_transform = 'Raw' if is_data else 'Standard'

                print(f"[{obj.name}] Baking {channel} at {current_res}x{current_res} ({scene.cycles.samples} sample)...")
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

                    short_obj = sanitize_filename(obj.name, 22)
                    internal_img_name = f"BK_{short_obj}_{mat_idx}_{channel}"
                    full_disk_filename = f"{sanitize_filename(obj.name, 60)}_{sanitize_filename(mat.name, 50)}_{channel}.png"

                    if internal_img_name in bpy.data.images:
                        bpy.data.images.remove(bpy.data.images[internal_img_name])

                    use_float = is_data or (channel == 'EMISSION')

                    img = bpy.data.images.new(
                        internal_img_name, 
                        width=current_res, 
                        height=current_res, 
                        alpha=False, 
                        float_buffer=use_float
                    )
                    img.colorspace_settings.name = 'Non-Color' if is_data else 'sRGB'

                    bake_node = nodes.new(type='ShaderNodeTexImage')
                    bake_node.image = img
                    bake_node.name = f"TEMP_BAKE_{channel}"
                    temp_cleanup_records.append(('REMOVE_NODE', nt, bake_node, None))

                    for n in nodes:
                        n.select = False
                    bake_node.select = True
                    nodes.active = bake_node

                    if channel == 'NORMAL':
                        prepare_normal_bake_override(nt, surf_node, temp_cleanup_records)
                    else:
                        emit_shader_out = build_channel_emission_shader(nt, surf_node, channel, temp_cleanup_records)
                        if emit_shader_out:
                            links.new(emit_shader_out, out_node.inputs['Surface'])
                            temp_cleanup_records.append(('RESTORE_LINK', nt, orig_surf_socket, out_node.inputs['Surface']))

                    pass_records.append((mat, img, is_data, full_disk_filename, temp_cleanup_records))

                bpy.context.view_layer.update()

                # Execute Cycles Bake
                try:
                    bake_type = 'NORMAL' if channel == 'NORMAL' else 'EMIT'
                    bpy.ops.object.bake(type=bake_type, margin=BAKE_MARGIN, use_clear=True)
                except RuntimeError as e:
                    print(f"Bake failed on {obj.name} ({channel}): {e}")

                # Save to disk AND safely restore nodes before removing image datablocks
                for mat, img, is_data, full_disk_filename, temp_cleanup_records in pass_records:
                    if mat in mats_needing_channel and img.has_data:
                        if channel == 'EMISSION':
                            detected_hdr_emission[mat] = normalize_hdr_emission_float_buffer(img)

                        filepath = os.path.join(SAVE_PATH, full_disk_filename)
                        img.filepath_raw = filepath
                        img.file_format = 'PNG'
                        scene.render.image_settings.color_depth = '16' if is_data else '8'
                        img.save()
                        saved_filepaths[mat][channel] = (filepath, is_data)

                    for action, target_nt, item1, item2 in reversed(temp_cleanup_records):
                        if action == 'RESTORE_LINK':
                            target_nt.links.new(item1, item2)
                        elif action == 'REMOVE_LINK':
                            for l in list(target_nt.links):
                                if l.from_socket == item1 and l.to_socket == item2:
                                    target_nt.links.remove(l)
                        elif action == 'REMOVE_NODE':
                            try:
                                target_nt.nodes.remove(item1)
                            except Exception:
                                pass
                        elif action == 'REMOVE_GROUP_SOCKET':
                            try:
                                remove_temp_group_output_socket(target_nt, item1)
                            except Exception:
                                pass

                    try:
                        img.buffers_free()
                        bpy.data.images.remove(img)
                    except Exception:
                        pass

                gc.collect()

            # --- STEP 5: REBUILD CLEAN PBR MATERIAL & CONNECT ALL BAKED MAPS ---
            if CLEANUP_NODES:
                for uv_name in list(obj.data.uv_layers.keys()):
                    if uv_name != "Bake_UV":
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
                    cior_sock = get_socket(bsdf, ['Coat IOR'])
                    if cior_sock:
                        cior_sock.default_value = s_props.get('Coat IOR', 1.5)
                    ctint_sock = get_socket(bsdf, ['Coat Tint'])
                    if ctint_sock:
                        ctint_sock.default_value = s_props.get('Coat Tint', (1.0, 1.0, 1.0, 1.0))

                    sw_sock = get_socket(bsdf, ['Sheen Weight', 'Sheen'])
                    if sw_sock:
                        sw_sock.default_value = s_props.get('Sheen Weight', 0.0)
                    sr_sock = get_socket(bsdf, ['Sheen Roughness'])
                    if sr_sock:
                        sr_sock.default_value = s_props.get('Sheen Roughness', 0.5)
                    st_sock = get_socket(bsdf, ['Sheen Tint'])
                    if st_sock:
                        st_sock.default_value = s_props.get('Sheen Tint', (1.0, 1.0, 1.0, 1.0))

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

                    y_pos = 350
                    for channel, (filepath, is_data) in saved_filepaths[mat].items():
                        for existing_img in list(bpy.data.images):
                            if existing_img.filepath_raw == filepath or existing_img.filepath == filepath:
                                if existing_img.users == 0:
                                    bpy.data.images.remove(existing_img)

                        loaded_img = bpy.data.images.load(filepath, check_existing=False)
                        loaded_img.colorspace_settings.name = 'Non-Color' if is_data else 'sRGB'

                        tex_node = nodes.new('ShaderNodeTexImage')
                        tex_node.image = loaded_img
                        tex_node.extension = 'EXTEND'
                        if use_4k_for_obj and channel == 'BASE_COLOR':
                            tex_node.interpolation = 'Cubic'
                        tex_node.label = f"Baked {channel}"
                        tex_node.location = (-500, y_pos)
                        links.new(uv_node.outputs['UV'], tex_node.inputs['Vector'])

                        if channel == 'BASE_COLOR':
                            links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
                        elif channel == 'METALLIC':
                            links.new(tex_node.outputs['Color'], bsdf.inputs['Metallic'])
                        elif channel == 'ROUGHNESS':
                            links.new(tex_node.outputs['Color'], bsdf.inputs['Roughness'])
                        elif channel == 'TRANSMISSION' and trans_sock:
                            links.new(tex_node.outputs['Color'], trans_sock)
                        elif channel == 'ALPHA':
                            links.new(tex_node.outputs['Color'], bsdf.inputs['Alpha'])
                        elif channel == 'SPECULAR' and spec_sock:
                            links.new(tex_node.outputs['Color'], spec_sock)
                        elif channel == 'SHEEN_WEIGHT' and sw_sock:
                            links.new(tex_node.outputs['Color'], sw_sock)
                        elif channel == 'EMISSION':
                            if emit_sock:
                                links.new(tex_node.outputs['Color'], emit_sock)
                            if str_sock:
                                str_sock.default_value = detected_hdr_emission.get(mat, 1.0)
                        elif channel == 'NORMAL':
                            norm_node = nodes.new('ShaderNodeNormalMap')
                            norm_node.uv_map = "Bake_UV"
                            norm_node.location = (-250, y_pos)
                            links.new(tex_node.outputs['Color'], norm_node.inputs['Color'])
                            links.new(norm_node.outputs['Normal'], bsdf.inputs['Normal'])

                        y_pos -= 280

            if obj not in coord_ref_objects:
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
        scene.cycles.filter_width = orig_filter_width
        scene.render.threads_mode = orig_threads_mode
        scene.render.threads = orig_threads
        scene.render.image_settings.color_depth = orig_color_depth
        if orig_margin_type and hasattr(scene.render.bake, 'margin_type'):
            scene.render.bake.margin_type = orig_margin_type

        restore_viewports(saved_viewport_shading)

    print(f"--- Master PBR Baker v4.5 Complete! Saved to: {SAVE_PATH} ---")

bake_selected_objects()
