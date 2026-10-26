# Blender Universal PBR Baker

A robust, one-click Python script for Blender that bakes complex node trees into perfect, game-ready PBR textures. 

Standard Cycles baking often fails on complex materials by rendering metallic surfaces as black, destroying Mix Shader blends, or losing micro-details due to poor UV scaling. This script solves these bottlenecks by dynamically routing channels through emission shaders for 1:1 lossless data extraction.

## Key Features
* **Lossless Metallic & Alpha Baking:** Bypasses the Cycles Diffuse pass bug by routing Base Color, Metallic, Roughness, and Alpha through a temporary Emission shader for exact value extraction.
* **Mix Shader Support:** Recursively traces complex node trees. If your material blends multiple Principled BSDFs using masks, the script dynamically builds a mathematical mix of all channels before baking.
* **Smart Displacement Handling:** Automatically detects Displacement nodes and temporarily converts them into Bump space so height details bake perfectly into the Normal map without skewing raycasts.
* **Adaptive Sampling:** Scans the node tree for raytraced nodes (like Bevel or Ambient Occlusion) and automatically increases sample counts only when necessary, keeping standard procedural bakes lightning-fast.
* **High-Density UV Packing:** Applies object scale automatically and uses tight UV island margins to ensure tiled textures retain maximum resolution.
* **Automatic Node Cleanup:** (Optional) Wipes complex procedural setups post-bake and replaces them with a single, clean Principled BSDF connected to your newly baked textures.

## Installation
1. Download `master_pbr_baker.py`.
2. Open Blender and navigate to the **Scripting** workspace.
3. Click **Open**, locate the downloaded script, and open it.

## Usage
1. Select one or more mesh objects in the 3D viewport.
2. Open the script in the Text Editor.
3. Modify the configuration variables at the top of the script if needed:
   - `SAVE_PATH`: Set your desired output directory for the baked `.png` files.
   - `IMAGE_SIZE`: Default is 4096 to preserve micro-details from scaled UVs.
   - `CLEANUP_NODES`: Set to `True` to automatically replace complex nodes with the baked textures, or `False` to keep your original node trees.
4. Click the **Run Script** button (the play icon) in the Text Editor header.

## Requirements
* Blender 3.0+ or 4.0+
* Cycles Render Engine (Script will automatically switch to Cycles during the bake)

## License
Distributed under the MIT License. See `LICENSE` for more information.
