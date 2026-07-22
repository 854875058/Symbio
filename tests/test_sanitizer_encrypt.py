"""批次D4：ENCRYPT 策略真加密（Fernet，可逆）。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from cryptography.fernet import Fernet

from symbio.security.sanitizer import (
    PIIType,
    SanitizeExecutor,
    SanitizeRule,
    SanitizeStrategy,
)


def _executor(tmp_path, key=None):
    return SanitizeExecutor(rules=[], encrypt_key=key, key_path=tmp_path / "sanitizer.key")


def test_encrypt_is_real_and_reversible(tmp_path):
    ex = _executor(tmp_path, key=Fernet.generate_key())
    rule = SanitizeRule(pii_type=PIIType.PHONE, strategy=SanitizeStrategy.ENCRYPT)

    token = ex.sanitize_value("13812345678", rule)
    assert token.startswith("ENC:")
    assert "13812345678" not in token  # 密文里看不到原文
    assert ex.decrypt(token) == "13812345678"  # 可还原


def test_encrypt_uses_fresh_iv_but_both_decrypt(tmp_path):
    ex = _executor(tmp_path, key=Fernet.generate_key())
    t1 = ex._encrypt("secret@example.com")
    t2 = ex._encrypt("secret@example.com")
    assert t1 != t2  # Fernet 带随机 IV，密文不同
    assert ex.decrypt(t1) == ex.decrypt(t2) == "secret@example.com"


def test_key_persists_across_instances(tmp_path):
    # 不显式给 key -> 首个实例生成并落盘，第二个实例同路径能解出第一个的密文
    a = SanitizeExecutor(rules=[], key_path=tmp_path / "k.key")
    token = a._encrypt("PII-VALUE")
    assert (tmp_path / "k.key").exists()

    b = SanitizeExecutor(rules=[], key_path=tmp_path / "k.key")
    assert b.decrypt(token) == "PII-VALUE"


def test_decrypt_passthrough_for_non_ciphertext(tmp_path):
    ex = _executor(tmp_path, key=Fernet.generate_key())
    assert ex.decrypt("plain text") == "plain text"
    assert ex.decrypt("ENC:garbage-not-valid") == "ENC:garbage-not-valid"
