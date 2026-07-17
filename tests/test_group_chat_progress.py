import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from web_server import GalleryServer  # noqa: E402


class GroupChatProgressTest(unittest.IsolatedAsyncioTestCase):
    def _make_server(self, root: Path) -> GalleryServer:
        data_dir = root / "data"
        config_path = root / "config" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("gallery:\n  port: 18889\n", encoding="utf-8")
        (root / "app" / "references").mkdir(parents=True, exist_ok=True)
        return GalleryServer(
            {"paths": {"project_root": str(root)}, "gallery": {"port": 18889}},
            str(data_dir),
            str(config_path),
        )

    @staticmethod
    def _create_room(server: GalleryServer) -> dict:
        return server.group_chat_store.create_room(
            name="测试群聊",
            participants=[{
                "character_id": "hermes",
                "display_name": "猪猪",
                "enabled": True,
            }],
        )

    @staticmethod
    async def _start_client(server: GalleryServer) -> TestClient:
        test_server = TestServer(server.app)
        await test_server.start_server(access_log=None)
        return TestClient(test_server)

    async def test_public_room_exposes_reply_progress_without_internal_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            room = self._create_room(server)
            token = server._set_group_chat_reply_progress(
                room["id"],
                "image",
                "hermes",
                "猪猪",
            )

            payload = server._public_group_room_payload(room, message_limit=10)

            self.assertTrue(token)
            self.assertEqual("image", payload["reply_progress"]["phase"])
            self.assertEqual("hermes", payload["reply_progress"]["character_id"])
            self.assertEqual("猪猪", payload["reply_progress"]["character_name"])
            self.assertNotIn("_token", payload["reply_progress"])

    async def test_internal_reasoning_is_not_public_or_reused_as_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            reasoning = (
                "首先，用户输入是‘我要看照片’，这是群聊中的消息。"
                "根据指令，我需要只输出 JSON，并设置 image_request 字段。"
            )
            messages = [
                {
                    "id": "reasoning",
                    "content": reasoning,
                    "type": "text",
                    "sender": {"display_name": "猪猪"},
                    "metadata": {"auto_reply": True},
                },
                {
                    "id": "image",
                    "content": "生成图片",
                    "type": "image",
                    "sender": {"display_name": "猪猪"},
                    "metadata": {
                        "auto_reply": True,
                        "image_filename": "photo.png",
                        "image_url": "/images/photo.png",
                        "prompt": reasoning,
                    },
                },
            ]

            public_messages = server._public_group_messages(messages)
            history = server._group_chat_history_text(messages)

            self.assertEqual(["image"], [item["id"] for item in public_messages])
            self.assertNotIn("prompt", public_messages[0]["metadata"])
            self.assertNotIn("首先，用户输入", history)

    async def test_reply_retries_without_persisting_internal_reasoning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            room = self._create_room(server)
            character = {"id": "hermes", "name": "猪猪", "agent_id": "Hermes Agent"}
            server._group_chat_reply_targets = lambda _room, _body: [character]
            outputs = [
                (
                    "首先，用户输入是‘我要看jk’，这是群聊中的消息。"
                    "根据指令，我需要只输出 JSON，并决定 image_request 字段。",
                    "test-model",
                ),
                ('{"message":"好呀，给你看看这套成年时尚造型。","image_request":null}', "test-model"),
            ]
            prompts = []

            async def fake_llm(prompt, **_kwargs):
                prompts.append(prompt)
                return outputs[len(prompts) - 1]

            server._call_group_chat_llm = fake_llm
            client = await self._start_client(server)
            try:
                response = await client.post(
                    f"/api/group-chat/rooms/{room['id']}/reply",
                    json={},
                )
                payload = await response.json()
            finally:
                await client.close()

            stored = server.group_chat_store.list_messages(room["id"])
            self.assertEqual(200, response.status)
            self.assertEqual(2, len(prompts))
            self.assertIn("上一次返回包含内部分析", prompts[1])
            self.assertEqual(["好呀，给你看看这套成年时尚造型。"], [item["content"] for item in stored])
            self.assertEqual("好呀，给你看看这套成年时尚造型。", payload["messages"][0]["content"])
            self.assertNotIn("首先，用户输入", str(payload))

    async def test_image_task_survives_cancelled_waiter_and_clears_progress(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            room = self._create_room(server)
            started = asyncio.Event()
            release = asyncio.Event()

            async def fake_generate(*_args, **_kwargs):
                started.set()
                await release.wait()
                return {"id": "public-image"}, {"id": "stored-image"}

            server._generate_group_chat_image_message = fake_generate
            token = server._set_group_chat_reply_progress(
                room["id"],
                "image",
                "hermes",
                "猪猪",
            )
            task = server._start_group_chat_image_task(
                token,
                room["id"],
                room,
                {"id": "hermes", "name": "猪猪"},
                {},
                {"prompt": "测试图片"},
                "准备发一张照片",
                "test-model",
                "",
                "trigger-message",
                "",
            )

            async def wait_for_image():
                return await asyncio.shield(task)

            waiter = asyncio.create_task(wait_for_image())
            await started.wait()
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter

            self.assertFalse(task.cancelled())
            self.assertIsNotNone(server._public_group_chat_reply_progress(room["id"]))

            release.set()
            result = await task

            self.assertEqual("public-image", result[0]["id"])
            self.assertIsNone(server._public_group_chat_reply_progress(room["id"]))
            self.assertNotIn(task, server._group_chat_background_tasks)

    async def test_room_endpoint_reports_image_phase_before_reply_finishes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            room = self._create_room(server)
            started = asyncio.Event()
            release = asyncio.Event()
            character = {"id": "hermes", "name": "猪猪", "agent_id": "Hermes Agent"}

            server._group_chat_reply_targets = lambda _room, _body: [character]

            async def fake_llm(*_args, **_kwargs):
                return (
                    '{"message":"我去拍一张给你看。",'
                    '"image_request":{"prompt":"卧室里的自然光自拍"}}',
                    "test-model",
                )

            async def fake_image(
                room_id,
                _room,
                image_character,
                _body,
                _image_request,
                _reply_text,
                _used_model,
                _preferred_model,
                _trigger_message_id,
                _rewind_message_id,
                parent_message_id="",
            ):
                started.set()
                await release.wait()
                message = server.group_chat_store.add_message(
                    room_id,
                    {
                        "content": "生成图片",
                        "type": "image",
                        "role": "assistant",
                        "sender": {
                            "type": "character",
                            "id": image_character["id"],
                            "display_name": image_character["name"],
                            "character_id": image_character["id"],
                        },
                        "metadata": {
                            "auto_reply": True,
                            "image_tool_call": True,
                            "parent_message_id": parent_message_id,
                            "image_filename": "test.png",
                            "image_url": "/images/test.png",
                        },
                    },
                    message_type="image",
                )
                return server._public_group_message(message), message

            server._call_group_chat_llm = fake_llm
            server._generate_group_chat_image_message = fake_image
            client = await self._start_client(server)
            try:
                reply_task = asyncio.create_task(
                    client.post(f"/api/group-chat/rooms/{room['id']}/reply", json={})
                )
                await asyncio.wait_for(started.wait(), timeout=2)

                room_response = await client.get(f"/api/group-chat/rooms/{room['id']}")
                room_payload = await room_response.json()

                self.assertEqual(200, room_response.status)
                self.assertEqual("image", room_payload["room"]["reply_progress"]["phase"])
                self.assertEqual("猪猪", room_payload["room"]["reply_progress"]["character_name"])
                self.assertEqual(
                    "我去拍一张给你看。",
                    room_payload["room"]["messages"][-1]["content"],
                )

                release.set()
                reply_response = await reply_task
                reply_payload = await reply_response.json()

                self.assertEqual(200, reply_response.status)
                self.assertEqual(2, len(reply_payload["messages"]))
                self.assertIsNone(reply_payload["room"]["reply_progress"])
            finally:
                release.set()
                await client.close()

    async def test_failed_image_regeneration_restores_original_messages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            room = self._create_room(server)
            character = {"id": "hermes", "name": "猪猪", "agent_id": "Hermes Agent"}
            trigger = server.group_chat_store.add_message(
                room["id"],
                {
                    "content": "发张照片",
                    "type": "text",
                    "sender": {"type": "user", "id": "user", "display_name": "我"},
                },
            )
            parent = server.group_chat_store.add_message(
                room["id"],
                {
                    "content": "好呀，我现在拍。",
                    "type": "text",
                    "role": "assistant",
                    "sender": {
                        "type": "character",
                        "id": "hermes",
                        "display_name": "猪猪",
                        "character_id": "hermes",
                    },
                    "metadata": {
                        "auto_reply": True,
                        "trigger_message_id": trigger["id"],
                        "llm_image_request": True,
                    },
                },
            )
            image = server.group_chat_store.add_message(
                room["id"],
                {
                    "content": "生成图片",
                    "type": "image",
                    "role": "assistant",
                    "sender": {
                        "type": "character",
                        "id": "hermes",
                        "display_name": "猪猪",
                        "character_id": "hermes",
                    },
                    "metadata": {
                        "auto_reply": True,
                        "image_tool_call": True,
                        "trigger_message_id": trigger["id"],
                        "parent_message_id": parent["id"],
                        "image_filename": "original.png",
                        "image_url": "/images/original.png",
                        "prompt": "成年人在咖啡店自拍",
                    },
                },
                message_type="image",
            )
            later = server.group_chat_store.add_message(
                room["id"],
                {
                    "content": "这张不错",
                    "type": "text",
                    "sender": {"type": "user", "id": "user", "display_name": "我"},
                },
            )
            original_ids = [trigger["id"], parent["id"], image["id"], later["id"]]

            server._group_chat_reply_targets = lambda _room, _body: [character]

            async def fake_llm(*_args, **_kwargs):
                return (
                    '{"message":"我重新拍一张。",'
                    '"image_request":{"prompt":"成年人在咖啡店重新自拍"}}',
                    "test-model",
                )

            async def fail_image(*_args, **_kwargs):
                raise RuntimeError("image_generation_failed")

            server._call_group_chat_llm = fake_llm
            server._generate_group_chat_image_message = fail_image
            client = await self._start_client(server)
            try:
                response = await client.post(
                    f"/api/group-chat/rooms/{room['id']}/reply",
                    json={"regenerate_message_id": image["id"]},
                )
                payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(502, response.status)
            self.assertEqual(
                "图片重新生成失败，已保留原图和原聊天记录。",
                payload["message"],
            )
            self.assertTrue(payload["rewind"]["restored"])
            self.assertEqual(
                original_ids,
                [item["id"] for item in server.group_chat_store.list_messages(room["id"])],
            )
            self.assertEqual(
                original_ids,
                [item["id"] for item in payload["room"]["messages"]],
            )


if __name__ == "__main__":
    unittest.main()
