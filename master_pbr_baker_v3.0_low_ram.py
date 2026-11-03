import bpy
import os
import gc

# ==============================================================================
# --- LOW-RAM / HIGH-SPEED CONFIGURATION (Optimized for i7-1255U + 16GB RAM) ---
# ==============================================================================
SAVE_PATH = r"C:\Users\hp\OneDrive\Desktop\Portfolio_2.O\Baked_Images"

# 2048 is 4x faster and uses 75% less RAM than 4096 while keeping crisp quality.
# Change to 4096 ONLY for massive hero objects if needed.
IMAGE_SIZE = 2048          
UV_ISLAND_MARGIN = 0.006   # Safe spacing between UV islands
CLEANUP_NODES = True       # Replaces complex nodes with clean Principled BSDF

# 'AUTO' preserves existing UV seams (instant) and fast-packs them without overlap
UV_STRATEGY = 'AUTO'       

# Leave Two Threads free so Windows and Blender UI never freeze or crash
MAX_CPU_THREADS = 8        

CHANNELS_TO_BAKE = [
    'BASE_COLOR', 
    'METALLIC', 
    'ROUGHNESS', 
    'NORMAL', 
    'ALPHA', 
    'SPECULAR', 
    'EMISSION'
]
# ==============================================================================

def optimize_raytraced_nodes(node_tree, visited=None):
    """
    Finds heavy Ambient Occlusion and Bevel nodes (like Image 10 & 14), 
    lowers their internal sample count from 16 to 4, and enables 'Only Local' 
    so Cycles doesn't raytrace the entire room/scene.
    """
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
        elif node.type == 'BEVEL':
            has_ray_nodes = True
            node.samples = 4
        elif node.type == 'GROUP' and node.node_tree:
            if optimize_raytraced_nodes(node.node_tree, visited):
                has_ray_nodes = True
    return has_ray_nodes

def get_active_output(nodes):
    for n in nodes:
        if n.type == 'OUTPUT_MATERIAL' and n.is_active_output:
            return n
    return next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)

def get_socket(node, names):
    for name in names:
        if name in node.inputs:
            return node.inputs[name]
    return None

def lock_original_uv_references(node_tree, orig_uv_name, visited=None):
    """Locks all UV and Normal Map nodes to the original UV layer before Bake_UV is created."""
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

def is_uv_already_packed_0_to_1(mesh, uv_name):
    """
    Fast scan: if the mesh has fewer than 50,000 loops and UVs are already within [0, 1],
    we can skip expensive UV repacking completely if desired, or fast-pack with AABB.
    """
    uv_layer = mesh.uv_layers.get(uv_name)
    if not uv_layer or len(uv_layer.data) == 0:
        return False
    # Check a stride of loops for speed on high-poly meshes
    step = max(1, len(uv_layer.data) // 2000)
    for i in range(0, len(uv_layer.data), step):
        u, v = uv_layer.data[i].uv
        if u < 0.0 or u > 1.0 or v < 0.0 or v > 1.0:
            return False
    return True

def setup_bake_uvs_fast(obj, view_3d_area, orig_uv_name):
    """Creates Bake_UV using ultra-fast AABB bounding-box packing (prevents CPU freeze on carvings)."""
    mesh = obj.data
    has_orig = orig_uv_name is not None

    if has_orig:
        mesh.uv_layers.active = mesh.uv_layers[orig_uv_name]

    if "Bake_UV" in mesh.uv_layers:
        mesh.uv_layers.remove(mesh.uv_layers["Bake_UV"])

    bake_uv = mesh.uv_layers.new(name="Bake_UV")
    bake_uv.active = True

    if has_orig:
        mesh.uv_layers[orig_uv_name].active_render = True

    if not view_3d_area:
        return

    with bpy.context.temp_override(area=view_3d_area):
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')

        if has_orig and UV_STRATEGY in {'AUTO', 'REPACK_ONLY'}:
            # Fast AABB packing with rotate=False prevents 10-minute C++ solver hangs on 100k+ poly carvings
            try:
                bpy.ops.uv.pack_islands(
                    rotate=False, 
                    scale=True, 
                    margin=UV_ISLAND_MARGIN, 
                    shape_method='AABB'
                )
            except TypeError:
                bpy.ops.uv.pack_islands(rotate=False, margin=UV_ISLAND_MARGIN)
        else:
            bpy.ops.uv.smart_project(
                angle_limit=1.15192,
                island_margin=UV_ISLAND_MARGIN,
                area_weight=1.0,
                correct_aspect=True,
                scale_to_bounds=False
            )

        bpy.ops.object.mode_set(mode='OBJECT')

def build_channel_source(nt, surface_node, channel, temp_nodes):
    nodes = nt.nodes
    links = nt.links

    socket_map = {
        'BASE_COLOR': (['Base Color'], 'RGBA', (0.8, 0.8, 0.8, 1.0)),
        'METALLIC':   (['Metallic'], 'VALUE', 0.0),
        'ROUGHNESS':  (['Roughness'], 'VALUE', 0.5),
        'ALPHA':      (['Alpha'], 'VALUE', 1.0),
        'SPECULAR':   (['Specular IOR Level', 'Specular'], 'VALUE', 0.5),
        'EMISSION':   (['Emission Color', 'Emission'], 'RGBA', (0.0, 0.0, 0.0, 1.0)),
    }

    if surface_node.type == 'BSDF_PRINCIPLED':
        sock_names, sock_type, default_val = socket_map[channel]
        sock = get_socket(surface_node, sock_names)
        if not sock:
            return None

        if channel == 'EMISSION':
            strength_sock = get_socket(surface_node, ['Emission Strength'])
            if strength_sock and not strength_sock.is_linked and strength_sock.default_value == 0.0:
                val_node = nodes.new('ShaderNodeRGB')
                temp_nodes.append(val_node)
                val_node.outputs[0].default_value = (0.0, 0.0, 0.0, 1.0)
                return val_node.outputs[0]

        if sock.is_linked:
            return sock.links[0].from_socket
        else:
            if sock_type == 'RGBA':
                rgb_node = nodes.new('ShaderNodeRGB')
                temp_nodes.append(rgb_node)
                rgb_node.outputs[0].default_value = sock.default_value
                return rgb_node.outputs[0]
            else:
                val_node = nodes.new('ShaderNodeValue')
                temp_nodes.append(val_node)
                val_node.outputs[0].default_value = sock.default_value
                return val_node.outputs[0]

    elif surface_node.type == 'MIX_SHADER':
        fac_sock = surface_node.inputs[0]
        s1_sock = surface_node.inputs[1]
        s2_sock = surface_node.inputs[2]

        s1 = s1_sock.links[0].from_node if s1_sock.is_linked else None
        s2 = s2_sock.links[0].from_node if s2_sock.is_linked else None

        out1 = build_channel_source(nt, s1, channel, temp_nodes) if s1 else None
        out2 = build_channel_source(nt, s2, channel, temp_nodes) if s2 else None

        if out1 and out2:
            mix_node = nodes.new('ShaderNodeMix')
            temp_nodes.append(mix_node)
            mix_node.data_type = 'RGBA' if channel in ['BASE_COLOR', 'EMISSION'] else 'FLOAT'
            
            if fac_sock.is_linked:
                links.new(fac_sock.links[0].from_socket, mix_node.inputs['Factor'])
            else:
                mix_node.inputs['Factor'].default_value = fac_sock.default_value

            if mix_node.data_type == 'RGBA':
                links.new(out1, mix_node.inputs[6])
                links.new(out2, mix_node.inputs[7])
                return mix_node.outputs[2]
            else:
                links.new(out1, mix_node.inputs[2])
                links.new(out2, mix_node.inputs[3])
                return mix_node.outputs[0]
        return out1 or out2

    return None

def should_bake_channel(nt, surface_node, channel):
    if channel in ['BASE_COLOR', 'ROUGHNESS', 'NORMAL']:
        return True
    if surface_node.type == 'MIX_SHADER':
        return True
    if surface_node.type == 'BSDF_PRINCIPLED':
        if channel == 'METALLIC':
            s = get_socket(surface_node, ['Metallic'])
            return s and (s.is_linked or s.default_value > 0.001)
        if channel == 'ALPHA':
            s = get_socket(surface_node, ['Alpha'])
            return s and (s.is_linked or s.default_value < 0.999)
        if channel == 'SPECULAR':
            s = get_socket(surface_node, ['Specular IOR Level', 'Specular'])
            return s and s.is_linked
        if channel == 'EMISSION':
            s_col = get_socket(surface_node, ['Emission Color', 'Emission'])
            s_str = get_socket(surface_node, ['Emission Strength'])
            has_str = (s_str is None) or s_str.is_linked or (s_str.default_value > 0.001)
            has_col = s_col and (s_col.is_linked or any(c > 0.001 for c in list(s_col.default_value)[:3]))
            return has_str and has_col
    return False

def handle_displacement(nt, mat_output, surface_node):
    if not mat_output or 'Displacement' not in mat_output.inputs:
        return
    disp_sock = mat_output.inputs['Displacement']
    if not disp_sock.is_linked:
        return

    disp_link = disp_sock.links[0]
    disp_node = disp_link.from_node
    
    height_source = None
    scale_val = 0.01
    if disp_node.type == 'DISPLACEMENT':
        if disp_node.inputs['Height'].is_linked:
            height_source = disp_node.inputs['Height'].links[0].from_socket
        scale_val = disp_node.inputs['Scale'].default_value
    else:
        height_source = disp_link.from_socket

    nt.links.remove(disp_link)

    if height_source and surface_node.type == 'BSDF_PRINCIPLED':
        norm_sock = surface_node.inputs['Normal']
        if not norm_sock.is_linked and scale_val > 0.005:
            bump_node = nt.nodes.new('ShaderNodeBump')
            bump_node.inputs['Strength'].default_value = min(max(scale_val * 5.0, 0.05), 0.5)
            bump_node.inputs['Distance'].default_value = 0.01
            nt.links.new(height_source, bump_node.inputs['Height'])
            nt.links.new(bump_node.outputs['Normal'], norm_sock)

def bake_selected_objects():
    if not os.path.exists(SAVE_PATH):
        os.makedirs(SAVE_PATH)

    scene = bpy.context.scene
    selected_meshes = [obj for obj in bpy.context.selected_objects if obj.type == 'MESH']
    if not selected_meshes:
        print("No mesh objects selected!")
        return

    # --- SAVE ORIGINAL SCENE SETTINGS ---
    orig_engine = scene.render.engine
    orig_samples = scene.cycles.samples
    orig_view_transform = scene.view_settings.view_transform
    orig_use_denoising = scene.cycles.use_denoising
    orig_use_auto_tile = scene.cycles.use_auto_tile
    orig_tile_size = scene.cycles.tile_size
    orig_max_bounces = scene.cycles.max_bounces
    orig_threads_mode = scene.render.threads_mode
    orig_threads = scene.render.threads
    orig_margin_type = getattr(scene.render.bake, 'margin_type', None)

    # --- APPLY ULTRA-FAST / LOW-RAM CYCLES SETTINGS ---
    scene.render.engine = 'CYCLES'
    scene.view_settings.view_transform = 'Standard'
    scene.cycles.device = 'CPU'              # Prevents Iris Xe shared-VRAM driver crashes
    scene.cycles.use_denoising = False       # Never run heavy OpenImageDenoise on texture bakes!
    scene.cycles.use_auto_tile = True        # Processes in 512px chunks so RAM never spikes
    scene.cycles.tile_size = 512
    scene.cycles.max_bounces = 0             # EMIT & NORMAL passes need 0 light bounces
    scene.render.threads_mode = 'FIXED'
    scene.render.threads = MAX_CPU_THREADS   # Leaves 4 logical threads free so OS stays responsive
    
    # 'EXTEND' is 50x faster than 'ADJACENT_FACES' on high-poly carved meshes
    if hasattr(scene.render.bake, 'margin_type'):
        scene.render.bake.margin_type = 'EXTEND'

    safe_pixel_margin = max(2, min(8, int((UV_ISLAND_MARGIN * IMAGE_SIZE) * 0.4)))

    # --- ISOLATE SCENE FOR ZERO-OVERHEAD BVH BUILDING ---
    # Temporarily disable render visibility on all other objects so Cycles doesn't load 
    # the entire temple/room into RAM during Ambient Occlusion raycasting!
    orig_hide_render = {o: o.hide_render for o in scene.objects}
    for o in scene.objects:
        o.hide_render = True

    # Temporarily disable Subdivision Surface modifiers during bake to prevent millions of extra polys
    disabled_subsurf_mods = []
    for obj in selected_meshes:
        for mod in obj.modifiers:
            if mod.type == 'SUBSURF' and mod.show_render:
                mod.show_render = False
                disabled_subsurf_mods.append(mod)

    view_3d_area = next((area for area in bpy.context.screen.areas if area.type == 'VIEW_3D'), None)

    try:
        for obj in selected_meshes:
            # Only make the current baking object visible to Cycles BVH
            obj.hide_render = False

            for o in bpy.context.selected_objects:
                o.select_set(False)
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj

            for slot in obj.material_slots:
                if slot.material and slot.material.users > 1:
                    slot.material = slot.material.copy()

            original_uvs = [uv.name for uv in obj.data.uv_layers if uv.name != "Bake_UV"]
            orig_uv_name = original_uvs[0] if original_uvs else None

            # --- STEP 1: LOCK ORIGINAL UV REFERENCES ---
            if orig_uv_name:
                for slot in obj.material_slots:
                    if slot.material and slot.material.use_nodes:
                        lock_original_uv_references(slot.material.node_tree, orig_uv_name)

            # --- STEP 2: FAST NON-OVERLAPPING UV SETUP ---
            print(f"[{obj.name}] Packing UVs...")
            setup_bake_uvs_fast(obj, view_3d_area, orig_uv_name)

            # --- STEP 3: OPTIMIZE NODES & DETECT RAYTRACING ---
            has_ao_or_bevel = False
            active_materials = []

            for slot in obj.material_slots:
                mat = slot.material
                if mat and mat.use_nodes and mat not in active_materials:
                    active_materials.append(mat)
                    if optimize_raytraced_nodes(mat.node_tree):
                        has_ao_or_bevel = True
                    
                    out_node = get_active_output(mat.node_tree.nodes)
                    if out_node and out_node.inputs['Surface'].is_linked:
                        surf_node = out_node.inputs['Surface'].links[0].from_node
                        handle_displacement(mat.node_tree, out_node, surf_node)

            # Store saved filepaths instead of holding giant uncompressed image buffers in RAM
            saved_filepaths = {mat: {} for mat in active_materials}

            # --- STEP 4: BAKE CHANNELS WITH IMMEDIATE RAM FLUSHING ---
            for channel in CHANNELS_TO_BAKE:
                mats_needing_channel = []
                for mat in active_materials:
                    out_node = get_active_output(mat.node_tree.nodes)
                    if out_node and out_node.inputs['Surface'].is_linked:
                        surf_node = out_node.inputs['Surface'].links[0].from_node
                        if should_bake_channel(mat.node_tree, surf_node, channel):
                            mats_needing_channel.append(mat)

                if not mats_needing_channel:
                    continue

                # ONLY use 4 samples if Base Color has AO/Bevel; 1 sample for EVERYTHING else!
                if channel == 'BASE_COLOR' and has_ao_or_bevel:
                    scene.cycles.samples = 4
                else:
                    scene.cycles.samples = 1

                print(f"[{obj.name}] Baking {channel} ({scene.cycles.samples} sample)...")
                temp_cleanup = []

                for mat in active_materials:
                    nt = mat.node_tree
                    nodes = nt.nodes
                    links = nt.links
                    out_node = get_active_output(nodes)
                    if not out_node or not out_node.inputs['Surface'].is_linked:
                        continue

                    orig_surf_socket = out_node.inputs['Surface'].links[0].from_socket
                    surf_node = orig_surf_socket.node
                    temp_nodes = []

                    img_name = f"{obj.name}_{mat.name}_{channel}"
                    if img_name in bpy.data.images:
                        bpy.data.images.remove(bpy.data.images[img_name])

                    is_data = channel not in ['BASE_COLOR', 'EMISSION']
                    # Use 8-bit buffer (float_buffer=False) to cut RAM usage by 75% (64MB vs 268MB per map)
                    img = bpy.data.images.new(img_name, width=IMAGE_SIZE, height=IMAGE_SIZE, alpha=False, float_buffer=False)
                    img.colorspace_settings.name = 'Non-Color' if is_data else 'sRGB'

                    bake_node = nodes.new(type='ShaderNodeTexImage')
                    bake_node.image = img
                    bake_node.name = f"TEMP_BAKE_{channel}"
                    temp_nodes.append(bake_node)

                    bake_uv_node = nodes.new(type='ShaderNodeUVMap')
                    bake_uv_node.uv_map = "Bake_UV"
                    temp_nodes.append(bake_uv_node)
                    links.new(bake_uv_node.outputs['UV'], bake_node.inputs['Vector'])

                    for n in nodes:
                        n.select = False
                    bake_node.select = True
                    nodes.active = bake_node

                    if channel != 'NORMAL':
                        source_socket = build_channel_source(nt, surf_node, channel, temp_nodes)
                        emit_node = nodes.new(type='ShaderNodeEmission')
                        temp_nodes.append(emit_node)
                        if source_socket:
                            links.new(source_socket, emit_node.inputs['Color'])
                        links.new(emit_node.outputs['Emission'], out_node.inputs['Surface'])

                    temp_cleanup.append((mat, temp_nodes, orig_surf_socket, img, out_node))

                # Execute Cycles Bake
                try:
                    bake_type = 'NORMAL' if channel == 'NORMAL' else 'EMIT'
                    bpy.ops.object.bake(type=bake_type, margin=safe_pixel_margin, use_clear=True)
                except RuntimeError as e:
                    print(f"Bake failed on {obj.name} ({channel}): {e}")

                # Save to disk AND immediately free RAM buffers
                for mat, temp_nodes, orig_surf_socket, img, out_node in temp_cleanup:
                    mat.node_tree.links.new(orig_surf_socket, out_node.inputs['Surface'])

                    if mat in mats_needing_channel and img.has_data:
                        filepath = os.path.join(SAVE_PATH, f"{img.name}.png")
                        img.filepath_raw = filepath
                        img.file_format = 'PNG'
                        img.save()
                        saved_filepaths[mat][channel] = (filepath, is_data)

                    # Free uncompressed C++ memory buffer & remove temporary image datablock
                    img.buffers_free()
                    bpy.data.images.remove(img)

                    for tn in temp_nodes:
                        mat.node_tree.nodes.remove(tn)

                # Force Python garbage collection between passes
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

                    old_principled = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
                    ior_val = old_principled.inputs['IOR'].default_value if (old_principled and 'IOR' in old_principled.inputs) else 1.45

                    nodes.clear()

                    out_node = nodes.new('ShaderNodeOutputMaterial')
                    out_node.location = (400, 0)

                    bsdf = nodes.new('ShaderNodeBsdfPrincipled')
                    bsdf.location = (0, 0)
                    if 'IOR' in bsdf.inputs:
                        bsdf.inputs['IOR'].default_value = ior_val
                    links.new(bsdf.outputs['BSDF'], out_node.inputs['Surface'])

                    uv_node = nodes.new('ShaderNodeUVMap')
                    uv_node.uv_map = "Bake_UV"
                    uv_node.location = (-800, 0)

                    y_pos = 300
                    for channel, (filepath, is_data) in saved_filepaths[mat].items():
                        loaded_img = bpy.data.images.load(filepath, check_existing=True)
                        loaded_img.colorspace_settings.name = 'Non-Color' if is_data else 'sRGB'

                        tex_node = nodes.new('ShaderNodeTexImage')
                        tex_node.image = loaded_img
                        tex_node.label = f"Baked {channel}"
                        tex_node.location = (-500, y_pos)
                        links.new(uv_node.outputs['UV'], tex_node.inputs['Vector'])

                        if channel == 'BASE_COLOR':
                            links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
                        elif channel == 'METALLIC':
                            links.new(tex_node.outputs['Color'], bsdf.inputs['Metallic'])
                        elif channel == 'ROUGHNESS':
                            links.new(tex_node.outputs['Color'], bsdf.inputs['Roughness'])
                        elif channel == 'ALPHA':
                            links.new(tex_node.outputs['Color'], bsdf.inputs['Alpha'])
                        elif channel == 'SPECULAR':
                            spec_sock = get_socket(bsdf, ['Specular IOR Level', 'Specular'])
                            if spec_sock:
                                links.new(tex_node.outputs['Color'], spec_sock)
                        elif channel == 'EMISSION':
                            emit_sock = get_socket(bsdf, ['Emission Color', 'Emission'])
                            if emit_sock:
                                links.new(tex_node.outputs['Color'], emit_sock)
                            str_sock = get_socket(bsdf, ['Emission Strength'])
                            if str_sock:
                                str_sock.default_value = 1.0
                        elif channel == 'NORMAL':
                            norm_node = nodes.new('ShaderNodeNormalMap')
                            norm_node.uv_map = "Bake_UV"
                            norm_node.location = (-250, y_pos)
                            links.new(tex_node.outputs['Color'], norm_node.inputs['Color'])
                            links.new(norm_node.outputs['Normal'], bsdf.inputs['Normal'])

                        y_pos -= 280

            # Hide object from render again before moving to the next selected object
            obj.hide_render = True
            gc.collect()

    finally:
        # --- ALWAYS RESTORE SCENE VISIBILITY & RENDER SETTINGS ---
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
        scene.cycles.use_denoising = orig_use_denoising
        scene.cycles.use_auto_tile = orig_use_auto_tile
        scene.cycles.tile_size = orig_tile_size
        scene.cycles.max_bounces = orig_max_bounces
        scene.render.threads_mode = orig_threads_mode
        scene.render.threads = orig_threads
        if orig_margin_type and hasattr(scene.render.bake, 'margin_type'):
            scene.render.bake.margin_type = orig_margin_type

    print(f"--- Fast Low-RAM PBR Bake Complete! Saved to: {SAVE_PATH} ---")

bake_selected_objects()
