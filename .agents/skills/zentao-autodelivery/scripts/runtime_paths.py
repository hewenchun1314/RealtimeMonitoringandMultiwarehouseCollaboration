"""Paths shared by the monitor, workers and isolated integration tests."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
CONFIG_PATH = Path(os.environ.get('ZENTAO_DELIVERY_CONFIG') or PROJECT_ROOT / '.agents/zentao-project.json').resolve()
LOCAL_DIRECTORY = CONFIG_PATH.parent.parent / '.local'
DATABASE_PATH = Path(os.environ.get('ZENTAO_DELIVERY_DATABASE') or
                     LOCAL_DIRECTORY / 'zentao-delivery-v2/workflow.sqlite3').resolve()
