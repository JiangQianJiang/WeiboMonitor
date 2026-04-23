import asyncio
from core.app import App


# Aliyun FC
def handler(event, context):
    asyncio.run(App().run_once())
    return


if __name__ == '__main__':
    asyncio.run(App().run_once())
