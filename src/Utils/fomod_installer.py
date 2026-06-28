"""
fomod_installer.py
Stateless logic engine for FOMOD installation.
No UI, no file I/O. All functions are pure.
"""

from __future__ import annotations

from Utils.fomod_parser import (
    Dependency, InstallStep, ModuleConfig, Plugin,
)


# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------

def evaluate_dependency(dep: Dependency, flag_state: dict[str, str],
                        installed_files: set[str],
                        active_files: set[str] | None = None,
                        version_pass: bool = False,
                        loose_files: set[str] | None = None) -> bool:
    """
    Recursively evaluate a Dependency tree.

    flag_state:      current flag name → value mapping
    installed_files: set of all plugin names known to be present (lower-case),
                     regardless of whether they are enabled or disabled.
    active_files:    set of enabled/active plugin names (lower-case).
                     If None, falls back to treating installed_files as active.
    version_pass:    When True, unevaluable version dependencies
                     (gameDependency, foseDependency, etc.) evaluate to True.
                     Use for step visibility so steps with version gates are
                     still shown. For typeDescriptor pattern matching leave
                     False so patterns fall through to the default type.
    loose_files:     Optional set of every deployed file's relative path
                     (lower-case, forward-slash separators). Used to resolve
                     <fileDependency> nodes that reference a loose asset path
                     (e.g. "textures/foo.dds") rather than a plugin name. MO2
                     evaluates fileDependency against the whole virtual file
                     tree, not just plugins; this set restores that behaviour.

    Returns True if the condition is satisfied.
    """
    if dep.dep_type == "composite":
        if not dep.sub_deps:
            return True  # Empty composite = no restriction = pass
        results = [evaluate_dependency(d, flag_state, installed_files,
                                        active_files, version_pass, loose_files)
                   for d in dep.sub_deps]
        if dep.operator.lower() == "or":
            return any(results)
        return all(results)  # default: "And"

    if dep.dep_type == "flag":
        return flag_state.get(dep.flag_name, "") == dep.flag_value

    if dep.dep_type == "file":
        # Case-insensitive — FOMOD was designed for Windows.
        key = dep.file_name.lower()
        # A fileDependency may reference an arbitrary loose asset path, not just
        # a plugin name. Detect that (path separator, or an extension that isn't
        # a plugin) and resolve it against the deployed file tree when we have
        # one. MO2 checks the whole virtual tree here.
        norm_key = key.replace("\\", "/")
        looks_loose = ("/" in norm_key
                       or not norm_key.endswith((".esp", ".esm", ".esl")))
        if looks_loose and loose_files is not None:
            present = norm_key in loose_files
            # Loose assets have no enable/disable concept — present == active.
            if dep.file_state == "Inactive":
                return False
            if dep.file_state == "Missing":
                return not present
            return present  # "Active"
        if looks_loose and loose_files is None:
            # No deployed file tree available to check a loose-asset gate.
            # Match the version-gate philosophy: don't silently fail closed.
            #   Active/Inactive  → satisfied when version_pass (lenient context)
            #   Missing          → satisfied (we can't prove it's present)
            if dep.file_state == "Missing":
                return True
            return version_pass

        present = key in installed_files
        if dep.file_state == "Active":
            # Active: file must be present AND enabled
            if active_files is not None:
                return key in active_files
            return present
        if dep.file_state == "Inactive":
            # Inactive: file is present (installed) but NOT enabled
            if active_files is not None:
                return present and key not in active_files
            return False
        # "Missing": file must not be installed at all
        return not present

    if dep.dep_type == "version":
        return version_pass

    if dep.dep_type == "unsatisfiable":
        return False

    # Unknown type — pass through
    return True


# ---------------------------------------------------------------------------
# Plugin type resolution
# ---------------------------------------------------------------------------

def resolve_plugin_type(plugin: Plugin, flag_state: dict[str, str],
                        installed_files: set[str],
                        active_files: set[str] | None = None,
                        loose_files: set[str] | None = None) -> str:
    """
    Evaluate a plugin's typeDescriptor to get its effective type string.
    For simple typeDescriptors returns the static type directly.
    For conditional typeDescriptors, evaluates patterns in order and returns
    the first matching type, or default_type if none match.

    Returns one of: "Optional" | "Required" | "Recommended" | "CouldBeUsable" | "NotUsable"
    """
    td = plugin.type_descriptor
    if not td.is_conditional:
        return td.plugin_type

    for dep, type_name in td.patterns:
        if evaluate_dependency(dep, flag_state, installed_files, active_files,
                               loose_files=loose_files):
            return type_name

    # No pattern matched. If the default is NotUsable but the pattern set
    # includes a Required outcome, it means this is a version-detection group
    # where we couldn't auto-detect the game version. Let the user choose freely.
    if td.default_type == "NotUsable" and any(t == "Required" for _, t in td.patterns):
        return "Optional"

    return td.default_type


# ---------------------------------------------------------------------------
# Pre-install module dependency gate
# ---------------------------------------------------------------------------

def describe_dependency(dep: Dependency, indent: int = 0) -> str:
    """Human-readable one-line-per-clause description of a Dependency tree."""
    pad = "  " * indent
    if dep.dep_type == "composite":
        if not dep.sub_deps:
            return f"{pad}(no conditions)"
        op = dep.operator.upper()
        lines = [f"{pad}{op}:"]
        for d in dep.sub_deps:
            lines.append(describe_dependency(d, indent + 1))
        return "\n".join(lines)
    if dep.dep_type == "flag":
        return f"{pad}flag {dep.flag_name!r} == {dep.flag_value!r}"
    if dep.dep_type == "file":
        return f"{pad}file {dep.file_name!r} state={dep.file_state}"
    if dep.dep_type == "version":
        return f"{pad}game/extender version (unchecked)"
    if dep.dep_type == "unsatisfiable":
        return f"{pad}<unknown dependency>"
    return f"{pad}{dep.dep_type}"


def check_module_dependencies(
    config: ModuleConfig,
    installed_files: set[str] | None = None,
    active_files: set[str] | None = None,
    loose_files: set[str] | None = None,
) -> tuple[bool, str]:
    """
    Evaluate <moduleDependencies> before the wizard runs.

    Returns (ok, message). `ok` is True when the gate passes (or is absent).
    When False, `message` is a human-readable description of what failed —
    suitable to show to the user so they can decide whether to proceed.

    Version / script-extender dependencies are treated as passing (version_pass
    is True) so that we don't block installs when we can't detect the game
    version.
    """
    if config.module_dependency is None:
        return True, ""
    inst = installed_files or set()
    ok = evaluate_dependency(config.module_dependency, {}, inst, active_files,
                             version_pass=True, loose_files=loose_files)
    if ok:
        return True, ""
    return False, describe_dependency(config.module_dependency)


# ---------------------------------------------------------------------------
# Step visibility
# ---------------------------------------------------------------------------

def get_visible_steps(config: ModuleConfig, flag_state: dict[str, str],
                      installed_files: set[str],
                      active_files: set[str] | None = None,
                      loose_files: set[str] | None = None) -> list[InstallStep]:
    """
    Filter config.steps to only those whose visible_condition is satisfied.
    Steps with no condition (None) are always visible.
    Returns the ordered list of visible InstallStep objects.
    """
    visible = []
    for step in config.steps:
        if step.visible_condition is None:
            visible.append(step)
        elif evaluate_dependency(step.visible_condition, flag_state,
                                  installed_files, active_files,
                                  version_pass=True, loose_files=loose_files):
            visible.append(step)
    return visible


# ---------------------------------------------------------------------------
# Default selections
# ---------------------------------------------------------------------------

def get_default_selections(step: InstallStep, flag_state: dict[str, str],
                           installed_files: set[str],
                           active_files: set[str] | None = None,
                           loose_files: set[str] | None = None) -> dict[str, list[str]]:
    """
    Compute default plugin selections for a step based on group types and plugin types.
    Returns {group_name: [plugin_name, ...]}
    """
    defaults: dict[str, list[str]] = {}

    for group in step.groups:
        plugins = group.plugins
        if not plugins:
            defaults[group.name] = []
            continue

        gtype = group.group_type
        plugin_types = [resolve_plugin_type(p, flag_state, installed_files,
                                            active_files, loose_files)
                        for p in plugins]

        if gtype == "SelectAll":
            defaults[group.name] = [p.name for p in plugins]

        elif gtype == "SelectExactlyOne":
            # Required → Recommended → first
            for i, p in enumerate(plugins):
                if plugin_types[i] == "Required":
                    defaults[group.name] = [p.name]
                    break
            else:
                for i, p in enumerate(plugins):
                    if plugin_types[i] == "Recommended":
                        defaults[group.name] = [p.name]
                        break
                else:
                    defaults[group.name] = [plugins[0].name]

        elif gtype == "SelectAtMostOne":
            # Required → Recommended → none
            for i, p in enumerate(plugins):
                if plugin_types[i] == "Required":
                    defaults[group.name] = [p.name]
                    break
            else:
                for i, p in enumerate(plugins):
                    if plugin_types[i] == "Recommended":
                        defaults[group.name] = [p.name]
                        break
                else:
                    defaults[group.name] = []

        elif gtype in ("SelectAtLeastOne", "SelectAny"):
            # All Required + Recommended; fallback to [first] for SelectAtLeastOne
            selected = [p.name for p, t in zip(plugins, plugin_types)
                        if t in ("Required", "Recommended")]
            if not selected and gtype == "SelectAtLeastOne":
                selected = [plugins[0].name]
            defaults[group.name] = selected

        else:
            defaults[group.name] = []

    return defaults


# ---------------------------------------------------------------------------
# Flag state update
# ---------------------------------------------------------------------------

def update_flags(step: InstallStep, selections: dict[str, list[str]],
                 flag_state: dict[str, str]) -> dict[str, str]:
    """
    After a step is completed, apply conditionFlags from all selected plugins.
    Returns an updated copy of flag_state.

    selections: {group_name: [plugin_name, ...]} for this step only.
    SelectAll groups contribute every plugin's flags even if the selections
    dict is empty for that group (matches MO2's preselect behaviour).
    """
    new_state = dict(flag_state)
    for group in step.groups:
        selected_names = set(selections.get(group.name, []))
        for plugin in group.plugins:
            if group.group_type == "SelectAll" or plugin.name in selected_names:
                new_state.update(plugin.condition_flags)
    return new_state


# ---------------------------------------------------------------------------
# File resolution
# ---------------------------------------------------------------------------

def resolve_files(config: ModuleConfig,
                  all_selections: dict[str, dict[str, list[str]]],
                  installed_files: set[str] | None = None,
                  active_files: set[str] | None = None,
                  loose_files: set[str] | None = None) -> list[tuple[str, str, bool]]:
    """
    Build the final file install list from required files + user selections
    + conditional file installs.

    all_selections: {step_name: {group_name: [plugin_name, ...]}}
    Returns list of (source_path, destination_path, is_folder) tuples with OS-normalized paths.

    Install order matches MO2's three-phase scheme:
      1. <requiredInstallFiles>        (always first, sorted by priority)
      2. <plugin> files from selections + alwaysInstall/installIfUsable files
      3. <conditionalFileInstalls>     (always last, sorted by priority)
    """
    inst_files = installed_files or set()

    required: list[tuple[int, str, str, bool]] = []
    options: list[tuple[int, str, str, bool]] = []
    conditional: list[tuple[int, str, str, bool]] = []

    for fi in config.required_files:
        required.append((fi.priority, fi.source_path,
                         fi.destination_path, fi.is_folder))

    # Build final flag state by replaying all steps in order
    flag_state: dict[str, str] = {}
    for i, step in enumerate(config.steps):
        # Skip steps whose visibility condition is not satisfied by the flags
        # accumulated so far. If a step was invisible the user never saw it,
        # so its SelectAll/selected plugins must not contribute files or flags.
        if step.visible_condition is not None:
            if not evaluate_dependency(step.visible_condition, flag_state,
                                       inst_files, active_files,
                                       version_pass=True, loose_files=loose_files):
                continue
        # Accept both new index-keyed format (str(i)) and old name-keyed format
        # for backward compatibility with previously saved selection JSON.
        step_selections = all_selections.get(str(i)) or all_selections.get(step.name, {})
        for group in step.groups:
            selected_names = set(step_selections.get(group.name, []))
            for plugin in group.plugins:
                ptype = resolve_plugin_type(plugin, flag_state, inst_files,
                                            active_files, loose_files)
                is_selected = (group.group_type == "SelectAll"
                               or plugin.name in selected_names)
                if is_selected:
                    for fi in plugin.files:
                        options.append((fi.priority, fi.source_path,
                                        fi.destination_path, fi.is_folder))
                    flag_state.update(plugin.condition_flags)
                else:
                    # alwaysInstall / installIfUsable files install even when
                    # the plugin is not selected.
                    for fi in plugin.files:
                        if fi.always_install or (fi.install_if_usable
                                                 and ptype != "NotUsable"):
                            options.append((fi.priority, fi.source_path,
                                            fi.destination_path, fi.is_folder))

    # Conditional file installs — evaluated against final flag state.
    # version_pass=True: unevaluable engine/game version gates
    # (gameDependency, foseDependency, nvseDependency, …) are treated as
    # satisfied, matching MO2/Vortex — we can't know the user's script-extender
    # version, so we install the gated payload rather than silently dropping it.
    # Without this, stepless FOMODs whose entire payload sits in
    # <conditionalFileInstalls> behind a version gate would install 0 files.
    for pattern in config.conditional_file_installs:
        if evaluate_dependency(pattern.dependency, flag_state, inst_files,
                               active_files, version_pass=True,
                               loose_files=loose_files):
            for fi in pattern.files:
                conditional.append((fi.priority, fi.source_path,
                                    fi.destination_path, fi.is_folder))

    required.sort(key=lambda x: x[0])
    options.sort(key=lambda x: x[0])
    conditional.sort(key=lambda x: x[0])

    result: list[tuple[str, str, bool]] = []
    for bucket in (required, options, conditional):
        for _, src, dst, is_folder in bucket:
            result.append((src, dst, is_folder))
    return result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_selections(step: InstallStep,
                        selections: dict[str, list[str]],
                        flag_state: dict[str, str] | None = None,
                        installed_files: set[str] | None = None,
                        active_files: set[str] | None = None,
                        loose_files: set[str] | None = None) -> list[str]:
    """
    Check if current selections satisfy each group's type constraint.
    Returns a list of error messages (empty = all valid).

    When the resolution context (flag_state / installed_files / active_files)
    is supplied, "select one/at-least-one" requirements are waived for groups
    in which every plugin resolves to NotUsable — such a group is impossible
    to satisfy and must not hard-block the install. This mirrors MO2, which
    lets the user proceed past a degenerate all-NotUsable group.
    """
    errors: list[str] = []
    inst = installed_files or set()
    flags = flag_state or {}

    for group in step.groups:
        selected = selections.get(group.name, [])
        count = len(selected)
        gtype = group.group_type

        # A group is "satisfiable" only if at least one plugin is selectable
        # (not NotUsable). Without the resolution context we assume it is, to
        # preserve the original strict behaviour for callers that don't pass it.
        if flag_state is None and installed_files is None and active_files is None:
            satisfiable = True
        else:
            satisfiable = any(
                resolve_plugin_type(p, flags, inst, active_files, loose_files) != "NotUsable"
                for p in group.plugins
            )

        if gtype == "SelectExactlyOne":
            if satisfiable and count != 1:
                errors.append(f'"{group.name}": select exactly one option.')
        elif gtype == "SelectAtLeastOne":
            if satisfiable and count < 1:
                errors.append(f'"{group.name}": select at least one option.')
        elif gtype == "SelectAtMostOne" and count > 1:
            errors.append(f'"{group.name}": select at most one option.')
        # SelectAny and SelectAll have no constraint to enforce here

    return errors
