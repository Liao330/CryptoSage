"""
SSL 工具 —— 统一 httpx AsyncClient 工厂，解决 macOS Python 3.14 环境下
OKX/Binance API 的 SSL RECORD_LAYER_FAILURE 问题。

核心策略：
1. 优先用 certifi 的 CA bundle 创建 SSL 上下文（避免系统证书缺失导致握手失败）
2. 环境变量 SSL_VERIFY=false 在开发环境跳过验证（仅限内网/测试）
3. 每次调用新建 client（无状态），兼容主事件循环与子线程双环境
4. SSLContext 缓存（避免每次 make_client 重建 + 重复读取 certifi.where()）
"""

import os
import ssl
import logging
from functools import lru_cache

import certifi
import httpx

from backend.config import config

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _cached_ssl_context() -> ssl.SSLContext:
    """构建并缓存 SSL 上下文：优先用 certifi CA bundle，兼容 macOS Python TLS 层问题。

    使用 lru_cache 确保整个进程生命周期只构建一次 SSLContext，
    避免高频 HTTP 调用时重复读取 certifi.where() 造成 I/O 开销。

    macOS 上 Python 3.13+ 的 TLS 实现在访问某些 Cloudflare 保护的域名
    （含 www.okx.com / fapi.binance.com）时可能触发 RECORD_LAYER_FAILURE。
    这是底层 TLS 记录层握手失败，与证书验证无关。解决方案：
    1. 显式指定 TLS 1.2 为最低版本（避免自动协商死锁）
    2. 关闭 HTTP/2 ALPN（httpx 默认启用 h2，但某些中间代理不兼容）
    3. 使用 certifi 的 CA bundle（避免系统 store 缺失）
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=certifi.where())
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    # 仅宣告 HTTP/1.1（关闭 h2 ALPN），避免中间代理/防火墙误判 HTTP/2 帧
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


def make_client(timeout: float = 30.0, proxy: str | None = None) -> httpx.AsyncClient:
    """创建 httpx AsyncClient（SSL 兼容 + 可按数据源指定代理 + 无状态设计）。

    企业网络环境下 OKX/Binance 可能被阻止或 SSL 被中间设备检查截断。
    解决方案优先级：
    1. 环境变量 HTTP_PROXY / HTTPS_PROXY 指定代理
    2. SSL_VERIFY=false 关闭证书验证（中间设备 SSL 检查时必需）
    3. 强制 HTTP/1.1 避免 h2 兼容问题
    """
    # 单数据源代理优先；其次兼容常见大小写和 ALL_PROXY 配置。httpx 仅在
    # 安装 socksio 时支持 socks5，未安装时会明确抛错并由调用方返回诊断。
    proxy = (
        proxy
        or os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("HTTP_PROXY")
        or os.getenv("http_proxy")
        or os.getenv("ALL_PROXY")
        or os.getenv("all_proxy")
        or None
    )
    if config.SSL_VERIFY:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            verify=_cached_ssl_context(),
            http2=False,
            proxy=proxy,
            limits=httpx.Limits(max_keepalive_connections=0),
        )
    logger.warning("⚠ SSL_VERIFY=false，证书验证已关闭 —— 仅限开发/测试环境使用")
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout),
        verify=False,
        http2=False,
        proxy=proxy,
        limits=httpx.Limits(max_keepalive_connections=0),
    )
