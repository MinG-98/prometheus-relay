import os, sys
import subprocess
import traceback
from playwright.sync_api import sync_playwright
from utils.config import DEBUG, _env_bool, get_environment, Environment

PLAYWRIGHT_BROWSERS_PATH = "../chrome"

def install_browser():
    """
    安装 Chromium 浏览器
    """
    try:
        subprocess.run(["playwright", "install", "chromium"], check=True)
        print("浏览器安装完成，请重新运行程序。")
    except subprocess.CalledProcessError as e:
        print(f"发生未知错误：{e}")


def get_browser():
    """
    启动浏览器实例
    :return: 浏览器实例
    """

    # Headless is the safe default for servers. Set DEBUG=true for verbose
    # local diagnostics, or override it explicitly with HEADLESS=false.
    headless = _env_bool(os.getenv("HEADLESS"), default=not DEBUG)

    env = get_environment()
    # Respect an explicitly supplied browser path. Docker uses the browsers
    # bundled in the Playwright base image; local/packed runs keep the legacy
    # project-relative path when no override is provided.
    if not os.getenv("PLAYWRIGHT_BROWSERS_PATH"):
        if env == Environment.LOCAL:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.abspath(
                os.path.join(os.path.dirname(__file__), PLAYWRIGHT_BROWSERS_PATH)
            )
        elif env == Environment.PACKED:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.abspath(
                os.path.join(os.path.dirname(sys.executable), PLAYWRIGHT_BROWSERS_PATH)
            )

    playwright = None
    try:
        # 启动浏览器
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=headless)
        return playwright, browser
    except Exception as e:
        if playwright is not None:
            playwright.stop()

        # 捕获浏览器启动错误
        if "Executable doesn't exist" in str(e) and env != Environment.GITHUBACTION:
            print("浏览器可执行文件不存在！")
            install_browser()
            sys.exit(1)
        else:
            traceback.print_exc()
            raise
