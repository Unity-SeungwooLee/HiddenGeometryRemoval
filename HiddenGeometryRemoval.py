bl_info = {
    "name": "Hidden Geometry Removal",
    "author": "Seungwoo Lee",
    "version": (0, 2, 1),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar (N) > Hidden Removal",
    "description": "Removes geometry that is not visible from multiple camera positions.",
    "warning": "",
    "doc_url": "https://github.com/Unity-SeungwooLee/HiddenGeometryRemoval",
    "category": "Object",
}

import bpy
import bmesh
import math
import random
import os
import contextlib

from concurrent.futures import ThreadPoolExecutor
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from bpy.props import IntProperty, FloatProperty, EnumProperty, BoolProperty
from bpy.types import Operator, Panel, PropertyGroup
from bpy.utils import register_class, unregister_class

CAM_COLLECTION = "HGR_Cameras"
CAM_TAG = "hgr_generated"


# ---------------------------------------------------------------------------
# Camera helpers
# ---------------------------------------------------------------------------

def get_or_create_camera_collection(context):
    coll = bpy.data.collections.get(CAM_COLLECTION)
    if coll is None:
        coll = bpy.data.collections.new(CAM_COLLECTION)
        context.scene.collection.children.link(coll)
    elif coll.name not in context.scene.collection.children:
        try:
            context.scene.collection.children.link(coll)
        except RuntimeError:
            pass
    return coll


def create_camera_ring(row_angle, height_angles, radius, center, collection, prefix="HGR_Cam"):
    cameras = []
    row_rad = math.radians(row_angle)

    for i, height_angle in enumerate(height_angles):
        height_rad = math.radians(height_angle)

        horizontal_radius = radius * math.cos(height_rad)
        x = horizontal_radius * math.cos(row_rad)
        y = horizontal_radius * math.sin(row_rad)
        z = radius * math.sin(height_rad)

        name = f"{prefix}.Row{row_angle:.0f}.{i + 1}"
        cam_data = bpy.data.cameras.new(name=name)
        cam_obj = bpy.data.objects.new(name, cam_data)
        cam_obj[CAM_TAG] = True

        collection.objects.link(cam_obj)

        offset = Vector((x, y, z))
        cam_obj.location = center + offset
        # -Z of the camera must point at the object's center
        cam_obj.rotation_euler = offset.to_track_quat('Z', 'Y').to_euler()

        cameras.append(cam_obj)

    return cameras


def create_camera_setup(context, rows, cameras_per_row, sphere_radius, center, keep_cameras):
    collection = get_or_create_camera_collection(context) if keep_cameras else context.scene.collection

    row_angle_step = 360.0 / rows
    cameras_per_half = max(1, cameras_per_row // 2)
    height_angle_step = 90.0 / (cameras_per_half + 1)

    height_angles = []
    for i in range(cameras_per_half):
        angle = height_angle_step * (i + 1)
        height_angles.extend([angle, -angle])

    all_cameras = []
    for i in range(rows):
        all_cameras.extend(
            create_camera_ring(i * row_angle_step, height_angles, sphere_radius, center, collection)
        )

    # Make sure matrix_world is up to date before we read it
    context.view_layer.update()
    return all_cameras


def delete_generated_cameras(context):
    """Only removes cameras this add-on created - never the user's own cameras."""
    for obj in list(bpy.data.objects):
        if obj.type == 'CAMERA' and obj.get(CAM_TAG):
            bpy.data.objects.remove(obj, do_unlink=True)

    coll = bpy.data.collections.get(CAM_COLLECTION)
    if coll is not None and len(coll.objects) == 0 and len(coll.children) == 0:
        bpy.data.collections.remove(coll)


# ---------------------------------------------------------------------------
# Mesh helpers
# ---------------------------------------------------------------------------

def material_is_transparent(mat):
    """A material counts as transparent if anything about it lets you see through."""
    if mat is None:
        return False

    if not mat.use_nodes or mat.node_tree is None:
        return mat.diffuse_color[3] < 1.0

    # Blender 4.2+ renamed blend_method to surface_render_method (EEVEE Next)
    render_method = getattr(mat, "surface_render_method", None)
    if render_method is not None:
        if render_method == 'BLENDED':
            return True
    else:
        blend_method = getattr(mat, "blend_method", None)
        if blend_method is not None and blend_method != 'OPAQUE':
            return True

    for node in mat.node_tree.nodes:
        if node.type in {'BSDF_GLASS', 'BSDF_TRANSPARENT', 'BSDF_REFRACTION'}:
            return True

        if node.type == 'BSDF_PRINCIPLED':
            alpha = node.inputs.get("Alpha")
            if alpha is not None:
                # A linked alpha is treated as transparent: we cannot evaluate the
                # texture here, and under-deleting is safer than over-deleting.
                if alpha.is_linked or alpha.default_value < 1.0:
                    return True

            # "Transmission" was renamed to "Transmission Weight" in 4.x
            for name in ("Transmission Weight", "Transmission"):
                socket = node.inputs.get(name)
                if socket is None:
                    continue
                if socket.is_linked or socket.default_value > 0.0:
                    return True
                break

    return mat.diffuse_color[3] < 1.0


def object_is_transparent(obj, cache):
    for slot in obj.material_slots:
        mat = slot.material
        if mat is None:
            continue
        key = mat.name_full
        if key not in cache:
            cache[key] = material_is_transparent(mat)
        if cache[key]:
            return True
    return False


def collect_transparent_objects(context):
    cache = {}
    return [
        o for o in context.scene.objects
        if o.type == 'MESH' and o.visible_get() and object_is_transparent(o, cache)
    ]


def merge_meshes(context, scope, exclude=()):
    if scope == 'SELECTED':
        mesh_objects = [o for o in context.selected_objects if o.type == 'MESH']
    else:
        mesh_objects = [o for o in context.scene.objects if o.type == 'MESH']

    excluded = set(exclude)
    mesh_objects = [o for o in mesh_objects if o.visible_get() and o not in excluded]
    if not mesh_objects:
        return None

    bpy.ops.object.select_all(action='DESELECT')
    for obj in mesh_objects:
        obj.select_set(True)
    context.view_layer.objects.active = mesh_objects[0]

    if len(mesh_objects) > 1:
        bpy.ops.object.join()

    return context.view_layer.objects.active


def build_scene_bvh(context, exclude=()):
    """Build one BVH from every visible mesh, in world space.

    Using all meshes (not just the target) keeps occlusion behaving the same as
    scene.ray_cast, so this stays correct whether or not Merge Meshes is on.
    """
    depsgraph = context.evaluated_depsgraph_get()
    excluded = set(exclude)

    verts = []
    polys = []

    for obj in context.scene.objects:
        if obj.type != 'MESH' or obj in excluded or not obj.visible_get():
            continue

        eval_obj = obj.evaluated_get(depsgraph)
        try:
            mesh = eval_obj.to_mesh()
        except RuntimeError:
            continue
        if mesh is None:
            continue

        try:
            matrix = eval_obj.matrix_world
            offset = len(verts)
            verts.extend(matrix @ v.co for v in mesh.vertices)
            for poly in mesh.polygons:
                polys.append(tuple(i + offset for i in poly.vertices))
        finally:
            eval_obj.to_mesh_clear()

    if not polys:
        return None

    return BVHTree.FromPolygons(verts, polys, all_triangles=False)


@contextlib.contextmanager
def temporarily_hidden(context, objects):
    """Pull objects out of the depsgraph so scene.ray_cast cannot hit them.

    hide_viewport removes an object from depsgraph evaluation entirely, which is
    what makes transparent geometry stop blocking rays. Restored on exit, even if
    the pass raises.
    """
    previous = [(obj, obj.hide_viewport) for obj in objects]
    try:
        for obj, _ in previous:
            obj.hide_viewport = True
        if previous:
            context.view_layer.update()
        yield
    finally:
        for obj, state in previous:
            obj.hide_viewport = state
        if previous:
            context.view_layer.update()


def are_faces_similar(face1, face2, max_angle_diff):
    try:
        angle = abs(face1.normal.angle(face2.normal))
    except ValueError:  # zero-length normal
        return False
    return math.degrees(angle) <= max_angle_diff


def camera_setup_data(cameras):
    """Flatten camera transforms into plain mathutils data usable off-thread."""
    data = []
    for cam in cameras:
        matrix = cam.matrix_world
        data.append((
            matrix.translation.copy(),
            (matrix.to_quaternion() @ Vector((0.0, 0.0, -1.0))).normalized(),
            (cam.data.angle if cam.data.type == 'PERSP' else math.radians(90.0)) / 2.0,
        ))
    return data


def face_sample_points(face, matrix, precision):
    points = [matrix @ face.calc_center_median()]
    if precision == 'HIGH':
        points.extend(matrix @ v.co for v in face.verts)
        points.extend(
            (matrix @ e.verts[0].co + matrix @ e.verts[1].co) / 2.0
            for e in face.edges
        )
    return points


def point_is_visible(points, cam_data, cast):
    """True if any sample point is directly reachable from any camera."""
    for cam_location, cam_direction, half_fov in cam_data:
        for point in points:
            delta = point - cam_location
            if delta.length_squared == 0.0:
                continue
            to_point = delta.normalized()
            if to_point.angle(cam_direction) >= half_fov:
                continue
            hit_loc = cast(cam_location, to_point)
            if hit_loc is not None and (hit_loc - point).length < 1e-3:
                return True
    return False


def make_caster(scene, depsgraph, bvh):
    if bvh is not None:
        def cast(origin, direction):
            return bvh.ray_cast(origin, direction)[0]
    else:
        def cast(origin, direction):
            result = scene.ray_cast(depsgraph=depsgraph, origin=origin, direction=direction)
            return result[1] if result[0] else None
    return cast


def resolve_thread_count(requested, job_size):
    if requested <= 0:
        requested = os.cpu_count() or 1
    return max(1, min(requested, job_size))


def select_visible_faces(context, obj, cameras, precision, experimental,
                         sampling_ratio, flatness_angle, bvh=None, thread_count=0):
    scene = context.scene
    depsgraph = context.evaluated_depsgraph_get()
    matrix = obj.matrix_world
    cam_data = camera_setup_data(cameras)

    mesh = obj.data
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.faces.ensure_lookup_table()

        for face in bm.faces:
            face.select = False

        total_faces = len(bm.faces)
        if total_faces == 0:
            return 0, 0

        if experimental:
            # Flood fill depends on selection state as it goes, so it stays serial.
            cast = make_caster(scene, depsgraph, bvh)
            sample_count = max(1, int(total_faces * (sampling_ratio / 100.0)))
            faces_to_check = set(random.sample(list(bm.faces), sample_count))
            checked = set()

            while faces_to_check:
                face = faces_to_check.pop()
                if face in checked or face.select:
                    continue
                checked.add(face)

                if point_is_visible(face_sample_points(face, matrix, precision), cam_data, cast):
                    face.select = True
                    for vert in face.verts:
                        for linked in vert.link_faces:
                            if (linked not in checked and not linked.select
                                    and are_faces_similar(face, linked, flatness_angle)):
                                faces_to_check.add(linked)
        else:
            # Sample points are extracted up front so worker threads never touch
            # BMesh or any Blender data - they only see mathutils Vectors.
            all_points = [face_sample_points(f, matrix, precision) for f in bm.faces]

            workers = resolve_thread_count(thread_count, total_faces) if bvh is not None else 1

            if workers > 1:
                def scan(bounds):
                    start, end = bounds
                    local_cast = make_caster(scene, depsgraph, bvh)
                    return [
                        i for i in range(start, end)
                        if point_is_visible(all_points[i], cam_data, local_cast)
                    ]

                step = math.ceil(total_faces / workers)
                chunks = [(s, min(s + step, total_faces)) for s in range(0, total_faces, step)]

                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for visible_indices in pool.map(scan, chunks):
                        for i in visible_indices:
                            bm.faces[i].select = True
            else:
                cast = make_caster(scene, depsgraph, bvh)
                for i, points in enumerate(all_points):
                    if point_is_visible(points, cam_data, cast):
                        bm.faces[i].select = True

        # Propagate the face selection DOWN to their verts/edges. select_flush(True)
        # flushes the other way (verts/edges -> faces), which left the mesh looking
        # unselected once you tabbed into Edit Mode.
        bm.select_mode = {'FACE'}
        for vert in bm.verts:
            vert.select = False
        for edge in bm.edges:
            edge.select = False
        bm.select_flush_mode()

        bm.to_mesh(mesh)
        mesh.update()
        return total_faces, sum(1 for f in bm.faces if f.select)
    finally:
        bm.free()



def delete_invisible_faces():
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_mode(type='FACE')
    bpy.ops.mesh.hide(unselected=False)          # hide the visible faces
    bpy.ops.mesh.select_all(action='SELECT')     # what's left is hidden geometry
    bpy.ops.mesh.delete(type='VERT')
    bpy.ops.mesh.reveal()
    bpy.ops.object.mode_set(mode='OBJECT')


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class HiddenRemovalProperties(PropertyGroup):
    rows: IntProperty(
        name="Number of Rows",
        description="Number of vertical camera splines around the object",
        default=4, min=2, max=12,
    )
    cameras_per_row: IntProperty(
        name="Cameras per Row",
        description="Number of cameras per vertical spline (rounded down to an even number)",
        default=4, min=2, max=12, step=2,
    )
    sphere_radius: FloatProperty(
        name="Camera Distance",
        description="Distance of cameras from the object center",
        default=10.0, min=0.1,
    )
    auto_distance: BoolProperty(
        name="Auto Distance",
        description="Derive the camera distance from the object's bounding box",
        default=True,
    )
    delete_select_mode: EnumProperty(
        name="Mode",
        items=[
            ('DELETE', "Delete", "Delete the hidden geometry"),
            ('OUTER_SELECT', "Outer Select", "Only select the visible geometry"),
        ],
        default='DELETE',
    )
    precision_mode: EnumProperty(
        name="Precision",
        items=[
            ('HIGH', "High", "Check face center, vertices and edge midpoints"),
            ('LOW', "Low", "Check face centers only (faster)"),
        ],
        default='HIGH',
    )
    keep_cameras: BoolProperty(
        name="Keep Cameras",
        description="Keep the generated cameras in an 'HGR_Cameras' collection",
        default=False,
    )
    experimental: BoolProperty(
        name="Experimental",
        description="Randomized face sampling with similar-face expansion",
        default=False,
    )
    sampling_ratio: IntProperty(
        name="Face Sampling Ratio",
        default=30, min=1, max=100, subtype='PERCENTAGE',
    )
    flatness_angle: FloatProperty(
        name="Flatness Angle",
        default=30.0, min=10.0, max=90.0,
    )
    merge_meshes: BoolProperty(
        name="Merge Meshes",
        description="Join meshes before processing (required for interior removal)",
        default=True,
    )
    merge_scope: EnumProperty(
        name="Merge Scope",
        items=[
            ('ALL', "All Visible", "Join every visible mesh in the scene"),
            ('SELECTED', "Selected Only", "Join only the selected meshes"),
        ],
        default='ALL',
    )
    use_bvh: BoolProperty(
        name="Fast Ray Casting",
        description=(
            "Build a BVH of the visible meshes once and reuse it, instead of querying "
            "the scene for every ray. Much faster, and required for multithreading"
        ),
        default=True,
    )
    thread_count: IntProperty(
        name="Threads",
        description="Number of worker threads. 0 uses every available core",
        default=0, min=0, max=64,
    )
    ignore_transparent: BoolProperty(
        name="Ignore Transparent",
        description=(
            "Treat meshes with a transparent material as see-through: they are left "
            "untouched and stop blocking visibility, so geometry behind glass is kept"
        ),
        default=True,
    )
    merge_by_distance: BoolProperty(
        name="Merge by Distance",
        description="Merge vertices that are very close to each other",
        default=True,
    )


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class OBJECT_OT_hidden_geometry_removal(Operator):
    bl_idname = "object.hidden_geometry_removal"
    bl_label = "Remove Hidden Geometry"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and context.mode in {'OBJECT', 'EDIT_MESH'}

    def execute(self, context):
        props = context.scene.hidden_removal_props

        if context.object and context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')

        transparent = collect_transparent_objects(context) if props.ignore_transparent else []

        if props.merge_meshes:
            obj = merge_meshes(context, props.merge_scope, exclude=transparent)
        else:
            obj = context.active_object

        if obj is None or obj.type != 'MESH':
            self.report({'ERROR'}, "Please select a mesh object")
            return {'CANCELLED'}

        context.view_layer.objects.active = obj
        obj.select_set(True)

        delete_generated_cameras(context)

        center = obj.matrix_world @ (
            sum((Vector(c) for c in obj.bound_box), Vector()) / 8.0
        )
        if props.auto_distance:
            radius = max(obj.dimensions) * 2.0 + 1.0
        else:
            radius = props.sphere_radius

        cameras = create_camera_setup(
            context,
            props.rows,
            props.cameras_per_row,
            radius,
            center,
            props.keep_cameras,
        )

        transparent = [o for o in transparent if o is not obj]

        try:
            with temporarily_hidden(context, transparent):
                bvh = build_scene_bvh(context, exclude=transparent) if props.use_bvh else None

                total_faces, visible_count = select_visible_faces(
                    context, obj, cameras,
                    props.precision_mode,
                    props.experimental,
                    props.sampling_ratio,
                    props.flatness_angle,
                    bvh=bvh,
                    thread_count=props.thread_count,
                )
        finally:
            if not props.keep_cameras:
                delete_generated_cameras(context)

        if total_faces == 0:
            self.report({'WARNING'}, "Mesh has no faces")
            return {'CANCELLED'}

        skipped = f", {len(transparent)} transparent skipped" if transparent else ""

        if props.delete_select_mode == 'DELETE':
            delete_invisible_faces()

            if props.merge_by_distance:
                bpy.ops.object.mode_set(mode='EDIT')
                bpy.ops.mesh.select_all(action='SELECT')
                bpy.ops.mesh.remove_doubles(threshold=0.0001)
                bpy.ops.object.mode_set(mode='OBJECT')

            visible_faces = len(obj.data.polygons)
            removal_percent = (total_faces - visible_faces) / total_faces * 100.0
            self.report(
                {'INFO'},
                f"{visible_faces}/{total_faces} faces kept "
                f"({removal_percent:.1f}% removed) using {len(cameras)} cameras{skipped}"
            )
        else:
            # Outer Select: leave the user in Edit Mode with the surviving faces
            # selected so they can inspect (or Ctrl+I to see what would be removed).
            context.tool_settings.mesh_select_mode = (False, False, True)
            bpy.ops.object.mode_set(mode='EDIT')

            hidden = total_faces - visible_count
            self.report(
                {'INFO'},
                f"{visible_count}/{total_faces} faces selected as visible "
                f"({hidden} hidden) using {len(cameras)} cameras{skipped}"
            )

        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class VIEW3D_PT_hidden_geometry_removal(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Hidden Removal"
    bl_label = "Hidden Geometry Removal"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        props = context.scene.hidden_removal_props

        col = layout.column(align=True)
        col.prop(props, "merge_meshes")
        sub = col.column(align=True)
        sub.enabled = props.merge_meshes
        sub.prop(props, "merge_scope")
        col.prop(props, "merge_by_distance")
        col.prop(props, "ignore_transparent")

        layout.separator()

        col = layout.column(align=True)
        col.prop(props, "rows")
        col.prop(props, "cameras_per_row")
        col.prop(props, "auto_distance")
        sub = col.column(align=True)
        sub.enabled = not props.auto_distance
        sub.prop(props, "sphere_radius")

        layout.separator()

        col = layout.column(align=True)
        col.prop(props, "delete_select_mode")
        col.prop(props, "precision_mode")
        col.prop(props, "keep_cameras")

        layout.separator()

        col = layout.column(align=True)
        col.prop(props, "experimental")
        if props.experimental:
            col.prop(props, "sampling_ratio")
            col.prop(props, "flatness_angle")

        layout.separator()

        col = layout.column(align=True)
        col.prop(props, "use_bvh")
        sub = col.column(align=True)
        sub.enabled = props.use_bvh and not props.experimental
        sub.prop(props, "thread_count")
        if props.use_bvh and props.experimental:
            col.label(text="Experimental mode runs single threaded", icon='INFO')

        layout.separator()

        row = layout.row()
        row.scale_y = 2.0
        row.operator(OBJECT_OT_hidden_geometry_removal.bl_idname, icon='MOD_DECIM')


classes = (
    HiddenRemovalProperties,
    OBJECT_OT_hidden_geometry_removal,
    VIEW3D_PT_hidden_geometry_removal,
)


def register():
    for cls in classes:
        register_class(cls)
    bpy.types.Scene.hidden_removal_props = bpy.props.PointerProperty(type=HiddenRemovalProperties)


def unregister():
    if hasattr(bpy.types.Scene, "hidden_removal_props"):
        del bpy.types.Scene.hidden_removal_props
    for cls in reversed(classes):
        unregister_class(cls)


if __name__ == "__main__":
    register()
