from pathlib import Path
import sys

import uvicorn


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.main import app  # noqa: E402


if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=421)
