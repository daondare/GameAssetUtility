# Addon metadata (name, version, maintainer, etc.) lives in
# blender_manifest.toml at the root of the extension package — bl_info has
# been removed because it is replaced by the manifest in the Blender 5.1
# extension format.

# ─────────────────────────────────────────────────────────────────────────────
# RENAME RISK — internal identifier prefix
#
# This addon was originally distributed as "Create Project Folders" / "ProFC".
# It has since been renamed to "GameAssetUtility" everywhere user-visible
# (bl_info, panel category/labels, log prefixes, packaged-zip filename).
#
# The internal Python identifiers — every `CPF_*` class name, every
# `cpf.*` operator `bl_idname`, the `cpf_settings` and `cpf_assets` Scene
# PointerProperty/CollectionProperty attribute names, and the
# `create_project_folders/...` preset folder names on disk — are
# DELIBERATELY NOT renamed. Renaming them would break:
#   * every saved .blend file that has `scene.cpf_settings` data
#     (the property would silently vanish on load)
#   * any user keymaps, macros, or pie-menus that call `bpy.ops.cpf.*`
#   * every preset .py the user has saved under
#     <config>/presets/create_project_folders/...
#   * any user CPython script that references these names
#
# The user-facing surface (panel tab, labels, tooltips, distribution zip)
# is the new name; the implementation identifiers stay for compatibility.
# ─────────────────────────────────────────────────────────────────────────────

import bpy
import os
import json
import math
import importlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from bpy.props import (
    BoolProperty, BoolVectorProperty, CollectionProperty, FloatProperty,
    FloatVectorProperty, IntProperty, IntVectorProperty,
    StringProperty, PointerProperty, EnumProperty,
)
from bpy.types import AddonPreferences, Menu, Panel, Operator, PropertyGroup, UIList
from bl_operators.presets import AddPresetBase


# ── Template storage ──────────────────────────────────────────────────────────

TEMPLATE_HEADER = (
    "# Folder structure\n"
    "# Folders are separated by \\ - parent\\child\\{assets}\n"
    "# {assets} is replaced with each asset entry\n"
    "# Lines starting with # are comments, blank lines are ignored\n"
    "\n"
)

DEFAULT_TEMPLATES = {
    "Game Asset": (
        TEMPLATE_HEADER +
        "base\n"
        "substance\\bake\\{assets}\n"
        "substance\\low\\{assets}\n"
        "substance\\high\\{assets}\n"
        "substance\\texture\\{assets}\n"
    ),
    "VFX Shot": (
        TEMPLATE_HEADER +
        "renders\\beauty\n"
        "renders\\passes\n"
        "renders\\preview\n"
        "cache\n"
        "references\n"
        "exports\n"
    ),
    "Simple": (
        TEMPLATE_HEADER +
        "renders\n"
        "wip\n"
        "references\n"
        "exports\n"
    ),
}


def _templates_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates.json")


def load_templates():
    path = _templates_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return dict(DEFAULT_TEMPLATES)


def save_templates(templates):
    with open(_templates_path(), "w", encoding="utf-8") as f:
        json.dump(templates, f, indent=2, ensure_ascii=False)


def _template_enum_items(self, context):
    templates = load_templates()
    if not templates:
        return [("__NONE__", "(No templates)", "")]
    return [(k, k, "") for k in templates]


def _on_template_change(self, context):
    """Auto-load template into the active text block when dropdown changes."""
    name = self.active_template
    if not name or name == "__NONE__":
        return
    try:
        templates = load_templates()
        if name not in templates:
            return
        content = templates[name]
        if self.text_block:
            self.text_block.clear()
            self.text_block.write(content)
        else:
            text = bpy.data.texts.get("temp_structure") or bpy.data.texts.new("temp_structure")
            text.clear()
            text.write(content)
            self.text_block = text
        self.save_name = name
    except Exception as e:
        print(f"[GameAssetUtility] Template load error: {e}")


def parse_paths(text, base_name):
    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        result.append(line.replace("{assets}", base_name))
    return result


# Module-level cache to keep EnumProperty items strings alive (Blender quirk).
_export_asset_enum_cache = [("__NONE__", "(No assets)", "")]


def _export_asset_enum_items(self, context):
    """Build the export 'Asset' dropdown from scene.cpf_assets."""
    global _export_asset_enum_cache
    if context is None:
        return _export_asset_enum_cache
    items = []
    seen = set()
    for a in context.scene.cpf_assets:
        n = a.name.strip()
        if n and n not in seen:
            seen.add(n)
            items.append((n, n, ""))
    if not items:
        items = [("__NONE__", "(No assets)", "")]
    _export_asset_enum_cache = items
    return _export_asset_enum_cache


# ── Addon preferences ─────────────────────────────────────────────────────────

class CPF_Preferences(AddonPreferences):
    bl_idname = __name__

    source_path: StringProperty(
        name="Source Path",
        description=(
            "Folder containing the source __init__.py. "
            "'Update & Reload' copies all .py files from here into the "
            "installed addon folder then hot-reloads"
        ),
        default="",
        subtype="DIR_PATH",
    )
    zip_path: StringProperty(
        name="ZIP Output Path",
        description=(
            "Folder where 'Package as .zip' saves the distributable ZIP. "
            "Leave empty to save next to the source folder"
        ),
        default="",
        subtype="DIR_PATH",
    )
    show_dev: BoolProperty(
        name="Developer",
        description="Show developer tools for reloading and packaging the addon",
        default=False,
    )

    def draw(self, context):
        layout = self.layout

        row = layout.row()
        row.prop(
            self, "show_dev",
            icon="TRIA_DOWN" if self.show_dev else "TRIA_RIGHT",
            icon_only=True, emboss=False,
        )
        row.label(text="Developer")

        if not self.show_dev:
            return

        box = layout.box()

        col = box.column(align=True)
        col.label(text="Source folder (Update & Reload):")
        col.prop(self, "source_path", text="")

        col = box.column(align=True)
        col.label(text="ZIP output folder:")
        col.prop(self, "zip_path", text="")

        box.separator()

        src = bpy.path.abspath(self.source_path.strip())
        src_valid = bool(src and os.path.isdir(src))
        zip_out = self.zip_path.strip()
        zip_valid = not zip_out or os.path.isdir(bpy.path.abspath(zip_out))

        row = box.row(align=True)
        row.enabled = src_valid
        row.operator("cpf.reload_addon", text="Reload", icon="FILE_REFRESH")
        row.operator("cpf.update_addon", text="Update & Reload", icon="IMPORT")

        pkg = box.row()
        pkg.enabled = bool(zip_out) and zip_valid
        pkg.operator("cpf.package_zip", text="Package as .zip", icon="EXPORT")


# ── Asset list ────────────────────────────────────────────────────────────────

class CPF_AssetItem(PropertyGroup):
    name: StringProperty(
        name="Asset",
        description="Asset name — replaces {assets} in the folder template",
        default="asset",
    )


class CPF_UL_Assets(UIList):
    bl_idname = "CPF_UL_assets"

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index):
        layout.prop(item, "name", text="", emboss=False, icon="OBJECT_DATA")

    def filter_items(self, context, data, propname):
        return [], []


# ── Preview path list (WindowManager — safe to modify inside draw) ────────────

class CPF_PathItem(PropertyGroup):
    path: StringProperty(default="")
    is_header: BoolProperty(default=False)


class CPF_UL_Paths(UIList):
    bl_idname = "CPF_UL_paths"

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index):
        if item.is_header:
            layout.label(text=item.path, icon="OBJECT_DATA")
        else:
            layout.label(text=item.path, icon="FILE_FOLDER")

    def filter_items(self, context, data, propname):
        return [], []


# ── Scene properties ──────────────────────────────────────────────────────────

# ── Bake-modifier stack ───────────────────────────────────────────────────────
# Stack storage strategy (per the user spec):
#   * The active stack lives in a CollectionProperty<CPF_BakeModifierItem> on
#     CPF_Settings — a real PropertyGroup with declared fields, NOT
#     id_properties / dict-style assignment on a bpy_struct.
#   * Per-export visibility (one bool per modifier per pass) is stored as four
#     JSON-string-list properties on CPF_Settings. Index matches stack position.
#   * Preset save/load serializes the entire stack + visibility to/from JSON
#     (saved via the existing AddPresetBase preset machinery).
#   * Settings UI for modifiers is drawn manually via layout.prop on the
#     CPF_BakeModifierItem fields — we do NOT use Blender's template_modifiers
#     because that requires a real object context and was breaking the panel.

_PASS_KEYS = ("low", "cage", "trans", "painter")

# ── Modifier type / property cache (built once at register) ───────────────────
# Per the user spec, all RNA introspection happens at register time. Draw
# functions only read from these caches.
#
# _MODIFIER_PROPERTY_CACHE: mod_type_id -> list of property descriptors
#     each descriptor: {"identifier", "name", "description", "type",
#                       "is_array", "field_name"}  (field_name is the
#                       prefixed attribute name on CPF_BakeModifierItem)
# _MODIFIER_CATEGORY_CACHE: category_label -> list of mod_type_id
# _MODIFIER_DISPLAY_NAMES: mod_type_id -> friendly enum name
# _MODIFIER_DEFAULTS: mod_type_id -> {field_name: default_value}
#                     (used to reset a stack item's prefixed fields when
#                      its modifier_type changes)
_MODIFIER_PROPERTY_CACHE = {}
_MODIFIER_CATEGORY_CACHE = {}
_MODIFIER_DISPLAY_NAMES = {}
_MODIFIER_DEFAULTS = {}

# Allowed mesh-modifier categories — Physics and any other built-in
# categories are explicitly excluded from the Add Modifier dropdown.
_ALLOWED_CATEGORIES = ("Edit", "Generate", "Deform", "Other")

# Stock-modifier category mapping for the three allowed categories. The list
# of stock modifier types in each category is what Blender's native
# OBJECT_MT_modifier_add_<category> menus expose for mesh objects.
# Modifiers not present in this map are not stock — they're either Physics
# (excluded entirely below) or third-party (routed to "Other" if mesh-
# compatible).
_BUILTIN_MODIFIER_CATEGORIES = {
    "Edit": [
        "DATA_TRANSFER", "MESH_CACHE", "MESH_SEQUENCE_CACHE", "NORMAL_EDIT",
        "WEIGHTED_NORMAL", "UV_PROJECT", "UV_WARP", "VERTEX_WEIGHT_EDIT",
        "VERTEX_WEIGHT_MIX", "VERTEX_WEIGHT_PROXIMITY",
    ],
    "Generate": [
        "ARRAY", "BEVEL", "BOOLEAN", "BUILD", "DECIMATE", "EDGE_SPLIT",
        "MASK", "MIRROR", "MULTIRES", "NODES", "REMESH", "SCREW", "SKIN",
        "SOLIDIFY", "SUBSURF", "TRIANGULATE", "WELD", "WIREFRAME",
    ],
    "Deform": [
        "ARMATURE", "CAST", "CURVE", "DISPLACE", "HOOK", "LAPLACIANDEFORM",
        "LATTICE", "MESH_DEFORM", "SHRINKWRAP", "SIMPLE_DEFORM", "SMOOTH",
        "CORRECTIVE_SMOOTH", "LAPLACIANSMOOTH", "SURFACE_DEFORM", "WARP",
        "WAVE", "VOLUME_DISPLACE",
    ],
}

# Physics modifier types — explicitly excluded from the dropdown even if
# they happen to be mesh-compatible.
_PHYSICS_MODIFIER_TYPES = {
    "CLOTH", "COLLISION", "DYNAMIC_PAINT", "EXPLODE", "FLUID", "OCEAN",
    "PARTICLE_INSTANCE", "PARTICLE_SYSTEM", "SOFT_BODY", "SURFACE",
}


def _categorize_modifier(mod_type_id):
    """Return the category label for a modifier type, or None if it should
    not appear in the dropdown at all (Physics types).
    Stock modifiers map to their declared Edit/Generate/Deform category;
    everything else mesh-compatible (third-party) lands in 'Other'."""
    if mod_type_id in _PHYSICS_MODIFIER_TYPES:
        return None
    for cat, types in _BUILTIN_MODIFIER_CATEGORIES.items():
        if mod_type_id in types:
            return cat
    return "Other"


def _post_register_mesh_filter():
    """Run the mesh-compatibility test (now that bpy.data is unrestricted)
    and prune _MODIFIER_CATEGORY_CACHE in place. Called once via a timer
    scheduled from register(). The dynamic CPF_BakeModifierItem class
    keeps fields for the wider set since it can't be safely re-registered
    at runtime; only the dropdown list is filtered."""
    try:
        mesh_compatible = _build_mesh_compatible_modifier_set()
    except Exception as e:
        print(f"[GameAssetUtility] Post-register mesh filter failed: {e}")
        return None  # one-shot
    for cat, types in list(_MODIFIER_CATEGORY_CACHE.items()):
        _MODIFIER_CATEGORY_CACHE[cat] = [
            t for t in types if t in mesh_compatible
        ]
    return None  # one-shot


def _build_mesh_compatible_modifier_set():
    """Return the set of modifier type identifiers that can actually be
    applied to a Blender mesh object. Determined at register / refresh time
    by attempting to add each registered modifier type to a throwaway mesh
    datablock that's immediately removed. This is the only fully reliable
    way to filter out non-mesh modifiers (e.g. sequencer, line-art-only,
    grease-pencil-only) since Blender doesn't expose modifier→object-type
    compatibility through Python RNA in any documented way."""
    compatible = set()
    type_prop = bpy.types.Modifier.bl_rna.properties.get("type")
    if type_prop is None:
        return compatible

    tmp_mesh = bpy.data.meshes.new("__cpf_tmp_modtest_mesh")
    tmp_obj = bpy.data.objects.new("__cpf_tmp_modtest_obj", tmp_mesh)
    try:
        for it in type_prop.enum_items:
            type_id = it.identifier
            try:
                m = tmp_obj.modifiers.new(name="t", type=type_id)
                if m is not None:
                    compatible.add(type_id)
                    try:
                        tmp_obj.modifiers.remove(m)
                    except Exception:
                        pass
            except Exception:
                # Type not valid for mesh objects — skip
                pass
    finally:
        try:
            bpy.data.objects.remove(tmp_obj, do_unlink=True)
        except Exception:
            pass
        try:
            bpy.data.meshes.remove(tmp_mesh, do_unlink=True)
        except Exception:
            pass
    return compatible


# Modifier base properties to skip — they're either common to all modifiers
# or shouldn't be exposed as part of stored stack settings.
_MODIFIER_BASE_PROPS_SKIP = {
    "name", "type", "rna_type", "bl_rna",
    "is_active", "is_override_data",
    "show_expanded", "show_in_editmode", "show_on_cage",
    "show_render", "show_viewport", "use_apply_on_spline",
    "execution_time", "persistent_uid",
}


def _modifier_class_map():
    """Iterate bpy.types looking for Modifier subclasses and return a dict
    mapping modifier-type-enum identifier (e.g. 'EDGE_SPLIT') to its bpy.types
    class (e.g. bpy.types.EdgeSplitModifier). Uses the SNAKE_CASE → PascalCase
    + 'Modifier' heuristic that matches Blender's stock naming convention."""
    result = {}
    type_prop = bpy.types.Modifier.bl_rna.properties.get("type")
    if type_prop is None:
        return result

    name_to_type = {}
    for it in type_prop.enum_items:
        parts = it.identifier.split("_")
        camel = "".join(p.capitalize() for p in parts) + "Modifier"
        name_to_type[camel] = it.identifier

    for attr in dir(bpy.types):
        if attr in name_to_type:
            cls = getattr(bpy.types, attr, None)
            if cls is not None:
                try:
                    if issubclass(cls, bpy.types.Modifier) and cls is not bpy.types.Modifier:
                        result[name_to_type[attr]] = cls
                except TypeError:
                    pass
    return result


def _safe_int_clamp(v):
    """Clamp a numeric value to a 32-bit-safe int range for IntProperty."""
    INT_MIN, INT_MAX = -2 ** 31, 2 ** 31 - 1
    try:
        v = int(v)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(INT_MIN, min(INT_MAX, v))


def _safe_float_clamp(v):
    """Clamp a numeric value to a finite-float range for FloatProperty bounds."""
    F_MAX = 1e38
    try:
        v = float(v)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if math.isnan(v):
        return 0.0
    if v > F_MAX:
        return F_MAX
    if v < -F_MAX:
        return -F_MAX
    return v


def _rna_to_blender_prop(rna_prop):
    """Convert a single RNA property descriptor to a bpy.props PropertyDef
    suitable for use as a PropertyGroup annotation. Returns (prop, default)
    or None if the property type is unsupported (POINTER/COLLECTION etc)."""
    pt = rna_prop.type
    name = rna_prop.name or rna_prop.identifier
    desc = rna_prop.description or ""
    common = dict(name=name, description=desc)

    try:
        if pt == "BOOLEAN":
            if getattr(rna_prop, "is_array", False):
                size = max(1, int(rna_prop.array_length))
                default = tuple(bool(x) for x in rna_prop.default_array)
                return BoolVectorProperty(default=default, size=size, **common), default
            default = bool(rna_prop.default)
            return BoolProperty(default=default, **common), default

        if pt == "INT":
            if getattr(rna_prop, "is_array", False):
                size = max(1, int(rna_prop.array_length))
                default = tuple(_safe_int_clamp(x) for x in rna_prop.default_array)
                return IntVectorProperty(default=default, size=size, **common), default
            default = _safe_int_clamp(rna_prop.default)
            kw = dict(common, default=default)
            kw["min"] = _safe_int_clamp(rna_prop.hard_min)
            kw["max"] = _safe_int_clamp(rna_prop.hard_max)
            return IntProperty(**kw), default

        if pt == "FLOAT":
            if getattr(rna_prop, "is_array", False):
                size = max(1, int(rna_prop.array_length))
                default = tuple(_safe_float_clamp(x) for x in rna_prop.default_array)
                return FloatVectorProperty(default=default, size=size, **common), default
            default = _safe_float_clamp(rna_prop.default)
            kw = dict(common, default=default)
            kw["min"] = _safe_float_clamp(rna_prop.hard_min)
            kw["max"] = _safe_float_clamp(rna_prop.hard_max)
            return FloatProperty(**kw), default

        if pt == "STRING":
            default = rna_prop.default or ""
            return StringProperty(default=default, **common), default

        if pt == "ENUM":
            items = []
            for it in rna_prop.enum_items:
                items.append((it.identifier, it.name, it.description or ""))
            if not items:
                return None
            try:
                default = rna_prop.default
            except Exception:
                default = items[0][0]
            valid_ids = {i[0] for i in items}
            if default not in valid_ids:
                default = items[0][0]
            return EnumProperty(items=items, default=default, **common), default
    except Exception as e:
        print(f"[GameAssetUtility] Skipping RNA property {rna_prop.identifier}: {e}")
        return None
    return None


def _build_modifier_caches(include_mesh_filter=True):
    """Discover every modifier type registered in this Blender session,
    filter to mesh-compatible Edit/Generate/Deform/Other (Physics excluded
    entirely), and populate the property/category/display-name/default
    caches. Called from register() (with `include_mesh_filter=False` —
    bpy.data is restricted there so we can't run the temporary-mesh test;
    the mesh filter is applied shortly after via a post-register timer)
    and from the refresh-cache operator (with the default `True`).
    Never called during draw."""
    global _MODIFIER_PROPERTY_CACHE, _MODIFIER_CATEGORY_CACHE
    global _MODIFIER_DISPLAY_NAMES, _MODIFIER_DEFAULTS
    _MODIFIER_PROPERTY_CACHE = {}
    _MODIFIER_CATEGORY_CACHE = {cat: [] for cat in _ALLOWED_CATEGORIES}
    _MODIFIER_DISPLAY_NAMES = {}
    _MODIFIER_DEFAULTS = {}

    type_prop = bpy.types.Modifier.bl_rna.properties.get("type")
    if type_prop is None:
        return

    # Discover which modifier types this Blender build actually allows on a
    # mesh object (sequencer / grease-pencil-only / line-art / etc are all
    # filtered out by this test). Skipped at register() because bpy.data is
    # restricted there — the post-register timer applies it then.
    mesh_compatible = (
        _build_mesh_compatible_modifier_set() if include_mesh_filter else None
    )

    type_to_class = _modifier_class_map()

    for enum_item in type_prop.enum_items:
        type_id = enum_item.identifier

        # Filter 1 — must be applicable to a mesh (only when test ran)
        if mesh_compatible is not None and type_id not in mesh_compatible:
            continue

        # Filter 2 — must belong to one of the allowed categories
        # (Edit / Generate / Deform / Other). Physics returns None and is
        # dropped here.
        category = _categorize_modifier(type_id)
        if category is None:
            continue

        _MODIFIER_DISPLAY_NAMES[type_id] = enum_item.name
        _MODIFIER_CATEGORY_CACHE[category].append(type_id)

        cls = type_to_class.get(type_id)
        if cls is None:
            _MODIFIER_PROPERTY_CACHE[type_id] = []
            _MODIFIER_DEFAULTS[type_id] = {}
            continue

        prefix = type_id.lower() + "_"
        descriptors = []
        defaults = {}
        for rna_prop in cls.bl_rna.properties:
            ident = rna_prop.identifier
            if ident in _MODIFIER_BASE_PROPS_SKIP:
                continue
            if ident.startswith("_"):
                continue
            if rna_prop.is_hidden or rna_prop.is_readonly:
                continue
            if rna_prop.type in ("COLLECTION", "POINTER"):
                # Skip pointers/collections per spec — they reference
                # other ID datablocks (textures, objects, etc) which
                # don't round-trip cleanly through preset JSON.
                continue
            converted = _rna_to_blender_prop(rna_prop)
            if converted is None:
                continue
            blender_prop, default = converted
            field_name = prefix + ident
            descriptors.append({
                "identifier": ident,
                "name": rna_prop.name or ident,
                "description": rna_prop.description or "",
                "type": rna_prop.type,
                "is_array": bool(getattr(rna_prop, "is_array", False)),
                "field_name": field_name,
                "_blender_prop": blender_prop,
            })
            defaults[field_name] = default

        _MODIFIER_PROPERTY_CACHE[type_id] = descriptors
        _MODIFIER_DEFAULTS[type_id] = defaults


def _build_bake_modifier_item_class():
    """Generate the CPF_BakeModifierItem PropertyGroup class dynamically,
    using the cached property descriptors. Each modifier type's properties
    are exposed as fields prefixed with the type's lowercase identifier
    (e.g. `subsurf_levels`, `triangulate_quad_method`). Called once at
    register() — the class structure is fixed for the session, so newly-
    installed modifier addons won't get their UI fields without restarting
    (their type still appears in the dropdown / category list however,
    and they're applied with default settings)."""
    annotations = {
        "modifier_type": StringProperty(
            name="Type",
            description="Modifier type identifier",
            default="TRIANGULATE",
        ),
        "modifier_name": StringProperty(
            name="Name",
            description="Modifier name shown in the stack and used when the modifier is added to target meshes",
            default="Modifier",
        ),
    }

    seen_keys = set(annotations)
    for type_id, descriptors in _MODIFIER_PROPERTY_CACHE.items():
        for d in descriptors:
            key = d["field_name"]
            if key in seen_keys:
                continue
            annotations[key] = d["_blender_prop"]
            seen_keys.add(key)

    cls = type("CPF_BakeModifierItem", (PropertyGroup,),
               {"__annotations__": annotations})
    return cls


# Will be assigned at register() — dynamic class.
CPF_BakeModifierItem = None


# ── Pass-visibility JSON helpers (unchanged) ──────────────────────────────────
def _load_json_bool_list(json_str):
    """Parse a JSON string into a list of bools; return [] on any error."""
    if not json_str:
        return []
    try:
        data = json.loads(json_str)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [bool(x) for x in data]


def _store_json_bool_list(settings, pass_key, values):
    """Serialize a list of bools to the matching `vis_<pass>_json` property."""
    setattr(settings, f"vis_{pass_key}_json", json.dumps([bool(v) for v in values]))


def _read_pass_visibility(settings, pass_key, length=None):
    """Return a length-padded list of bools for the given pass. Missing
    entries (when `length` exceeds the stored list) default to True so any
    newly-added modifier is visible by default for every export."""
    vis = _load_json_bool_list(getattr(settings, f"vis_{pass_key}_json", "[]"))
    if length is not None and length > len(vis):
        vis = vis + [True] * (length - len(vis))
    return vis


# ── Stack item / JSON serialization driven by the cache ───────────────────────
def _stack_item_to_dict(item):
    """Serialize one stack item's per-type settings to a JSON-safe dict.
    Iterates the cached property descriptors for the item's modifier_type
    and reads the matching prefixed fields off the item."""
    t = item.modifier_type
    descriptors = _MODIFIER_PROPERTY_CACHE.get(t, [])
    settings = {}
    for d in descriptors:
        try:
            value = getattr(item, d["field_name"])
        except AttributeError:
            continue
        # Convert array properties to plain Python lists for JSON
        if d["is_array"]:
            value = list(value)
        elif d["type"] == "STRING":
            value = str(value)
        elif d["type"] == "ENUM":
            value = str(value)
        elif d["type"] == "BOOLEAN":
            value = bool(value)
        elif d["type"] == "INT":
            value = int(value)
        elif d["type"] == "FLOAT":
            value = float(value)
        settings[d["identifier"]] = value
    return settings


def _apply_dict_to_stack_item(item, settings_dict):
    """Inverse of _stack_item_to_dict — write a settings dict onto the
    matching prefixed fields of a stack item. Skips unknown keys silently."""
    if not isinstance(settings_dict, dict):
        return
    t = item.modifier_type
    descriptors = _MODIFIER_PROPERTY_CACHE.get(t, [])
    by_ident = {d["identifier"]: d for d in descriptors}
    for ident, value in settings_dict.items():
        d = by_ident.get(ident)
        if d is None:
            continue
        try:
            setattr(item, d["field_name"], value)
        except Exception:
            # Type mismatch / out-of-range / removed enum item — skip
            pass


def _stack_to_json(stack):
    """Serialize the modifier stack CollectionProperty to a JSON string."""
    items = []
    for item in stack:
        items.append({
            "type": item.modifier_type,
            "name": item.modifier_name,
            "settings": _stack_item_to_dict(item),
        })
    return json.dumps(items)


def _stack_from_json(stack, json_str):
    """Rebuild the modifier stack CollectionProperty from a JSON string."""
    stack.clear()
    if not json_str:
        return
    try:
        data = json.loads(json_str)
    except Exception:
        return
    if not isinstance(data, list):
        return
    for entry in data:
        if not isinstance(entry, dict):
            continue
        item = stack.add()
        item.modifier_type = entry.get("type", "TRIANGULATE")
        item.modifier_name = entry.get("name", item.modifier_type.title())
        _apply_dict_to_stack_item(item, entry.get("settings", {}))


def _populate_default_modifier_stack(settings):
    """Populate CPF_Settings.bake_modifier_stack with the original 5-entry
    bake sequence, written through the cached field names. Idempotent —
    only fills if the stack is currently empty. Visibility JSONs are seeded
    to match the historical hardcoded behavior."""
    stack = settings.bake_modifier_stack
    if len(stack) > 0:
        return

    def _add(type_id, name, settings_dict):
        item = stack.add()
        item.modifier_type = type_id
        item.modifier_name = name
        _apply_dict_to_stack_item(item, settings_dict)

    _add("TRIANGULATE", "Triangulate",
         {"quad_method": "SHORTEST_DIAGONAL", "ngon_method": "BEAUTY"})
    _add("SUBSURF", "Subdivision",
         {"subdivision_type": "SIMPLE", "levels": 1, "render_levels": 1})
    _add("TRIANGULATE", "Triangulate.001",
         {"quad_method": "FIXED", "ngon_method": "BEAUTY"})
    _add("DISPLACE", "Displace",
         {"direction": "NORMAL", "strength": 1.0, "mid_level": 0.0})
    _add("EDGE_SPLIT", "EdgeSplit",
         {"use_edge_angle": False, "use_edge_sharp": True})

    _store_json_bool_list(settings, "low",     [True,  True,  True,  False, True])
    _store_json_bool_list(settings, "cage",    [True,  True,  True,  True,  True])
    _store_json_bool_list(settings, "trans",   [True,  False, False, False, True])
    _store_json_bool_list(settings, "painter", [True,  False, False, False, True])


def _apply_stack_to_object(stack, obj):
    """Append a Blender modifier per stack item to `obj.modifiers`, copying
    every cached editable property value from the item onto the new modifier.
    Modifier types not present in the cache (e.g. third-party ones registered
    after this session started) are still added — Blender will use its own
    defaults for their settings since we have no cached field mapping for
    them. Returns the number of modifiers appended."""
    count = 0
    for item in stack:
        type_id = item.modifier_type
        try:
            new_mod = obj.modifiers.new(
                name=item.modifier_name or type_id.title(),
                type=type_id,
            )
        except Exception:
            # Modifier type not valid for this object type — skip
            continue
        for d in _MODIFIER_PROPERTY_CACHE.get(type_id, []):
            try:
                value = getattr(item, d["field_name"])
            except AttributeError:
                continue
            try:
                setattr(new_mod, d["identifier"], value)
            except Exception:
                # Setting failed (read-only at runtime, value out of range,
                # removed property in this Blender version) — skip silently.
                pass
        count += 1
    return count


class CPF_Settings(PropertyGroup):
    active_template: EnumProperty(
        name="Template",
        description="Switching templates auto-loads the structure into the Active Structure text block",
        items=_template_enum_items,
        update=_on_template_change,
    )
    text_block: PointerProperty(
        name="Active Structure",
        description=(
            "Text block defining the folder structure. "
            "Edit it in Blender's Text Editor (window icon opens one). "
            "Syntax: one path per line, {assets} replaced by each asset name, "
            "lines starting with # are comments"
        ),
        type=bpy.types.Text,
    )
    save_name: StringProperty(
        name="Template Name",
        description="Name to save the Active Structure under — overwrites an existing entry",
        default="",
    )
    asset_index: IntProperty(default=0)

    # ── Section collapse state ────────────────────────────────────────────────
    show_folder_structure: BoolProperty(
        name="Setup Folder Structure",
        description="Show the folder structure setup section",
        default=False,
    )
    show_bake_assets: BoolProperty(
        name="Setup Bake Assets",
        description="Show the bake assets section",
        default=False,
    )
    show_rename_meshes: BoolProperty(
        name="Rename Mesh Objects",
        description="Show the rename mesh objects sub-section",
        default=False,
    )
    show_add_modifiers: BoolProperty(
        name="Add Bake Modifiers",
        description="Show the add bake modifiers sub-section",
        default=False,
    )
    show_set_vertex_color: BoolProperty(
        name="Set Vertex Color",
        description="Show the set vertex color sub-section",
        default=False,
    )
    show_set_material: BoolProperty(
        name="Set Material",
        description="Show the set material sub-section",
        default=False,
    )

    # ── Bake assets — shared ──────────────────────────────────────────────────
    bake_collection: PointerProperty(
        name="Collection",
        description="Collection whose MESH objects are operated on by every Bake Assets sub-section",
        type=bpy.types.Collection,
    )

    # ── Bake assets — rename mesh objects ─────────────────────────────────────
    bake_prefix: StringProperty(
        name="Prefix",
        description="Prefix prepended to mesh object names",
        default="",
    )
    mesh_suffix: StringProperty(
        name="Suffix",
        description="Suffix appended to mesh object names",
        default="",
    )

    # ── Bake assets — modifier stack ──────────────────────────────────────────
    # The stack is a CollectionProperty of the dynamically-built
    # CPF_BakeModifierItem class — its annotation is injected into
    # CPF_Settings.__annotations__ in register() AFTER the dynamic class is
    # generated and registered, since the class structure depends on the
    # cached modifier RNA (which is itself only available at register time).
    # Per-export visibility lives in four StringProperty JSON-list fields,
    # indexed by stack position.
    bake_modifier_stack_index: IntProperty(default=0)
    show_bake_modifier_stack: BoolProperty(
        name="Modifier Stack",
        description="Show the editable modifier stack",
        default=False,
    )

    # Per-export viewport visibility — JSON list of bools, one per stack item
    vis_low_json: StringProperty(default="[]")
    vis_cage_json: StringProperty(default="[]")
    vis_trans_json: StringProperty(default="[]")
    vis_painter_json: StringProperty(default="[]")

    # ── Per-export modifier visibility — collapsible state per pass ───────────
    show_naming_fields: BoolProperty(
        name="Naming",
        description="Show the Export Folder Name and per-export suffix fields",
        default=False,
    )
    show_export_bake_options: BoolProperty(
        name="Export Options",
        description=(
            "Show the per-export options between the preset row and the "
            "Export Bake Assets button (naming fields, per-export modifier "
            "visibility lists, Space Preview Mesh Objects checkbox, "
            "Preview Mesh Object Gap field)"
        ),
        default=False,
    )
    show_low_visibility: BoolProperty(
        name="_low Modifier Visibility",
        description="Show the per-modifier viewport visibility list for the _low export",
        default=False,
    )
    show_cage_visibility: BoolProperty(
        name="_cage Modifier Visibility",
        description="Show the per-modifier viewport visibility list for the _cage export",
        default=False,
    )
    show_trans_visibility: BoolProperty(
        name="_trans Modifier Visibility",
        description="Show the per-modifier viewport visibility list for the _trans export",
        default=False,
    )
    show_painter_visibility: BoolProperty(
        name="_painter Modifier Visibility",
        description="Show the per-modifier viewport visibility list for the _painter export",
        default=False,
    )

    # ── Bake assets — set material ────────────────────────────────────────────
    bake_material: PointerProperty(
        name="Material",
        description="Material to assign to every MESH object in the shared collection",
        type=bpy.types.Material,
    )

    # ── Export Bake Assets ────────────────────────────────────────────────────
    show_export_bake_assets: BoolProperty(
        name="Export Bake Assets",
        description="Show the export bake assets section",
        default=False,
    )
    show_fbx_settings: BoolProperty(
        name="FBX Export Settings",
        description="Show the FBX export settings sub-section",
        default=False,
    )
    export_collection: PointerProperty(
        name="Collection",
        description="Collection whose MESH objects will be exported as a single FBX",
        type=bpy.types.Collection,
    )
    export_asset: EnumProperty(
        name="Asset",
        description="Asset (defined in 'Setup Folder Structure') whose subfolder is the export destination",
        items=_export_asset_enum_items,
    )
    export_folder_name: StringProperty(
        name="Folder Name",
        description=(
            "Plain folder name placed between root and the asset folder. "
            "E.g. 'low' resolves to <root>/low/<asset>/<asset>_<suffix>.fbx"
        ),
        default="low",
    )
    export_suffix_low: StringProperty(
        name="Suffix Low",
        description="Filename suffix for the low-poly variant (modifier viewport: tri1, subsurf, tri2, edge_split visible; displace hidden; objects snapped to origin)",
        default="_low",
    )
    export_suffix_cage: StringProperty(
        name="Suffix Cage",
        description="Filename suffix for the cage variant (all five modifiers visible)",
        default="_cage",
    )
    export_suffix_trans: StringProperty(
        name="Suffix Trans",
        description="Filename suffix for the trans variant (modifier viewport: tri1 + edge_split visible; subsurf, tri2, displace hidden)",
        default="_trans",
    )
    export_suffix_painter: StringProperty(
        name="Suffix Painter",
        description="Filename suffix for the painter variant (modifier viewport: tri1 + edge_split visible; subsurf, tri2, displace hidden; objects optionally spaced out on XY plane)",
        default="_painter",
    )
    space_objects: BoolProperty(
        name="Space Objects",
        description=(
            "When checked, the _painter export temporarily lays the collection's "
            "MESH objects out on the XY plane so their bounding boxes don't "
            "overlap. When unchecked, the _painter export uses the original "
            "world positions captured before the export sequence began"
        ),
        default=False,
    )
    object_gap: FloatProperty(
        name="Object Gap",
        description="Gap (in Blender units) between bounding-box edges of consecutive objects during the _painter spacing step",
        default=1.0,
        min=0.0,
        soft_max=10.0,
        unit="LENGTH",
    )

    # ── FBX export — Transform ────────────────────────────────────────────────
    fbx_global_scale: FloatProperty(
        name="Scale",
        description="Scale all data (Some importers do not support scaled armatures!)",
        default=1.0, min=0.001, max=1000.0,
    )
    fbx_apply_scale_options: EnumProperty(
        name="Apply Scalings",
        description="How to apply custom and units scalings in generated FBX file",
        items=[
            ("FBX_SCALE_NONE", "All Local",
             "Apply custom scaling and units scaling to each object transformation, FBX scale remains at 1.0"),
            ("FBX_SCALE_UNITS", "FBX Units Scale",
             "Apply custom scaling to each object transformation, and units scaling to FBX scale"),
            ("FBX_SCALE_CUSTOM", "FBX Custom Scale",
             "Apply custom scaling to FBX scale, and units scaling to each object transformation"),
            ("FBX_SCALE_ALL", "FBX All",
             "Apply custom scaling and units scaling to FBX scale"),
        ],
        default="FBX_SCALE_NONE",
    )
    fbx_axis_forward: EnumProperty(
        name="Forward",
        items=[
            ("X", "X Forward", ""),
            ("Y", "Y Forward", ""),
            ("Z", "Z Forward", ""),
            ("-X", "-X Forward", ""),
            ("-Y", "-Y Forward", ""),
            ("-Z", "-Z Forward", ""),
        ],
        default="-Z",
    )
    fbx_axis_up: EnumProperty(
        name="Up",
        items=[
            ("X", "X Up", ""),
            ("Y", "Y Up", ""),
            ("Z", "Z Up", ""),
            ("-X", "-X Up", ""),
            ("-Y", "-Y Up", ""),
            ("-Z", "-Z Up", ""),
        ],
        default="Y",
    )
    fbx_apply_unit_scale: BoolProperty(
        name="Apply Unit",
        description="Take into account current Blender units settings (if unset, raw Blender Units values are used as-is)",
        default=True,
    )
    fbx_use_space_transform: BoolProperty(
        name="Use Space Transform",
        description="Apply global space transform to the object rotations. When disabled, only the axis space is written to the file, all object transforms are left as-is",
        default=True,
    )
    fbx_bake_space_transform: BoolProperty(
        name="Apply Transform",
        description="Bake space transform into object data, avoids getting unwanted rotations to objects when target space is not aligned with Blender's space (WARNING! experimental option, use at own risk, known broken with armatures/animations)",
        default=False,
    )

    # ── FBX export — Geometry ─────────────────────────────────────────────────
    fbx_mesh_smooth_type: EnumProperty(
        name="Smoothing",
        description="Export smoothing information (prefer 'Normals Only' option if your target importer understand split normals)",
        items=[
            ("OFF", "Normals Only", "Export only normals instead of writing edge or face smoothing data"),
            ("FACE", "Face", "Write face smoothing"),
            ("EDGE", "Edge", "Write edge smoothing"),
        ],
        default="OFF",
    )
    fbx_use_subsurf: BoolProperty(
        name="Export Subdivision Surface",
        description="Export the last Catmull-Rom subdivision modifier as FBX subdivision (does not apply the modifier even if 'Apply Modifiers' is enabled)",
        default=False,
    )
    fbx_use_mesh_modifiers: BoolProperty(
        name="Apply Modifiers",
        description="Apply modifiers to mesh objects (except Armature ones) — WARNING: prevents exporting shape keys",
        default=True,
    )
    fbx_use_mesh_edges: BoolProperty(
        name="Loose Edges",
        description="Export loose edges (as two-vertices polygons)",
        default=False,
    )
    fbx_use_triangles: BoolProperty(
        name="Triangulate Faces",
        description="Convert all faces to triangles",
        default=False,
    )
    fbx_use_tspace: BoolProperty(
        name="Tangent Space",
        description="Add binormal and tangent vectors, together with normal they form the tangent space (will only work correctly with tris/quads only meshes!)",
        default=False,
    )
    fbx_colors_type: EnumProperty(
        name="Vertex Colors",
        description="Export vertex color attributes",
        items=[
            ("NONE", "None", "Do not export color attributes"),
            ("SRGB", "sRGB", "Export colors in sRGB color space"),
            ("LINEAR", "Linear", "Export colors in linear color space"),
        ],
        default="SRGB",
    )
    fbx_prioritize_active_color: BoolProperty(
        name="Prioritize Active Color",
        description="Make sure active color will be exported first. Could be important since some other software can discard other color attributes besides the first one",
        default=False,
    )

    # ── FBX export — Other ────────────────────────────────────────────────────
    fbx_use_custom_props: BoolProperty(
        name="Custom Properties",
        description="Export custom properties",
        default=False,
    )
    fbx_embed_textures: BoolProperty(
        name="Embed Textures",
        description="Embed textures in FBX binary file (only for \"Copy\" path mode!)",
        default=False,
    )
    fbx_use_metadata: BoolProperty(
        name="Custom Metadata",
        default=True,
    )

    # ── Export Game Assets ────────────────────────────────────────────────────
    show_export_game_assets: BoolProperty(
        name="Export Game Assets",
        description="Show the game assets export section",
        default=False,
    )
    show_game_fbx_settings: BoolProperty(
        name="FBX Export Settings",
        description="Show the FBX export settings sub-section",
        default=False,
    )
    game_export_collection: PointerProperty(
        name="Collection",
        description="Collection used by the Export Collection button",
        type=bpy.types.Collection,
    )
    game_export_path: StringProperty(
        name="Export Path",
        description="Target directory for game asset .fbx exports",
        default="",
        subtype="DIR_PATH",
    )

    # ── Game FBX export — Transform ───────────────────────────────────────────
    game_fbx_global_scale: FloatProperty(
        name="Scale",
        description="Scale all data",
        default=1.0, min=0.001, max=1000.0,
    )
    game_fbx_apply_scale_options: EnumProperty(
        name="Apply Scalings",
        description="How to apply custom and units scalings in generated FBX file",
        items=[
            ("FBX_SCALE_NONE", "All Local", ""),
            ("FBX_SCALE_UNITS", "FBX Units Scale", ""),
            ("FBX_SCALE_CUSTOM", "FBX Custom Scale", ""),
            ("FBX_SCALE_ALL", "FBX All", ""),
        ],
        default="FBX_SCALE_NONE",
    )
    game_fbx_axis_forward: EnumProperty(
        name="Forward",
        items=[
            ("X", "X Forward", ""),
            ("Y", "Y Forward", ""),
            ("Z", "Z Forward", ""),
            ("-X", "-X Forward", ""),
            ("-Y", "-Y Forward", ""),
            ("-Z", "-Z Forward", ""),
        ],
        default="-Z",
    )
    game_fbx_axis_up: EnumProperty(
        name="Up",
        items=[
            ("X", "X Up", ""),
            ("Y", "Y Up", ""),
            ("Z", "Z Up", ""),
            ("-X", "-X Up", ""),
            ("-Y", "-Y Up", ""),
            ("-Z", "-Z Up", ""),
        ],
        default="Y",
    )
    game_fbx_apply_unit_scale: BoolProperty(name="Apply Unit", default=True)
    game_fbx_use_space_transform: BoolProperty(name="Use Space Transform", default=True)
    game_fbx_bake_space_transform: BoolProperty(name="Apply Transform", default=False)

    # ── Game FBX export — Geometry ────────────────────────────────────────────
    game_fbx_mesh_smooth_type: EnumProperty(
        name="Smoothing",
        items=[
            ("OFF", "Normals Only", ""),
            ("FACE", "Face", ""),
            ("EDGE", "Edge", ""),
        ],
        default="OFF",
    )
    game_fbx_use_subsurf: BoolProperty(name="Export Subdivision Surface", default=False)
    game_fbx_use_mesh_modifiers: BoolProperty(name="Apply Modifiers", default=True)
    game_fbx_use_mesh_edges: BoolProperty(name="Loose Edges", default=False)
    game_fbx_use_triangles: BoolProperty(name="Triangulate Faces", default=False)
    game_fbx_use_tspace: BoolProperty(name="Tangent Space", default=False)
    game_fbx_colors_type: EnumProperty(
        name="Vertex Colors",
        items=[
            ("NONE", "None", ""),
            ("SRGB", "sRGB", ""),
            ("LINEAR", "Linear", ""),
        ],
        default="SRGB",
    )
    game_fbx_prioritize_active_color: BoolProperty(
        name="Prioritize Active Color", default=False,
    )

    # ── Game FBX export — Armature ────────────────────────────────────────────
    game_fbx_primary_bone_axis: EnumProperty(
        name="Primary Bone Axis",
        items=[
            ("X", "X Axis", ""), ("Y", "Y Axis", ""), ("Z", "Z Axis", ""),
            ("-X", "-X Axis", ""), ("-Y", "-Y Axis", ""), ("-Z", "-Z Axis", ""),
        ],
        default="Y",
    )
    game_fbx_secondary_bone_axis: EnumProperty(
        name="Secondary Bone Axis",
        items=[
            ("X", "X Axis", ""), ("Y", "Y Axis", ""), ("Z", "Z Axis", ""),
            ("-X", "-X Axis", ""), ("-Y", "-Y Axis", ""), ("-Z", "-Z Axis", ""),
        ],
        default="X",
    )
    game_fbx_armature_nodetype: EnumProperty(
        name="Armature FBXNode Type",
        items=[
            ("NULL", "Null", "'Null' FBX node, similar to Blender's Empty (default)"),
            ("ROOT", "Root", "'Root' FBX node, supposed to be the root of chains of bones..."),
            ("LIMBNODE", "LimbNode", "'LimbNode' FBX node, a regular node in chains of bones..."),
        ],
        default="NULL",
    )
    game_fbx_use_armature_deform_only: BoolProperty(
        name="Only Deform Bones",
        description="Only write deforming bones (and non-deforming ones when they have deforming children)",
        default=False,
    )
    game_fbx_add_leaf_bones: BoolProperty(
        name="Add Leaf Bones",
        description="Append a final bone to the end of each chain to specify last bone length",
        default=True,
    )

    # ── Game FBX export — Animation ───────────────────────────────────────────
    game_fbx_export_animations: BoolProperty(
        name="Export Animations",
        description=(
            "Master toggle for animation export. When unchecked, no NLA "
            "tracks, actions, or keyframe data are included in the .fbx "
            "(equivalent to Blender's native 'Baked Animation' off). When "
            "checked, animation export behaves per the sub-options below"
        ),
        default=True,
    )
    game_fbx_bake_anim: BoolProperty(name="Baked Animation", default=True)
    game_fbx_bake_anim_use_all_bones: BoolProperty(name="Key All Bones", default=True)
    game_fbx_bake_anim_use_nla_strips: BoolProperty(name="NLA Strips", default=True)
    game_fbx_bake_anim_use_all_actions: BoolProperty(name="All Actions", default=True)
    game_fbx_bake_anim_force_startend_keying: BoolProperty(
        name="Force Start/End Keying", default=True,
    )
    game_fbx_bake_anim_step: FloatProperty(
        name="Sampling Rate", default=1.0, min=0.01, max=100.0,
    )
    game_fbx_bake_anim_simplify_factor: FloatProperty(
        name="Simplify", default=1.0, min=0.0, max=100.0,
    )

    # ── Game FBX export — Object Types (matches Blender's native FBX dialog) ──
    # Each checkbox toggles inclusion of one Blender object type in the FBX
    # `object_types` set. Defaults match Blender's native exporter — only
    # Armature and Mesh are checked.
    game_fbx_obj_empty: BoolProperty(
        name="Empty",
        description="Include EMPTY-type objects in the export",
        default=False,
    )
    game_fbx_obj_camera: BoolProperty(
        name="Camera",
        description="Include CAMERA-type objects in the export",
        default=False,
    )
    game_fbx_obj_lamp: BoolProperty(
        name="Lamp",
        description="Include LIGHT-type objects in the export",
        default=False,
    )
    game_fbx_obj_armature: BoolProperty(
        name="Armature",
        description="Include ARMATURE-type objects in the export",
        default=True,
    )
    game_fbx_obj_mesh: BoolProperty(
        name="Mesh",
        description="Include MESH-type objects in the export",
        default=True,
    )
    game_fbx_obj_other: BoolProperty(
        name="Other",
        description="Include other object types (curves, surfaces, fonts, metas, grease-pencils, etc.) in the export",
        default=False,
    )

    # ── Game FBX export — Other ───────────────────────────────────────────────
    game_fbx_use_custom_props: BoolProperty(name="Custom Properties", default=False)
    game_fbx_embed_textures: BoolProperty(name="Embed Textures", default=False)
    game_fbx_use_metadata: BoolProperty(name="Custom Metadata", default=True)


# ── Operators ─────────────────────────────────────────────────────────────────

class CPF_OT_AddAsset(Operator):
    """Add a new asset entry to the list"""
    bl_idname = "cpf.add_asset"
    bl_label = "Add Asset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        assets = context.scene.cpf_assets
        item = assets.add()
        item.name = "asset"
        context.scene.cpf_settings.asset_index = len(assets) - 1
        return {"FINISHED"}


class CPF_OT_RemoveAsset(Operator):
    """Remove the selected asset from the list"""
    bl_idname = "cpf.remove_asset"
    bl_label = "Remove Asset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        assets = context.scene.cpf_assets
        settings = context.scene.cpf_settings
        idx = settings.asset_index
        if 0 <= idx < len(assets):
            assets.remove(idx)
            settings.asset_index = max(0, idx - 1)
        return {"FINISHED"}


class CPF_OT_NewStructure(Operator):
    """Create a new blank folder-structure text block and set it as the Active Structure"""
    bl_idname = "cpf.new_structure"
    bl_label = "New Structure"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        text = bpy.data.texts.new("structure")
        text.write(TEMPLATE_HEADER + "base\n")
        context.scene.cpf_settings.text_block = text
        return {"FINISHED"}


class CPF_OT_SaveTemplate(Operator):
    """Save the Active Structure text block to the template library.
Overwrites an existing entry with the same name"""
    bl_idname = "cpf.save_template"
    bl_label = "Save Template"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.cpf_settings
        if not settings.text_block:
            self.report({"WARNING"}, "No Active Structure — load or create one first")
            return {"CANCELLED"}

        name = settings.save_name.strip() or settings.text_block.name
        if not name:
            self.report({"WARNING"}, "Enter a name in the Save field")
            return {"CANCELLED"}

        templates = load_templates()
        templates[name] = settings.text_block.as_string()
        save_templates(templates)
        settings.save_name = name
        self.report({"INFO"}, f"Saved template '{name}'")
        return {"FINISHED"}


class CPF_OT_DeleteTemplate(Operator):
    """Permanently delete the selected template from the library (confirmation required)"""
    bl_idname = "cpf.delete_template"
    bl_label = "Delete Template"
    bl_options = {"REGISTER"}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        settings = context.scene.cpf_settings
        name = settings.active_template
        if not name or name == "__NONE__":
            return {"CANCELLED"}
        templates = load_templates()
        if name in templates:
            del templates[name]
            save_templates(templates)
            self.report({"INFO"}, f"Deleted '{name}'")
        return {"FINISHED"}


class CPF_OT_OpenTextEditor(Operator):
    """Open the Active Structure in a new Text Editor window for editing.

Syntax:
  • One folder path per line, e.g.  parent\\child\\{assets}
  • Folders are separated by \\
  • {assets} is replaced with each asset entry
  • Lines starting with # are comments, blank lines are ignored

Creates a blank structure if none is loaded yet"""
    bl_idname = "cpf.open_text_editor"
    bl_label = "Edit Structure"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.cpf_settings
        if not settings.text_block:
            text = bpy.data.texts.get("temp_structure") or bpy.data.texts.new("temp_structure")
            text.write(TEMPLATE_HEADER + "base\n")
            settings.text_block = text
        bpy.ops.wm.window_new()
        new_win = context.window_manager.windows[-1]
        area = new_win.screen.areas[0]
        area.type = "TEXT_EDITOR"
        area.spaces[0].text = settings.text_block
        return {"FINISHED"}


class CPF_OT_CreateFolders(Operator):
    """Create all folder paths from the Active Structure next to the saved .blend file.
Each asset name replaces {assets} in the template"""
    bl_idname = "cpf.create_folders"
    bl_label = "Create Folders"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.cpf_settings

        if not bpy.data.filepath:
            self.report({"ERROR"}, "Save your .blend file first so the target directory is known")
            return {"CANCELLED"}
        if not settings.text_block:
            self.report({"ERROR"}, "Select or create a folder structure text block")
            return {"CANCELLED"}

        base_names = [a.name.strip() for a in context.scene.cpf_assets if a.name.strip()]
        if not base_names:
            self.report({"ERROR"}, "Add at least one asset to the list")
            return {"CANCELLED"}

        root = Path(bpy.data.filepath).parent
        template_text = settings.text_block.as_string()

        seen = set()
        all_paths = []
        for base in base_names:
            for rel in parse_paths(template_text, base):
                if rel not in seen:
                    seen.add(rel)
                    all_paths.append(rel)

        if not all_paths:
            self.report({"WARNING"}, "No folder paths found — add non-comment lines to the template")
            return {"CANCELLED"}

        for rel in all_paths:
            (root / rel).mkdir(parents=True, exist_ok=True)

        self.report({"INFO"}, f"Created {len(all_paths)} folder(s) in {root}")
        return {"FINISHED"}


class CPF_OT_OpenExplorer(Operator):
    """Open the directory containing the saved .blend file in Windows Explorer"""
    bl_idname = "cpf.open_explorer"
    bl_label = "Open in Explorer"
    bl_options = {"REGISTER"}

    def execute(self, context):
        if not bpy.data.filepath:
            self.report({"WARNING"}, ".blend file not saved yet")
            return {"CANCELLED"}
        folder = str(Path(bpy.data.filepath).parent)
        subprocess.Popen(f'explorer "{folder}"')
        return {"FINISHED"}


class CPF_OT_RenameMeshObjects(Operator):
    """Rename every MESH object in the selected collection.
Each becomes: prefix + original_name + suffix"""
    bl_idname = "cpf.rename_mesh_objects"
    bl_label = "Rename Mesh Objects"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.cpf_settings
        coll = settings.bake_collection
        if not coll:
            self.report({"ERROR"}, "Select a collection first")
            return {"CANCELLED"}
        prefix = settings.bake_prefix
        suffix = settings.mesh_suffix
        count = 0
        for obj in coll.objects:
            if obj.type == "MESH":
                obj.name = prefix + obj.name + suffix
                count += 1
        self.report({"INFO"}, f"Renamed {count} mesh object(s) in '{coll.name}'")
        return {"FINISHED"}


class CPF_OT_AddBakeModifiers(Operator):
    """Apply the configured bake modifier stack to every MESH object in the
shared collection, in stack order, with the stored per-modifier settings"""
    bl_idname = "cpf.add_bake_modifiers"
    bl_label = "Add Bake Modifiers"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.cpf_settings
        coll = settings.bake_collection
        if not coll:
            self.report({"ERROR"}, "Select a collection first")
            return {"CANCELLED"}

        stack = settings.bake_modifier_stack
        if len(stack) == 0:
            self.report(
                {"ERROR"},
                "Modifier stack is empty — add modifiers below or load a preset",
            )
            return {"CANCELLED"}

        mesh_count = 0
        for obj in coll.objects:
            if obj.type != "MESH":
                continue
            _apply_stack_to_object(stack, obj)
            mesh_count += 1

        self.report(
            {"INFO"},
            f"Added {len(stack)} modifier(s) to {mesh_count} mesh(es)",
        )
        return {"FINISHED"}


class CPF_OT_BakeModAdd(Operator):
    """Append a new modifier of the given type to the bake modifier stack"""
    bl_idname = "cpf.bake_mod_add"
    bl_label = "Add Modifier"
    bl_options = {"REGISTER", "UNDO"}

    type: StringProperty(
        name="Type",
        description="Modifier type identifier (e.g. TRIANGULATE, SUBSURF)",
        default="TRIANGULATE",
    )

    def execute(self, context):
        settings = context.scene.cpf_settings
        type_id = self.type or "TRIANGULATE"
        item = settings.bake_modifier_stack.add()
        item.modifier_type = type_id
        item.modifier_name = _MODIFIER_DISPLAY_NAMES.get(type_id) or type_id.title()

        # Append a True (visible) entry to every per-pass visibility list
        for pk in _PASS_KEYS:
            vis = _read_pass_visibility(settings, pk, len(settings.bake_modifier_stack) - 1)
            vis.append(True)
            _store_json_bool_list(settings, pk, vis)

        settings.bake_modifier_stack_index = len(settings.bake_modifier_stack) - 1
        return {"FINISHED"}


class CPF_OT_BakeModRefreshCache(Operator):
    """Rebuild the modifier RNA / category caches. Run this once after
installing a third-party modifier addon so its type appears in the Add
Modifier dropdown. NOTE: full per-property settings UI for newly-discovered
modifier types only becomes available after restarting Blender, because the
CPF_BakeModifierItem PropertyGroup class is generated only at addon load —
the refresh updates the dropdown and category list, but adding fields to a
registered PropertyGroup at runtime is not supported by Blender's RNA"""
    bl_idname = "cpf.bake_mod_refresh_cache"
    bl_label = "Refresh Modifier Cache"
    bl_options = {"REGISTER"}

    def execute(self, context):
        _build_modifier_caches()
        n = sum(len(v) for v in _MODIFIER_CATEGORY_CACHE.values())
        self.report({"INFO"}, f"Refreshed cache — {n} modifier types known")
        return {"FINISHED"}


class CPF_MT_BakeModAdd(Menu):
    """Categorized 'Add Modifier' dropdown rendered as side-by-side columns,
    mirroring Blender's native Add Modifier menu layout."""
    bl_label = "Add Modifier"

    def draw(self, context):
        layout = self.layout
        # Fixed column order — Edit / Generate / Deform / Other.
        # Physics and other categories are filtered out at cache-build time
        # and never appear here.
        row = layout.row()
        for cat in _ALLOWED_CATEGORIES:
            mods = _MODIFIER_CATEGORY_CACHE.get(cat, [])
            if not mods:
                continue
            col = row.column()
            col.label(text=cat)
            col.separator()
            for type_id in mods:
                op = col.operator(
                    "cpf.bake_mod_add",
                    text=_MODIFIER_DISPLAY_NAMES.get(type_id, type_id),
                )
                op.type = type_id


class CPF_OT_BakeModRemove(Operator):
    """Remove the modifier at the given index from the bake modifier stack"""
    bl_idname = "cpf.bake_mod_remove"
    bl_label = "Remove Modifier"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(default=-1)

    def execute(self, context):
        settings = context.scene.cpf_settings
        stack = settings.bake_modifier_stack
        if not (0 <= self.index < len(stack)):
            return {"CANCELLED"}
        original_len = len(stack)
        stack.remove(self.index)
        settings.bake_modifier_stack_index = max(0, min(
            settings.bake_modifier_stack_index, len(stack) - 1
        ))
        # Drop the matching entry from every per-pass visibility list
        for pk in _PASS_KEYS:
            vis = _read_pass_visibility(settings, pk, original_len)
            if 0 <= self.index < len(vis):
                vis.pop(self.index)
            _store_json_bool_list(settings, pk, vis)
        return {"FINISHED"}


class CPF_OT_BakeModMove(Operator):
    """Move the modifier at the given index up or down in the bake modifier stack"""
    bl_idname = "cpf.bake_mod_move"
    bl_label = "Move Modifier"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(default=-1)
    direction: EnumProperty(
        items=[("UP", "Up", ""), ("DOWN", "Down", "")],
        default="UP",
    )

    def execute(self, context):
        settings = context.scene.cpf_settings
        stack = settings.bake_modifier_stack
        i = self.index
        if not (0 <= i < len(stack)):
            return {"CANCELLED"}
        if self.direction == "UP" and i == 0:
            return {"CANCELLED"}
        if self.direction == "DOWN" and i == len(stack) - 1:
            return {"CANCELLED"}
        j = i - 1 if self.direction == "UP" else i + 1
        stack.move(i, j)
        # Mirror the move in every per-pass visibility list
        for pk in _PASS_KEYS:
            vis = _read_pass_visibility(settings, pk, len(stack))
            if 0 <= i < len(vis) and 0 <= j < len(vis):
                vis[i], vis[j] = vis[j], vis[i]
            _store_json_bool_list(settings, pk, vis)
        settings.bake_modifier_stack_index = j
        return {"FINISHED"}


class CPF_OT_ToggleExportModVisibility(Operator):
    """Toggle this modifier's viewport visibility for the given export pass"""
    bl_idname = "cpf.toggle_export_mod_visibility"
    bl_label = "Toggle Export Modifier Visibility"
    bl_options = {"REGISTER", "UNDO"}

    modifier_index: IntProperty(default=-1)
    pass_key: StringProperty(default="")

    def execute(self, context):
        if self.pass_key not in _PASS_KEYS:
            return {"CANCELLED"}
        settings = context.scene.cpf_settings
        n = len(settings.bake_modifier_stack)
        if not (0 <= self.modifier_index < n):
            return {"CANCELLED"}
        vis = _read_pass_visibility(settings, self.pass_key, n)
        vis[self.modifier_index] = not vis[self.modifier_index]
        _store_json_bool_list(settings, self.pass_key, vis)
        return {"FINISHED"}


class CPF_OT_AddCageVertexGroup(Operator):
    """Adds a CAGE vertex group to all mesh objects in the collection and assigns it to any Displace modifiers"""
    bl_idname = "cpf.add_cage_vertex_group"
    bl_label = "Add Vertex Group"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.cpf_settings
        coll = settings.bake_collection
        if not coll:
            self.report({"ERROR"}, "Select a collection first")
            return {"CANCELLED"}

        added = 0
        skipped = 0
        displace_assigned = 0
        for obj in coll.objects:
            if obj.type != "MESH":
                continue
            # Add CAGE vertex group if missing; otherwise leave as-is.
            vg = obj.vertex_groups.get("CAGE")
            if vg is None:
                vg = obj.vertex_groups.new(name="CAGE")
                added += 1
            else:
                skipped += 1
            # Assign to every Displace modifier on the object.
            for m in obj.modifiers:
                if m.type == "DISPLACE":
                    try:
                        m.vertex_group = "CAGE"
                        displace_assigned += 1
                    except (RuntimeError, AttributeError):
                        pass

        self.report(
            {"INFO"},
            f"CAGE vertex group: added on {added}, already present on {skipped}, "
            f"assigned to {displace_assigned} Displace modifier(s)",
        )
        return {"FINISHED"}


class CPF_OT_ClearCageVertexGroup(Operator):
    """Removes the CAGE vertex group from all mesh objects in the collection and clears it from any Displace modifiers"""
    bl_idname = "cpf.clear_cage_vertex_group"
    bl_label = "Clear Vertex Group"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.cpf_settings
        coll = settings.bake_collection
        if not coll:
            self.report({"ERROR"}, "Select a collection first")
            return {"CANCELLED"}

        removed = 0
        cleared_modifiers = 0
        for obj in coll.objects:
            if obj.type != "MESH":
                continue
            # Clear CAGE assignment from every Displace modifier first so
            # we don't leave stale references after the vgroup is gone.
            for m in obj.modifiers:
                if m.type == "DISPLACE":
                    try:
                        if getattr(m, "vertex_group", "") == "CAGE":
                            m.vertex_group = ""
                            cleared_modifiers += 1
                    except (RuntimeError, AttributeError):
                        pass
            # Remove the CAGE vertex group if present; skip silently otherwise.
            vg = obj.vertex_groups.get("CAGE")
            if vg is not None:
                obj.vertex_groups.remove(vg)
                removed += 1

        self.report(
            {"INFO"},
            f"CAGE vertex group: removed from {removed} mesh(es), "
            f"cleared from {cleared_modifiers} Displace modifier(s)",
        )
        return {"FINISHED"}


class CPF_OT_SetVertexColor(Operator):
    """Add a default white (1, 1, 1, 1) vertex color attribute to every MESH object
in the shared collection. Skips meshes that already have one"""
    bl_idname = "cpf.set_vertex_color"
    bl_label = "Set Vertex Color"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.cpf_settings
        coll = settings.bake_collection
        if not coll:
            self.report({"ERROR"}, "Select a collection first")
            return {"CANCELLED"}

        added = 0
        skipped = 0
        for obj in coll.objects:
            if obj.type != "MESH":
                continue
            mesh = obj.data
            if len(mesh.color_attributes) > 0:
                skipped += 1
                continue
            attr = mesh.color_attributes.new(
                name="Attribute", type="BYTE_COLOR", domain="CORNER",
            )
            for el in attr.data:
                el.color = (1.0, 1.0, 1.0, 1.0)
            mesh.color_attributes.active_color = attr
            mesh.color_attributes.render_color_index = (
                list(mesh.color_attributes).index(attr)
            )
            added += 1

        self.report({"INFO"}, f"Added vertex color to {added} mesh(es), skipped {skipped}")
        return {"FINISHED"}


class CPF_OT_SetMaterial(Operator):
    """Replace all material slots on every MESH object in the shared collection with the picked material"""
    bl_idname = "cpf.set_material"
    bl_label = "Set Material"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.cpf_settings
        coll = settings.bake_collection
        if not coll:
            self.report({"ERROR"}, "Select a collection first")
            return {"CANCELLED"}
        mat = settings.bake_material
        if not mat:
            self.report({"ERROR"}, "Select a material first")
            return {"CANCELLED"}

        count = 0
        for obj in coll.objects:
            if obj.type != "MESH":
                continue
            obj.data.materials.clear()
            obj.data.materials.append(mat)
            count += 1

        self.report({"INFO"}, f"Set material '{mat.name}' on {count} mesh(es)")
        return {"FINISHED"}


def _save_all_mod_visibility(objs):
    """Snapshot show_viewport on every modifier of every object."""
    saved = []
    for o in objs:
        for m in o.modifiers:
            saved.append((m, m.show_viewport))
    return saved


def _restore_all_mod_visibility(saved):
    for m, original in saved:
        try:
            m.show_viewport = original
        except (RuntimeError, ReferenceError):
            pass


def _apply_pass_visibility(objs, pass_key):
    """For each target object, set show_viewport on its modifiers from the
    active stack's per-pass visibility JSON. Match is by modifier name —
    the names assigned at Add Bake Modifiers time correspond to the stack
    item's modifier_name. Modifiers without a matching name in the stack
    (e.g. modifiers the user added directly on the target meshes) are left
    alone. The stack + visibility JSON is the single source of truth — no
    custom properties on Modifier instances are read or written."""
    settings = bpy.context.scene.cpf_settings
    stack = settings.bake_modifier_stack
    if len(stack) == 0:
        return
    vis = _read_pass_visibility(settings, pass_key, len(stack))
    name_to_visible = {item.modifier_name: bool(vis[i])
                       for i, item in enumerate(stack)
                       if i < len(vis)}
    for o in objs:
        for m in o.modifiers:
            if m.name in name_to_visible:
                try:
                    m.show_viewport = name_to_visible[m.name]
                except (RuntimeError, ReferenceError):
                    pass


def _save_object_locations(objs):
    return [(o, o.location.copy()) for o in objs]


def _restore_object_locations(saved):
    for o, loc in saved:
        try:
            o.location = loc
        except (RuntimeError, ReferenceError):
            pass


def _snap_objects_to_origin(objs):
    for o in objs:
        o.location = (0.0, 0.0, 0.0)


def _spread_objects_on_xy(objs, gap=1.0):
    """Lay objects out along the Y axis (in the XY plane, X=0) with `gap` units
    between consecutive bounding boxes so they cannot overlap. Z is preserved
    per object — height is never modified by this function.

    Bounding boxes are evaluated with every modifier temporarily forced visible
    in the viewport so the spacing reflects each object's *full* mesh extent,
    not just the (possibly tiny) base mesh shape after the painter pass's
    modifier-visibility toggles. The original modifier visibility state is
    restored before the function returns."""
    import mathutils

    # Force every modifier visible so bound_box reports the full evaluated mesh
    saved_vis = []
    for o in objs:
        for m in o.modifiers:
            saved_vis.append((m, m.show_viewport))
            m.show_viewport = True
    try:
        bpy.context.view_layer.update()

        sorted_objs = sorted(objs, key=lambda o: o.name)
        bbox_info = []
        for o in sorted_objs:
            corners = [o.matrix_world @ mathutils.Vector(c) for c in o.bound_box]
            min_y = min(c.y for c in corners)
            max_y = max(c.y for c in corners)
            bbox_info.append((o, min_y, max_y - min_y))
    finally:
        for m, vis in saved_vis:
            try:
                m.show_viewport = vis
            except (RuntimeError, ReferenceError):
                pass
        try:
            bpy.context.view_layer.update()
        except RuntimeError:
            pass

    cursor = 0.0
    for o, min_y, depth in bbox_info:
        # Shift Y so the bbox min lands at `cursor`
        o.location.y = o.location.y + (cursor - min_y)
        # Line objects up on the Y axis (X reset to 0; Z left alone)
        o.location.x = 0.0
        cursor += depth + gap


class CPF_OT_ExportBakeAssets(Operator):
    """Export four FBX variants in sequence (_low, _cage, _trans, _painter) into:
<root>/<folder_name>/<asset>/<asset><suffix>.fbx

For each pass the bake-modifier viewport visibility is temporarily toggled, then
restored. The _low pass also temporarily snaps objects to origin; the _painter
pass temporarily spaces objects out on the XY plane. Render visibility and
permanent state are never altered"""
    bl_idname = "cpf.export_bake_assets"
    bl_label = "Export Bake Assets"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.cpf_settings

        coll = settings.export_collection
        if not coll:
            self.report({"ERROR"}, "Select a collection first")
            return {"CANCELLED"}

        asset = settings.export_asset
        if not asset or asset == "__NONE__":
            self.report({"ERROR"}, "Select an asset first (define one in Setup Folder Structure)")
            return {"CANCELLED"}

        folder_name = settings.export_folder_name.strip()
        if not folder_name:
            self.report({"ERROR"}, "Folder Name is empty")
            return {"CANCELLED"}

        if not bpy.data.filepath:
            self.report({"ERROR"}, "Save your .blend file first")
            return {"CANCELLED"}

        mesh_objs = [o for o in coll.objects if o.type == "MESH"]
        if not mesh_objs:
            self.report({"ERROR"}, f"No MESH objects in '{coll.name}'")
            return {"CANCELLED"}

        # Resolve destination by searching the existing project folder tree
        # for a directory named `folder_name` containing a subdirectory named
        # after the selected asset. Do NOT create any folders.
        root = Path(bpy.data.filepath).parent
        target_dir = None
        for dirpath, dirnames, _ in os.walk(root):
            if folder_name in dirnames:
                candidate = Path(dirpath) / folder_name / asset
                if candidate.is_dir():
                    target_dir = candidate
                    break
        if target_dir is None:
            self.report(
                {"ERROR"},
                f"Could not find existing '{folder_name}/{asset}' folder "
                f"under {root} — run Create Folders first or check names",
            )
            return {"CANCELLED"}

        # Pass definitions: (suffix_attr, transform_kind, pass_key)
        # transform_kind: None | "origin" | "spread"
        # pass_key drives per-modifier visibility via _cpf_<key> custom props
        # carried on each modifier (set by Add Bake Modifiers / preset load).
        passes = (
            ("export_suffix_low",     "origin", "low"),
            ("export_suffix_cage",    None,     "cage"),
            ("export_suffix_trans",   None,     "trans"),
            ("export_suffix_painter", "spread", "painter"),
        )

        # Save current selection state once; mesh selection is reused for all 4 passes
        original_selection = list(context.selected_objects)
        original_active = context.view_layer.objects.active

        try:
            bpy.ops.object.select_all(action="DESELECT")
        except RuntimeError:
            pass
        for o in mesh_objs:
            try:
                o.select_set(True)
            except RuntimeError:
                pass
        context.view_layer.objects.active = mesh_objs[0]

        # Shared FBX kwargs (filepath varies per pass)
        fbx_kwargs = dict(
            check_existing=False,
            use_selection=True,
            object_types={"MESH"},
            # Transform
            global_scale=settings.fbx_global_scale,
            apply_unit_scale=settings.fbx_apply_unit_scale,
            apply_scale_options=settings.fbx_apply_scale_options,
            use_space_transform=settings.fbx_use_space_transform,
            bake_space_transform=settings.fbx_bake_space_transform,
            axis_forward=settings.fbx_axis_forward,
            axis_up=settings.fbx_axis_up,
            # Geometry
            use_mesh_modifiers=settings.fbx_use_mesh_modifiers,
            use_mesh_modifiers_render=settings.fbx_use_mesh_modifiers,
            mesh_smooth_type=settings.fbx_mesh_smooth_type,
            use_subsurf=settings.fbx_use_subsurf,
            use_mesh_edges=settings.fbx_use_mesh_edges,
            use_triangles=settings.fbx_use_triangles,
            use_tspace=settings.fbx_use_tspace,
            colors_type=settings.fbx_colors_type,
            prioritize_active_color=settings.fbx_prioritize_active_color,
            use_custom_props=settings.fbx_use_custom_props,
            # Other
            embed_textures=settings.fbx_embed_textures,
            use_metadata=settings.fbx_use_metadata,
            # Animation forced off (per spec — animation settings omitted)
            bake_anim=False,
        )

        # Capture every object's world position ONCE, before any of the four
        # exports begin. Used to restore the originals after the _painter pass
        # (regardless of mode) and as the source-of-truth positions when
        # 'Space Objects' is unchecked.
        world_origins = _save_object_locations(mesh_objs)

        exported = []
        try:
            for suffix_attr, transform_kind, pass_key in passes:
                suffix = getattr(settings, suffix_attr)
                target_path = target_dir / f"{asset}{suffix}.fbx"

                # Save state for this pass — visibility always; locations only
                # if a transform is applied. The painter pass always restores
                # to the captured `world_origins` rather than a pass-local
                # snapshot.
                saved_visibility = _save_all_mod_visibility(mesh_objs)
                if transform_kind == "origin":
                    saved_locations = _save_object_locations(mesh_objs)
                elif transform_kind == "spread":
                    saved_locations = world_origins
                else:
                    saved_locations = None

                try:
                    _apply_pass_visibility(mesh_objs, pass_key)

                    if transform_kind == "origin":
                        _snap_objects_to_origin(mesh_objs)
                    elif transform_kind == "spread":
                        if settings.space_objects:
                            _spread_objects_on_xy(
                                mesh_objs, gap=settings.object_gap,
                            )
                        else:
                            # Restore to original positions captured at the
                            # very start of the export sequence — guarantees
                            # the painter export uses the originals even if a
                            # previous pass left objects elsewhere.
                            _restore_object_locations(world_origins)

                    bpy.ops.export_scene.fbx(
                        filepath=str(target_path), **fbx_kwargs
                    )
                    exported.append(target_path)
                finally:
                    _restore_all_mod_visibility(saved_visibility)
                    if saved_locations is not None:
                        _restore_object_locations(saved_locations)
        finally:
            # Restore previous selection
            try:
                bpy.ops.object.select_all(action="DESELECT")
            except RuntimeError:
                pass
            for o in original_selection:
                try:
                    o.select_set(True)
                except (RuntimeError, ReferenceError):
                    pass
            try:
                context.view_layer.objects.active = original_active
            except (RuntimeError, ReferenceError):
                pass

        self.report(
            {"INFO"},
            f"Exported {len(exported)} variant(s) to {target_dir}",
        )
        return {"FINISHED"}


# ── FBX export presets ────────────────────────────────────────────────────────
# Standard Blender preset folder convention: <config>/presets/<subdir>/*.py
#
# RENAME RISK — the four `_*_PRESET_SUBDIR` constants below intentionally
# keep `create_project_folders/...` (the addon's original folder name)
# instead of `game_asset_utility/...`. These paths point at directories on
# the user's disk where their preset .py files already live; renaming would
# orphan every saved preset until the user manually moves the files. Treat
# the subdir like a saved preference and leave it alone.
_FBX_PRESET_SUBDIR = "create_project_folders/fbx_export"
_FBX_DEFAULT_PRESET_NAME = "Default"

# Property paths (as `op.<expr>`-style strings) used by AddPresetBase to write
# preset files and by execute_preset to apply them.
_FBX_PRESET_VALUES = (
    "settings.fbx_global_scale",
    "settings.fbx_apply_scale_options",
    "settings.fbx_axis_forward",
    "settings.fbx_axis_up",
    "settings.fbx_apply_unit_scale",
    "settings.fbx_use_space_transform",
    "settings.fbx_bake_space_transform",
    "settings.fbx_mesh_smooth_type",
    "settings.fbx_use_subsurf",
    "settings.fbx_use_mesh_modifiers",
    "settings.fbx_use_mesh_edges",
    "settings.fbx_use_triangles",
    "settings.fbx_use_tspace",
    "settings.fbx_colors_type",
    "settings.fbx_prioritize_active_color",
    "settings.fbx_use_custom_props",
    "settings.fbx_embed_textures",
    "settings.fbx_use_metadata",
)


def _ensure_default_fbx_preset():
    """Write/refresh a 'Default.py' preset file mirroring the addon's built-in
    FBX property defaults, so users can always return to them. The file is
    rewritten on every register so the preset stays in sync if defaults change."""
    preset_dir = bpy.utils.user_resource(
        "SCRIPTS", path="presets/" + _FBX_PRESET_SUBDIR, create=True,
    )
    default_path = os.path.join(preset_dir, _FBX_DEFAULT_PRESET_NAME + ".py")
    lines = [
        "import bpy",
        "settings = bpy.context.scene.cpf_settings",
        "",
        "settings.fbx_global_scale = 1.0",
        "settings.fbx_apply_scale_options = 'FBX_SCALE_NONE'",
        "settings.fbx_axis_forward = '-Z'",
        "settings.fbx_axis_up = 'Y'",
        "settings.fbx_apply_unit_scale = True",
        "settings.fbx_use_space_transform = True",
        "settings.fbx_bake_space_transform = False",
        "settings.fbx_mesh_smooth_type = 'OFF'",
        "settings.fbx_use_subsurf = False",
        "settings.fbx_use_mesh_modifiers = True",
        "settings.fbx_use_mesh_edges = False",
        "settings.fbx_use_triangles = False",
        "settings.fbx_use_tspace = False",
        "settings.fbx_colors_type = 'SRGB'",
        "settings.fbx_prioritize_active_color = False",
        "settings.fbx_use_custom_props = False",
        "settings.fbx_embed_textures = False",
        "settings.fbx_use_metadata = True",
    ]
    try:
        with open(default_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"[GameAssetUtility] Could not write default FBX preset: {e}")


class CPF_MT_FBX_Presets(Menu):
    """Dropdown listing all FBX export presets stored on disk."""
    bl_label = "FBX Presets"
    preset_subdir = _FBX_PRESET_SUBDIR
    preset_operator = "script.execute_preset"
    draw = Menu.draw_preset


class CPF_OT_FBXPresetAdd(AddPresetBase, Operator):
    """Save the current FBX export settings as a named preset, or remove the
active preset (the 'Default' preset is protected and cannot be removed)"""
    bl_idname = "cpf.fbx_preset_add"
    bl_label = "Save FBX Preset"
    preset_menu = "CPF_MT_FBX_Presets"
    preset_subdir = _FBX_PRESET_SUBDIR

    preset_defines = [
        "settings = bpy.context.scene.cpf_settings",
    ]
    preset_values = list(_FBX_PRESET_VALUES)

    def execute(self, context):
        # Refuse to remove the protected Default preset
        if self.remove_active and not self.remove_name:
            menu_cls = getattr(bpy.types, self.preset_menu, None)
            if menu_cls is not None and menu_cls.bl_label == _FBX_DEFAULT_PRESET_NAME:
                self.report(
                    {"WARNING"},
                    f"Cannot delete the '{_FBX_DEFAULT_PRESET_NAME}' preset",
                )
                return {"CANCELLED"}
        return super().execute(context)


# ── Game FBX export presets ───────────────────────────────────────────────────
# Same preset-folder convention as the bake FBX system above. Covers the full
# game-export FBX setting set including armature + animation values.
_GAME_FBX_PRESET_SUBDIR = "create_project_folders/game_fbx_export"
_GAME_FBX_DEFAULT_PRESET_NAME = "Default"

_GAME_FBX_PRESET_VALUES = (
    "settings.game_fbx_global_scale",
    "settings.game_fbx_apply_scale_options",
    "settings.game_fbx_axis_forward",
    "settings.game_fbx_axis_up",
    "settings.game_fbx_apply_unit_scale",
    "settings.game_fbx_use_space_transform",
    "settings.game_fbx_bake_space_transform",
    "settings.game_fbx_mesh_smooth_type",
    "settings.game_fbx_use_subsurf",
    "settings.game_fbx_use_mesh_modifiers",
    "settings.game_fbx_use_mesh_edges",
    "settings.game_fbx_use_triangles",
    "settings.game_fbx_use_tspace",
    "settings.game_fbx_colors_type",
    "settings.game_fbx_prioritize_active_color",
    "settings.game_fbx_primary_bone_axis",
    "settings.game_fbx_secondary_bone_axis",
    "settings.game_fbx_armature_nodetype",
    "settings.game_fbx_use_armature_deform_only",
    "settings.game_fbx_add_leaf_bones",
    "settings.game_fbx_export_animations",
    "settings.game_fbx_bake_anim",
    "settings.game_fbx_bake_anim_use_all_bones",
    "settings.game_fbx_bake_anim_use_nla_strips",
    "settings.game_fbx_bake_anim_use_all_actions",
    "settings.game_fbx_bake_anim_force_startend_keying",
    "settings.game_fbx_bake_anim_step",
    "settings.game_fbx_bake_anim_simplify_factor",
    "settings.game_fbx_obj_empty",
    "settings.game_fbx_obj_camera",
    "settings.game_fbx_obj_lamp",
    "settings.game_fbx_obj_armature",
    "settings.game_fbx_obj_mesh",
    "settings.game_fbx_obj_other",
    "settings.game_fbx_use_custom_props",
    "settings.game_fbx_embed_textures",
    "settings.game_fbx_use_metadata",
)


def _ensure_default_game_fbx_preset():
    """Write/refresh a 'Default.py' preset file mirroring the addon's built-in
    Game FBX property defaults. Rewritten on every register so the file
    stays in sync if defaults change."""
    preset_dir = bpy.utils.user_resource(
        "SCRIPTS", path="presets/" + _GAME_FBX_PRESET_SUBDIR, create=True,
    )
    default_path = os.path.join(
        preset_dir, _GAME_FBX_DEFAULT_PRESET_NAME + ".py",
    )
    lines = [
        "import bpy",
        "settings = bpy.context.scene.cpf_settings",
        "",
        "settings.game_fbx_global_scale = 1.0",
        "settings.game_fbx_apply_scale_options = 'FBX_SCALE_NONE'",
        "settings.game_fbx_axis_forward = '-Z'",
        "settings.game_fbx_axis_up = 'Y'",
        "settings.game_fbx_apply_unit_scale = True",
        "settings.game_fbx_use_space_transform = True",
        "settings.game_fbx_bake_space_transform = False",
        "settings.game_fbx_mesh_smooth_type = 'OFF'",
        "settings.game_fbx_use_subsurf = False",
        "settings.game_fbx_use_mesh_modifiers = True",
        "settings.game_fbx_use_mesh_edges = False",
        "settings.game_fbx_use_triangles = False",
        "settings.game_fbx_use_tspace = False",
        "settings.game_fbx_colors_type = 'SRGB'",
        "settings.game_fbx_prioritize_active_color = False",
        "settings.game_fbx_primary_bone_axis = 'Y'",
        "settings.game_fbx_secondary_bone_axis = 'X'",
        "settings.game_fbx_armature_nodetype = 'NULL'",
        "settings.game_fbx_use_armature_deform_only = False",
        "settings.game_fbx_add_leaf_bones = True",
        "settings.game_fbx_export_animations = True",
        "settings.game_fbx_bake_anim = True",
        "settings.game_fbx_bake_anim_use_all_bones = True",
        "settings.game_fbx_bake_anim_use_nla_strips = True",
        "settings.game_fbx_bake_anim_use_all_actions = True",
        "settings.game_fbx_bake_anim_force_startend_keying = True",
        "settings.game_fbx_bake_anim_step = 1.0",
        "settings.game_fbx_bake_anim_simplify_factor = 1.0",
        "settings.game_fbx_obj_empty = False",
        "settings.game_fbx_obj_camera = False",
        "settings.game_fbx_obj_lamp = False",
        "settings.game_fbx_obj_armature = True",
        "settings.game_fbx_obj_mesh = True",
        "settings.game_fbx_obj_other = False",
        "settings.game_fbx_use_custom_props = False",
        "settings.game_fbx_embed_textures = False",
        "settings.game_fbx_use_metadata = True",
    ]
    try:
        with open(default_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"[GameAssetUtility] Could not write default game FBX preset: {e}")


class CPF_MT_GameFBX_Presets(Menu):
    """Dropdown listing all Game FBX export presets stored on disk."""
    bl_label = "Game FBX Presets"
    preset_subdir = _GAME_FBX_PRESET_SUBDIR
    preset_operator = "script.execute_preset"
    draw = Menu.draw_preset


class CPF_OT_GameFBXPresetAdd(AddPresetBase, Operator):
    """Save the current Game FBX export settings as a named preset, or remove
the active preset (the 'Default' preset is protected and cannot be removed)"""
    bl_idname = "cpf.game_fbx_preset_add"
    bl_label = "Save Game FBX Preset"
    preset_menu = "CPF_MT_GameFBX_Presets"
    preset_subdir = _GAME_FBX_PRESET_SUBDIR

    preset_defines = [
        "settings = bpy.context.scene.cpf_settings",
    ]
    preset_values = list(_GAME_FBX_PRESET_VALUES)

    def execute(self, context):
        if self.remove_active and not self.remove_name:
            menu_cls = getattr(bpy.types, self.preset_menu, None)
            if menu_cls is not None and menu_cls.bl_label == _GAME_FBX_DEFAULT_PRESET_NAME:
                self.report(
                    {"WARNING"},
                    f"Cannot delete the '{_GAME_FBX_DEFAULT_PRESET_NAME}' preset",
                )
                return {"CANCELLED"}
        return super().execute(context)


def _game_fbx_object_types_set(settings):
    """Build the `object_types` set passed to bpy.ops.export_scene.fbx from
    the six Object Types checkboxes in the FBX Export Settings UI. Maps the
    user-visible 'Lamp' to Blender's internal LIGHT identifier."""
    types = set()
    if settings.game_fbx_obj_empty:
        types.add("EMPTY")
    if settings.game_fbx_obj_camera:
        types.add("CAMERA")
    if settings.game_fbx_obj_lamp:
        types.add("LIGHT")
    if settings.game_fbx_obj_armature:
        types.add("ARMATURE")
    if settings.game_fbx_obj_mesh:
        types.add("MESH")
    if settings.game_fbx_obj_other:
        types.add("OTHER")
    return types


def _game_fbx_kwargs(settings):
    """Shared kwargs dict for export_scene.fbx, sourced from the game FBX
    settings on CPF_Settings. Object types come from the six checkboxes in
    the FBX Export Settings sub-section."""
    return dict(
        check_existing=False,
        object_types=_game_fbx_object_types_set(settings),
        # Transform
        global_scale=settings.game_fbx_global_scale,
        apply_unit_scale=settings.game_fbx_apply_unit_scale,
        apply_scale_options=settings.game_fbx_apply_scale_options,
        use_space_transform=settings.game_fbx_use_space_transform,
        bake_space_transform=settings.game_fbx_bake_space_transform,
        axis_forward=settings.game_fbx_axis_forward,
        axis_up=settings.game_fbx_axis_up,
        # Geometry
        use_mesh_modifiers=settings.game_fbx_use_mesh_modifiers,
        use_mesh_modifiers_render=settings.game_fbx_use_mesh_modifiers,
        mesh_smooth_type=settings.game_fbx_mesh_smooth_type,
        use_subsurf=settings.game_fbx_use_subsurf,
        use_mesh_edges=settings.game_fbx_use_mesh_edges,
        use_triangles=settings.game_fbx_use_triangles,
        use_tspace=settings.game_fbx_use_tspace,
        colors_type=settings.game_fbx_colors_type,
        prioritize_active_color=settings.game_fbx_prioritize_active_color,
        # Armature
        primary_bone_axis=settings.game_fbx_primary_bone_axis,
        secondary_bone_axis=settings.game_fbx_secondary_bone_axis,
        armature_nodetype=settings.game_fbx_armature_nodetype,
        use_armature_deform_only=settings.game_fbx_use_armature_deform_only,
        add_leaf_bones=settings.game_fbx_add_leaf_bones,
        # Animation — master gate AND'd with the existing bake-anim toggle
        # so unchecking 'Export Animations' fully disables animation export.
        bake_anim=(settings.game_fbx_export_animations
                   and settings.game_fbx_bake_anim),
        bake_anim_use_all_bones=settings.game_fbx_bake_anim_use_all_bones,
        bake_anim_use_nla_strips=settings.game_fbx_bake_anim_use_nla_strips,
        bake_anim_use_all_actions=settings.game_fbx_bake_anim_use_all_actions,
        bake_anim_force_startend_keying=settings.game_fbx_bake_anim_force_startend_keying,
        bake_anim_step=settings.game_fbx_bake_anim_step,
        bake_anim_simplify_factor=settings.game_fbx_bake_anim_simplify_factor,
        # Other
        use_custom_props=settings.game_fbx_use_custom_props,
        embed_textures=settings.game_fbx_embed_textures,
        use_metadata=settings.game_fbx_use_metadata,
    )


def _find_associated_armatures(mesh_obj):
    """Return the list of armature objects driving `mesh_obj` via either
    parenting (parent is an Armature) or an Armature modifier with an
    armature object set. Deduplicated, parents first."""
    found = []
    seen = set()
    if mesh_obj.parent and mesh_obj.parent.type == "ARMATURE":
        if mesh_obj.parent.name not in seen:
            seen.add(mesh_obj.parent.name)
            found.append(mesh_obj.parent)
    for m in mesh_obj.modifiers:
        if m.type == "ARMATURE":
            arm = getattr(m, "object", None)
            if arm is not None and arm.type == "ARMATURE" and arm.name not in seen:
                seen.add(arm.name)
                found.append(arm)
    return found


class CPF_OT_ExportGameCollection(Operator):
    """Batch-export every MESH object in the selected collection (plus its
associated armature, if any) as a separate .fbx file into the Export Path"""
    bl_idname = "cpf.export_game_collection"
    bl_label = "Export Collection"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.cpf_settings

        coll = settings.game_export_collection
        if not coll:
            self.report({"ERROR"}, "Select a collection first")
            return {"CANCELLED"}

        out_dir_raw = settings.game_export_path.strip()
        if not out_dir_raw:
            self.report({"ERROR"}, "Set the Export Path first")
            return {"CANCELLED"}
        out_dir = bpy.path.abspath(out_dir_raw)
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            self.report({"ERROR"}, f"Could not create export directory: {e}")
            return {"CANCELLED"}

        mesh_objs = [o for o in coll.objects if o.type == "MESH"]
        if not mesh_objs:
            self.report({"ERROR"}, f"No MESH objects in '{coll.name}'")
            return {"CANCELLED"}

        original_selection = list(context.selected_objects)
        original_active = context.view_layer.objects.active
        fbx_kwargs = _game_fbx_kwargs(settings)

        exported = []
        try:
            for mesh_obj in mesh_objs:
                armatures = _find_associated_armatures(mesh_obj)

                try:
                    bpy.ops.object.select_all(action="DESELECT")
                except RuntimeError:
                    pass

                # Make sure every armature referenced by an Armature modifier
                # on this mesh is also in the export selection (otherwise the
                # FBX exporter sees a binding to an absent rig and drops it).
                pair_objs = {mesh_obj.name: mesh_obj}
                for arm in armatures:
                    pair_objs.setdefault(arm.name, arm)
                for m in mesh_obj.modifiers:
                    if m.type == "ARMATURE":
                        arm_obj = getattr(m, "object", None)
                        if arm_obj is not None and arm_obj.type == "ARMATURE":
                            pair_objs.setdefault(arm_obj.name, arm_obj)

                # Force-enable Armature-modifier viewport+render visibility for
                # the duration of the export so the FBX exporter recognizes
                # the modifier and exports the skin binding (instead of
                # baking the deformed mesh and dropping the rig link).
                # Saved state is restored in the inner finally below.
                arm_mod_states = []
                for m in mesh_obj.modifiers:
                    if m.type == "ARMATURE":
                        arm_mod_states.append(
                            (m, m.show_viewport, m.show_render),
                        )
                        try:
                            m.show_viewport = True
                            m.show_render = True
                        except (RuntimeError, ReferenceError):
                            pass

                for o in pair_objs.values():
                    try:
                        o.select_set(True)
                    except RuntimeError:
                        pass
                context.view_layer.objects.active = mesh_obj

                target_path = Path(out_dir) / f"{mesh_obj.name}.fbx"
                try:
                    bpy.ops.export_scene.fbx(
                        filepath=str(target_path),
                        use_selection=True,
                        **fbx_kwargs,
                    )
                    exported.append(target_path)
                except Exception as e:
                    self.report(
                        {"WARNING"},
                        f"Failed to export '{mesh_obj.name}': {e}",
                    )
                finally:
                    # Restore Armature-modifier visibility regardless of
                    # whether the export succeeded.
                    for m, vis, ren in arm_mod_states:
                        try:
                            m.show_viewport = vis
                            m.show_render = ren
                        except (RuntimeError, ReferenceError):
                            pass
        finally:
            try:
                bpy.ops.object.select_all(action="DESELECT")
            except RuntimeError:
                pass
            for o in original_selection:
                try:
                    o.select_set(True)
                except (RuntimeError, ReferenceError):
                    pass
            try:
                context.view_layer.objects.active = original_active
            except (RuntimeError, ReferenceError):
                pass

        self.report(
            {"INFO"},
            f"Exported {len(exported)} asset(s) to {out_dir}",
        )
        return {"FINISHED"}


class CPF_OT_OpenGameExportExplorer(Operator):
    """Open the Export Path directory in the system file explorer.
Cross-platform (Windows / macOS / Linux)"""
    bl_idname = "cpf.open_game_export_explorer"
    bl_label = "Open in Explorer"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.cpf_settings
        path_raw = settings.game_export_path.strip()
        if not path_raw:
            self.report({"ERROR"}, "Export Path is empty")
            return {"CANCELLED"}
        path = bpy.path.abspath(path_raw)
        if not os.path.isdir(path):
            self.report({"ERROR"}, f"Path does not exist: {path}")
            return {"CANCELLED"}
        try:
            if sys.platform == "win32":
                subprocess.Popen(f'explorer "{path}"')
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                # Linux + other Unixes — xdg-open is the standard handler.
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            self.report({"ERROR"}, f"Could not open file explorer: {e}")
            return {"CANCELLED"}
        return {"FINISHED"}


class CPF_OT_ExportGameSelection(Operator):
    """Export the currently selected MESH/ARMATURE objects as a single .fbx
file into the Export Path. Filename is the active object's name, or
'selection.fbx' if there is no active object"""
    bl_idname = "cpf.export_game_selection"
    bl_label = "Export Selection"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.cpf_settings

        selected = list(context.selected_objects)
        # Filter the selection to whatever object types the FBX Export
        # Settings checkboxes have enabled. 'OTHER' covers anything not in
        # the explicit five Blender object types (curves, surfaces, fonts,
        # metas, grease-pencils, etc).
        types = _game_fbx_object_types_set(settings)
        explicit = {"EMPTY", "CAMERA", "LIGHT", "ARMATURE", "MESH"}
        def _match(o):
            if o.type in types:
                return True
            if "OTHER" in types and o.type not in explicit:
                return True
            return False
        valid = [o for o in selected if _match(o)]
        if not valid:
            self.report(
                {"ERROR"},
                "No selected object matches the enabled Object Types in the FBX Export Settings",
            )
            return {"CANCELLED"}

        out_dir_raw = settings.game_export_path.strip()
        if not out_dir_raw:
            self.report({"ERROR"}, "Set the Export Path first")
            return {"CANCELLED"}
        out_dir = bpy.path.abspath(out_dir_raw)
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            self.report({"ERROR"}, f"Could not create export directory: {e}")
            return {"CANCELLED"}

        active = context.view_layer.objects.active
        name = (active.name if active is not None and active in valid
                else "selection")
        target_path = Path(out_dir) / f"{name}.fbx"

        fbx_kwargs = _game_fbx_kwargs(settings)

        # Force-enable every Armature-modifier on every selected mesh for
        # the duration of the export so the FBX exporter recognizes the
        # bindings (instead of baking the deformed mesh and dropping the
        # rig link). State is saved + restored.
        arm_mod_states = []
        for o in valid:
            if o.type != "MESH":
                continue
            for m in o.modifiers:
                if m.type == "ARMATURE":
                    arm_mod_states.append(
                        (m, m.show_viewport, m.show_render),
                    )
                    try:
                        m.show_viewport = True
                        m.show_render = True
                    except (RuntimeError, ReferenceError):
                        pass

        try:
            bpy.ops.export_scene.fbx(
                filepath=str(target_path),
                use_selection=True,
                **fbx_kwargs,
            )
        except Exception as e:
            self.report({"ERROR"}, f"Export failed: {e}")
            return {"CANCELLED"}
        finally:
            for m, vis, ren in arm_mod_states:
                try:
                    m.show_viewport = vis
                    m.show_render = ren
                except (RuntimeError, ReferenceError):
                    pass

        self.report({"INFO"}, f"Exported selection to {target_path}")
        return {"FINISHED"}


# ── Naming presets (Folder Name + four suffixes) ──────────────────────────────
# Same convention as the FBX preset system above.
_NAMING_PRESET_SUBDIR = "create_project_folders/naming"
_NAMING_DEFAULT_PRESET_NAME = "Default"

_NAMING_PRESET_VALUES = (
    "settings.export_folder_name",
    "settings.export_suffix_low",
    "settings.export_suffix_cage",
    "settings.export_suffix_trans",
    "settings.export_suffix_painter",
)


def _ensure_default_naming_preset():
    """Write/refresh a 'Default.py' preset file mirroring the addon's built-in
    naming defaults (Folder Name + four suffixes), so users can always return
    to them. Rewritten on every register so the preset stays in sync if
    defaults change."""
    preset_dir = bpy.utils.user_resource(
        "SCRIPTS", path="presets/" + _NAMING_PRESET_SUBDIR, create=True,
    )
    default_path = os.path.join(preset_dir, _NAMING_DEFAULT_PRESET_NAME + ".py")
    lines = [
        "import bpy",
        "settings = bpy.context.scene.cpf_settings",
        "",
        "settings.export_folder_name = 'low'",
        "settings.export_suffix_low = '_low'",
        "settings.export_suffix_cage = '_cage'",
        "settings.export_suffix_trans = '_trans'",
        "settings.export_suffix_painter = '_painter'",
    ]
    try:
        with open(default_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"[GameAssetUtility] Could not write default naming preset: {e}")


class CPF_MT_Naming_Presets(Menu):
    """Dropdown listing all naming presets (Folder Name + four suffixes)."""
    bl_label = "Naming Presets"
    preset_subdir = _NAMING_PRESET_SUBDIR
    preset_operator = "script.execute_preset"
    draw = Menu.draw_preset


class CPF_OT_NamingPresetAdd(AddPresetBase, Operator):
    """Save the current naming settings (Folder Name + four suffixes) as a
named preset, or remove the active preset (the 'Default' preset is protected
and cannot be removed)"""
    bl_idname = "cpf.naming_preset_add"
    bl_label = "Save Naming Preset"
    preset_menu = "CPF_MT_Naming_Presets"
    preset_subdir = _NAMING_PRESET_SUBDIR

    preset_defines = [
        "settings = bpy.context.scene.cpf_settings",
    ]
    preset_values = list(_NAMING_PRESET_VALUES)

    def execute(self, context):
        # Refuse to remove the protected Default preset
        if self.remove_active and not self.remove_name:
            menu_cls = getattr(bpy.types, self.preset_menu, None)
            if menu_cls is not None and menu_cls.bl_label == _NAMING_DEFAULT_PRESET_NAME:
                self.report(
                    {"WARNING"},
                    f"Cannot delete the '{_NAMING_DEFAULT_PRESET_NAME}' preset",
                )
                return {"CANCELLED"}
        return super().execute(context)


# ── Bake-modifier-stack presets ───────────────────────────────────────────────
# Same convention as the FBX / Naming preset systems above. The stack is a
# variable-length CollectionProperty so we override AddPresetBase.add() to
# write a custom preset file that rebuilds the collection.
_BAKEMODS_PRESET_SUBDIR = "create_project_folders/bake_modifiers"
_BAKEMODS_DEFAULT_PRESET_NAME = "Default"


def _build_bakemods_preset_source(stack_json, vis_low_json, vis_cage_json, vis_trans_json, vis_painter_json):
    """Build the Python source text for a bake-modifiers preset .py file.
    The file simply assigns the four JSON strings onto cpf_settings via
    `bpy.context.scene.cpf_settings.<prop> = '<json>'`. Loading is then a
    matter of executing this preset, then rebuilding the live
    bake_modifier_stack CollectionProperty from the JSON via
    `_stack_from_json` (called by an update callback / load handler)."""
    return (
        "import bpy\n"
        "import json\n"
        "settings = bpy.context.scene.cpf_settings\n"
        f"settings.bake_modifier_stack_json = {stack_json!r}\n"
        f"settings.vis_low_json = {vis_low_json!r}\n"
        f"settings.vis_cage_json = {vis_cage_json!r}\n"
        f"settings.vis_trans_json = {vis_trans_json!r}\n"
        f"settings.vis_painter_json = {vis_painter_json!r}\n"
        "# Rebuild the live CollectionProperty from the stack JSON. We import\n"
        "# the addon module explicitly because preset files are exec'd in a\n"
        "# bare namespace. The module name is captured at preset-write time\n"
        "# from the live __name__, so re-saving presets after a folder rename\n"
        "# (e.g. CreateProjectFolders → GameAssetUtility) keeps them working.\n"
        "import importlib\n"
        f"_cpf_mod = importlib.import_module({__name__!r})\n"
        "_cpf_mod._stack_from_json(settings.bake_modifier_stack, settings.bake_modifier_stack_json)\n"
        "settings.bake_modifier_stack_index = 0\n"
    )


def _default_bakemods_preset_source():
    """Source text for the built-in 'Default' bake-modifiers preset. Mirrors
    the original 5-entry hardcoded stack and historical per-pass visibility."""
    stack = [
        {"type": "TRIANGULATE", "name": "Triangulate",
         "settings": {"quad_method": "SHORTEST_DIAGONAL", "ngon_method": "BEAUTY"}},
        {"type": "SUBSURF", "name": "Subdivision",
         "settings": {"subdivision_type": "SIMPLE", "levels": 1, "render_levels": 1}},
        {"type": "TRIANGULATE", "name": "Triangulate.001",
         "settings": {"quad_method": "FIXED", "ngon_method": "BEAUTY"}},
        {"type": "DISPLACE", "name": "Displace",
         "settings": {"direction": "NORMAL", "strength": 1.0, "mid_level": 0.0}},
        {"type": "EDGE_SPLIT", "name": "EdgeSplit",
         "settings": {"use_edge_angle": False, "use_edge_sharp": True}},
    ]
    return _build_bakemods_preset_source(
        json.dumps(stack),
        json.dumps([True,  True,  True,  False, True]),   # _low
        json.dumps([True,  True,  True,  True,  True]),   # _cage
        json.dumps([True,  False, False, False, True]),   # _trans
        json.dumps([True,  False, False, False, True]),   # _painter
    )


def _ensure_default_bakemods_preset():
    """Write/refresh a 'Default.py' preset file with the built-in 5-entry
    stack and historical per-pass visibility. Rewritten on every register
    so the file stays in sync if defaults change."""
    preset_dir = bpy.utils.user_resource(
        "SCRIPTS", path="presets/" + _BAKEMODS_PRESET_SUBDIR, create=True,
    )
    default_path = os.path.join(preset_dir, _BAKEMODS_DEFAULT_PRESET_NAME + ".py")
    try:
        with open(default_path, "w", encoding="utf-8") as f:
            f.write(_default_bakemods_preset_source())
    except Exception as e:
        print(f"[GameAssetUtility] Could not write default bake modifiers preset: {e}")


class CPF_MT_BakeMods_Presets(Menu):
    """Dropdown listing all bake modifier stack presets."""
    bl_label = "Bake Modifier Presets"
    preset_subdir = _BAKEMODS_PRESET_SUBDIR
    preset_operator = "script.execute_preset"
    draw = Menu.draw_preset


class CPF_OT_BakeModsPresetAdd(AddPresetBase, Operator):
    """Save the current bake modifier stack as a named preset, or remove the
active preset (the 'Default' preset is protected and cannot be removed)"""
    bl_idname = "cpf.bake_mods_preset_add"
    bl_label = "Save Bake Modifiers Preset"
    preset_menu = "CPF_MT_BakeMods_Presets"
    preset_subdir = _BAKEMODS_PRESET_SUBDIR

    # Required by AddPresetBase but unused — we override add() to write the
    # variable-length collection ourselves.
    preset_defines = []
    preset_values = []

    def add(self, context, filepath):
        settings = context.scene.cpf_settings
        # Make sure the JSON snapshot reflects the current Collection state
        # before writing the preset file.
        settings.bake_modifier_stack_json = _stack_to_json(settings.bake_modifier_stack)
        source = _build_bakemods_preset_source(
            settings.bake_modifier_stack_json,
            settings.vis_low_json or "[]",
            settings.vis_cage_json or "[]",
            settings.vis_trans_json or "[]",
            settings.vis_painter_json or "[]",
        )
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(source)
        except Exception as e:
            self.report({"ERROR"}, f"Could not write preset: {e}")

    def execute(self, context):
        # Refuse to remove the protected Default preset
        if self.remove_active and not self.remove_name:
            menu_cls = getattr(bpy.types, self.preset_menu, None)
            if menu_cls is not None and menu_cls.bl_label == _BAKEMODS_DEFAULT_PRESET_NAME:
                self.report(
                    {"WARNING"},
                    f"Cannot delete the '{_BAKEMODS_DEFAULT_PRESET_NAME}' preset",
                )
                return {"CANCELLED"}
        return super().execute(context)


class CPF_OT_PackageZip(Operator):
    """Create a distributable GameAssetUtility.zip in the configured ZIP
output folder, structured as a Blender 5.1 extension package.

The zip is FLAT: blender_manifest.toml and every .py file sit at the root
of the archive (no nesting subfolder), as required by Blender's extension
installer (Edit > Preferences > Get Extensions > Install from Disk)"""
    bl_idname = "cpf.package_zip"
    bl_label = "Package as .zip"
    bl_options = {"REGISTER"}

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        addon_dir = os.path.dirname(os.path.abspath(__file__))
        out_dir = bpy.path.abspath(prefs.zip_path.strip()) if prefs.zip_path.strip() else os.path.dirname(addon_dir)
        zip_path = os.path.join(out_dir, "GameAssetUtility.zip")

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # blender_manifest.toml MUST be present at the root of the archive
            # for the extension installer to recognize the package.
            manifest_path = os.path.join(addon_dir, "blender_manifest.toml")
            if os.path.isfile(manifest_path):
                zf.write(manifest_path, "blender_manifest.toml")
            # Every .py module sits at the root alongside the manifest —
            # the extension format expects a flat layout, no nesting folder.
            for fname in os.listdir(addon_dir):
                if fname.endswith(".py"):
                    zf.write(os.path.join(addon_dir, fname), fname)

        self.report({"INFO"}, f"Saved: {zip_path}")
        return {"FINISHED"}


# ── Reload helpers ────────────────────────────────────────────────────────────

def _do_reload(module_name):
    module = sys.modules.get(module_name)
    if module and hasattr(module, "unregister") and hasattr(module, "register"):
        try:
            module.unregister()
            importlib.reload(module)
            module.register()
        except Exception as e:
            print(f"[GameAssetUtility] Reload error: {e}")
    return None


class CPF_OT_ReloadAddon(Operator):
    """Hot-reload this addon from disk without restarting Blender"""
    bl_idname = "cpf.reload_addon"
    bl_label = "Reload"
    bl_options = {"REGISTER"}

    def execute(self, context):
        mn = __name__
        bpy.app.timers.register(lambda: _do_reload(mn), first_interval=0.05)
        self.report({"INFO"}, "Reloading…")
        return {"FINISHED"}


class CPF_OT_UpdateAddon(Operator):
    """Copy all .py files from the Source Path into the installed addon folder, then hot-reload"""
    bl_idname = "cpf.update_addon"
    bl_label = "Update & Reload"
    bl_options = {"REGISTER"}

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        src = bpy.path.abspath(prefs.source_path.strip()).rstrip("\\/")

        if not src or not os.path.isdir(src):
            self.report({"ERROR"}, f"Source folder not found: {src!r} — check Preferences")
            return {"CANCELLED"}

        dst = os.path.dirname(os.path.abspath(__file__))

        if os.path.normpath(src) != os.path.normpath(dst):
            copied = []
            for fname in os.listdir(src):
                if fname.endswith(".py"):
                    shutil.copy2(os.path.join(src, fname), os.path.join(dst, fname))
                    copied.append(fname)
            if not copied:
                self.report({"WARNING"}, "No .py files found in source folder")
                return {"CANCELLED"}

        mn = __name__
        bpy.app.timers.register(lambda: _do_reload(mn), first_interval=0.05)
        self.report({"INFO"}, "Files copied — reloading…")
        return {"FINISHED"}


# ── N-Panel ───────────────────────────────────────────────────────────────────

# ── Each main section gets its own Blender Panel — same N-panel tab,
# independent collapsible frame, ordered top-to-bottom via bl_order.
# All panels share the same CPF_Settings PropertyGroup (Scene-attached) and
# the same module-level caches; no per-panel state is duplicated.
# Collapsibility uses bl_options={'DEFAULT_CLOSED'} so each panel starts
# collapsed (matching the previous default of show_* = False).
# NOTE: Panel draws must be read-only — creating data or writing custom
# properties from inside draw() silently breaks the panel. State is
# initialised in register() / _auto_init() / operators.

_PT_BASE = {
    "bl_space_type": "VIEW_3D",
    "bl_region_type": "UI",
    "bl_category": "GAU",
    "bl_options": {"DEFAULT_CLOSED"},
}


class CPF_PT_ExportGameAssets(Panel):
    bl_label = "Export Game Assets"
    bl_idname = "CPF_PT_export_game_assets"
    bl_space_type = _PT_BASE["bl_space_type"]
    bl_region_type = _PT_BASE["bl_region_type"]
    bl_category = _PT_BASE["bl_category"]
    bl_options = _PT_BASE["bl_options"]
    bl_order = 1

    def draw(self, context):
        layout = self.layout
        settings = context.scene.cpf_settings
        body = layout.column()

        # ── Always-visible: action buttons + collection + export path ─────
        box = body.box()
        row = box.row()
        row.scale_y = 1.4
        row.operator("cpf.export_game_collection", icon="EXPORT")
        row = box.row()
        row.scale_y = 1.4
        row.operator("cpf.export_game_selection", icon="RESTRICT_SELECT_OFF")
        box.prop(settings, "game_export_collection", text="Collection")
        box.prop(settings, "game_export_path", text="Export Path")
        row = box.row()
        row.operator(
            "cpf.open_game_export_explorer",
            icon="FILE_FOLDER",
        )

        # ── Game FBX preset row — mirrors the other preset systems ────────
        preset_row = body.row(align=True)
        preset_row.menu(
            "CPF_MT_GameFBX_Presets",
            text=CPF_MT_GameFBX_Presets.bl_label,
        )
        preset_row.operator(
            "cpf.game_fbx_preset_add", text="", icon="ADD",
        )
        preset_row.operator(
            "cpf.game_fbx_preset_add", text="", icon="REMOVE",
        ).remove_active = True

        body.separator(factor=1.0)

        # ── Sub-section: FBX Export Settings (collapsible) ────────────────
        sub_header = body.row()
        sub_header.prop(
            settings, "show_game_fbx_settings",
            icon="TRIA_DOWN" if settings.show_game_fbx_settings else "TRIA_RIGHT",
            icon_only=True, emboss=False,
        )
        sub_header.label(text="FBX Export Settings", icon="SETTINGS")

        if settings.show_game_fbx_settings:
            box = body.box()

            # Include
            col = box.column(align=True, heading="Include")
            col.prop(settings, "game_fbx_use_custom_props", text="Custom Properties")
            # Object Types — six independent checkboxes, mirrors Blender's
            # native FBX exporter Object Types multi-select.
            col = box.column(align=True, heading="Object Types")
            col.prop(settings, "game_fbx_obj_empty",    text="Empty")
            col.prop(settings, "game_fbx_obj_camera",   text="Camera")
            col.prop(settings, "game_fbx_obj_lamp",     text="Lamp")
            col.prop(settings, "game_fbx_obj_armature", text="Armature")
            col.prop(settings, "game_fbx_obj_mesh",     text="Mesh")
            col.prop(settings, "game_fbx_obj_other",    text="Other")

            # Transform
            box.separator()
            box.label(text="Transform")
            col = box.column(align=True)
            col.prop(settings, "game_fbx_global_scale", text="Scale")
            col.prop(settings, "game_fbx_apply_scale_options", text="Apply Scalings")
            col.prop(settings, "game_fbx_axis_forward", text="Forward")
            col.prop(settings, "game_fbx_axis_up", text="Up")
            col = box.column(align=True, heading="Apply")
            col.prop(settings, "game_fbx_apply_unit_scale", text="Unit")
            col.prop(settings, "game_fbx_use_space_transform", text="Use Space Transform")
            col.prop(settings, "game_fbx_bake_space_transform", text="Apply Transform")

            # Geometry
            box.separator()
            box.label(text="Geometry")
            col = box.column(align=True)
            col.prop(settings, "game_fbx_mesh_smooth_type", text="Smoothing")
            col.prop(settings, "game_fbx_use_subsurf", text="Export Subdivision Surface")
            col.prop(settings, "game_fbx_use_mesh_modifiers", text="Apply Modifiers")
            col.prop(settings, "game_fbx_use_mesh_edges", text="Loose Edges")
            col.prop(settings, "game_fbx_use_triangles", text="Triangulate Faces")
            col.prop(settings, "game_fbx_use_tspace", text="Tangent Space")
            col.prop(settings, "game_fbx_colors_type", text="Vertex Colors")
            col.prop(settings, "game_fbx_prioritize_active_color", text="Prioritize Active Color")

            # Armature
            box.separator()
            box.label(text="Armature")
            col = box.column(align=True)
            col.prop(settings, "game_fbx_primary_bone_axis", text="Primary Bone Axis")
            col.prop(settings, "game_fbx_secondary_bone_axis", text="Secondary Bone Axis")
            col.prop(settings, "game_fbx_armature_nodetype", text="Armature FBXNode Type")
            col.prop(settings, "game_fbx_use_armature_deform_only", text="Only Deform Bones")
            col.prop(settings, "game_fbx_add_leaf_bones", text="Add Leaf Bones")

            # Animation
            box.separator()
            box.label(text="Animation")
            col = box.column(align=True)
            col.prop(settings, "game_fbx_export_animations", text="Export Animations")
            anim_sub = col.column(align=True)
            anim_sub.enabled = settings.game_fbx_export_animations
            anim_sub.prop(settings, "game_fbx_bake_anim", text="Baked Animation")
            sub = anim_sub.column(align=True)
            sub.enabled = settings.game_fbx_bake_anim
            sub.prop(settings, "game_fbx_bake_anim_use_all_bones", text="Key All Bones")
            sub.prop(settings, "game_fbx_bake_anim_use_nla_strips", text="NLA Strips")
            sub.prop(settings, "game_fbx_bake_anim_use_all_actions", text="All Actions")
            sub.prop(settings, "game_fbx_bake_anim_force_startend_keying",
                     text="Force Start/End Keying")
            sub.prop(settings, "game_fbx_bake_anim_step", text="Sampling Rate")
            sub.prop(settings, "game_fbx_bake_anim_simplify_factor", text="Simplify")

            # Other
            box.separator()
            box.label(text="Other")
            col = box.column(align=True)
            col.prop(settings, "game_fbx_embed_textures", text="Embed Textures")
            col.prop(settings, "game_fbx_use_metadata", text="Custom Metadata")


class CPF_PT_SetupFolderStructure(Panel):
    bl_label = "Setup Folder Structure"
    bl_idname = "CPF_PT_setup_folder_structure"
    bl_space_type = _PT_BASE["bl_space_type"]
    bl_region_type = _PT_BASE["bl_region_type"]
    bl_category = _PT_BASE["bl_category"]
    bl_options = _PT_BASE["bl_options"]
    bl_order = 2

    def _rebuild_lists(self, context):
        wm = context.window_manager
        settings = context.scene.cpf_settings

        # Structure: template lines with {assets} stripped from display
        wm.cpf_structure_paths.clear()
        if settings.text_block:
            for line in settings.text_block.as_string().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                item = wm.cpf_structure_paths.add()
                item.path = line.replace("{assets}", "").rstrip("\\/")

        # Assets: one entry per asset item
        wm.cpf_resolved_paths.clear()
        for a in context.scene.cpf_assets:
            if a.name.strip():
                item = wm.cpf_resolved_paths.add()
                item.path = a.name
                item.is_header = True

    def draw(self, context):
        layout = self.layout
        settings = context.scene.cpf_settings
        wm = context.window_manager
        scene = context.scene

        self._rebuild_lists(context)
        body = layout.column()

        # ── Root folder hint ──────────────────────────────────────────────
        if bpy.data.filepath:
            folder = str(Path(bpy.data.filepath).parent)
            box = body.box()
            col = box.column(align=True)
            col.scale_y = 0.65
            col.label(text="Root folder:", icon="FILE_BLEND")
            col.label(text=folder)
        else:
            row = body.row()
            row.alert = True
            row.label(text="Save your .blend file first", icon="ERROR")

        body.separator(factor=1.0)

        # ── Assets list ───────────────────────────────────────────────────
        body.label(text="Assets:", icon="OBJECT_DATA")
        row = body.row()
        row.template_list(
            "CPF_UL_assets", "",
            scene, "cpf_assets",
            settings, "asset_index",
            rows=3,
        )
        col = row.column(align=True)
        col.operator("cpf.add_asset", icon="ADD", text="")
        col.operator("cpf.remove_asset", icon="REMOVE", text="")

        body.separator(factor=1.5)

        # ── Template library (auto-loads on change) ───────────────────────
        body.label(text="Template Library:", icon="ASSET_MANAGER")
        row = body.row(align=True)
        row.prop(settings, "active_template", text="")
        row.operator("cpf.delete_template", text="", icon="TRASH")

        body.separator(factor=1.5)

        # ── Active structure ──────────────────────────────────────────────
        body.label(text="Active Structure:", icon="TEXT")
        row = body.row()
        row.scale_y = 1.6
        row.operator("cpf.open_text_editor", icon="TEXT")

        # ── Two-column preview ────────────────────────────────────────────
        split = body.split(factor=0.5, align=True)

        col_l = split.column(align=True)
        col_l.label(text="Structure:", icon="LINENUMBERS_OFF")
        if wm.cpf_structure_paths:
            col_l.template_list(
                "CPF_UL_paths", "structure",
                wm, "cpf_structure_paths",
                wm, "cpf_structure_index",
                rows=3,
            )
        else:
            col_l.label(text="—", icon="INFO")

        col_r = split.column(align=True)
        col_r.label(text="Assets:", icon="OBJECT_DATA")
        if wm.cpf_resolved_paths:
            col_r.template_list(
                "CPF_UL_paths", "resolved",
                wm, "cpf_resolved_paths",
                wm, "cpf_resolved_index",
                rows=3,
            )
        else:
            col_r.label(text="—", icon="INFO")

        body.separator(factor=1.5)

        # ── Save to library ───────────────────────────────────────────────
        box = body.box()
        box.label(text="Save to Library:", icon="BOOKMARKS")
        row = box.row(align=True)
        row.prop(settings, "save_name", text="")
        row.operator("cpf.save_template", text="", icon="FILE_TICK")

        body.separator(factor=1.5)

        # ── Action buttons ────────────────────────────────────────────────
        col = body.column(align=True)
        col.scale_y = 1.6
        col.operator("cpf.create_folders", icon="NEWFOLDER")
        col.operator("cpf.open_explorer", icon="FILE_FOLDER")


class CPF_PT_SetupBakeAssets(Panel):
    bl_label = "Setup Bake Assets"
    bl_idname = "CPF_PT_setup_bake_assets"
    bl_space_type = _PT_BASE["bl_space_type"]
    bl_region_type = _PT_BASE["bl_region_type"]
    bl_category = _PT_BASE["bl_category"]
    bl_options = _PT_BASE["bl_options"]
    bl_order = 3

    def draw(self, context):
        layout = self.layout
        settings = context.scene.cpf_settings
        body = layout.column()

        # ── Shared collection picker ──────────────────────────────────────
        box = body.box()
        box.prop(settings, "bake_collection", text="Collection")

        body.separator(factor=1.0)

        # ── Sub-section: Rename Mesh Objects (always visible) ─────────────
        body.label(text="Rename Mesh Objects", icon="OUTLINER_OB_MESH")
        box = body.box()
        box.prop(settings, "bake_prefix", text="Prefix")
        box.prop(settings, "mesh_suffix", text="Suffix")
        row = box.row()
        row.scale_y = 1.4
        row.operator("cpf.rename_mesh_objects", icon="SORTALPHA")

        body.separator(factor=1.0)

        # ── Sub-section: Add Bake Modifiers (always visible) ──────────────
        # All four modifier-related elements (preset row, Add Bake Modifiers
        # button, modifier stack collapsible, CAGE vertex-group row) are
        # wrapped in a single outer body.box() so they form one visually
        # unified group, mirroring the Rename Mesh Objects pattern above
        # (label header outside, everything else inside the box).
        body.label(text="Add Bake Modifiers", icon="MODIFIER")
        box = body.box()

        # 1. Preset row — mirrors the FBX / Naming preset UI
        preset_row = box.row(align=True)
        preset_row.menu(
            "CPF_MT_BakeMods_Presets",
            text=CPF_MT_BakeMods_Presets.bl_label,
        )
        preset_row.operator(
            "cpf.bake_mods_preset_add", text="", icon="ADD",
        )
        preset_row.operator(
            "cpf.bake_mods_preset_add", text="", icon="REMOVE",
        ).remove_active = True

        # 2. Add Bake Modifiers button (always visible, above the stack)
        btn_box = box.box()
        row = btn_box.row()
        row.scale_y = 1.4
        row.operator("cpf.add_bake_modifiers", icon="MODIFIER_ON")

        # 3. Modifier stack collapsible
        stack_header = box.row()
        stack_header.prop(
            settings, "show_bake_modifier_stack",
            icon="TRIA_DOWN" if settings.show_bake_modifier_stack else "TRIA_RIGHT",
            icon_only=True, emboss=False,
        )
        stack_header.label(text="Modifier Stack")

        if settings.show_bake_modifier_stack:
            stack_box = box.box()

            # Add-modifier row: categorized columns Menu (mirrors
            # Blender's native Add Modifier menu) + a refresh-cache
            # button for picking up modifiers registered by third-party
            # addons after this addon loaded.
            add_row = stack_box.row(align=True)
            add_row.menu("CPF_MT_BakeModAdd", icon="ADD")
            add_row.operator(
                "cpf.bake_mod_refresh_cache",
                text="", icon="FILE_REFRESH",
            )

            stack = settings.bake_modifier_stack
            if len(stack) == 0:
                stack_box.label(text="(empty)", icon="INFO")
            else:
                for i, item in enumerate(stack):
                    entry_box = stack_box.box()

                    # Header: editable name field + type label + move/remove
                    h = entry_box.row(align=True)
                    h.prop(item, "modifier_name", text="", icon="MODIFIER")
                    h.label(text=_MODIFIER_DISPLAY_NAMES.get(
                        item.modifier_type, item.modifier_type,
                    ))
                    ctrls = h.row(align=True)
                    op_up = ctrls.operator(
                        "cpf.bake_mod_move", text="", icon="TRIA_UP",
                    )
                    op_up.index = i
                    op_up.direction = "UP"
                    op_dn = ctrls.operator(
                        "cpf.bake_mod_move", text="", icon="TRIA_DOWN",
                    )
                    op_dn.index = i
                    op_dn.direction = "DOWN"
                    op_rm = ctrls.operator(
                        "cpf.bake_mod_remove", text="", icon="X",
                    )
                    op_rm.index = i

                    # Per-type settings — every editable property cached
                    # at register time is drawn here via layout.prop on
                    # the dynamically-generated prefixed field.
                    descriptors = _MODIFIER_PROPERTY_CACHE.get(
                        item.modifier_type, [],
                    )
                    if not descriptors:
                        entry_box.label(
                            text="(applied with default settings)",
                            icon="INFO",
                        )
                    else:
                        col = entry_box.column(align=True)
                        for d in descriptors:
                            col.prop(
                                item, d["field_name"], text=d["name"],
                            )

        # 4. CAGE vertex-group helpers (side-by-side row, always visible)
        row = box.row(align=True)
        row.scale_y = 1.4
        row.operator("cpf.add_cage_vertex_group", icon="GROUP_VERTEX")
        row.operator("cpf.clear_cage_vertex_group", icon="X")

        body.separator(factor=1.0)

        # ── Sub-section: Set Vertex Color (always visible) ────────────────
        body.label(text="Set Vertex Color", icon="COLOR")
        box = body.box()
        row = box.row()
        row.scale_y = 1.4
        row.operator("cpf.set_vertex_color", icon="VPAINT_HLT")

        body.separator(factor=1.0)

        # ── Sub-section: Set Material (always visible) ────────────────────
        body.label(text="Set Material", icon="MATERIAL")
        box = body.box()
        box.prop(settings, "bake_material", text="Material")
        row = box.row()
        row.scale_y = 1.4
        row.operator("cpf.set_material", icon="MATERIAL_DATA")


class CPF_PT_ExportBakeAssets(Panel):
    bl_label = "Export Bake Assets"
    bl_idname = "CPF_PT_export_bake_assets"
    bl_space_type = _PT_BASE["bl_space_type"]
    bl_region_type = _PT_BASE["bl_region_type"]
    bl_category = _PT_BASE["bl_category"]
    bl_options = _PT_BASE["bl_options"]
    bl_order = 4

    def draw(self, context):
        layout = self.layout
        settings = context.scene.cpf_settings
        body = layout.column()

        # ── Group 1: Collection + Asset ───────────────────────────────────
        box = body.box()
        box.prop(settings, "export_collection", text="Collection")
        box.prop(settings, "export_asset", text="Asset")

        body.separator(factor=1.0)

        # ── Naming preset row — mirrors the FBX preset UI ─────────────────
        preset_row = body.row(align=True)
        preset_row.menu(
            "CPF_MT_Naming_Presets",
            text=CPF_MT_Naming_Presets.bl_label,
        )
        preset_row.operator(
            "cpf.naming_preset_add", text="", icon="ADD",
        )
        preset_row.operator(
            "cpf.naming_preset_add", text="", icon="REMOVE",
        ).remove_active = True

        body.separator(factor=1.0)

        # ── Outer collapsible: Export Options ─────────────────────────────
        # Wraps the entire area between the preset row above and the
        # Export Bake Assets button below — naming fields collapsible,
        # per-export modifier visibility lists, Space Preview Mesh Objects
        # checkbox, and Preview Mesh Object Gap field. Defaults to
        # collapsed; inner sub-collapsibles are independently toggleable.
        outer_hdr = body.row()
        outer_hdr.prop(
            settings, "show_export_bake_options",
            icon="TRIA_DOWN" if settings.show_export_bake_options else "TRIA_RIGHT",
            icon_only=True, emboss=False,
        )
        outer_hdr.label(text="Export Options")

        if settings.show_export_bake_options:
            # ── Group 2: Naming (collapsible) + visibility lists + spacing ────
            box = body.box()

            # Naming — collapsible sub-area that wraps Export Folder Name
            # and the four per-export suffix fields. Always-visible content
            # (visibility lists, Space Objects, Object Gap) sits below it.
            naming_hdr = box.row()
            naming_hdr.prop(
                settings, "show_naming_fields",
                icon="TRIA_DOWN" if settings.show_naming_fields else "TRIA_RIGHT",
                icon_only=True, emboss=False,
            )
            naming_hdr.label(text="Naming")
            if settings.show_naming_fields:
                naming_box = box.box()
                naming_box.prop(settings, "export_folder_name", text="Export Folder Name")
                naming_box.prop(settings, "export_suffix_low", text="Low Mesh")
                naming_box.prop(settings, "export_suffix_cage", text="Cage Mesh")
                naming_box.prop(settings, "export_suffix_trans", text="Transfer Mesh")
                naming_box.prop(settings, "export_suffix_painter", text="Preview Mesh")

            # Per-export modifier visibility lists — read from the active
            # bake_modifier_stack CollectionProperty + the four
            # `vis_<pass>_json` StringProperty values. Each list has its own
            # pass-identifying header now that the suffix field that used to
            # label it has moved into the Naming collapsible above.
            stack = settings.bake_modifier_stack

            def _draw_pass_visibility(box, label, show_attr, pass_key):
                hdr = box.row()
                hdr.prop(
                    settings, show_attr,
                    icon="TRIA_DOWN" if getattr(settings, show_attr) else "TRIA_RIGHT",
                    icon_only=True, emboss=False,
                )
                hdr.label(text=f"{label} Modifier Visibility")
                if getattr(settings, show_attr):
                    sub = box.box()
                    if len(stack) == 0:
                        sub.label(text="(no modifiers in stack)", icon="INFO")
                    else:
                        vis = _read_pass_visibility(settings, pass_key, len(stack))
                        for i, item in enumerate(stack):
                            row = sub.row(align=True)
                            row.label(
                                text=f"{item.modifier_name}  ({item.modifier_type})",
                                icon="MODIFIER_DATA",
                            )
                            visible = bool(vis[i]) if i < len(vis) else True
                            op = row.operator(
                                "cpf.toggle_export_mod_visibility",
                                text="",
                                icon="RESTRICT_VIEW_OFF" if visible else "RESTRICT_VIEW_ON",
                                emboss=False,
                            )
                            op.modifier_index = i
                            op.pass_key = pass_key

            _draw_pass_visibility(box, "Low Mesh",     "show_low_visibility",     "low")
            _draw_pass_visibility(box, "Cage Mesh",    "show_cage_visibility",    "cage")
            _draw_pass_visibility(box, "Transfer Mesh", "show_trans_visibility",   "trans")
            _draw_pass_visibility(box, "Preview Mesh", "show_painter_visibility", "painter")

            box.prop(settings, "space_objects", text="Space Preview Mesh Objects")
            box.prop(settings, "object_gap", text="Preview Mesh Object Gap")

        body.separator(factor=1.0)

        # ── Group 3: Export button ────────────────────────────────────────
        box = body.box()
        row = box.row()
        row.scale_y = 1.4
        row.operator("cpf.export_bake_assets", icon="EXPORT")

        body.separator(factor=1.0)

        # ── Sub-section: FBX Export Settings (collapsible) ────────────────
        sub_header = body.row()
        sub_header.prop(
            settings, "show_fbx_settings",
            icon="TRIA_DOWN" if settings.show_fbx_settings else "TRIA_RIGHT",
            icon_only=True, emboss=False,
        )
        sub_header.label(text="FBX Export Settings", icon="SETTINGS")

        if settings.show_fbx_settings:
            box = body.box()

            # Preset row — mirrors Blender's native exporter preset UI
            preset_row = box.row(align=True)
            preset_row.menu(
                "CPF_MT_FBX_Presets", text=CPF_MT_FBX_Presets.bl_label,
            )
            preset_row.operator(
                "cpf.fbx_preset_add", text="", icon="ADD",
            )
            preset_row.operator(
                "cpf.fbx_preset_add", text="", icon="REMOVE",
            ).remove_active = True

            box.separator()

            # Include
            col = box.column(align=True, heading="Include")
            col.prop(settings, "fbx_use_custom_props", text="Custom Properties")

            # Transform
            box.separator()
            box.label(text="Transform")
            col = box.column(align=True)
            col.prop(settings, "fbx_global_scale", text="Scale")
            col.prop(settings, "fbx_apply_scale_options", text="Apply Scalings")
            col.prop(settings, "fbx_axis_forward", text="Forward")
            col.prop(settings, "fbx_axis_up", text="Up")
            col = box.column(align=True, heading="Apply")
            col.prop(settings, "fbx_apply_unit_scale", text="Unit")
            col.prop(settings, "fbx_use_space_transform", text="Use Space Transform")
            col.prop(settings, "fbx_bake_space_transform", text="Apply Transform")

            # Geometry
            box.separator()
            box.label(text="Geometry")
            col = box.column(align=True)
            col.prop(settings, "fbx_mesh_smooth_type", text="Smoothing")
            col.prop(settings, "fbx_use_subsurf", text="Export Subdivision Surface")
            col.prop(settings, "fbx_use_mesh_modifiers", text="Apply Modifiers")
            col.prop(settings, "fbx_use_mesh_edges", text="Loose Edges")
            col.prop(settings, "fbx_use_triangles", text="Triangulate Faces")
            col.prop(settings, "fbx_use_tspace", text="Tangent Space")
            col.prop(settings, "fbx_colors_type", text="Vertex Colors")
            col.prop(settings, "fbx_prioritize_active_color", text="Prioritize Active Color")

            # Other
            box.separator()
            box.label(text="Other")
            col = box.column(align=True)
            col.prop(settings, "fbx_embed_textures", text="Embed Textures")
            col.prop(settings, "fbx_use_metadata", text="Custom Metadata")


# ── Auto-initialisation ───────────────────────────────────────────────────────

def _auto_init():
    """Load the first folder-structure template into temp_structure if no
    text block is set yet, and populate the default bake modifier stack +
    per-pass visibility JSON if the stack is empty."""
    try:
        scene = bpy.context.scene
        if not scene:
            return None

        settings = scene.cpf_settings

        # Populate the default bake modifier stack + visibility JSONs on
        # first run (idempotent — only fills if the stack is empty).
        _populate_default_modifier_stack(settings)
        # Keep the JSON snapshot in sync with the live Collection.
        settings.bake_modifier_stack_json = _stack_to_json(settings.bake_modifier_stack)

        # Auto-load the first template if no text block is bound yet
        if not settings.text_block:
            templates = load_templates()
            if templates:
                first_name, content = next(iter(templates.items()))
                text = bpy.data.texts.get("temp_structure") or bpy.data.texts.new("temp_structure")
                text.clear()
                text.write(content)
                settings.text_block = text
                settings.save_name = first_name
    except Exception as e:
        print(f"[GameAssetUtility] Auto-init error: {e}")
    return None


@bpy.app.handlers.persistent
def _load_post_handler(_):
    bpy.app.timers.register(_auto_init, first_interval=0.1)


# ── Register ──────────────────────────────────────────────────────────────────

classes = (
    CPF_Preferences,
    CPF_AssetItem,
    CPF_UL_Assets,
    CPF_PathItem,
    CPF_UL_Paths,
    # CPF_BakeModifierItem is generated dynamically in register() — do NOT
    # register it via this tuple.
    CPF_Settings,
    CPF_OT_AddAsset,
    CPF_OT_RemoveAsset,
    CPF_OT_NewStructure,
    CPF_OT_SaveTemplate,
    CPF_OT_DeleteTemplate,
    CPF_OT_OpenTextEditor,
    CPF_OT_CreateFolders,
    CPF_OT_OpenExplorer,
    CPF_OT_RenameMeshObjects,
    CPF_OT_AddBakeModifiers,
    CPF_OT_BakeModAdd,
    CPF_OT_BakeModRefreshCache,
    CPF_MT_BakeModAdd,
    CPF_OT_BakeModRemove,
    CPF_OT_BakeModMove,
    CPF_OT_ToggleExportModVisibility,
    CPF_OT_AddCageVertexGroup,
    CPF_OT_ClearCageVertexGroup,
    CPF_OT_SetVertexColor,
    CPF_OT_SetMaterial,
    CPF_OT_ExportBakeAssets,
    CPF_MT_FBX_Presets,
    CPF_OT_FBXPresetAdd,
    CPF_MT_GameFBX_Presets,
    CPF_OT_GameFBXPresetAdd,
    CPF_OT_ExportGameCollection,
    CPF_OT_OpenGameExportExplorer,
    CPF_OT_ExportGameSelection,
    CPF_MT_Naming_Presets,
    CPF_OT_NamingPresetAdd,
    CPF_MT_BakeMods_Presets,
    CPF_OT_BakeModsPresetAdd,
    CPF_OT_PackageZip,
    CPF_OT_ReloadAddon,
    CPF_OT_UpdateAddon,
    CPF_PT_ExportGameAssets,
    CPF_PT_SetupFolderStructure,
    CPF_PT_SetupBakeAssets,
    CPF_PT_ExportBakeAssets,
)


def register():
    if not os.path.exists(_templates_path()):
        save_templates(DEFAULT_TEMPLATES)
    _ensure_default_fbx_preset()
    _ensure_default_naming_preset()
    _ensure_default_bakemods_preset()
    _ensure_default_game_fbx_preset()

    # Build the modifier-property + category caches first so the dynamic
    # CPF_BakeModifierItem class can be generated against them. The
    # mesh-compatibility test is skipped here because bpy.data is restricted
    # during register() — it's applied by the post-register timer below
    # (and by the explicit cpf.bake_mod_refresh_cache operator).
    _build_modifier_caches(include_mesh_filter=False)

    # Generate and register the dynamic bake-modifier-item class.
    global CPF_BakeModifierItem
    CPF_BakeModifierItem = _build_bake_modifier_item_class()
    bpy.utils.register_class(CPF_BakeModifierItem)

    # Inject the bake_modifier_stack annotation onto CPF_Settings BEFORE
    # registering it (annotations are read by register_class).
    CPF_Settings.__annotations__["bake_modifier_stack"] = CollectionProperty(
        type=CPF_BakeModifierItem,
    )

    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.cpf_settings = PointerProperty(type=CPF_Settings)
    bpy.types.Scene.cpf_assets = CollectionProperty(type=CPF_AssetItem)
    # WindowManager: non-persistent, non-undoable — safe to modify inside draw()
    bpy.types.WindowManager.cpf_structure_paths = CollectionProperty(type=CPF_PathItem)
    bpy.types.WindowManager.cpf_structure_index = IntProperty(default=0)
    bpy.types.WindowManager.cpf_resolved_paths = CollectionProperty(type=CPF_PathItem)
    bpy.types.WindowManager.cpf_resolved_index = IntProperty(default=0)
    bpy.app.handlers.load_post.append(_load_post_handler)
    bpy.app.timers.register(_auto_init, first_interval=0.2)
    # Apply the mesh-compatibility filter as soon as bpy.data is available.
    bpy.app.timers.register(_post_register_mesh_filter, first_interval=0.3)


def unregister():
    if _load_post_handler in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_load_post_handler)
    del bpy.types.WindowManager.cpf_resolved_index
    del bpy.types.WindowManager.cpf_resolved_paths
    del bpy.types.WindowManager.cpf_structure_index
    del bpy.types.WindowManager.cpf_structure_paths
    del bpy.types.Scene.cpf_assets
    del bpy.types.Scene.cpf_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    # Tear down the dynamically-generated CPF_BakeModifierItem and remove
    # the injected annotation so a clean re-register works.
    global CPF_BakeModifierItem
    if CPF_BakeModifierItem is not None:
        try:
            bpy.utils.unregister_class(CPF_BakeModifierItem)
        except Exception:
            pass
        CPF_BakeModifierItem = None
    CPF_Settings.__annotations__.pop("bake_modifier_stack", None)


if __name__ == "__main__":
    register()
