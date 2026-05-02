import multiprocessing
import sys

import uvicorn

from app.config import HOST, PORT


if __name__ == "__main__":
    multiprocessing.set_executable(sys.executable)
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)
