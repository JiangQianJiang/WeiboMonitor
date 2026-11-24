import aiohttp
from loguru import logger


class PageResult(list):
    """A list of pushable weibo entries that also preserves the raw page ID sequence.

    The raw ID sequence includes every ID from the API response (before filtering
    malformed entries), so boundary detection remains correct even when entries
    are skipped.  Iterating / indexing yields only the pushable items.
    """

    def __init__(self, items, raw_ids=None):
        super().__init__(items)
        self.raw_ids = raw_ids if raw_ids is not None else [str(item['id']) for item in items]


class WeiboMonitor:
    def __init__(self, session: aiohttp.ClientSession, cookie: str = ""):
        self.session = session
        self.headers = {'Referer': 'https://weibo.com/'}
        if cookie:
            self.headers['Cookie'] = cookie

    async def get_weibo_list(self, uid):
        """获取微博列表（第一页），返回 PageResult（list 子类，含 raw_ids 用于边界检测）。

        空列表或请求失败时返回 []。
        """
        api_url = f'https://www.weibo.com/ajax/statuses/mymblog?uid={uid}&page=1&feature=0'
        try:
            # Prevent the request-specific Cookie from following redirects.
            async with self.session.get(
                api_url, headers=self.headers, timeout=5, allow_redirects=False
            ) as response:
                response.raise_for_status()
                if 300 <= response.status < 400:
                    logger.warning(f"微博接口重定向: {uid} (HTTP {response.status})")
                    return []
                data = await response.json()

                raw_ids = []
                result = []
                for w in data['data']['list']:
                    try:
                        # Record raw ID before filtering for boundary detection
                        if 'id' in w:
                            raw_ids.append(str(w['id']))
                        # Skip entries missing required fields
                        if 'text_raw' not in w:
                            logger.warning(f"跳过缺少 text_raw 的微博条目: {uid}")
                            continue
                        result.append({
                            'text': w['text_raw'],
                            'id': str(w['id']),
                            'time': w.get('created_at', ''),
                            'screen_name': w.get('user', {}).get('screen_name', ''),
                            'source': w.get('source', ''),
                            'region_name': w.get('region_name', ''),
                        })
                    except Exception as error:
                        logger.warning(f"跳过解析失败的微博条目: {uid} ({type(error).__name__})")
                return PageResult(result, raw_ids)
        except aiohttp.ClientResponseError as error:
            logger.error(f"微博接口响应错误: {uid} (HTTP {error.status})")
            return []
        except Exception as error:
            logger.error(f"获取微博失败: {uid} ({type(error).__name__})")
            return []
