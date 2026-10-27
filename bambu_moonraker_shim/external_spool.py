"""Single external-spool macro descriptions and validated filament settings."""

import re


DEFAULT_FILAMENT = {
    "material": "PLA", "color": "FFFFFFFF", "profile_id": "GFL99",
    "nozzle_temp_min": 190, "nozzle_temp_max": 240,
}

# Generic filament IDs from BambuStudio's BBL/filament/Generic <type> @base.json.
# Temperature ranges are defaults for the editable metadata, not heater targets.
MATERIALS = {
    "PLA": ("GFL99", 190, 240), "PETG": ("GFG99", 220, 270),
    "ABS": ("GFB99", 240, 280), "ASA": ("GFB98", 240, 280),
    "TPU": ("GFU99", 200, 250), "PC": ("GFC99", 250, 300),
    "PA": ("GFN99", 250, 300),
}

MATERIAL_PRESETS = {
    f"SET_{material}": {
        "MATERIAL": material, "PROFILE_ID": profile,
        "TEMP_MIN": str(minimum), "TEMP_MAX": str(maximum),
    }
    for material, (profile, minimum, maximum) in MATERIALS.items()
}

# P1S action catalog: FILAMENT_EXTRUDED / RETRY_FILAMENT_EXTRUDED.
# These answer the native dialog, rather than resuming a paused print.
EXTRUSION_PROMPT_ERRORS = {0x07FF8007, 0x07FFC00A}
EXTERNAL_LOAD_CHOICES = {
    "EXTERNAL_FILAMENT_EXTRUDED": "done",
    "EXTERNAL_FILAMENT_RETRY": "resume",
}

EXTERNAL_MACROS = {
    "LOAD_EXTERNAL_FILAMENT": {
        "description": "Request external-spool loading (operator must be present)",
        "gcode": "LOAD_EXTERNAL_FILAMENT",
    },
    "UNLOAD_EXTERNAL_FILAMENT": {
        "description": "Request external-spool unloading; TEMP overrides the material loading temperature",
        "gcode": "{% set temperature = params.TEMP|default('')|int %}\nUNLOAD_EXTERNAL_FILAMENT TEMP={temperature}",
    },
    "SET_EXTERNAL_FILAMENT": {
        "description": "Set external-spool MATERIAL/COLOR and optional nozzle range/profile; does not heat",
        # Mainsail reads params.* expressions to build its macro parameter menu.
        # These are discovery templates: execution is handled by the shim.
        "gcode": "\n".join([
            "{% set material = params.MATERIAL|default('')|string %}",
            "{% set color = params.COLOR|default('')|string %}",
            "{% set minimum = params.TEMP_MIN|default('')|int %}",
            "{% set maximum = params.TEMP_MAX|default('')|int %}",
            "{% set profile = params.PROFILE_ID|default('')|string %}",
            "SET_EXTERNAL_FILAMENT MATERIAL={material} COLOR={color} TEMP_MIN={minimum} TEMP_MAX={maximum} PROFILE_ID={profile}",
        ]),
    },
}
EXTERNAL_MACROS.update({
    name: {
        "description": f"Set external spool to generic {params['MATERIAL']}; preserve colour; does not heat",
        "gcode": "SET_EXTERNAL_FILAMENT " + " ".join(f"{key}={value}" for key, value in params.items()),
    }
    for name, params in MATERIAL_PRESETS.items()
})
EXTERNAL_MACROS.update({
    name: {
        "description": ("Yes, filament extruded: finish external loading" if action == "done"
                        else "No filament extruded: retry external loading"),
        "gcode": name,
    }
    for name, action in EXTERNAL_LOAD_CHOICES.items()
})


def filament_settings(current, params):
    """Merge edits without resetting unspecified settings or accepting bad IDs."""
    unknown = set(params) - {"MATERIAL", "COLOR", "TEMP_MIN", "TEMP_MAX", "PROFILE_ID"}
    if unknown:
        raise ValueError("Unknown external filament parameter: " + ", ".join(sorted(unknown)))
    if not params or not any(str(value).strip() for value in params.values()):
        raise ValueError("Provide MATERIAL, COLOR, TEMP_MIN, TEMP_MAX, or PROFILE_ID")
    known_material = str(current.get("material") or params.get("MATERIAL") or "PLA").strip().upper()
    profile, minimum, maximum = MATERIALS.get(known_material, ("", 190, 240))
    result = {**DEFAULT_FILAMENT, "profile_id": profile, "nozzle_temp_min": minimum,
              "nozzle_temp_max": maximum, **current}
    material = str(params.get("MATERIAL") or result["material"]).strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9 +_-]{0,31}", material):
        raise ValueError("Invalid material type")
    if material != result["material"]:
        if material not in MATERIALS:
            if not all(params.get(key) for key in ("PROFILE_ID", "TEMP_MIN", "TEMP_MAX")):
                raise ValueError("Custom materials require PROFILE_ID, TEMP_MIN, and TEMP_MAX")
        else:
            profile, minimum, maximum = MATERIALS[material]
            result.update(profile_id=profile, nozzle_temp_min=minimum, nozzle_temp_max=maximum)
    result["material"] = material
    if params.get("COLOR"):
        color = str(params["COLOR"]).strip().lstrip("#").upper()
        if not re.fullmatch(r"[0-9A-F]{6}([0-9A-F]{2})?", color):
            raise ValueError("COLOR must be RRGGBB or RRGGBBAA hex")
        result["color"] = color + "FF" if len(color) == 6 else color
    if params.get("PROFILE_ID"):
        profile = str(params["PROFILE_ID"]).strip().upper()
        if not re.fullmatch(r"[A-Z0-9_-]{1,64}", profile):
            raise ValueError("Invalid PROFILE_ID")
        result["profile_id"] = profile
    for parameter, field in (("TEMP_MIN", "nozzle_temp_min"), ("TEMP_MAX", "nozzle_temp_max")):
        if params.get(parameter) not in (None, ""):
            try:
                result[field] = int(params[parameter])
            except (TypeError, ValueError):
                raise ValueError(f"{parameter} must be an integer")
    if not result["profile_id"]:
        raise ValueError("PROFILE_ID is required for this material")
    if not 170 <= result["nozzle_temp_min"] <= result["nozzle_temp_max"] <= 300:
        raise ValueError("Nozzle metadata range must satisfy 170 <= TEMP_MIN <= TEMP_MAX <= 300")
    return result


def macro_variables(settings=None):
    return {
        **(settings or {}), "loaded": None, "last_action": "", "sync_state": "unknown",
        "awaiting_extrusion_confirmation": False,
        "last_error": "",
    }
