# -*- coding: utf-8 -*-
"""
DBCheck 凭证加密模块

基于 cryptography.Fernet 做对称加密，SecretKey 入库前加密。
主密钥优先级：
    1. 环境变量 DBCHECK_FERNET_MATERIAL（任意字符串，先 SHA256 再 base64-urlsafe 派生）
    2. backend/.fernet_key 文件（首次启动生成，权限 0600）
"""

import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


KEY_PATH = Path(__file__).parent / ".fernet_key"
ENV_MATERIAL = "DBCHECK_FERNET_MATERIAL"


def _derive_key(material: str) -> bytes:
    """任意字符串 → 32 字节 → urlsafe base64，得到 Fernet 接受的 key。"""
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _get_fernet() -> Fernet:
    material = os.getenv(ENV_MATERIAL)
    if material:
        return Fernet(_derive_key(material))

    if KEY_PATH.exists():
        return Fernet(KEY_PATH.read_bytes().strip())

    # 首次启动：生成随机 key 并落盘（0600 权限）
    key = Fernet.generate_key()
    KEY_PATH.write_bytes(key)
    try:
        os.chmod(KEY_PATH, 0o600)
    except OSError:
        # Windows 等平台不支持 chmod，忽略
        pass
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    """加密任意字符串，返回 urlsafe base64 token。"""
    if plaintext is None:
        return None
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """解密 token；token 无效时抛出 InvalidToken。"""
    if token is None:
        return None
    return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")


def reset_key() -> None:
    """删除已有 .fernet_key（用于密钥轮换；会令旧密文无法解密，慎用）。"""
    if KEY_PATH.exists():
        KEY_PATH.unlink()