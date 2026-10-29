"""Test launcher for the unchanged real backend; isolated temporary storage only."""
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if __name__ == "__main__":
    import uvicorn
    with tempfile.TemporaryDirectory(prefix="dil-front-") as storage:
        os.environ["STORAGE_DIR"] = storage
        # The production default 200 MiB body cap and 30-second core deadline remain in use.
        from backend.main import app
        uvicorn.run(app, host="127.0.0.1", port=8000, access_log=False, log_level="critical")
