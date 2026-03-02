import aiohttp
from loguru import logger


class WeiboMonitor:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

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
