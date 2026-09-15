"""UP (Unified Patch) Muppet configuration generator and parser.

Muppet tools (e.g. in firmware.fxt-tools.fxt snapshots or env packages) take a
config ini file (via `python muppet.py -c <config.ini>`) containing [default],
[FIT1], [SOC], and [ucode_section] sections.
"""
import configparser
from pathlib import Path
from typing import Optional

from .result import ok, err, ErrorCode

# Standard ingredient mapping to section in UP config
FIT1_KEYS = {"mcheck", "tcode", "acode", "ucode", "xucode", "fit1_sign",
             "fit1_build_script", "ucode_options", "ucode_proj"}
SOC_KEYS = {"icode", "oobmsm", "s3m", "pcode-io", "pcode_io", "pcode-c", "pcode_c",
            "soc_primary", "soc_override", "soc_sign", "soc_key", "soc_build_script"}
UCODE_KEYS = {"ucode_config", "ucode_template"}


def generate_up_config_ini(output_path: str,
                           default_params: Optional[dict] = None,
                           ingredients: Optional[dict] = None,
                           template_path: Optional[str] = None,
                           fit1_params: Optional[dict] = None,
                           soc_params: Optional[dict] = None) -> dict:
    """Generate or update a UP Muppet config ini file.

    Args:
        output_path: Destination path where the .ini file will be saved.
        default_params: Parameters for [default] section (build_id, version, build_path,
                        cpu_id, stepping, platform_id, svn, se_svn, project_name, etc.).
        ingredients: Dict of ingredient names to file paths.
                     E.g. {"mcheck": "/path/mcheck.patchlet", "s3m": "/path/s3m.bin", ...}
        template_path: Optional base ini file (e.g. F0010002.ini) to copy and override.
        fit1_params: Optional extra parameters for [FIT1] section.
        soc_params: Optional extra parameters for [SOC] section.
    """
    if not output_path:
        return err(ErrorCode.INVALID_ARGUMENT, "output_path is required",
                   {"param": "output_path"})

    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str  # preserve case

    if template_path and Path(template_path).is_file():
        try:
            cp.read(template_path)
        except configparser.Error as exc:
            return err(ErrorCode.CONFIG_INVALID, f"failed to parse template ini: {exc}",
                       {"template_path": template_path})
    else:
        # Initialize default sections
        cp["default"] = {
            "build_id": "01",
            "version": "0xF0010001",
            "build_path": "c:\\patch",
            "force": "no",
            "cpu_id": "",
            "stepping": "",
            "platform_id": "0x01",
            "release": "no",
            "svn": "1",
            "se_svn": "0",
        }
        cp["FIT1"] = {
            "fit1_sign": "none",
            "fit1_build_script": "",
            "ucode_options": "",
            "ucode_proj": "",
            "xucode": "none",
            "mcheck": "none",
            "tcode": "none",
            "acode": "none",
            "ucode": "none",
        }
        cp["SOC"] = {
            "soc_primary": "",
            "soc_override": "",
            "soc_sign": "none",
            "soc_key": "path",
            "soc_build_script": "",
            "icode": "none",
            "oobmsm": "none",
            "s3m": "none",
            "pcode-io": "none",
            "pcode-c": "none",
        }
        cp["ucode_section"] = {
            "ucode_config": "none",
            "ucode_template": "none",
        }

    # Ensure sections exist
    for sec in ("default", "FIT1", "SOC", "ucode_section"):
        if sec not in cp:
            cp[sec] = {}

    # Apply default_params
    if default_params:
        for k, v in default_params.items():
            cp["default"][k] = str(v)

    # Apply extra FIT1 / SOC params
    if fit1_params:
        for k, v in fit1_params.items():
            cp["FIT1"][k] = str(v)
    if soc_params:
        for k, v in soc_params.items():
            cp["SOC"][k] = str(v)

    # Apply ingredients to appropriate sections
    if ingredients:
        for raw_k, v in ingredients.items():
            k_lower = raw_k.lower().replace("_", "-")
            val = str(v)
            if k_lower in ("mcheck", "tcode", "acode", "ucode", "xucode"):
                cp["FIT1"][k_lower] = val
                if k_lower == "ucode" and "ucode_section" in cp:
                    cp["ucode_section"]["ucode_config"] = val
            elif k_lower in ("icode", "oobmsm", "s3m", "pcode-io", "pcode-c"):
                cp["SOC"][k_lower] = val
            elif k_lower in UCODE_KEYS:
                cp["ucode_section"][raw_k] = val
            elif k_lower in FIT1_KEYS:
                cp["FIT1"][raw_k] = val
            elif k_lower in SOC_KEYS:
                cp["SOC"][raw_k] = val
            else:
                # Default to SOC or FIT1 depending on name
                cp["SOC"][raw_k] = val

    out_file = Path(output_path)
    try:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        with open(out_file, "w", encoding="utf-8") as fp:
            cp.write(fp)
    except OSError as exc:
        return err(ErrorCode.INTERNAL_ERROR, f"failed to write UP config ini: {exc}",
                   {"output_path": output_path})

    return ok({
        "config_ini_path": str(out_file.resolve()),
        "sections": list(cp.sections()),
    })
