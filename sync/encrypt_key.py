#!/usr/bin/env python3
"""
產生網頁用的「加密版 JSONBin 金鑰」。

網頁原始碼放在 GitHub 上是公開的，所以不能直接放 Master Key。
這支程式用你的網站密碼把 Key 加密（PBKDF2 + AES-GCM），
網頁只存加密後的字串；輸入正確密碼才解得開。

用法：
  pip install cryptography
  python sync/encrypt_key.py
然後把印出的那一行 JSON 貼到 index.html 的 ENCRYPTED_KEY = ... 那一行。
"""
import base64
import getpass
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERATIONS = 310_000


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def encrypt(secret: str, password: str) -> dict:
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS).derive(password.encode())
    ct = AESGCM(key).encrypt(iv, secret.encode(), None)
    return {"v": 1, "iter": ITERATIONS, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


if __name__ == "__main__":
    secret = getpass.getpass("JSONBin Master Key（輸入時不會顯示）: ").strip()
    pw = getpass.getpass("網站登入密碼（建議 12 字以上）: ")
    if pw != getpass.getpass("再輸入一次密碼: "):
        raise SystemExit("兩次密碼不一致")
    print("\n把下面這行貼到 index.html：\n")
    print("const ENCRYPTED_KEY = " + json.dumps(encrypt(secret, pw)) + ";")
