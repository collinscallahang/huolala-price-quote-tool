import shutil
import os
import socket
import webbrowser
from datetime import datetime
from pathlib import Path

from .automation import ThreadedQuoteClient
from .runner import RunManager, configured_path
from .site_config import load_site_config, save_site_config


ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT_DIR / "configs" / "site.huolala.json"
UPLOAD_DIR = ROOT_DIR / "data" / "uploads"
STATIC_DIR = Path(__file__).resolve().parent / "static"
VERSION_PATH = ROOT_DIR / "VERSION"

manager = RunManager(ROOT_DIR, CONFIG_PATH)
preview_browser: ThreadedQuoteClient | None = None


def create_app():
    try:
        from fastapi import FastAPI, File, Form, HTTPException, UploadFile
        from fastapi.responses import FileResponse, HTMLResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:
        raise RuntimeError(
            "缺少服务依赖。请先运行：python -m pip install -r requirements.txt"
        ) from exc

    app = FastAPI(title="批量查价工具")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def no_cache_for_local_ui(request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/config")
    def get_config():
        config = load_site_config(CONFIG_PATH)
        input_dir = configured_path(config.get("default_input_dir"), ROOT_DIR, ROOT_DIR)
        output_root = configured_path(config.get("output_root"), ROOT_DIR, ROOT_DIR / "outputs" / "runs")
        return {
            "app_version": read_app_version(),
            "default_url": config.get("default_url"),
            "config_name": config.get("name"),
            "default_input_dir": str(input_dir),
            "output_root": str(output_root),
        }

    @app.post("/api/config/paths")
    def update_config_paths(
        default_input_dir: str = Form(...),
        output_root: str = Form(...),
    ):
        input_dir = configured_path(default_input_dir, ROOT_DIR, ROOT_DIR / "input")
        output_dir = configured_path(output_root, ROOT_DIR, ROOT_DIR / "output")
        try:
            input_dir.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"目录创建失败：{exc}") from exc

        config = load_site_config(CONFIG_PATH)
        config["default_input_dir"] = str(input_dir)
        config["output_root"] = str(output_dir)
        save_site_config(CONFIG_PATH, config)
        return {
            "ok": True,
            "message": "目录设置已保存",
            "default_input_dir": str(input_dir),
            "output_root": str(output_dir),
        }

    @app.api_route("/api/folder/select", methods=["GET", "POST"])
    def select_folder():
        try:
            selected = choose_folder_dialog()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"无法打开文件夹选择窗口：{exc}") from exc
        return {"path": selected}

    @app.get("/api/input-files")
    def input_files():
        input_dir = _configured_input_dir()
        if not input_dir.exists():
            return {"input_dir": str(input_dir), "files": []}
        files = sorted(path for path in input_dir.glob("*.xlsx") if not path.name.startswith("~$"))
        return {"input_dir": str(input_dir), "files": [path.name for path in files]}

    @app.post("/api/browser/open")
    def open_browser(site_url: str = Form("")):
        global preview_browser
        if preview_browser is not None:
            try:
                preview_browser.close()
            except Exception:
                pass
            finally:
                preview_browser = None
        config = load_site_config(CONFIG_PATH)
        preview_browser = ThreadedQuoteClient(site_url or config.get("default_url", ""), config, ROOT_DIR)
        try:
            preview_browser.open()
        except Exception as exc:
            preview_browser = None
            raise HTTPException(status_code=500, detail=browser_error_message(exc)) from exc
        return {"ok": True, "message": "专用 Edge 已打开，请完成登录后回到控制页开始查价"}

    @app.post("/api/runs")
    def create_run(
        files: list[UploadFile] = File(...),
        site_url: str = Form(...),
        retry_count: int = Form(2),
        overwrite: bool = Form(False),
    ):
        if not files:
            raise HTTPException(status_code=400, detail="请至少选择一个 Excel 文件")
        upload_run_dir = UPLOAD_DIR / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        upload_run_dir.mkdir(parents=True, exist_ok=True)

        paths: list[Path] = []
        for upload in files:
            if not upload.filename.lower().endswith(".xlsx"):
                raise HTTPException(status_code=400, detail=f"只支持 .xlsx：{upload.filename}")
            target = upload_run_dir / Path(upload.filename).name
            with target.open("wb") as f:
                shutil.copyfileobj(upload.file, f)
            paths.append(target)

        run = manager.create_run(paths, site_url=site_url, retry_count=retry_count, overwrite=overwrite)
        return run.snapshot()

    @app.post("/api/runs/from-input")
    def create_run_from_input(
        site_url: str = Form(...),
        retry_count: int = Form(2),
        overwrite: bool = Form(False),
    ):
        input_dir = _configured_input_dir()
        if not input_dir.exists():
            raise HTTPException(status_code=400, detail=f"输入目录不存在：{input_dir}")
        paths = sorted(path for path in input_dir.glob("*.xlsx") if not path.name.startswith("~$"))
        if not paths:
            raise HTTPException(status_code=400, detail=f"输入目录没有 .xlsx 文件：{input_dir}")
        run = manager.create_run(paths, site_url=site_url, retry_count=retry_count, overwrite=overwrite)
        return run.snapshot()

    @app.post("/api/runs/{run_id}/start")
    def start_run(run_id: str):
        global preview_browser
        if preview_browser is not None:
            try:
                preview_browser.close()
            except Exception:
                pass
            finally:
                preview_browser = None
        try:
            run = manager.get(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="未找到任务")
        run.start()
        return run.snapshot()

    @app.post("/api/runs/{run_id}/pause")
    def pause_run(run_id: str):
        run = _get_run_or_404(run_id)
        run.pause()
        return run.snapshot()

    @app.post("/api/runs/{run_id}/resume")
    def resume_run(run_id: str):
        run = _get_run_or_404(run_id)
        run.resume()
        return run.snapshot()

    @app.post("/api/runs/{run_id}/stop")
    def stop_run(run_id: str):
        run = _get_run_or_404(run_id)
        run.stop()
        return run.snapshot()

    @app.get("/api/runs/{run_id}/status")
    def run_status(run_id: str):
        run = _get_run_or_404(run_id)
        return run.snapshot()

    @app.get("/api/runs/{run_id}/files")
    def run_files(run_id: str):
        run = _get_run_or_404(run_id)
        return {"files": [path.name for path in run.download_files()]}

    @app.get("/api/runs/{run_id}/download/{filename}")
    def download_file(run_id: str, filename: str):
        run = _get_run_or_404(run_id)
        path = run.output_dir / filename
        if not path.exists() or path.parent != run.output_dir:
            raise HTTPException(status_code=404, detail="文件不存在")
        return FileResponse(path, filename=path.name)

    def _get_run_or_404(run_id: str):
        try:
            return manager.get(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="未找到任务")

    def _configured_input_dir() -> Path:
        config = load_site_config(CONFIG_PATH)
        return configured_path(config.get("default_input_dir"), ROOT_DIR, ROOT_DIR)

    return app


app = create_app()


def read_app_version() -> str:
    try:
        version = VERSION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return "dev"
    return version or "dev"


def browser_error_message(exc: Exception) -> str:
    text = str(exc)
    if "BrowserType.launch_persistent_context" in text or "Target page, context or browser has been closed" in text:
        return "专用 Edge 启动失败，请先关闭正在运行的专用 Edge 窗口后重试。"
    if "Timeout" in text or "Timed out" in text:
        return "打开网页超时，请检查查价网址是否可访问，并确认登录页面没有卡住。"
    return f"打开专用 Edge 失败：{text.splitlines()[0][:120]}"


def choose_folder_dialog() -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(title="选择文件夹")
    finally:
        root.destroy()
    return selected or ""


def choose_port() -> int:
    env_port = os.environ.get("PRICE_QUOTE_PORT")
    if env_port:
        return int(env_port)
    for port in (18765, 8765, 28765):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("8765、18765、28765 都不可用，请关闭占用端口的程序后重试")


def main() -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("缺少 uvicorn。请先运行：python -m pip install -r requirements.txt") from exc
    port = choose_port()
    url = f"http://127.0.0.1:{port}"
    (ROOT_DIR / "outputs").mkdir(parents=True, exist_ok=True)
    (ROOT_DIR / "outputs" / "server_url.txt").write_text(url, encoding="utf-8")
    if os.environ.get("PRICE_QUOTE_NO_BROWSER") != "1":
        webbrowser.open(url)
    uvicorn.run("price_quote_tool.server:app", host="127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    main()
