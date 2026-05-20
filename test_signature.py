"""
测试火山引擎签名算法
"""
import hmac
import hashlib
import base64

# 测试数据
secret_key = "TXprM09HUTJZalV6TkRRME5EZ3lPV0l3T1dKbU4yUTBOVEk0TUdZeFpESQ=="
date = "20260504"
string_to_sign = "test"

# 方式1：直接使用字符串
print("方式1：直接使用字符串")
k_date1 = hmac.new(secret_key.encode("utf-8"), date.encode("utf-8"), hashlib.sha256).digest()
k_region1 = hmac.new(k_date1, "cn-north-1".encode("utf-8"), hashlib.sha256).digest()
k_service1 = hmac.new(k_region1, "cv".encode("utf-8"), hashlib.sha256).digest()
k_signing1 = hmac.new(k_service1, b"request", hashlib.sha256).digest()
signature1 = hmac.new(k_signing1, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
print(f"签名: {signature1}")

# 方式2：添加VOLC前缀
print("\n方式2：添加VOLC前缀")
k_date2 = hmac.new(("VOLC" + secret_key).encode("utf-8"), date.encode("utf-8"), hashlib.sha256).digest()
k_region2 = hmac.new(k_date2, "cn-north-1".encode("utf-8"), hashlib.sha256).digest()
k_service2 = hmac.new(k_region2, "cv".encode("utf-8"), hashlib.sha256).digest()
k_signing2 = hmac.new(k_service2, b"request", hashlib.sha256).digest()
signature2 = hmac.new(k_signing2, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
print(f"签名: {signature2}")

# 方式3：base64解码后使用
print("\n方式3：base64解码后使用")
try:
    decoded = base64.b64decode(secret_key)
    print(f"解码后长度: {len(decoded)}")
    print(f"解码后内容: {decoded.hex()[:32]}...")
    
    k_date3 = hmac.new(decoded, date.encode("utf-8"), hashlib.sha256).digest()
    k_region3 = hmac.new(k_date3, "cn-north-1".encode("utf-8"), hashlib.sha256).digest()
    k_service3 = hmac.new(k_region3, "cv".encode("utf-8"), hashlib.sha256).digest()
    k_signing3 = hmac.new(k_service3, b"request", hashlib.sha256).digest()
    signature3 = hmac.new(k_signing3, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    print(f"签名: {signature3}")
except Exception as e:
    print(f"解码失败: {e}")

# 方式4：base64解码后添加VOLC前缀
print("\n方式4：base64解码后添加VOLC前缀")
try:
    decoded = base64.b64decode(secret_key)
    k_date4 = hmac.new(b"VOLC" + decoded, date.encode("utf-8"), hashlib.sha256).digest()
    k_region4 = hmac.new(k_date4, "cn-north-1".encode("utf-8"), hashlib.sha256).digest()
    k_service4 = hmac.new(k_region4, "cv".encode("utf-8"), hashlib.sha256).digest()
    k_signing4 = hmac.new(k_service4, b"request", hashlib.sha256).digest()
    signature4 = hmac.new(k_signing4, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    print(f"签名: {signature4}")
except Exception as e:
    print(f"解码失败: {e}")