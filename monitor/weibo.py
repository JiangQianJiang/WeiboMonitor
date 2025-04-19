import aiohttp
from loguru import logger


class WeiboMonitor:
    def __init__(self):
        # self.session = requests.Session()
        # 设置移动端请求头
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0',
            'Cookie': 'SUB=_2AkMfX4f1f8NxqwFRmfEQzm7iaYVxwwrEieKpA3YuJRMxHRl-yT9yqm1YtRB6NN-pGm0-6c-pJ_DzN9rdBxqfpCWqn6v4'
        }
        self.session = aiohttp.ClientSession(headers=self.headers)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def close(self):
        if not self.session.closed:
            await self.session.close()
            logger.debug("WeiboMonitor session closed")

    async def get_latest_weibo(self, uid):
        """ 获取最新微博内容 """
        api_url = f'https://www.weibo.com/ajax/statuses/mymblog?uid={uid}&page=1&feature=0'
        try:
            async with self.session.get(api_url, timeout=5) as response:
                response.raise_for_status()
                data = await response.json()

                latest_weibo = data['data']['list'][0]

                return {'text': latest_weibo['text_raw'], 'id': latest_weibo['id'],
                        'time': latest_weibo['created_at'], 'screen_name': latest_weibo['user']['screen_name'],
                        'source': latest_weibo['source'], 'region_name': latest_weibo['region_name'], }
        except aiohttp.ClientError as e:
            logger.exception(f"网络请求失败: {api_url}")
            return None
        except (KeyError, IndexError) as e:
            logger.exception(f"数据解析失败: {api_url}")
            return None
        except Exception as e:
            logger.exception(f"获取微博失败: {api_url}")
            return None
