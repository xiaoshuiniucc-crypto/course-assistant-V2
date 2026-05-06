"""
QQ 课程助手 — 真实机器人启动脚本
用法:
    python start_bot.py          # 直接启动
    python start_bot.py --check  # 仅检查配置，不启动
"""
import os
import sys
import asyncio
import logging

# 配置日志（清晰格式，适合联调）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-16s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot_run.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("start_bot")

# 添加 src 到 path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# 加载 .env
from dotenv import load_dotenv
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if not os.path.exists(_env_path):
    print("❌ .env 文件不存在！请先创建 .env 配置文件")
    sys.exit(1)

load_dotenv(_env_path, override=True)


def check_config():
    """检查配置完整性"""
    print("\n" + "="*55)
    print("  QQ 课程助手 — 配置检查")
    print("="*55)

    ok = True

    # QQ Bot 配置
    appid = os.environ.get("QQ_APPID", "")
    secret = os.environ.get("QQ_SECRET", "")
    sandbox = os.environ.get("QQ_SANDBOX", "0")

    print(f"\n【QQ 机器人配置】")
    if appid and appid != "your-qq-appid-here":
        print(f"  ✅ QQ_APPID   = {appid}")
    else:
        print(f"  ❌ QQ_APPID   = 未配置")
        ok = False

    if secret and secret != "your-qq-secret-here":
        print(f"  ✅ QQ_SECRET  = {secret[:4]}{'*' * (len(secret)-4)}")
    else:
        print(f"  ❌ QQ_SECRET  = 未配置")
        ok = False

    print(f"  {'🌐' if sandbox == '0' else '🧪'} QQ_SANDBOX  = {'正式环境' if sandbox == '0' else '沙箱环境'}")

    # DeepSeek 配置
    ds_key = os.environ.get("DEEPSEEK_API_KEY", "")
    print(f"\n【AI 配置】")
    if ds_key and ds_key != "your-deepseek-api-key-here":
        print(f"  ✅ DEEPSEEK_API_KEY = {ds_key[:8]}{'*' * 10}")
    else:
        print(f"  ⚠️  DEEPSEEK_API_KEY = 未配置（意图识别将降级到规则模式）")

    # 依赖检查
    print(f"\n【依赖检查】")
    try:
        import botpy
        print(f"  ✅ qq-botpy 已安装")
    except ImportError:
        print(f"  ❌ qq-botpy 未安装: pip install qq-botpy")
        ok = False

    try:
        import aiohttp
        print(f"  ✅ aiohttp 已安装")
    except ImportError:
        print(f"  ❌ aiohttp 未安装: pip install aiohttp")
        ok = False

    print("\n" + "="*55)
    if ok:
        print("  ✅ 配置检查通过，可以启动！")
    else:
        print("  ❌ 配置有问题，请先修复上述错误")
    print("="*55 + "\n")

    return ok


async def main():
    """主启动函数"""
    check_only = "--check" in sys.argv

    ok = check_config()
    if not ok or check_only:
        sys.exit(0 if check_only else 1)

    print("🚀 正在启动 QQ 课程助手...")
    print("   按 Ctrl+C 可以停止\n")

    try:
        from main import QQCourseAssistant
        assistant = QQCourseAssistant()
        print(f"  模式: {assistant.qclaw.mode}")
        print(f"  AppID: {os.environ.get('QQ_APPID', '未知')}")
        await assistant.run()

        # Bot 在后台线程运行，主线程保持活跃
        print("\n✅ QQ 机器人已连接，等待消息...\n")
        logger.info("Bot 已启动，等待 QQ 消息事件...")

        # 保持运行直到 Ctrl+C
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass

    except Exception as e:
        logger.error(f"启动失败: {e}", exc_info=True)
        print(f"\n❌ 启动失败: {e}")
        print("\n常见原因:")
        print("  1. AppID 或 Secret 错误")
        print("  2. 机器人未在 QQ 开放平台上线")
        print("  3. 网络连接问题")
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n🛑 已停止运行")
