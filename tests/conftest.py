"""Never let importing app.main migrate a developer's real business database."""
import atexit
import os
from pathlib import Path
import tempfile

_test_data = tempfile.TemporaryDirectory(prefix='feihua-suite-')
atexit.register(_test_data.cleanup)
_original_data = Path(os.environ.get('FEIHUA_DATA_DIR', Path(__file__).resolve().parents[1] / 'data'))
for name in ('poetry.sqlite3',):
    source = _original_data / name
    if source.exists():
        (Path(_test_data.name) / name).symlink_to(source.resolve())
os.environ['FEIHUA_DATA_DIR'] = _test_data.name
os.environ.pop('FEIHUA_FEISHU_WEBHOOK', None)
os.environ.pop('FEIHUA_FEISHU_SECRET', None)
