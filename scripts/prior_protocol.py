"""Use the shared pure-Python resource protocol without importing image PlantVelo.

The analysis package and IRagent share one directory-lock/registry implementation.
Loading that source module explicitly leaves the pinned extraction package intact.
"""
from pathlib import Path
import runpy

SOURCE=Path(__file__).resolve().parents[1]/'plantvelo/plantvelo/prior_resources.py'
_protocol=runpy.run_path(str(SOURCE))
resource_lock=_protocol['resource_lock']
read_registry=_protocol['read_registry']
validate_entry=_protocol['validate_entry']
PENDING_NAME=_protocol['PENDING_NAME']
