import bpy
import os

# ==============================================================================
# --- MASTER CONFIGURATION ---
# ==============================================================================
SAVE_PATH = r"C:\Users\hp\OneDrive\Desktop\Portfolio_2.O\Baked_Images"
IMAGE_SIZE = 4096          # Use 4096 to preserve micro-details from tiled textures (3x-6x scale)
BAKE_MARGIN = 16           # Pixel padding around UV islands
UV_ISLAND_MARGIN = 0.005   # Tight margin maximizes texture area (0.03 wastes 30%+ UV space!)
APPLY_OBJECT_SCALE = True  # Crucial: Prevents stretched/distorted UV islands
CLEANUP_NODES = True       # Replaces complex nodes with a clean, game-ready Principled BSDF

# Channels to automatically detect and bake if used by the material
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

def needs_high_samples(node_tree):
    """Detects raytraced nodes (Bevel, Ambient Occlusion) that require multiple samples."""
    for node in node_tree.nodes:
        if node.type in {'BEVEL', 'AMBIENT_OCCLUSION'}:
            return True
        if node.type == 'GROUP' and node.node_tree:
            if needs_high_samples(node.node_tree):
                return True
    return False

def get_active_output(nodes):
    """Finds the active Material Output node."""
    for n in nodes:
        if n.type == 'OUTPUT_MATERIAL' and n.is_active_output:
            return n
    return next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)

def get_socket(node, names):
    """Safely gets an input socket across Blender 3.x and 4.x naming conventions."""
    for name in names:
        if name in node.inputs:
            return node.inputs[name]
    return None

def build_channel_source(nt, surface_node, channel, temp_nodes):
    """
    Recursively extracts the exact visual value or link for any channel, 
    even if the material uses a Mix Shader combining two Principled BSDFs (e.g., Image 8).
    """
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

        # Special handling for Emission Strength multiplier
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
                links.new(out1, mix_node.inputs[6]) # Socket A (RGBA)
                links.new(out2, mix_node.inputs[7]) # Socket B (RGBA)
                return mix_node.outputs[2]          # Result (RGBA)
            else:
                links.new(out1, mix_node.inputs[2]) # Socket A (Float)
                links.new(out2, mix_node.inputs[3]) # Socket B (Float)
                return mix_node.outputs[0]          # Result (Float)
        return out1 or out2

    return None

def should_bake_channel(nt, surface_node, channel):
    """Checks if a channel is actively driven by nodes or differs from default Principled values."""
    if channel in ['BASE_COLOR', 'ROUGHNESS', 'NORMAL']:
        return True # Core PBR trio always baked

    if surface_node.type == 'MIX_SHADER':
        return True # Mix shaders combine properties dynamically

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

def convert_displacement_to_bump(nt, mat_output, surface_node):
    """
    If a material uses a Displacement texture (like Image 3), disconnects it from 
    Material Output (which breaks normal baking) and converts it into a Bump node 
    chained into the Principled BSDF Normal input so all height detail bakes into the Normal Map.
    """
    if not mat_output or 'Displacement' not in mat_output.inputs:
        return
    disp_sock = mat_output.inputs['Displacement']
    if not disp_sock.is_linked:
        return

    disp_link = disp_sock.links[0]
    disp_node = disp_link.from_node
    
    height_source = None
    scale_val = 0.1
    if disp_node.type == 'DISPLACEMENT':
        if disp_node.inputs['Height'].is_linked:
            height_source = disp_node.inputs['Height'].links[0].from_socket
        scale_val = disp_node.inputs['Scale'].default_value
    else:
        height_source = disp_link.from_socket

    # Disconnect Displacement from Material Output so it doesn't skew Cycles raycasting
    nt.links.remove(disp_link)

    if height_source and surface_node.type == 'BSDF_PRINCIPLED':
        norm_sock = surface_node.inputs['Normal']
        bump_node = nt.nodes.new('ShaderNodeBump')
        bump_node.inputs['Strength'].default_value = min(max(scale_val * 10.0, 0.1), 1.0)
        bump_node.inputs['Distance'].default_value = 0.02
        nt.links.new(height_source, bump_node.inputs['Height'])

        if norm_sock.is_linked:
            existing_norm = norm_sock.links[0].from_socket
            nt.links.new(existing_norm, bump_node.inputs['Normal'])
        nt.links.new(bump_node.outputs['Normal'], norm_sock)

def bake_selected_objects():
    if not os.path.exists(SAVE_PATH):
        os.makedirs(SAVE_PATH)

    scene = bpy.context.scene
    
    # Save original render settings
    orig_engine = scene.render.engine
    orig_samples = scene.cycles.samples
    orig_view_transform = scene.view_settings.view_transform
    
    scene.render.engine = 'CYCLES'
    # Set Color Management to Standard so saved PNGs aren't washed out by AgX/Filmic tone mapping
    scene.view_settings.view_transform = 'Standard'

    selected_meshes = [obj for obj in bpy.context.selected_objects if obj.type == 'MESH']
    if not selected_meshes:
        print("No mesh objects selected!")
        return

    view_3d_area = next((area for area in bpy.context.screen.areas if area.type == 'VIEW_3D'), None)

    for obj in selected_meshes:
        for o in bpy.context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj

        # Ensure materials are unique to this object if we are cleaning up nodes
        if CLEANUP_NODES:
            for slot in obj.material_slots:
                if slot.material and slot.material.users > 1:
                    slot.material = slot.material.copy()

        # --- STEP 1: APPLY SCALE & PREPARE HIGH-DENSITY UV MAP ---
        if APPLY_OBJECT_SCALE:
            bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

        original_uvs = [uv.name for uv in obj.data.uv_layers]

        # Explicitly anchor existing UV nodes to the original UV layer name
        # Otherwise, nodes using implicit default UVs will switch to Bake_UV during baking!
        if original_uvs:
            orig_uv_name = original_uvs[0]
            for slot in obj.material_slots:
                if slot.material and slot.material.use_nodes:
                    for n in slot.material.node_tree.nodes:
                        if n.type == 'UVMAP' and not n.uv_map:
                            n.uv_map = orig_uv_name

        if "Bake_UV" in obj.data.uv_layers:
            obj.data.uv_layers.remove(obj.data.uv_layers["Bake_UV"])

        bake_uv = obj.data.uv_layers.new(name="Bake_UV")
        bake_uv.active = True
        if original_uvs:
            obj.data.uv_layers[original_uvs[0]].active_render = True

        if view_3d_area:
            with bpy.context.temp_override(area=view_3d_area):
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.normals_make_consistent(inside=False)
                # Smart UV Project with tight island margin and area weight
                bpy.ops.uv.smart_project(
                    angle_limit=1.15192, 
                    island_margin=UV_ISLAND_MARGIN, 
                    area_weight=1.0, 
                    correct_aspect=True, 
                    scale_to_bounds=False
                )
                bpy.ops.object.mode_set(mode='OBJECT')

        # --- STEP 2: PRE-PROCESS MATERIALS & DETECT REQUIRED PASSES ---
        high_samples_needed = False
        active_materials = []

        for slot in obj.material_slots:
            mat = slot.material
            if mat and mat.use_nodes and mat not in active_materials:
                active_materials.append(mat)
                if needs_high_samples(mat.node_tree):
                    high_samples_needed = True
                
                out_node = get_active_output(mat.node_tree.nodes)
                if out_node and out_node.inputs['Surface'].is_linked:
                    surf_node = out_node.inputs['Surface'].links[0].from_node
                    convert_displacement_to_bump(mat.node_tree, out_node, surf_node)

        # Store baked image references per material: {mat: {channel: bpy.types.Image}}
        baked_results = {mat: {} for mat in active_materials}

        # --- STEP 3: BAKE ALL CHANNELS NON-DESTRUCTIVELY ---
        for channel in CHANNELS_TO_BAKE:
            # Check if at least one material on this object needs this channel
            mats_needing_channel = []
            for mat in active_materials:
                out_node = get_active_output(mat.node_tree.nodes)
                if out_node and out_node.inputs['Surface'].is_linked:
                    surf_node = out_node.inputs['Surface'].links[0].from_node
                    if should_bake_channel(mat.node_tree, surf_node, channel):
                        mats_needing_channel.append(mat)

            if not mats_needing_channel:
                continue

            print(f"=== Baking {channel} for {obj.name} ===")

            # Set optimal sample count (1 sample for pure math/textures, 32 if Bevel/AO is used)
            scene.cycles.samples = 32 if (high_samples_needed or channel == 'NORMAL') else 1

            temp_cleanup = [] # Stores (mat, temp_nodes_list, orig_surface_socket)

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

                # Create Target Image
                img_name = f"{obj.name}_{mat.name}_{channel}"
                if img_name in bpy.data.images:
                    bpy.data.images.remove(bpy.data.images[img_name])

                is_data = channel not in ['BASE_COLOR', 'EMISSION']
                # Use 32-bit float buffer for Normal maps to prevent 8-bit stair-stepping artifacts
                use_float = (channel == 'NORMAL')
                img = bpy.data.images.new(img_name, width=IMAGE_SIZE, height=IMAGE_SIZE, alpha=False, float_buffer=use_float)
                img.colorspace_settings.name = 'Non-Color' if is_data else 'sRGB'

                bake_node = nodes.new(type='ShaderNodeTexImage')
                bake_node.image = img
                bake_node.name = f"TEMP_BAKE_{channel}"
                temp_nodes.append(bake_node)

                for n in nodes:
                    n.select = False
                bake_node.select = True
                nodes.active = bake_node

                # Route non-normal channels through a pure Emission Shader for 100% exact values
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
                bpy.ops.object.bake(type=bake_type, margin=BAKE_MARGIN, use_clear=True)
            except RuntimeError as e:
                print(f"Bake failed on {obj.name} ({channel}): {e}")

            # Save Baked Images and Restore Original Node Graph Immediately
            for mat, temp_nodes, orig_surf_socket, img, out_node in temp_cleanup:
                # Restore original surface connection
                mat.node_tree.links.new(orig_surf_socket, out_node.inputs['Surface'])

                # Save image to disk if this material needed the channel
                if mat in mats_needing_channel and img.has_data:
                    filepath = os.path.join(SAVE_PATH, f"{img.name}.png")
                    img.filepath_raw = filepath
                    img.file_format = 'PNG'
                    img.save()
                    baked_results[mat][channel] = img
                else:
                    bpy.data.images.remove(img)

                # Remove temporary baking nodes
                for tn in temp_nodes:
                    mat.node_tree.nodes.remove(tn)

        # --- STEP 4: REBUILD CLEAN PBR MATERIAL & ASSIGN BAKED MAPS ---
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

                # Preserve key scalar settings (like IOR or Transmission) before wiping nodes
                old_principled = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
                ior_val = old_principled.inputs['IOR'].default_value if (old_principled and 'IOR' in old_principled.inputs) else 1.45

                # Clear all old complex procedural/image nodes
                nodes.clear()

                # Create Clean Output, Principled BSDF, and UV Map Node
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

                # Connect all baked textures for this material
                y_pos = 300
                for channel, img in baked_results[mat].items():
                    tex_node = nodes.new('ShaderNodeTexImage')
                    tex_node.image = img
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

    # Restore original selection & render settings
    for obj in selected_meshes:
        obj.select_set(True)

    scene.render.engine = orig_engine
    scene.cycles.samples = orig_samples
    scene.view_settings.view_transform = orig_view_transform

    print(f"--- Master PBR Bake Complete! Textures saved to: {SAVE_PATH} ---")

bake_selected_objects()
