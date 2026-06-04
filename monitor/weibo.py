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
        self.raw_ids = raw_ids if raw_ids is not None else [item['id'] for item in items]


class WeiboMonitor:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def get_weibo_list(self, uid):
        """获取微博列表（第一页），返回 PageResult（list 子类，含 raw_ids 用于边界检测）。

        空列表或请求失败时返回 []。
        """
        api_url = f'https://www.weibo.com/ajax/statuses/mymblog?uid={uid}&page=1&feature=0'
        headers = {'Referer': 'https://weibo.com/'}
        try:
            async with self.session.get(api_url, headers=headers, timeout=5) as response:
                response.raise_for_status()
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
                    except Exception:
                        logger.warning(f"跳过解析失败的微博条目: {uid}")
                return PageResult(result, raw_ids)
        except aiohttp.ClientError:
            logger.exception(f"网络请求失败: {api_url}")
            return []
        except (KeyError, IndexError):
            logger.exception(f"数据解析失败: {api_url}")
            return []
        except Exception:
            logger.exception(f"获取微博失败: {api_url}")
            return []
