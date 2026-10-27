# Blender Universal PBR Baker

A suite of robust, one-click Python scripts for Blender that bake complex procedural node trees and multi-layered materials into clean, game-ready PBR textures. 

Standard Cycles baking often fails on complex materials by rendering metallic surfaces as black, destroying Mix Shader blends, shifting normal maps on sculpted geometry, or crashing low-RAM systems on high-poly meshes. This repository solves these bottlenecks by dynamically routing channels through emission shaders for 1:1 lossless data extraction and managing UV/memory pipelines automatically.

---

## Which Script Version Should You Use?

This repository includes **three versions** tailored for different geometry types and hardware configurations:

| Script File | Target Use Case | UV & Geometry Handling | Hardware / RAM Profile |
| :--- | :--- | :--- | :--- |
| **`master_pbr_baker.py`** *(v1 - Standard)* | **Simple Hard-Surface & Architectural Meshes** (Furniture, walls, doors, basic props) | Applies object scale (`transform_apply`), recalculates normals, and generates a fresh `Smart UV Project` at `4096px`. | **Medium-High** (Requires 16GB+ RAM & dedicated GPU for 4K float buffers). |
| **`master_pbr_baker_v2.py`** *(v2 - High-Poly & Sculpted)* | **Intricate Carvings, Sculpted Meshes & Vertex-Painted Models** | Preserves existing UV seams (`AUTO` / `REPACK_ONLY`), guarantees 0% island overlap via C++ `pack_islands`, locks blank `Normal Map` & `Texture Coordinate` UVs, and preserves sculpted normals/AO scale. | **High / Workstation** (Uses 32-bit float normal buffers, 32 raytraced samples, and `ADJACENT_FACES` margin calculation). |
| **`master_pbr_baker_v3_low_ram.py`** *(v3 - Ultra-Fast / Low-RAM)* | **Laptops, Integrated GPUs, Low RAM Systems, & Heavy Scenes** | Fast `AABB` bounding-box UV packing (`rotate=False`), scene BVH isolation (`hide_render`), and instant `EXTEND` margins. | **Ultra-Light & Fast** (Bakes in under 2 minutes; purges C++ RAM buffers after every pass with `buffers_free()`). |

### Logical Decision Guide
* **Use `master_pbr_baker.py` when:** You have box-like or simple meshes without existing UV seams, and you want the script to automatically apply object scale, fix flipped normals, and generate a brand-new Smart UV layout from scratch.
* **Use `master_pbr_baker_v2.py` when:** You are working on a powerful desktop PC and baking dense, ornate 3D carvings, models with `Color Attributes` (vertex paint), or `Object Space` Ambient Occlusion where recalculating normals or running Smart UV Project would cause black blotches or color bleeding.
* **Use `master_pbr_baker_v3_low_ram.py` when:** Blender freezes, crashes, or takes 30+ minutes to bake on your machine, or when you are baking a single asset inside a massive `.blend` scene. It isolates the active mesh from the rest of the scene's BVH tree and flushes memory after every single texture pass.

---

## Key Features

* **Lossless Metallic & Alpha Baking:** Bypasses the Cycles `DIFFUSE` pass bug (which renders metals black) by routing `Base Color`, `Metallic`, `Roughness`, `Specular`, and `Alpha` through a temporary `Emission` shader (`EMIT` pass) for exact 1:1 value extraction.
* **Recursive Mix Shader Support:** Traces complex node trees automatically. If your material blends multiple `Principled BSDF` shaders using masks (such as scratches, dirt, or fingerprints), the script dynamically builds a mathematical mix of all channels before baking.
* **Complete UV Reference Locking (`v2` & `v3`):** Automatically locks blank `Normal Map` nodes, `UV Map` nodes, and `Texture Coordinate -> UV` links to your original UV layer prior to creating `Bake_UV`, preventing textures from shifting mid-bake.
* **Smart Displacement Handling:** Automatically detects `Displacement` nodes and disconnects them from the `Material Output` so they don't skew Cycles raycasting, converting height data into bump space only when needed.
* **Adaptive Raytrace Sampling:** Scans the node tree (including nested Node Groups) for raytraced nodes like `Bevel` or `Ambient Occlusion` and adjusts sample counts automatically—keeping pure math/texture passes at `1` sample for instant baking.
* **Zero-RAM-Accumulation Pipeline (`v3`):** Uses `512x512` memory tiling, disables unnecessary light bounces (`max_bounces = 0`), sets AO nodes to `Only Local`, and runs `img.buffers_free()` + Python garbage collection (`gc.collect()`) immediately after saving each PNG.
* **Automatic Node Cleanup:** (Optional) Wipes complex procedural setups post-bake and replaces them with a single, clean `Principled BSDF` connected to your newly baked textures—ready for direct `.glb` / `.gltf` or game engine export.

---

## Installation

1. Download the script version that matches your workflow (`master_pbr_baker.py`, `master_pbr_baker_v2.py`, or `master_pbr_baker_v3_low_ram.py`).
2. Open Blender and navigate to the **Scripting** workspace.
3. Click **Open**, locate the downloaded `.py` script, and load it into the Text Editor.

---

## Usage

1. *(Recommended for low-RAM systems)* Switch your 3D Viewport shading mode to **Solid Mode** (`Z` -> `Solid`) so Eevee doesn't hold textures in shared GPU/system memory during the Cycles bake.
2. Select one or more mesh objects in the 3D Viewport.
3. Modify the configuration variables at the top of the script if needed:
   * `SAVE_PATH`: Set your desired output directory for the baked `.png` files.
   * `IMAGE_SIZE`: Set to `2048` (default in `v3` for speed/RAM efficiency) or `4096` (default in `v1`/`v2` for maximum micro-detail).
   * `UV_STRATEGY` *(in `v2` & `v3`)*:
     * `'AUTO'`: Preserves existing UV seams and repacks islands cleanly into `0–1` space if UVs exist; otherwise runs Smart UV Project.
     * `'REPACK_ONLY'`: Strictly keeps existing UV islands and packs them without overlap.
     * `'SMART_PROJECT'`: Forces a fresh Smart UV Project.
   * `CLEANUP_NODES`: Set to `True` to automatically replace complex nodes with the baked textures, or `False` to keep your original node trees intact.
   * `MAX_CPU_THREADS` *(in `v3`)*: Caps CPU thread usage (default `8`) so your operating system stays responsive during baking.
4. Click the **Run Script** button (the play icon) in the Text Editor header.

---

## Requirements

* **Blender:** 3.0+ or 4.0+
* **Render Engine:** Cycles (The script automatically switches to Cycles during the bake and restores your original engine afterward).

---

## License

Distributed under the MIT License. See `LICENSE` for more information.
