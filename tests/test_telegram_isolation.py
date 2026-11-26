from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from core import config
from core.app import App
from notifer.notifer import Notifer, format_telegram_message
from state.repository import StateRepository


@pytest.mark.parametrize("text", ["", "  \n"])
def test_empty_telegram_post_uses_its_url(text):
    url = "https://weibo.com/123/456"
    assert format_telegram_message({"telegram_template": "{text}"}, {"text": text}, url) == (
        r"https://weibo\.com/123/456"
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_example_template_supports_new_and_legacy_separator(legacy):
    example = config.PROJECT_ROOT / "users.yaml.example"
    notification = yaml.safe_load(example.read_text(encoding="utf-8"))["notification"]
    template = notification["telegram_template"]
    assert r"{region_name} \| {source}" in template
    if legacy:
        notification["telegram_template"] = template.replace(r" \| ", " | ")
    message = format_telegram_message(
        notification,
        {"screen_name": "Test_User", "text": r"C:\tmp [notes]", "region_name": None, "source": "Web"},
        "https://weibo.com/1/2",
    )
    assert message == "\n".join((
        r"*Test\_User* 发表了新微博：",
        r"C:\\tmp \[notes\]",
        r" \| Web",
        r"[查看详情](https://weibo\.com/1/2)",
        "",
    ))


def test_config_only_warns_about_enabled_channels(tmp_path, monkeypatch):
    path = tmp_path / "users.yaml"
    path.write_text(
        "notification:\n"
        "  enable_telegram: false\n"
        "  telegram_template: '{unknown}'\n",
        encoding="utf-8",
    )
    warning = Mock()
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(config.logger, "warning", warning)

    assert config.load_config()["notification"]["enable_telegram"] is False
    warning.assert_called_once_with("Server酱 配置需要填写 notification.sendkey")


@pytest.mark.parametrize(
    ("notification", "expected_results"),
    [
        (
            {"enable_telegram": False},
            [("serverchan", "success")],
        ),
        (
            {"telegram_template": "{bad_placeholder}"},
            [("telegram", "failed"), ("serverchan", "success")],
        ),
    ],
)
async def test_app_delivers_to_serverchan_without_a_renderable_telegram_template(
    notification, expected_results, tmp_path,
):
    app = App()
    app.config = {"notification": notification}
    app.monitor = SimpleNamespace(get_weibo_list=AsyncMock(return_value=[{
        "id": "2",
        "screen_name": "Weibo User",
        "text": "Newest post",
        "source": "Web",
        "region_name": "Shanghai",
    }]))
    app.repository = StateRepository()
    app.repository._db_path = tmp_path / "state.db"
    await app.repository.initialize()
    await app.repository.set_latest_id("123", "1")
    app.notifer = Notifer(session=None, notification_config=notification)
    app.notifer.telegram_send = AsyncMock()
    app.notifer.ms_send = AsyncMock()

    await app._check_single({"weiboid": "123"})

    app.notifer.telegram_send.assert_not_awaited()
    app.notifer.ms_send.assert_awaited_once()
    assert "Newest post" in app.notifer.ms_send.await_args.args[0]
    assert app.repository.get_latest_id("123") == "2"
    async with app.repository._connect() as database:
        async with database.execute("SELECT channel, status FROM event_push_log ORDER BY rowid") as cursor:
            assert await cursor.fetchall() == expected_results


async def test_disabled_serverchan_does_not_render():
    notifier = Notifer(session=None, notification_config={"enable_serverchan": False})
    notifier.telegram_send = AsyncMock()
    serverchan_message = Mock(side_effect=AssertionError("disabled channel rendered"))

    result = await notifier.send_message(serverchan_message, "Telegram text", "title")

    serverchan_message.assert_not_called()
    notifier.telegram_send.assert_awaited_once_with("Telegram text")
    assert result == {"telegram": (True, None)}
