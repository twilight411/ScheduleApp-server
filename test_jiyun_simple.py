"""
简单测试即梦API连接性
"""
import asyncio
import httpx
import time

async def test_jiyun_connectivity():
    """测试即梦API连接性"""
    url = "https://visual.volcengineapi.com?Action=CVSync2AsyncSubmitTask&Version=2022-08-31"
    
    body = {
        "req_key": "jimeng_t2i_v30",
        "prompt": "test",
        "seed": -1,
        "width": 1024,
        "height": 1024,
        "use_pre_llm": True,
    }
    
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, json=body)
            print(f"Status: {resp.status_code}")
            print(f"Response: {resp.text[:500]}")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_jiyun_connectivity())