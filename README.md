# Hidden Geometry Removal (Compatible with Blender 4.2+)
### A Blender Add-on for Optimizing 3D Models

![Blender Version](https://img.shields.io/badge/Blender-4.2%2B-orange)
![Version](https://img.shields.io/badge/Version-0.2.1-blue)
![License](https://img.shields.io/badge/License-MIT-green)

## Overview

Hidden Geometry Removal is a powerful Blender add-on that automatically identifies and removes geometry that cannot be seen from any viewing angle. This tool is perfect for optimizing 3D models for real-time applications, game engines, or web-based 3D viewers. Important: Only merged objects can properly delete inside/hidden meshes - the addon includes built-in mesh merging functionality to handle this requirement efficiently.

![image](https://github.com/user-attachments/assets/67b32d17-c5ef-45a9-93be-28ccedfc7532)

## Features

- 🎥 Intelligent camera placement using spherical distribution
- 🔄 Customizable number of viewing angles
- ⚡ High-precision geometry analysis
- 🎯 Support for both deletion and selection modes
- 🛠️ User-friendly interface
- 📷 Option to keep cameras for visualization
- 🧪 Experimental mode with randomized face selection
- 📊 Configurable face sampling and flatness threshold
- 🔗 Built-in mesh merging functionality for proper internal geometry removal
- 🪟 Transparency aware: meshes with a transparent material stop blocking visibility, so geometry seen through glass is preserved
- 🚀 BVH-accelerated ray casting with optional multithreading
- 📦 Cameras are positioned from the object's bounding box, so objects away from the world origin are handled correctly

## Installation

1. Download the latest release (`HiddenGeometryRemoval.py`)
2. Open Blender and go to `Edit > Preferences > Add-ons`
3. Click `Install from Disk` and select the downloaded file
4. Enable the add-on by checking the box

## Usage

1. Select your mesh object in the 3D viewport
2. Open the sidebar (`N` key) and find the "Hidden Removal" tab
3. Adjust the settings:
   - **Merge Meshes**: Enable to combine meshes (required for proper internal geometry removal). When disabled, only the active object is processed
   - **Merge Scope**: Choose between joining all visible meshes or only the selected ones
   - **Merge by Distance**: Option to merge vertices that are close to each other
   - **Ignore Transparent**: Treat meshes with a transparent material as see-through
   - **Number of Rows**: Controls the number of vertical camera splines around the object
   - **Cameras per Row**: Sets how many cameras are placed along each spline
   - **Auto Distance**: Enabled by default. Derives the camera distance from the object's bounding box, so cameras always sit outside the mesh regardless of its scale
   - **Camera Distance**: Only used when Auto Distance is off. Sets how far cameras are placed from the object's center
   - **Delete/Select Mode**: Choose between removing hidden geometry or selecting visible faces
   - **Precision**: Toggle between high and low precision analysis
   - **Keep Cameras**: Option to retain cameras in an 'HGR_Cameras' collection for visualization
   - **Experimental Mode**: Enable advanced face selection techniques
   - **Fast Ray Casting**: Reuse a prebuilt BVH instead of querying the scene per ray
   - **Threads**: Number of worker threads, or 0 for every available core
4. Click "Remove Hidden Geometry" to process your mesh

## How It Works

The add-on creates a spherical distribution of cameras around your object using:
- Vertical splines (rows) evenly distributed around the object
- Multiple camera positions along each spline
- Automatic camera positioning and targeting
- Ray-casting for visibility checks

For proper internal geometry removal, the add-on first merges all selected meshes into a single object. This ensures that occluded geometry within complex models (like interior walls or internal components) can be properly identified and removed.

## Settings Explained

### Mesh Processing
- **Merge Meshes**: When enabled, mesh objects are combined before processing. When disabled, only the active object is analyzed - other objects are left untouched but still block visibility
- **Merge Scope**: All Visible joins every visible mesh in the scene, Selected Only joins just the selected ones
- **Merge by Distance**: Cleans up the mesh by merging vertices that are very close to each other
- **Ignore Transparent**: Meshes with a transparent material are left untouched and stop blocking visibility, so geometry behind glass is kept. A material counts as transparent when any of the following holds:
     - the node tree contains a Glass, Transparent or Refraction BSDF
     - a Principled BSDF has Alpha below 1.0, or Alpha driven by a link
     - a Principled BSDF has Transmission above 0.0, or Transmission driven by a link
     - the material's render method is Blended
     - the viewport display color has an alpha below 1.0
  If any material slot on an object qualifies, the whole object is treated as transparent.

### Camera Distribution
- **Rows**: More rows = more thorough horizontal coverage
  - Minimum: 2
  - Maximum: 12
  - Default: 4

- **Cameras per Row**: More cameras = better vertical coverage
  - Minimum: 2
  - Maximum: 12
  - Default: 4
  - Odd values are rounded down
 
- **Auto Distance**: When enabled, the camera radius is computed from the object's largest dimension with margin added. This is the recommended setting and handles both tiny props and large architectural models

- **Camera Distance**: Manual radius, greyed out unless Auto Distance is off. Useful when the object's bounding box is much larger than the geometry you actually care about — for example a long, thin mesh rotated at an angle, where the axis-aligned bounding box overestimates the size and pushes cameras further away than necessary

### Processing Options
- **High Precision**: Checks vertices and edge midpoints (slower but more accurate)
- **Low Precision**: Only checks face centers (faster but less precise)
- **Delete Mode**: Removes the hidden geometry outright
- **Outer Select Mode**: Deletes nothing. Leaves you in Edit Mode with the visible faces selected so you can review what would survive. Press Ctrl+I to invert and see exactly what Delete mode would remove
- **Keep Cameras**: When enabled, cameras are kept in a 'Cameras' collection for visualization and debugging

### Performance
- **Fast Ray Casting**: Builds a single BVH from the visible meshes and reuses it, instead of querying the scene for every ray. The BVH covers every visible mesh rather than just the target, so occlusion behaves the same whether or not Merge Meshes is enabled. There is no accuracy trade-off - leave this on unless you are comparing against the older code path
- **Threads**: Splits the visibility pass across worker threads. 0 uses every available core. Requires Fast Ray Casting, and is ignored in Experimental mode

### Experimental Mode Options
- **Face Sampling Ratio**: Percentage of faces to randomly sample for visibility check
  - Minimum: 1%
  - Maximum: 100%
  - Default: 30%
- **Flatness Angle**: Maximum angle difference for considering faces similar
  - Minimum: 10°
  - Maximum: 90°
  - Default: 30°

## Best Practices

1. **Always Enable Mesh Merging**: To properly remove internal geometry, keep the "Merge Meshes" option enabled
2. **Camera Distance**: Leave Auto Distance enabled unless you have a reason not to. If you turn it off, set the distance larger than your object's maximum dimension, or the cameras will end up inside the mesh
3. **Number of Cameras**: Start with default values and increase if needed
4. **Precision Mode**: Use 'High' for final processing, 'Low' for testing
5. **Backup**: Always save your file before processing large models
6. **Visualization**: Enable 'Keep Cameras' option to understand camera placement for complex cases

## Performance Tips

- Leave Fast Ray Casting on. It costs no accuracy and is the single largest speedup available
- Ray count scales as faces x sample points per face x cameras. Low precision uses one sample point per face while High uses nine on a quad, so switching precision changes the workload dramatically
- Run a Low precision pass with few cameras first to check the result, then raise the settings only as far as you need
- Processing is CPU bound. Single-core speed matters more than core count for the serial portions, and the GPU is not used at all
- RAM is a threshold rather than a dial: enough to hold the mesh, the BVH and the sample point list is all that helps
- High precision precomputes every sample point up front, so peak memory grows with face count. On very dense meshes, start with Low precision
- Use 'Outer Select' mode to preview what will be removed
- Disable 'Keep Cameras' for faster processing on large scenes

## Limitations

- Works best with manifold geometry
- Processing time increases with camera count and mesh complexity
- Very small details might require higher camera counts to detect
- Low precision samples only the face center, so a large face that is partially exposed can be missed and removed
- Experimental mode runs single threaded, because its flood fill depends on selection state as it progresses
- Alpha or Transmission inputs driven by a texture are treated as transparent without evaluating the texture. This is intentional - under-deleting is easier to recover from than over-deleting

## Experimental Features

The experimental mode allows for:
- Randomized face sampling to reduce processing time
- Similar face detection based on normal angle
- More flexible and adaptive geometry removal

## Requirements

Blender 4.2 or newer. No external dependencies - the add-on only uses modules bundled with Blender (bpy, bmesh, mathutils) and the Python standard library.

## Feedback

Feel free to submit issues or feature requests.

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Author

Seungwoo Lee
