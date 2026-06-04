import aiohttp
from loguru import logger


class WeiboMonitor:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def get_weibo_list(self, uid):
        """获取微博列表（第一页），返回所有条目。空列表或请求失败时返回 []。"""
        api_url = f'https://www.weibo.com/ajax/statuses/mymblog?uid={uid}&page=1&feature=0'
        headers = {'Referer': 'https://weibo.com/'}
        try:
            async with self.session.get(api_url, headers=headers, timeout=5) as response:
                response.raise_for_status()
                data = await response.json()

                result = []
                for w in data['data']['list']:
                    try:
                        result.append({
                            'text': w.get('text_raw', ''),
                            'id': str(w['id']),
                            'time': w.get('created_at', ''),
                            'screen_name': w.get('user', {}).get('screen_name', ''),
                            'source': w.get('source', ''),
                            'region_name': w.get('region_name', ''),
                        })
                    except Exception:
                        logger.warning(f"跳过解析失败的微博条目: {uid}")
                return result
        except aiohttp.ClientError as e:
            logger.exception(f"网络请求失败: {api_url}")
            return []
        except (KeyError, IndexError) as e:
            logger.exception(f"数据解析失败: {api_url}")
            return []
        except Exception as e:
            logger.exception(f"获取微博失败: {api_url}")
            return []
